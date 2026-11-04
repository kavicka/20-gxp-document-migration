"""Measure the validated migration against a naive lift-and-shift, plus the integrity controls.

1. Defect detection: each injected defect must end in quarantine with the right issue code; clean documents
   must not be rejected (false rejects).
2. Metadata accuracy: loaded fields vs the true values the generator knows (document type, study, version,
   state, date, country). Lift-and-shift copies the legacy strings as they are.
3. Integrity controls, each tested by breaking something on purpose: idempotent re-run, tampering with the
   audit trail, corrupting a stored file, editing an archived CSV.

Run:  uv run python -m gxpmig.source && uv run python experiment.py   → results.json
"""

import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from gxpmig import archive, audit
from gxpmig.migrate import read_source, run
from gxpmig.source import LEGACY
from gxpmig.transform import (
    E_CHECKSUM,
    E_DATE,
    E_DATE_FUTURE,
    E_DOC_TYPE,
    E_DUPLICATE,
    E_FILE_MISSING,
    E_STUDY_UNKNOWN,
    E_TITLE,
    E_VERSION,
)  # fmt: skip
from gxpmig.vault import Vault, sha256_file

EXPECTED_CODE = {"unknown_doc_type": E_DOC_TYPE, "invalid_date": E_DATE, "future_date": E_DATE_FUTURE,
                 "unknown_study": E_STUDY_UNKNOWN, "missing_file": E_FILE_MISSING, "checksum_mismatch": E_CHECKSUM,
                 "duplicate": E_DUPLICATE, "invalid_version": E_VERSION, "missing_title": E_TITLE}  # fmt: skip
FIELDS = ["document_type", "study_number", "major_version", "minor_version", "lifecycle_state", "effective_date",
          "country"]  # fmt: skip
RAW = {"document_type": "doc_type", "study_number": "study", "lifecycle_state": "status",
       "effective_date": "effective_date", "country": "country"}  # fmt: skip


def lift_and_shift(rows: list[dict]) -> dict[str, dict]:
    """Baseline: copy every document whose file exists, metadata exactly as typed in the legacy system."""
    out = {}
    for r in rows:
        if (LEGACY / "files" / r["file_name"]).exists():
            ver = r["version"].split(".")
            out[r["legacy_id"]] = {
                **{f: r[raw] or None for f, raw in RAW.items()},
                "major_version": ver[0],
                "minor_version": ver[1] if len(ver) > 1 else None,
            }
    return out


def field_accuracy(loaded: dict[str, dict], truth: dict) -> dict:
    clean = [lid for lid, t in truth.items() if t["defect"] is None and lid in loaded]
    acc = {f: round(sum(str(loaded[lid][f]) == str(truth[lid]["expected"][f]) for lid in clean) / len(clean), 3)
           for f in FIELDS}  # fmt: skip
    all_ok = sum(all(str(loaded[lid][f]) == str(truth[lid]["expected"][f]) for f in FIELDS) for lid in clean)
    return {"per_field": acc, "records_fully_correct": round(all_ok / len(clean), 3), "n": len(clean)}


