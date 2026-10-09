"""Storage for the shop: plans, orders, discount codes and customers (wallet, referrals, resellers)."""
import hashlib
import time
from dataclasses import dataclass
from typing import Optional

import db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    gb REAL NOT NULL,
    days INTEGER NOT NULL,
    price INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    plan_id INTEGER NOT NULL,
    kind TEXT NOT NULL,              -- new | renew
    user_id INTEGER,                 -- renew target
    account_name TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL,
    discount_code TEXT NOT NULL DEFAULT '',
    wallet_used INTEGER NOT NULL DEFAULT 0,
    final_price INTEGER NOT NULL,
    status TEXT NOT NULL,            -- waiting | pending | approved | rejected | canceled
    receipt TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    decided_at INTEGER NOT NULL DEFAULT 0,
    decided_by INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discounts (
    code TEXT PRIMARY KEY,
    percent INTEGER NOT NULL,
    max_uses INTEGER NOT NULL DEFAULT 0,
    used INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sms_log (
    hash TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    at INTEGER NOT NULL,
    order_id INTEGER,
    result TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS customers (
    tg_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    referrer INTEGER,
    test_used INTEGER NOT NULL DEFAULT 0,
    balance INTEGER NOT NULL DEFAULT 0,
    reseller_percent INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
"""


@dataclass
class Plan:
    id: int
    title: str
    gb: float
    days: int
    price: int
    active: int
    kind: str = "plan"  # plan | addon (extra traffic for an existing account)


@dataclass
class Order:
    id: int
    tg_id: int
    plan_id: int
    kind: str
    user_id: Optional[int]
    account_name: str
    price: int
    discount_code: str
    wallet_used: int
    final_price: int
    status: str
    receipt: str
    created_at: int
    decided_at: int
    decided_by: int


@dataclass
class Customer:
    tg_id: int
    name: str
    referrer: Optional[int]
    test_used: int
    balance: int
    reseller_percent: int
    created_at: int


def init() -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "owner_tg" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN owner_tg INTEGER")
        if "kind" not in {r["name"] for r in c.execute("PRAGMA table_info(plans)")}:
            c.execute("ALTER TABLE plans ADD COLUMN kind TEXT NOT NULL DEFAULT 'plan'")


# ---------- plans ----------

def plans(only_active: bool = True, kind: str = "plan") -> list:
    q = "SELECT * FROM plans WHERE kind=?" + (" AND active=1" if only_active else "") + " ORDER BY price"
    with db.connect() as c:
        return [Plan(**dict(r)) for r in c.execute(q, (kind,))]


def addons(only_active: bool = True) -> list:
    return plans(only_active, "addon")


def plan(plan_id: int) -> Optional[Plan]:
    with db.connect() as c:
        r = c.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return Plan(**dict(r)) if r else None


def add_plan(title: str, gb: float, days: int, price: int, kind: str = "plan") -> None:
    with db.connect() as c:
        c.execute("INSERT INTO plans (title, gb, days, price, kind) VALUES (?, ?, ?, ?, ?)",
                  (title, gb, days, price, kind))


def set_price(plan_id: int, price: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE plans SET price=? WHERE id=?", (price, plan_id))


def toggle_plan(plan_id: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE plans SET active=1-active WHERE id=?", (plan_id,))


def delete_plan(plan_id: int) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM plans WHERE id=?", (plan_id,))


# ---------- customers ----------

def customer(tg_id: int, name: str = "", referrer: Optional[int] = None) -> Customer:
    """Get or create; the referrer only sticks on the very first visit."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM customers WHERE tg_id=?", (tg_id,)).fetchone()
        if not r:
            ref = referrer if referrer and referrer != tg_id else None
            c.execute("INSERT INTO customers (tg_id, name, referrer, created_at) VALUES (?, ?, ?, ?)",
                      (tg_id, name, ref, int(time.time())))
            r = c.execute("SELECT * FROM customers WHERE tg_id=?", (tg_id,)).fetchone()
        elif name and r["name"] != name:
            c.execute("UPDATE customers SET name=? WHERE tg_id=?", (name, tg_id))
        return Customer(**dict(r))


def update_customer(tg_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as c:
        c.execute(f"UPDATE customers SET {cols} WHERE tg_id=?", (*fields.values(), tg_id))


def reset_trial_history() -> int:
    """Allow one fresh trial per customer; preserve accounts, wallets and orders."""
    with db.connect() as c:
        return c.execute("UPDATE customers SET test_used=0 WHERE test_used<>0").rowcount


def add_balance(tg_id: int, amount: int) -> None:
    with db.connect() as c:
        c.execute("UPDATE customers SET balance=balance+? WHERE tg_id=?", (amount, tg_id))


def resellers() -> list:
    with db.connect() as c:
        return [Customer(**dict(r)) for r in c.execute("SELECT * FROM customers WHERE reseller_percent>0")]


def referral_count(tg_id: int) -> int:
    with db.connect() as c:
        return c.execute("SELECT count(*) FROM customers WHERE referrer=?", (tg_id,)).fetchone()[0]


def customer_count() -> int:
    with db.connect() as c:
        return c.execute("SELECT count(*) FROM customers").fetchone()[0]


# ---------- discounts ----------

def discount(code: str) -> Optional[dict]:
    """A usable code (exists, not expired, uses left) or None."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM discounts WHERE code=?", (code.upper(),)).fetchone()
    if not r:
        return None
    d = dict(r)
    if d["expires_at"] and d["expires_at"] < time.time():
        return None
    if d["max_uses"] and d["used"] >= d["max_uses"]:
        return None
    return d


def discounts() -> list:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM discounts ORDER BY code")]


def add_discount(code: str, percent: int, max_uses: int, days: int) -> None:
    with db.connect() as c:
        c.execute("INSERT OR REPLACE INTO discounts (code, percent, max_uses, used, expires_at) VALUES (?, ?, ?, 0, ?)",
                  (code.upper(), percent, max_uses, int(time.time()) + days * db.DAY if days else 0))


def delete_discount(code: str) -> None:
    with db.connect() as c:
        c.execute("DELETE FROM discounts WHERE code=?", (code,))


def use_discount(code: str) -> None:
    with db.connect() as c:
        c.execute("UPDATE discounts SET used=used+1 WHERE code=?", (code,))


# ---------- orders ----------

def create_order(**fields) -> Order:
    fields.setdefault("created_at", int(time.time()))
    cols = ", ".join(fields)
    with db.connect() as c:
        cur = c.execute(f"INSERT INTO orders ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
        return order(cur.lastrowid, c)


def order(order_id: int, conn=None) -> Optional[Order]:
    c = conn or db.connect()
    r = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return Order(**dict(r)) if r else None


def update_order(order_id: int, **fields) -> None:
    cols = ", ".join(f"{k}=?" for k in fields)
    with db.connect() as c:
        c.execute(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), order_id))


def decide_order(order_id: int, status: str, admin_id: int) -> bool:
    """Atomically move pending -> approved/rejected; False if someone else already decided."""
    with db.connect() as c:
        cur = c.execute("UPDATE orders SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
                        (status, int(time.time()), admin_id, order_id))
        return cur.rowcount == 1


def auto_approve(order_id: int) -> bool:
    """waiting/pending -> approved by SMS; False if already decided."""
    with db.connect() as c:
        cur = c.execute("UPDATE orders SET status='approved', decided_at=?, decided_by=0 "
                        "WHERE id=? AND status IN ('waiting','pending')", (int(time.time()), order_id))
        return cur.rowcount == 1


def open_orders(since: int) -> list:
    with db.connect() as c:
        return [Order(**dict(r)) for r in c.execute(
            "SELECT * FROM orders WHERE status IN ('waiting','pending') AND created_at>=? ORDER BY id", (since,))]


def expire_waiting(before: int) -> list:
    """Cancel unpaid orders (no receipt) older than `before`; returns them so wallets can be refunded."""
    with db.connect() as c:
        rows = [Order(**dict(r)) for r in c.execute(
            "SELECT * FROM orders WHERE status='waiting' AND created_at<?", (before,))]
        c.execute("UPDATE orders SET status='canceled' WHERE status='waiting' AND created_at<?", (before,))
        return rows


def log_sms(text: str) -> bool:
    """Remember an SMS; False if this exact message was already processed."""
    h = hashlib.sha256(text.encode()).hexdigest()
    with db.connect() as c:
        if c.execute("SELECT 1 FROM sms_log WHERE hash=?", (h,)).fetchone():
            return False
        c.execute("INSERT INTO sms_log (hash, text, at) VALUES (?, ?, ?)", (h, text[:1000], int(time.time())))
        return True


def sms_result(text: str, order_id, result: str) -> None:
    h = hashlib.sha256(text.encode()).hexdigest()
    with db.connect() as c:
        c.execute("UPDATE sms_log SET order_id=?, result=? WHERE hash=?", (order_id, result, h))


def last_sms(n: int = 5) -> list:
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM sms_log ORDER BY at DESC LIMIT ?", (n,))]


def pending_orders() -> list:
    with db.connect() as c:
        return [Order(**dict(r)) for r in c.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id")]


def sales_since(ts: int) -> tuple:
    with db.connect() as c:
        r = c.execute("SELECT count(*) n, COALESCE(SUM(final_price),0) s FROM orders "
                      "WHERE status='approved' AND decided_at>=?", (ts,)).fetchone()
        return r["n"], r["s"]
