"""SQLite storage for users and key/value settings."""
import os
import secrets
import sqlite3
import time
import uuid as uuidlib
from dataclasses import dataclass
from typing import Optional

from config import cfg

GB = 1024 ** 3
DAY = 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    uuid TEXT NOT NULL,
    sub_token TEXT NOT NULL UNIQUE,
    traffic_limit INTEGER NOT NULL DEFAULT 0,
    up INTEGER NOT NULL DEFAULT 0,
    down INTEGER NOT NULL DEFAULT 0,
    expire_at INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    disabled_reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    tg_id INTEGER,
    warned INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT NOT NULL,
    name TEXT NOT NULL,
    bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, name)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class User:
    id: int
    name: str
    uuid: str
    sub_token: str
    traffic_limit: int
    up: int
    down: int
    expire_at: int
    enabled: int
    disabled_reason: str
    created_at: int
    tg_id: Optional[int]
    warned: int
    note: str
    pending_days: int = 0  # >0: validity starts at first use

    @property
    def used(self) -> int:
        return self.up + self.down

    @property
    def expired(self) -> bool:
        return bool(self.expire_at) and self.expire_at <= time.time()

    @property
    def over_limit(self) -> bool:
        return bool(self.traffic_limit) and self.used >= self.traffic_limit


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(cfg.db_path), exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as c:
        c.executescript(_SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "pending_days" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN pending_days INTEGER NOT NULL DEFAULT 0")


def _row(r) -> Optional[User]:
    return User(**dict(r)) if r else None


def create_user(name: str, limit_gb: float, days: int, first_use: bool = False) -> User:
    now = int(time.time())
    with connect() as c:
        c.execute(
            "INSERT INTO users (name, uuid, sub_token, traffic_limit, expire_at, created_at, pending_days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                str(uuidlib.uuid4()),
                secrets.token_urlsafe(16),
                int(limit_gb * GB),
                now + days * DAY if days and not first_use else 0,
                now,
                days if first_use else 0,
            ),
        )
    return get_by_name(name)


def get(user_id: int) -> Optional[User]:
    with connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_by_name(name: str) -> Optional[User]:
    with connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone())


def get_by_token(token: str) -> Optional[User]:
    with connect() as c:
        return _row(c.execute("SELECT * FROM users WHERE sub_token=?", (token,)).fetchone())


def get_by_tg(tg_id: int) -> list:
    with connect() as c:
        return [_row(r) for r in c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))]


def all_users() -> list:
    with connect() as c:
        return [_row(r) for r in c.execute("SELECT * FROM users ORDER BY id")]


def active_users() -> list:
    return [u for u in all_users() if u.enabled]


def search(q: str) -> list:
    with connect() as c:
        return [_row(r) for r in c.execute(
            "SELECT * FROM users WHERE name LIKE ? OR uuid LIKE ? ORDER BY id", (f"%{q}%", f"%{q}%"))]


def update(user_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with connect() as c:
        c.execute(f"UPDATE users SET {cols} WHERE id=?", (*fields.values(), user_id))


def add_traffic(stats: dict) -> None:
    """stats: {name: (up_delta, down_delta)}"""
    today = time.strftime("%Y-%m-%d")
    with connect() as c:
        for name, (up, down) in stats.items():
            c.execute("UPDATE users SET up=up+?, down=down+? WHERE name=?", (up, down, name))
            c.execute("INSERT INTO usage_daily (day, name, bytes) VALUES (?, ?, ?) "
                      "ON CONFLICT(day, name) DO UPDATE SET bytes=bytes+excluded.bytes", (today, name, up + down))
        # "valid from first use" accounts start their clock now
        c.execute("UPDATE users SET expire_at=?+pending_days*?, pending_days=0 "
                  "WHERE pending_days>0 AND up+down>0", (int(time.time()), DAY))


def usage_since(day: str) -> int:
    with connect() as c:
        return c.execute("SELECT COALESCE(SUM(bytes),0) FROM usage_daily WHERE day>=?", (day,)).fetchone()[0]


def top_usage_since(day: str, limit: int = 5) -> list:
    with connect() as c:
        return [(r["name"], r["b"]) for r in c.execute(
            "SELECT name, SUM(bytes) b FROM usage_daily WHERE day>=? GROUP BY name ORDER BY b DESC LIMIT ?",
            (day, limit))]


def user_daily(name: str, days: int = 7) -> list:
    with connect() as c:
        return [(r["day"], r["bytes"]) for r in c.execute(
            "SELECT day, bytes FROM usage_daily WHERE name=? ORDER BY day DESC LIMIT ?", (name, days))]


def daily_totals(days: int = 7) -> list:
    with connect() as c:
        return [(r["day"], r["b"]) for r in c.execute(
            "SELECT day, SUM(bytes) b FROM usage_daily GROUP BY day ORDER BY day DESC LIMIT ?", (days,))]


def delete(user_id: int) -> None:
    with connect() as c:
        c.execute("DELETE FROM users WHERE id=?", (user_id,))


def extra_admins() -> list:
    return [int(x) for x in get_setting("admins").split(",") if x.strip().lstrip("-").isdigit()]


def admin_ids() -> set:
    """Owners from ADMIN_IDS (cannot be removed from the bot) plus admins added in the bot."""
    return set(cfg.admin_ids) | set(extra_admins())


def get_setting(key: str, default: str = "") -> str:
    with connect() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with connect() as c:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
