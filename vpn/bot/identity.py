"""Per-account REALITY identity: short-id cohorts and spiderX.

One shortId shared by every account means a leaked or burned identity cannot be replaced
without reissuing every link. Each account is pinned to a cohort the first time it needs
one and keeps it afterwards, so replacing a single entry in the list only changes the links
of that cohort. The server always serves the whole list, so links issued with any entry
still in it keep working.

spiderX is the client's initial crawler path. Upstream says it should differ per client;
a constant value across a whole userbase is itself a shared marker. The derived value is
stored, not regenerated, so refreshing a subscription does not churn the link.
"""
import logging
import re
import secrets

import db
from config import cfg

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_identity (
    user_id INTEGER PRIMARY KEY,
    cohort INTEGER NOT NULL DEFAULT 0,
    spider TEXT NOT NULL DEFAULT ''
);
"""

# Xray accepts up to 16 hex characters, even length.
SHORT_ID_RE = re.compile(r"^[0-9a-f]{2,16}$")
DEFAULT_COHORTS = 4


def init() -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)


def _valid(value: str) -> bool:
    value = value.strip().lower()
    return bool(SHORT_ID_RE.match(value)) and len(value) % 2 == 0


def short_ids() -> list:
    """The shortIds the server accepts. Falls back to the installed one so an upgrade of
    an existing deployment keeps serving the links already in customers' hands."""
    raw = db.get_setting("reality_short_ids", "")
    values = [v.strip().lower() for v in raw.split(",") if _valid(v)]
    if values:
        return values
    return [cfg.reality_short_id] if _valid(cfg.reality_short_id or "") else []


def set_short_ids(values: list) -> list:
    """Store a validated list. Invalid entries are dropped rather than served."""
    clean, seen = [], set()
    for value in values:
        value = str(value).strip().lower()
        if _valid(value) and value not in seen:
            seen.add(value)
            clean.append(value)
    if not clean:
        raise ValueError("no valid shortId")
    db.set_setting("reality_short_ids", ",".join(clean))
    return clean


def ensure_pool(size: int = DEFAULT_COHORTS) -> list:
    """Grow the pool to `size` entries, keeping existing ones (and therefore existing links)
    in place. Returns the stored list."""
    values = short_ids()
    if not values:
        return []
    while len(values) < max(1, size):
        values.append(secrets.token_hex(8))
    return set_short_ids(values)


def ensure_runtime_pool(server_active: bool) -> list:
    """Bot-only upgrades must not publish identities the running Xray never loaded.

    Fresh installations can provision all cohorts before starting Xray. An active
    deployment keeps its existing pool; expansion needs an explicit data-plane reload.
    """
    return ensure_pool(1 if server_active else DEFAULT_COHORTS)


def rotate(cohort: int) -> str:
    """Replace one entry with a fresh secret. Only this cohort's accounts are affected; the
    caller must rewrite the Xray config for the server to accept the new value."""
    values = short_ids()
    if not 0 <= cohort < len(values):
        raise ValueError("unknown cohort")
    values[cohort] = secrets.token_hex(8)
    return set_short_ids(values)[cohort]


def spider_enabled() -> bool:
    """On by default; a single switch in case a client turns out to mishandle `spx`."""
    return db.get_setting("reality_spider", "1") == "1"


def _record(user_id: int) -> tuple:
    """Read this account's (cohort, spider), assigning them once on first use."""
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='user_identity'").fetchone():
            return 0, ""
        row = c.execute("SELECT cohort,spider FROM user_identity WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return int(row["cohort"]), str(row["spider"] or "")
        pool = max(1, len(short_ids()))
        cohort = secrets.randbelow(pool)
        spider = "/" + secrets.token_hex(4)
        c.execute("INSERT INTO user_identity (user_id, cohort, spider) VALUES (?,?,?) "
                  "ON CONFLICT(user_id) DO NOTHING", (user_id, cohort, spider))
        row = c.execute("SELECT cohort,spider FROM user_identity WHERE user_id=?", (user_id,)).fetchone()
    return (int(row["cohort"]), str(row["spider"] or "")) if row else (cohort, spider)


def cohort(user) -> int:
    return _record(user.id)[0]


def short_id(user) -> str:
    """The shortId this account's links carry. A pool that shrank below the stored cohort
    falls back to a served entry instead of handing out one the server would reject."""
    values = short_ids()
    if not values:
        return cfg.reality_short_id or ""
    return values[_record(user.id)[0] % len(values)]


def spider(user) -> str:
    return _record(user.id)[1] if spider_enabled() else ""


def members() -> dict:
    """cohort index -> number of accounts, for the admin panel."""
    counts = {index: 0 for index in range(len(short_ids()))}
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='user_identity'").fetchone():
            return counts
        pool = max(1, len(short_ids()))
        for row in c.execute("SELECT cohort, COUNT(*) AS n FROM user_identity GROUP BY cohort"):
            counts[int(row["cohort"]) % pool] = counts.get(int(row["cohort"]) % pool, 0) + int(row["n"])
    return counts


def forget(user_id: int) -> None:
    with db.connect() as c:
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='user_identity'").fetchone():
            c.execute("DELETE FROM user_identity WHERE user_id=?", (user_id,))
