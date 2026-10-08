"""Persistent support tickets and their conversation history."""

import time
from dataclasses import dataclass

import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    user_id INTEGER,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT 'normal',
    assigned_to INTEGER,
    diagnostic TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    sender_role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'text',
    text TEXT NOT NULL DEFAULT '',
    file_id TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    FOREIGN KEY(ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_support_tickets_status_updated
    ON support_tickets(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_tickets_customer_updated
    ON support_tickets(tg_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_messages_ticket
    ON support_messages(ticket_id, created_at);
"""

OPEN_STATUSES = ("open", "in_progress", "waiting_customer")


@dataclass
class Ticket:
    id: int
    tg_id: int
    user_id: int | None
    category: str
    status: str
    priority: str
    assigned_to: int | None
    diagnostic: str
    created_at: int
    updated_at: int


@dataclass
class TicketMessage:
    id: int
    ticket_id: int
    sender_id: int
    sender_role: str
    kind: str
    text: str
    file_id: str
    created_at: int


def init() -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)


def _ticket(row) -> Ticket | None:
    return Ticket(**dict(row)) if row else None


def create_ticket(
    tg_id: int,
    category: str,
    user_id: int | None = None,
    diagnostic: str = "",
    priority: str = "normal",
) -> Ticket:
    now = int(time.time())
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO support_tickets "
            "(tg_id, user_id, category, status, priority, diagnostic, created_at, updated_at) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (tg_id, user_id, category, priority, diagnostic[:4000], now, now),
        )
        return _ticket(
            c.execute(
                "SELECT * FROM support_tickets WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        )


def get(ticket_id: int) -> Ticket | None:
    with db.connect() as c:
        return _ticket(
            c.execute(
                "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
            ).fetchone()
        )


def add_message(
    ticket_id: int,
    sender_id: int,
    sender_role: str,
    text: str = "",
    kind: str = "text",
    file_id: str = "",
) -> TicketMessage:
    now = int(time.time())
    with db.connect() as c:
        cur = c.execute(
            "INSERT INTO support_messages "
            "(ticket_id, sender_id, sender_role, kind, text, file_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, sender_id, sender_role, kind, text[:4000], file_id, now),
        )
        c.execute(
            "UPDATE support_tickets SET updated_at=? WHERE id=?", (now, ticket_id)
        )
        row = c.execute(
            "SELECT * FROM support_messages WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return TicketMessage(**dict(row))


def messages(ticket_id: int, limit: int = 20) -> list[TicketMessage]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM (SELECT * FROM support_messages WHERE ticket_id=? "
            "ORDER BY created_at DESC LIMIT ?) ORDER BY created_at",
            (ticket_id, limit),
        )
        return [TicketMessage(**dict(r)) for r in rows]


def list_open(limit: int = 50) -> list[Ticket]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM support_tickets WHERE status IN (?, ?, ?) "
            "ORDER BY CASE priority WHEN 'urgent' THEN 0 ELSE 1 END, updated_at DESC LIMIT ?",
            (*OPEN_STATUSES, limit),
        )
        return [_ticket(r) for r in rows]


def list_for_customer(tg_id: int, limit: int = 20) -> list[Ticket]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT * FROM support_tickets WHERE tg_id=? ORDER BY updated_at DESC LIMIT ?",
            (tg_id, limit),
        )
        return [_ticket(r) for r in rows]


def update(ticket_id: int, **fields) -> Ticket | None:
    allowed = {"status", "priority", "assigned_to", "diagnostic"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return get(ticket_id)
    clean["updated_at"] = int(time.time())
    cols = ", ".join(f"{key}=?" for key in clean)
    with db.connect() as c:
        c.execute(
            f"UPDATE support_tickets SET {cols} WHERE id=?",  # noqa: S608 -- columns are allow-listed above
            (*clean.values(), ticket_id),
        )
    return get(ticket_id)


def counts() -> dict[str, int]:
    with db.connect() as c:
        rows = c.execute(
            "SELECT status, count(*) AS n FROM support_tickets GROUP BY status"
        )
        return {r["status"]: r["n"] for r in rows}
