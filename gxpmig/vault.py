"""The target: a small Vault-like document management system (SQLite metadata + content-addressed files).

Content-addressed storage (the file is stored under its SHA-256) gives two things for free: identical content is
stored once, and any later corruption is detectable by re-hashing. MD5 from the legacy system is only used to
check the source; the target uses SHA-256 because MD5 collisions can be manufactured.
"""

import hashlib
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from gxpmig import audit

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_number TEXT PRIMARY KEY,
    legacy_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    document_type TEXT NOT NULL CHECK (document_type IN
        ('Protocol Synopsis', 'Informed Consent Form', 'Clinical Study Report Synopsis')),
    study_number TEXT NOT NULL CHECK (study_number GLOB 'NCT[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    major_version INTEGER NOT NULL CHECK (major_version >= 0),
    minor_version INTEGER NOT NULL CHECK (minor_version >= 0),
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('Draft', 'Approved', 'Superseded')),
    effective_date TEXT NOT NULL,
    country TEXT,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    migrated_at TEXT NOT NULL,
    batch_id TEXT NOT NULL
);
"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


class Vault:
    def __init__(self, root: Path):
        self.root = root
        (root / "content").mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(root / "vault.db", isolation_level=None)  # autocommit; we open transactions
        self.con.executescript(SCHEMA)
        audit.init(self.con)

    def content_path(self, sha: str) -> Path:
        return self.root / "content" / sha[:2] / sha

    def get(self, legacy_id: str) -> dict | None:
        cur = self.con.execute("SELECT * FROM documents WHERE legacy_id = ?", (legacy_id,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row, strict=True)) if row else None

    def known_hashes(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT sha256 FROM documents")}

    def next_number(self) -> str:
        n = self.con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] + 1
        return f"VV-{n:06d}"

    def load(self, meta: dict, src: Path, sha: str, batch_id: str, actor: str) -> str:
        """Copy content, verify it, insert metadata and write the audit entry in one transaction."""
        dst = self.content_path(sha)
        dst.parent.mkdir(exist_ok=True)
        if not dst.exists():
            shutil.copyfile(src, dst)
        if sha256_file(dst) != sha:  # post-copy verification: never trust a copy you haven't re-read
            dst.unlink()
            raise OSError(f"content verification failed for {meta['legacy_id']}")
        doc_number = self.next_number()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.con.execute("BEGIN")
        try:
            self.con.execute(
                "INSERT INTO documents VALUES (:doc_number, :legacy_id, :name, :document_type, :study_number,"
                " :major_version, :minor_version, :lifecycle_state, :effective_date, :country, :sha256, :size,"
                " :migrated_at, :batch_id)",
                {
                    **meta,
                    "doc_number": doc_number,
                    "sha256": sha,
                    "size": dst.stat().st_size,
                    "migrated_at": now,
                    "batch_id": batch_id,
                },  # fmt: skip
            )
            audit.record(self.con, actor, "MIGRATE_LOAD", doc_number, {**meta, "sha256": sha, "batch_id": batch_id})
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return doc_number

    def close(self) -> None:
        self.con.close()
