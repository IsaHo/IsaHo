"""Persistent path-health checks used by the bot dashboard and diagnostics."""

import time
from dataclasses import dataclass

import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS path_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path_key TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    ok INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    checked_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_path_checks_key_time
    ON path_checks(path_key, checked_at DESC);
CREATE INDEX IF NOT EXISTS idx_path_checks_time
    ON path_checks(checked_at DESC);
"""


@dataclass
class Check:
    id: int
    path_key: str
    label: str
    kind: str
    origin: str
    ok: int
    latency_ms: int
    detail: str
    checked_at: int


def init() -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)


def add(
    path_key: str,
    label: str,
    kind: str,
    origin: str,
    ok: bool,
    latency_ms: int = 0,
    detail: str = "",
    checked_at: int | None = None,
) -> Check:
    stamp = checked_at or int(time.time())
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO path_checks "
            "(path_key, label, kind, origin, ok, latency_ms, detail, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                path_key,
                label[:120],
                kind[:32],
                origin[:32],
                int(ok),
                max(0, int(latency_ms)),
                detail[:500],
                stamp,
            ),
        )
        row = c.execute(
            "SELECT * FROM path_checks WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return Check(**dict(row))


def latest() -> list[Check]:
    """Latest check for every stable path key."""
    with db.connect() as c:
        rows = c.execute(
            "SELECT p.* FROM path_checks p JOIN "
            "(SELECT path_key, max(id) AS id FROM path_checks GROUP BY path_key) x "
            "ON p.id=x.id ORDER BY p.kind, p.label"
        )
        return [Check(**dict(r)) for r in rows]


def get(check_id: int) -> Check | None:
    with db.connect() as c:
        row = c.execute("SELECT * FROM path_checks WHERE id=?", (check_id,)).fetchone()
        return Check(**dict(row)) if row else None


def history(path_key: str, limit: int = 12) -> list[Check]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM path_checks WHERE path_key=? ORDER BY id DESC LIMIT ?",
            (path_key, limit),
        )
        return [Check(**dict(r)) for r in rows]


def recent(limit: int = 100) -> list[Check]:
    with db.connect() as c:
        rows = c.execute("SELECT * FROM path_checks ORDER BY id DESC LIMIT ?", (limit,))
        return [Check(**dict(r)) for r in rows]


def failures(path_key: str, limit: int = 3) -> int:
    rows = history(path_key, limit)
    count = 0
    for row in rows:
        if row.ok:
            break
        count += 1
    return count


def prune(days: int = 14) -> None:
    before = int(time.time()) - days * db.DAY
    with db.connect() as c:
        c.execute("DELETE FROM path_checks WHERE checked_at<?", (before,))


def path_score(check: Check, now: int | None = None) -> int:
    age = (now or int(time.time())) - check.checked_at
    if age > 15 * 60 or not check.ok:
        return 0
    if check.latency_ms <= 300:
        return 100
    if check.latency_ms <= 800:
        return 90
    if check.latency_ms <= 1500:
        return 75
    return 60


def score(checks: list[Check] | None = None, now: int | None = None) -> int | None:
    rows = checks if checks is not None else latest()
    if not rows:
        return None
    return round(sum(path_score(row, now) for row in rows) / len(rows))