def main() -> None:
    truth = json.loads((LEGACY / "ground_truth.json").read_text())
    rows = read_source()
    defects = {lid: t["defect"] for lid, t in truth.items() if t["defect"]}
    res: dict = {"source": {"documents": len(rows), "injected_defects": len(defects),
                            "clean": len(rows) - len(defects)}}  # fmt: skip

    # --- baseline ---
    base = lift_and_shift(rows)
    res["baseline"] = {"loaded": len(base), "defective_loaded": sum(lid in base for lid in defects),
                       "accuracy": field_accuracy(base, truth)}  # fmt: skip

    with tempfile.TemporaryDirectory() as tmp:
        vdir = Path(tmp) / "vault"
        # --- validated pipeline: dry run, real run, re-run ---
        dry = run(vdir, "B001", dry_run=True)
        t0 = time.perf_counter()
        rep = run(vdir, "B001")
        secs = time.perf_counter() - t0
        con = sqlite3.connect(vdir / "vault.db")
        cur = con.execute("SELECT * FROM documents")
        cols = [c[0] for c in cur.description]
        loaded = {r[cols.index("legacy_id")]: dict(zip(cols, r, strict=True)) for r in cur.fetchall()}
        snapshot = sorted(con.execute("SELECT legacy_id, sha256 FROM documents").fetchall())
        con.close()
        rerun = run(vdir, "B001")
        con = sqlite3.connect(vdir / "vault.db")
        same_after_rerun = sorted(con.execute("SELECT legacy_id, sha256 FROM documents").fetchall()) == snapshot
        con.close()

        per_defect = {}
        for d, code in EXPECTED_CODE.items():
            ids = [lid for lid, x in defects.items() if x == d]
            per_defect[d] = {"n": len(ids), "detected": sum(code in rep.quarantined.get(lid, []) for lid in ids),
                             "loaded": sum(lid in loaded for lid in ids)}  # fmt: skip
        clean_ids = [lid for lid in truth if lid not in defects]
        res["pipeline"] = {
            "dry_run_matches_real": dry.summary()["issues"] == rep.summary()["issues"]
            and len(dry.loaded) == len(rep.loaded),
            "loaded": len(rep.loaded),
            "quarantined": len(rep.quarantined),
            "defective_loaded": sum(lid in loaded for lid in defects),
            "false_rejects": sum(lid in rep.quarantined for lid in clean_ids),
            "per_defect": per_defect,
            "detection_rate": round(sum(v["detected"] for v in per_defect.values()) / len(defects), 3),
            "accuracy": field_accuracy(loaded, truth),
            "reconciled": rep.reconciled,
            "content_verified": rep.verified_content,
            "docs_per_second": round(len(rows) / secs, 1),
            "rerun_loaded": len(rerun.loaded),
            "rerun_target_unchanged": same_after_rerun,
        }

        # --- integrity controls, each broken on purpose ---
        v = Vault(vdir)
        try:
            v.con.execute("UPDATE audit_trail SET actor = 'mallory' WHERE seq = 5")
            update_blocked = False
        except sqlite3.DatabaseError:
            update_blocked = True
        # someone with file access drops the trigger and edits an entry: the hash chain must notice
        v.con.execute("DROP TRIGGER audit_no_update")
        v.con.execute('UPDATE audit_trail SET details = \'{"lifecycle_state": "Draft"}\' WHERE seq = 42')
        chain_ok, broken_at = audit.verify(v.con)
        # bit rot in the content store: verification must find exactly that file
        some_sha = v.con.execute("SELECT sha256 FROM documents LIMIT 1 OFFSET 10").fetchone()[0]
        p = v.content_path(some_sha)
        p.write_bytes(p.read_bytes()[:-5])
        bad = [s for (s,) in v.con.execute("SELECT sha256 FROM documents") if sha256_file(v.content_path(s)) != s]
        v.close()
        res["controls"] = {"audit_update_blocked": update_blocked, "tamper_detected": not chain_ok,
                           "tamper_first_broken_seq": broken_at, "corrupted_files_found": len(bad),
                           "corrupted_file_is_the_one": bad == [some_sha]}  # fmt: skip

        # --- archiving the legacy app ---
        out = Path(tmp) / "archive"
        m = archive.export(LEGACY / "legacy_ctms.db", out)
        ok = archive.verify(out)["all_ok"]
        csv_path = out / "tables" / "studies.csv"
        csv_path.write_text(csv_path.read_text().replace("COMPLETED", "TERMINATED", 1))
        tamper_found = not archive.verify(out)["all_ok"]
        res["archive"] = {"tables": len(m["tables"]), "rows": sum(t["rows"] for t in m["tables"].values()),
                          "verified": ok, "tamper_detected": tamper_found, "retain_until": m["retain_until"],
                          "seconds": m["seconds"]}  # fmt: skip
        shutil.rmtree(out)

    Path("results.json").write_text(json.dumps(res, indent=1) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
