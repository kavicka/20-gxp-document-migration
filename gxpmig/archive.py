"""Retire a legacy application: export its data to open formats, prove the export is complete, then lock it.

Why open formats: the archive must stay readable for the whole retention period (25 years for a clinical trial
master file under EU Regulation 536/2014), long after the app and its database engine are gone. CSV + a JSON
schema + a checksum manifest can be read by anything; a proprietary database file can't be relied on.

Run:  uv run python -m gxpmig.archive
"""

import csv
import hashlib
import json
import shutil
import sqlite3
import time
from datetime import UTC, date, datetime
from pathlib import Path

from gxpmig.source import LEGACY, MIGRATION_DATE

ROOT = Path(__file__).resolve().parent.parent
RETENTION_YEARS = 25


def _table_digest(rows: list[tuple]) -> str:
    """Order-independent fingerprint of a table's content (rows as text, sorted)."""
    h = hashlib.sha256()
    for r in sorted("\x1f".join("" if v is None else str(v) for v in row) for row in rows):
        h.update(r.encode() + b"\n")
    return h.hexdigest()


def export(db: Path, out: Path, app_name: str = "legacy_ctms", today: date = MIGRATION_DATE) -> dict:
    t0 = time.perf_counter()
    if out.exists():
        shutil.rmtree(out)
    (out / "tables").mkdir(parents=True)
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # read-only: the export must not change the source
    tables = [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    manifest = {"application": app_name, "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "retain_until": today.replace(year=today.year + RETENTION_YEARS).isoformat(),
                "format": "CSV (RFC 4180, UTF-8) + JSON table schema", "tables": {}}  # fmt: skip
    for t in tables:
        cols = src.execute(f'PRAGMA table_info("{t}")').fetchall()  # (cid, name, type, notnull, default, pk)
        rows = src.execute(f'SELECT * FROM "{t}"').fetchall()
        path = out / "tables" / f"{t}.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([c[1] for c in cols])
            w.writerows(rows)
        schema = {"name": t, "fields": [{"name": c[1], "type": c[2] or "TEXT", "required": bool(c[3]),
                                          "primary_key": bool(c[5])} for c in cols]}  # fmt: skip
        (out / "tables" / f"{t}.schema.json").write_text(json.dumps(schema, indent=1))
        manifest["tables"][t] = {
            "rows": len(rows),
            "content_sha256": _table_digest(rows),
            "file": f"tables/{t}.csv",
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    src.close()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    manifest["seconds"] = round(time.perf_counter() - t0, 3)
    return manifest


def verify(out: Path) -> dict:
    """Independent check: rebuild every table from the CSVs alone and compare with the manifest."""
    manifest = json.loads((out / "manifest.json").read_text())
    report = {}
    for t, m in manifest["tables"].items():
        path = out / m["file"]
        file_ok = hashlib.sha256(path.read_bytes()).hexdigest() == m["file_sha256"]
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
        # CSV has no types: compare as text, None exported as ""
        rows_t = [tuple(None if v == "" else v for v in r) for r in rows]
        report[t] = {"rows_ok": len(rows) == m["rows"], "file_ok": file_ok,
                     "content_ok": _table_digest(rows_t) == m["content_sha256"]}  # fmt: skip
    report["all_ok"] = all(v["rows_ok"] and v["file_ok"] and v["content_ok"] for v in report.values())
    return report


def retire(db: Path) -> None:
    """After a verified export, the app's database becomes read-only (the step before decommissioning)."""
    db.chmod(0o444)


def main() -> None:
    out = ROOT / "data" / "archive" / "legacy_ctms"
    m = export(LEGACY / "legacy_ctms.db", out)
    v = verify(out)
    print(json.dumps({"manifest": m, "verify": v}, indent=1))


if __name__ == "__main__":
    main()
