"""Partner commissions: immutable order snapshots and atomic withdrawal reservations.

Purchase-wallet credit is deliberately separate from cash commission. Old orders
without a snapshot never acquire a retrospective financial entitlement.
"""
import time
from types import SimpleNamespace

import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS partners (
 tg_id INTEGER PRIMARY KEY, percent INTEGER NOT NULL DEFAULT 0 CHECK(percent BETWEEN 0 AND 100),
 enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS partner_sales (
 order_id INTEGER PRIMARY KEY, partner_id INTEGER, percent INTEGER NOT NULL,
 amount INTEGER NOT NULL, kind TEXT NOT NULL, credited_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS partner_withdrawals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, partner_id INTEGER NOT NULL,
 amount INTEGER NOT NULL CHECK(amount>0), destination TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','rejected')),
 created_at INTEGER NOT NULL, decided_at INTEGER NOT NULL DEFAULT 0,
 decided_by INTEGER NOT NULL DEFAULT 0, reference TEXT NOT NULL DEFAULT '',
 request_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS partner_sales_owner ON partner_sales(partner_id, credited_at);
CREATE INDEX IF NOT EXISTS partner_withdraw_owner ON partner_withdrawals(partner_id, status);
CREATE TABLE IF NOT EXISTS partner_assignments (
 customer_id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS partner_rate_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, partner_id INTEGER NOT NULL,
 old_percent INTEGER, new_percent INTEGER NOT NULL,
 old_enabled INTEGER, new_enabled INTEGER NOT NULL, owner_id INTEGER NOT NULL, at INTEGER NOT NULL
);
"""


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)
        c.execute("INSERT OR IGNORE INTO partners(tg_id) SELECT tg_id FROM customers WHERE reseller_percent>0")
        # Preserve the old generic reward for orders already open at deployment;
        # they must not acquire a new cash-commission liability retroactively.
        rows = c.execute("SELECT o.* FROM orders o LEFT JOIN partner_sales s ON s.order_id=o.id "
                         "WHERE s.order_id IS NULL AND o.status IN ('waiting','pending')").fetchall()
        for row in rows:
            snapshot(c, SimpleNamespace(**dict(row)), legacy=True)


def profile(tg_id):
    with db.connect() as c:
        r = c.execute("SELECT * FROM partners WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(r) if r else None


def configure(tg_id, percent, enabled=True, owner_id=0):
    from config import cfg
    if owner_id and owner_id not in cfg.admin_ids:
        raise PermissionError("فقط مالک اجازه تغییر نرخ دارد")
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError("درصد باید بین صفر و صد باشد")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM customers WHERE tg_id=?", (tg_id,)).fetchone():
            raise ValueError("نماینده باید ابتدا ربات را شروع کند")
        old = c.execute("SELECT * FROM partners WHERE tg_id=?", (tg_id,)).fetchone()
        c.execute("INSERT INTO partners VALUES(?,?,?) ON CONFLICT(tg_id) DO UPDATE SET "
                  "percent=excluded.percent, enabled=excluded.enabled", (tg_id, percent, int(enabled)))
        c.execute("INSERT INTO partner_rate_audit(partner_id,old_percent,new_percent,old_enabled,new_enabled,owner_id,at) "
                  "VALUES(?,?,?,?,?,?,?)", (tg_id, old['percent'] if old else None, percent,
                  old['enabled'] if old else None, int(enabled), owner_id, int(time.time())))


def snapshot(c, o, legacy=False):
    """Called inside the order-creation transaction; rate and attribution cannot drift."""
    buyer = c.execute("SELECT referrer FROM customers WHERE tg_id=?", (o.tg_id,)).fetchone()
    ref = buyer["referrer"] if buyer else None
    p = c.execute("SELECT * FROM partners WHERE tg_id=?", (ref,)).fetchone()
    kind, pct = "none", 0
    if ref and ref != o.tg_id and o.kind in ("new", "renew", "addon"):
        if p and not legacy:  # no double payment through the generic referral wallet
            kind, pct = "commission", p["percent"] if p["enabled"] else 0
        else:
            kind = "wallet"
            raw = c.execute("SELECT value FROM settings WHERE key='shop_ref_percent'").fetchone()
            pct = max(0, min(100, int(raw[0] or 0))) if raw else 0
    # Only external, actually-paid cash; wallet gifts / free tests earn no cash.
    amount = max(0, o.final_price) * pct // 100
    c.execute("INSERT INTO partner_sales VALUES(?,?,?,?,?,0)", (o.id, ref, pct, amount, kind))


def settle(order_id):
    """Exactly once after service fulfillment; approved status is also checked in SQL."""
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT s.* FROM partner_sales s JOIN orders o ON o.id=s.order_id "
                        "WHERE s.order_id=? AND s.credited_at=0 AND o.status='approved'", (order_id,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE partner_sales SET credited_at=? WHERE order_id=?", (int(time.time()), order_id))
        if row["kind"] == "wallet" and row["amount"]:
            c.execute("UPDATE customers SET balance=balance+? WHERE tg_id=?", (row["amount"], row["partner_id"]))
        return dict(row) if row["amount"] else None


def _summary(c, tg_id):
    r = c.execute("SELECT COUNT(*) sales, COALESCE(SUM(amount),0) earned FROM partner_sales "
                  "WHERE partner_id=? AND kind='commission' AND credited_at>0 AND amount>0", (tg_id,)).fetchone()
    w = c.execute("SELECT COALESCE(SUM(CASE WHEN status='pending' THEN amount ELSE 0 END),0) reserved, "
                  "COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END),0) paid "
                  "FROM partner_withdrawals WHERE partner_id=?", (tg_id,)).fetchone()
    customers = c.execute("SELECT COUNT(*) FROM customers WHERE referrer=?", (tg_id,)).fetchone()[0]
    return {**dict(r), **dict(w), "available": r["earned"] - w["reserved"] - w["paid"], "customers": customers}


def summary(tg_id):
    with db.connect() as c:
        return _summary(c, tg_id)


def pending_count():
    with db.connect() as c:
        return c.execute("SELECT COUNT(*) FROM partner_withdrawals WHERE status='pending'").fetchone()[0]


def request(tg_id, amount, destination, request_key):
    if (type(amount) is not int or not 0 < amount <= 2**63-1 or not request_key
            or len(request_key) > 100 or not destination.strip() or len(destination) > 200):
        raise ValueError("مبلغ و مقصد برداشت معتبر نیست")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT * FROM partner_withdrawals WHERE request_key=?", (request_key,)).fetchone()
        if old:
            if old["partner_id"] != tg_id or old["amount"] != amount or old["destination"] != destination:
                raise ValueError("درخواست تکراری ناسازگار است")
            return dict(old), False
        p = c.execute("SELECT enabled FROM partners WHERE tg_id=?", (tg_id,)).fetchone()
        if not p:
            raise ValueError("نمایندگی ثبت نشده است")
        if _summary(c, tg_id)["available"] < amount:
            raise ValueError("موجودی قابل برداشت کافی نیست")
        if c.execute("SELECT 1 FROM partner_withdrawals WHERE partner_id=? AND status='pending'", (tg_id,)).fetchone():
            raise ValueError("ابتدا منتظر بررسی درخواست قبلی بمانید")
        cur = c.execute("INSERT INTO partner_withdrawals(partner_id,amount,destination,created_at,request_key) "
                        "VALUES(?,?,?,?,?)", (tg_id, amount, destination[:200], int(time.time()), request_key))
        return dict(c.execute("SELECT * FROM partner_withdrawals WHERE id=?", (cur.lastrowid,)).fetchone()), True


def withdrawal(request_id):
    with db.connect() as c:
        r = c.execute("SELECT * FROM partner_withdrawals WHERE id=?", (request_id,)).fetchone()
        return dict(r) if r else None


def decide(request_id, status, owner_id, reference):
    from config import cfg
    if owner_id not in cfg.admin_ids:
        raise PermissionError("فقط مالک اجازه تسویه دارد")
    if status not in ("paid", "rejected") or not reference.strip():
        raise ValueError("شناسه انتقال یا علت رد لازم است")
    with db.connect() as c:
        return c.execute("UPDATE partner_withdrawals SET status=?,decided_at=?,decided_by=?,reference=? "
                         "WHERE id=? AND status='pending'", (status, int(time.time()), owner_id,
                         reference[:200], request_id)).rowcount == 1


def history(tg_id, page=0):
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT * FROM partner_sales WHERE partner_id=? AND kind='commission' "
                    "AND credited_at>0 AND amount>0 ORDER BY order_id DESC LIMIT 10 OFFSET ?", (tg_id, max(0, page)*10))]


def withdrawals(tg_id=None, page=0):
    with db.connect() as c:
        q = "SELECT * FROM partner_withdrawals WHERE "
        where, args = ("partner_id=?", (tg_id,)) if tg_id is not None else ("status='pending'", ())
        return [dict(r) for r in c.execute(q + where + " ORDER BY id DESC LIMIT 10 OFFSET ?", (*args, max(0, page)*10))]


def customers(tg_id, page=0):
    with db.connect() as c:
        # Aggregate only: never expose customer account credentials to their referrer.
        return [dict(r) for r in c.execute("SELECT c.name,c.created_at,COUNT(s.order_id) purchases FROM customers c "
            "LEFT JOIN orders o ON o.tg_id=c.tg_id LEFT JOIN partner_sales s ON s.order_id=o.id "
            "AND s.kind='commission' AND s.credited_at>0 AND s.partner_id=? "
            "WHERE c.referrer=? GROUP BY c.tg_id ORDER BY c.created_at DESC LIMIT 10 OFFSET ?",
            (tg_id, tg_id, max(0, page)*10))]


def partners():
    with db.connect() as c:
        return [dict(r) for r in c.execute("SELECT p.*,c.name,c.reseller_percent FROM partners p "
                                         "JOIN customers c ON c.tg_id=p.tg_id ORDER BY p.enabled DESC,p.tg_id")]


def assign(partner_id, customer_id, owner_id):
    """Owner-only first attribution for old customers, never overwrites a referrer."""
    from config import cfg
    if owner_id not in cfg.admin_ids:
        raise PermissionError("فقط مالک اجازه اتصال مشتری دارد")
    if partner_id == customer_id:
        raise ValueError("معرف و مشتری نمی‌توانند یک نفر باشند")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM partners WHERE tg_id=? AND enabled=1", (partner_id,)).fetchone():
            raise ValueError("نمایندگی فعال نیست")
        changed = c.execute("UPDATE customers SET referrer=? WHERE tg_id=? AND referrer IS NULL",
                            (partner_id, customer_id)).rowcount
        if not changed:
            raise ValueError("مشتری وجود ندارد یا قبلاً معرف دارد؛ معرف قبلی تغییر نمی‌کند")
        c.execute("INSERT INTO partner_assignments VALUES(?,?,?,?)",
                  (customer_id, partner_id, owner_id, int(time.time())))
