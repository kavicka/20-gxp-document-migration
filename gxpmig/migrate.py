"""The migration run: extract → transform → validate → load → verify → reconcile, with a report for sign-off.

- dry run: does everything except loading, so the business can review exceptions before the real run,
- idempotent: re-running a batch skips documents already migrated, so a crash halfway is safe to restart,
- every document ends in exactly one bucket (loaded, already loaded, quarantined) and the reconciliation proves it.

Run:  uv run python -m gxpmig.migrate --dry-run
      uv run python -m gxpmig.migrate --batch B001
"""

import argparse
import csv
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from gxpmig import audit
from gxpmig.source import LEGACY, MIGRATION_DATE
from gxpmig.transform import E_CHECKSUM, E_DUPLICATE, E_FILE_MISSING, load_mapping, transform
from gxpmig.vault import Vault, sha256_file

ROOT = Path(__file__).resolve().parent.parent
ACTOR = "migration-service"  # in production: a named service account, and the run is started by a named person


@dataclass
class RunReport:
    batch_id: str
    dry_run: bool
    source_count: int = 0
    loaded: list[str] = field(default_factory=list)
    already_loaded: list[str] = field(default_factory=list)
    quarantined: dict[str, list[str]] = field(default_factory=dict)  # legacy_id → issue codes
    verified_content: int = 0
    seconds: float = 0.0
    audit_ok: bool = False
    audit_head: str = ""

    @property
    def reconciled(self) -> bool:
        """Every source document is in exactly one bucket."""
        ids = self.loaded + self.already_loaded + list(self.quarantined)
        return len(ids) == len(set(ids)) == self.source_count

    def summary(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "dry_run": self.dry_run,
            "source": self.source_count,
            "loaded": len(self.loaded),
            "already_loaded": len(self.already_loaded),
            "quarantined": len(self.quarantined),
            "issues": dict(Counter(c for codes in self.quarantined.values() for c in codes).most_common()),
            "reconciled": self.reconciled,
            "content_verified": self.verified_content,
            "audit_chain_ok": self.audit_ok,
            "audit_head": self.audit_head,
            "seconds": round(self.seconds, 2),
        }


def read_source(legacy: Path = LEGACY) -> list[dict]:
    with (legacy / "metadata.csv").open() as f:
        return list(csv.DictReader(f))


def run(vault_dir: Path, batch_id: str, dry_run: bool = False, legacy: Path = LEGACY,
        today: date = MIGRATION_DATE) -> RunReport:  # fmt: skip
    t0 = time.perf_counter()
    rows = read_source(legacy)
    studies = set(json.loads((legacy / "study_registry.json").read_text()))
    maps = {m: load_mapping(m) for m in ("doc_types", "states", "countries")}
    vault = Vault(vault_dir)
    rep = RunReport(batch_id, dry_run, len(rows))
    seen = vault.known_hashes()  # content already in the target counts for duplicate detection
    try:
        for row in rows:
            lid = row["legacy_id"]
            if (existing := vault.get(lid)) is not None:
                rep.already_loaded.append(lid)
                seen.add(existing["sha256"])
                continue
            res = transform(row, maps, studies, today)
            src = legacy / "files" / row["file_name"]
            sha = None
            if not src.exists():
                res.issues.append(E_FILE_MISSING)
            else:
                data = src.read_bytes()
                if hashlib.md5(data).hexdigest() != row["md5"]:
                    res.issues.append(E_CHECKSUM)  # the file is not the one that was checked in
                sha = hashlib.sha256(data).hexdigest()
                if sha in seen:
                    res.issues.append(E_DUPLICATE)
            if res.issues:
                rep.quarantined[lid] = res.issues
                if not dry_run:
                    audit.record(
                        vault.con, ACTOR, "MIGRATE_QUARANTINE", lid, {"issues": res.issues, "batch_id": batch_id}
                    )
                continue
            seen.add(sha)
            if dry_run:
                rep.loaded.append(lid)  # would be loaded
                continue
            vault.load(res.meta, src, sha, batch_id, ACTOR)
            rep.loaded.append(lid)
        # verification pass over everything in the target, not just this batch
        for (sha,) in vault.con.execute("SELECT sha256 FROM documents"):
            if sha256_file(vault.content_path(sha)) == sha:
                rep.verified_content += 1
        rep.audit_ok, _ = audit.verify(vault.con)
        rep.audit_head = audit.last_hash(vault.con)
    finally:
        vault.close()
    rep.seconds = time.perf_counter() - t0
    return rep


def write_report(rep: RunReport, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(rep.summary(), indent=1))
    with (out_dir / "exceptions.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["legacy_id", "issues"])
        for lid, codes in sorted(rep.quarantined.items()):
            w.writerow([lid, ";".join(codes)])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="B001")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--vault", type=Path, default=ROOT / "data" / "vault")
    args = ap.parse_args()
    rep = run(args.vault, args.batch, args.dry_run)
    write_report(rep, ROOT / "reports" / (args.batch + ("-dry-run" if args.dry_run else "")))
    print(json.dumps(rep.summary(), indent=1))


if __name__ == "__main__":
    main()
