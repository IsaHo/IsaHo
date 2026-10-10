"""Per-path traffic accounting and the burned-address registry.

Measurement from inside proxy infrastructure (Alaraj & Wustrow, ASIA CCS '25) shows that
Iranian networks block a destination IP in proportion to the client volume they observe on
it: a range split into subranges with increasing connection weights was blocked in weight
order. So the number worth watching per endpoint is bytes-since-last-rotation, not uptime.

Thresholds default to the cadence operators report as working (~50 GB or ~4 days per
endpoint, and about a week of idling before a blocked address becomes usable again). They
are reported figures, not measured here; both are settings so they can be retuned once the
operator has their own numbers. Crossing one only raises an alert — nothing is rotated
automatically, because rotating an address is not reversible from the bot's side.
"""
import time

import db

GB = 1024 ** 3
DEFAULT_ROTATE_GB = 50
DEFAULT_ROTATE_DAYS = 4
DEFAULT_COOLDOWN_DAYS = 7
WARN_EVERY = 12 * 3600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS path_usage (
    path_key TEXT PRIMARY KEY,
    bytes INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    rotated_at INTEGER NOT NULL DEFAULT 0,
    warned_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS burned_addresses (
    address TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT '',
    burned_at INTEGER NOT NULL DEFAULT 0,
    cooldown_until INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT ''
);
"""


def init() -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)


def _exists(c, table: str) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())


def rotate_bytes() -> int:
    try:
        value = float(db.get_setting("rotate_gb", "") or DEFAULT_ROTATE_GB)
    except ValueError:
        value = DEFAULT_ROTATE_GB
    return int(max(1.0, value) * GB)


def rotate_age() -> int:
    try:
        days = float(db.get_setting("rotate_days", "") or DEFAULT_ROTATE_DAYS)
    except ValueError:
        days = DEFAULT_ROTATE_DAYS
    return int(max(1.0, days) * db.DAY)


def cooldown_seconds() -> int:
    try:
        days = float(db.get_setting("burn_cooldown_days", "") or DEFAULT_COOLDOWN_DAYS)
    except ValueError:
        days = DEFAULT_COOLDOWN_DAYS
    return int(max(0.0, days) * db.DAY)


def add_usage(samples: dict) -> None:
    """samples: {path_key: bytes_since_last_sample}. Counters are deltas, so a restart of
    Xray loses at most one interval rather than resetting the accumulated total."""
    now = int(time.time())
    with db.connect() as c:
        if not _exists(c, "path_usage"):
            return
        for key, value in samples.items():
            value = int(value or 0)
            if not key or value <= 0:
                continue
            c.execute(
                "INSERT INTO path_usage (path_key, bytes, total_bytes, rotated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(path_key) DO UPDATE SET bytes=bytes+excluded.bytes, "
                "total_bytes=total_bytes+excluded.bytes",
                (str(key)[:64], value, value, now))


def rows() -> list:
    """Every tracked path with how close it is to the rotation thresholds."""
    now = int(time.time())
    limit, max_age = rotate_bytes(), rotate_age()
    out = []
    with db.connect() as c:
        if not _exists(c, "path_usage"):
            return out
        for row in c.execute("SELECT * FROM path_usage ORDER BY bytes DESC"):
            item = dict(row)
            age = max(0, now - int(item["rotated_at"] or now))
            item["age"] = age
            item["by_bytes"] = int(item["bytes"]) >= limit
            item["by_age"] = age >= max_age
            item["due"] = item["by_bytes"] or item["by_age"]
            out.append(item)
    return out


def due() -> list:
    return [row for row in rows() if row["due"]]


def should_warn(path_key: str) -> bool:
    """One alert per path per WARN_EVERY, so a path left unrotated does not spam admins."""
    now = int(time.time())
    with db.connect() as c:
        if not _exists(c, "path_usage"):
            return False
        row = c.execute("SELECT warned_at FROM path_usage WHERE path_key=?", (path_key,)).fetchone()
        if row and now - int(row["warned_at"] or 0) < WARN_EVERY:
            return False
        c.execute("UPDATE path_usage SET warned_at=? WHERE path_key=?", (now, path_key))
    return True


def rotate(path_key: str) -> None:
    """Reset a path's baseline after its address actually changed. Lifetime total is kept."""
    now = int(time.time())
    with db.connect() as c:
        if _exists(c, "path_usage"):
            c.execute("UPDATE path_usage SET bytes=0, rotated_at=?, warned_at=0 WHERE path_key=?",
                      (now, path_key))


def forget(path_key: str) -> None:
    with db.connect() as c:
        if _exists(c, "path_usage"):
            c.execute("DELETE FROM path_usage WHERE path_key=?", (path_key,))


# --- burned addresses ---------------------------------------------------------------

def burn(address: str, kind: str = "", note: str = "") -> int:
    """Park a retired address and return when it is worth trying again. Re-burning an
    address restarts its cooldown, since the clock runs from the last time it saw traffic."""
    address = str(address).strip()[:64]
    if not address:
        raise ValueError("empty address")
    now = int(time.time())
    until = now + cooldown_seconds()
    with db.connect() as c:
        if not _exists(c, "burned_addresses"):
            return until
        c.execute(
            "INSERT INTO burned_addresses (address, kind, burned_at, cooldown_until, note) "
            "VALUES (?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET kind=excluded.kind, "
            "burned_at=excluded.burned_at, cooldown_until=excluded.cooldown_until, note=excluded.note",
            (address, str(kind)[:16], now, until, str(note)[:200]))
    return until


def parked() -> list:
    """Burned addresses, soonest-reusable first, each with whether its cooldown has passed."""
    now = int(time.time())
    out = []
    with db.connect() as c:
        if not _exists(c, "burned_addresses"):
            return out
        for row in c.execute("SELECT * FROM burned_addresses ORDER BY cooldown_until"):
            item = dict(row)
            item["reusable"] = now >= int(item["cooldown_until"] or 0)
            item["remaining"] = max(0, int(item["cooldown_until"] or 0) - now)
            out.append(item)
    return out


def reusable() -> list:
    return [row for row in parked() if row["reusable"]]


def release(address: str) -> None:
    """Drop an address from the registry, e.g. once it is back in service or given up."""
    with db.connect() as c:
        if _exists(c, "burned_addresses"):
            c.execute("DELETE FROM burned_addresses WHERE address=?", (str(address).strip()[:64],))
