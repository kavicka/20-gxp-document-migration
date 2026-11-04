"""Append-only, hash-chained audit trail.

Why: GxP data integrity (ALCOA+: attributable, legible, contemporaneous, original, accurate, …) and 21 CFR Part 11
require a secure, computer-generated, time-stamped record of who did what and when, which can't be silently edited.
Each entry stores the hash of the previous one, so changing or deleting any past entry breaks every hash after it.
A database admin could still rewrite the whole chain; production systems anchor the latest hash somewhere else
(a WORM bucket, a signed report). Here the final hash is printed in the migration report for that purpose.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_trail (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_id TEXT NOT NULL,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_trail
BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_trail
BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
"""


def _digest(prev: str, ts: str, actor: str, action: str, object_id: str, details: str) -> str:
    payload = json.dumps([prev, ts, actor, action, object_id, details], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def last_hash(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT hash FROM audit_trail ORDER BY seq DESC LIMIT 1").fetchone()
    return row[0] if row else GENESIS


def record(con: sqlite3.Connection, actor: str, action: str, object_id: str, details: dict) -> str:
    ts = datetime.now(UTC).isoformat(timespec="microseconds")
    det = json.dumps(details, sort_keys=True, ensure_ascii=False)
    prev = last_hash(con)
    h = _digest(prev, ts, actor, action, object_id, det)
    con.execute(
        "INSERT INTO audit_trail (ts, actor, action, object_id, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
        (ts, actor, action, object_id, det, prev, h),
    )
    return h


def verify(con: sqlite3.Connection) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (ok, first broken seq)."""
    prev = GENESIS
    for seq, ts, actor, action, obj, det, p, h in con.execute(
        "SELECT seq, ts, actor, action, object_id, details, prev_hash, hash FROM audit_trail ORDER BY seq"
    ):
        if p != prev or _digest(p, ts, actor, action, obj, det) != h:
            return False, seq
        prev = h
    return True, None
