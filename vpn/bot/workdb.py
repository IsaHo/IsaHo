"""Private partner CRM and idempotent wholesale batches. All amounts are toman."""
import re
import secrets
import time
import uuid

import db
import partnerdb
import shopdb
import smspay

MAX_BATCH = 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_contacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, partner_id INTEGER NOT NULL,
 label TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
 UNIQUE(partner_id,label)
);
CREATE TABLE IF NOT EXISTS partner_batches (
 order_id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL, request_key TEXT NOT NULL UNIQUE,
 gb REAL NOT NULL, days INTEGER NOT NULL, quantity INTEGER NOT NULL, unit_price INTEGER NOT NULL,
 last_attempt INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS partner_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, position INTEGER NOT NULL,
 contact_id INTEGER NOT NULL, retail INTEGER NOT NULL, cost INTEGER NOT NULL,
 user_id INTEGER UNIQUE, applied INTEGER NOT NULL DEFAULT 0, UNIQUE(order_id,position)
);
CREATE TABLE IF NOT EXISTS partner_retail_sales (
 item_id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL, amount INTEGER NOT NULL,
 at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS partner_contacts_owner ON partner_contacts(partner_id,id);
CREATE INDEX IF NOT EXISTS partner_batches_owner ON partner_batches(partner_id,order_id);
CREATE TABLE IF NOT EXISTS partner_legacy_links (
 user_id INTEGER PRIMARY KEY, partner_id INTEGER NOT NULL, contact_id INTEGER NOT NULL, at INTEGER NOT NULL
);
"""


def init():
    with db.connect() as c:
        c.executescript(SCHEMA)
        if 'last_attempt' not in {r['name'] for r in c.execute('PRAGMA table_info(partner_batches)')}:
            c.execute('ALTER TABLE partner_batches ADD COLUMN last_attempt INTEGER NOT NULL DEFAULT 0')


def _active(c, partner_id):
    if not c.execute('SELECT 1 FROM partners WHERE tg_id=? AND enabled=1', (partner_id,)).fetchone():
        raise ValueError('نمایندگی فعال نیست')


def _contact(c, partner_id, label, note=''):
    label, note = label.strip(), note.strip()
    if not 1 <= len(label) <= 60 or len(note) > 200 or any(ord(ch) < 32 for ch in label):
        raise ValueError('نام مشتری باید ۱ تا ۶۰ نویسه و یادداشت حداکثر ۲۰۰ نویسه باشد')
    c.execute('INSERT OR IGNORE INTO partner_contacts(partner_id,label,note,created_at) VALUES(?,?,?,?)',
              (partner_id, label, note, int(time.time())))
    return c.execute('SELECT id FROM partner_contacts WHERE partner_id=? AND label=?', (partner_id, label)).fetchone()[0]


def add_contact(partner_id, label, note=''):
    with db.connect() as c:
        _active(c, partner_id)
        return _contact(c, partner_id, label, note)


def contacts(partner_id, page=0, search=''):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT c.*,COUNT(i.user_id) + '
            '(SELECT COUNT(*) FROM partner_legacy_links l JOIN users u ON u.id=l.user_id '
            'WHERE l.contact_id=c.id AND l.partner_id=c.partner_id AND u.owner_tg=c.partner_id) accounts FROM partner_contacts c '
            'LEFT JOIN partner_items i ON i.contact_id=c.id LEFT JOIN orders o ON o.id=i.order_id '
            'WHERE c.partner_id=? AND instr(c.label,?)>0 GROUP BY c.id ORDER BY c.id DESC LIMIT 10 OFFSET ?',
            (partner_id, search[:60], max(0, page)*10))]


def contact(partner_id, contact_id):
    with db.connect() as c:
        r = c.execute('SELECT * FROM partner_contacts WHERE partner_id=? AND id=?', (partner_id, contact_id)).fetchone()
        return dict(r) if r else None


def link_account(partner_id, contact_id, user_id):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM partner_contacts WHERE partner_id=? AND id=?', (partner_id, contact_id)).fetchone():
            raise ValueError('مشتری متعلق به شما پیدا نشد')
        if not c.execute('SELECT 1 FROM users WHERE owner_tg=? AND id=?', (partner_id, user_id)).fetchone():
            raise ValueError('فقط اکانتی که خودتان به‌عنوان نماینده خریده‌اید قابل اتصال است')
        if c.execute('SELECT 1 FROM partner_items WHERE user_id=?', (user_id,)).fetchone():
            raise ValueError('این اکانت گروهی قبلاً مشتری اختصاصی دارد')
        old = c.execute('SELECT * FROM partner_legacy_links WHERE user_id=?', (user_id,)).fetchone()
        if old:
            if old['partner_id'] != partner_id or old['contact_id'] != contact_id:
                raise ValueError('این اکانت قبلاً به مشتری دیگری متصل شده است')
            return False
        c.execute('INSERT INTO partner_legacy_links VALUES(?,?,?,?)', (user_id, partner_id, contact_id, int(time.time())))
        return True


def legacy_accounts(partner_id, contact_id, page=0):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT u.id,u.name FROM partner_legacy_links l JOIN users u ON u.id=l.user_id '
            'WHERE l.partner_id=? AND l.contact_id=? AND u.owner_tg=? ORDER BY u.id DESC LIMIT 10 OFFSET ?',
            (partner_id, contact_id, partner_id, max(0, page)*10))]


def batch(partner_id, order_id):
    with db.connect() as c:
        r = c.execute('SELECT b.*,o.status,o.final_price,o.wallet_used FROM partner_batches b '
                      'JOIN orders o ON o.id=b.order_id WHERE b.partner_id=? AND b.order_id=?', (partner_id, order_id)).fetchone()
        return dict(r) if r else None


def batches(partner_id, page=0):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT b.*,o.status,COUNT(CASE WHEN i.applied=1 THEN 1 END) ready '
            'FROM partner_batches b JOIN orders o ON o.id=b.order_id JOIN partner_items i ON i.order_id=b.order_id '
            'WHERE b.partner_id=? GROUP BY b.order_id ORDER BY b.order_id DESC LIMIT 10 OFFSET ?', (partner_id, max(0, page)*10))]


def items(partner_id, order_id=None, contact_id=None, item_id=None, page=0, limit=100):
    query = ('SELECT i.*,c.label,s.amount received FROM partner_items i '
             'JOIN partner_batches b ON b.order_id=i.order_id JOIN partner_contacts c ON c.id=i.contact_id '
             'LEFT JOIN partner_retail_sales s ON s.item_id=i.id WHERE b.partner_id=?')
    args = [partner_id]
    if order_id is not None:
        query += ' AND i.order_id=?'
        args.append(order_id)
    if contact_id is not None:
        query += ' AND i.contact_id=?'
        args.append(contact_id)
    if item_id is not None:
        query += ' AND i.id=?'
        args.append(item_id)
    with db.connect() as c:
        return [dict(r) for r in c.execute(query + ' ORDER BY i.id DESC LIMIT ? OFFSET ?',
                                          (*args, max(1, min(100, limit)), max(0, page)*limit))]


def parse_rows(text):
    rows = []
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        parts = line.rsplit('|', 1)
        if len(parts) != 2:
            raise ValueError('هر سطر: نام مشتری | قیمت فروش به تومان')
        label, raw = parts[0].strip(), parts[1].strip()
        raw = raw.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
        raw = re.sub(r'[\s,٬]', '', raw)
        if not 1 <= len(label) <= 60 or not re.fullmatch(r'[0-9]{1,12}', raw):
            raise ValueError('نام حداکثر ۶۰ نویسه و قیمت یک عدد صحیح نامنفی است')
        rows.append((label, int(raw)))
    if not 1 <= len(rows) <= MAX_BATCH or len({label for label, _ in rows}) != len(rows):
        raise ValueError(f'هر بسته ۱ تا {MAX_BATCH} مشتری با نام‌های غیرتکراری دارد')
    return rows


def checkout(partner_id, plan_id, rows, request_key, quoted_unit, quoted_terms=None):
    """Reserve purchase wallet and create ONE payable order/batch in one transaction."""
    if not request_key or len(request_key) > 100:
        raise ValueError('فرم نامعتبر است')
    rows = parse_rows('\n'.join(f'{label} | {retail}' for label, retail in rows))
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        prior = c.execute('SELECT order_id,partner_id FROM partner_batches WHERE request_key=?', (request_key,)).fetchone()
        if prior:
            if prior['partner_id'] != partner_id:
                raise ValueError('این فرم متعلق به شما نیست')
            return shopdb.order(prior['order_id'], c), False
        _active(c, partner_id)
        p = c.execute("SELECT * FROM plans WHERE id=? AND active=1 AND kind='plan'", (plan_id,)).fetchone()
        buyer = c.execute('SELECT * FROM customers WHERE tg_id=?', (partner_id,)).fetchone()
        if not p or not 0 < p['price'] < 10**12:
            raise ValueError('پلن معتبر فروش موجود نیست')
        if quoted_terms is not None and tuple(quoted_terms) != (p['gb'], p['days']):
            raise ValueError('حجم یا مدت پلن عوض شده؛ پیش‌نمایش تازه بگیرید')
        unit = p['price'] * (100-buyer['reseller_percent']) // 100
        if unit <= 0:
            raise ValueError('قیمت عمده باید مثبت باشد')
        if unit != quoted_unit:
            raise ValueError('قیمت یا تخفیف عوض شده؛ پیش‌نمایش تازه بگیرید')
        total = unit * len(rows)
        wallet = min(max(0, buyer['balance']), total)
        final = smspay.unique_amount(total-wallet) if total-wallet else 0
        status = 'waiting' if final else 'pending'
        cur = c.execute("INSERT INTO orders(tg_id,plan_id,kind,price,wallet_used,final_price,status,created_at) "
                        "VALUES(?,?,'bulk',?,?,?,?,?)", (partner_id, plan_id, total, wallet, final, status, int(time.time())))
        order_id = cur.lastrowid
        c.execute('UPDATE customers SET balance=balance-? WHERE tg_id=?', (wallet, partner_id))
        c.execute('INSERT INTO partner_batches(order_id,partner_id,request_key,gb,days,quantity,unit_price) VALUES(?,?,?,?,?,?,?)',
                  (order_id, partner_id, request_key, p['gb'], p['days'], len(rows), unit))
        # Actual acquisition includes both wallet and cash, including bank identification digits.
        cost, remainder = divmod(wallet+final, len(rows))
        for pos, (label, retail) in enumerate(rows):
            cid = _contact(c, partner_id, label)
            c.execute('INSERT INTO partner_items(order_id,position,contact_id,retail,cost) VALUES(?,?,?,?,?)',
                      (order_id, pos, cid, retail, cost + int(pos < remainder)))
        o = shopdb.order(order_id, c)
        partnerdb.snapshot(c, o)  # wholesale batches do not generate referral commissions
        return o, True


def provision(partner_id, order_id):
    """Create missing accounts atomically with the item linkage; never duplicate on retry."""
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        b = c.execute("SELECT b.* FROM partner_batches b JOIN orders o ON o.id=b.order_id "
                      "WHERE b.partner_id=? AND b.order_id=? AND o.status='approved'", (partner_id, order_id)).fetchone()
        if not b:
            raise ValueError('سفارش تأییدشدهٔ متعلق به شما پیدا نشد')
        for r in c.execute('SELECT * FROM partner_items WHERE order_id=? AND user_id IS NULL', (order_id,)).fetchall():
            name = f"rp{order_id}_{r['position']+1}_{secrets.token_hex(4)}"
            cur = c.execute("INSERT INTO users(name,uuid,sub_token,traffic_limit,expire_at,created_at,owner_tg,enabled,disabled_reason) "
                "VALUES(?,?,?,?,?,?,?,0,'provisioning')", (name, str(uuid.uuid4()), secrets.token_urlsafe(16),
                int(b['gb'] * db.GB), 0, int(time.time()), partner_id))
            # Validity begins on first use so retries and inventory do not consume paid days.
            c.execute('UPDATE users SET pending_days=? WHERE id=?', (b['days'], cur.lastrowid))
            c.execute('UPDATE partner_items SET user_id=? WHERE id=?', (cur.lastrowid, r['id']))
    return items(partner_id, order_id)


def attempt(partner_id, order_id):
    with db.connect() as c:
        c.execute('UPDATE partner_batches SET last_attempt=? WHERE partner_id=? AND order_id=?',
                  (int(time.time()), partner_id, order_id))


def applied(partner_id, item_id):
    with db.connect() as c:
        c.execute('UPDATE partner_items SET applied=1 WHERE id=? AND order_id IN '
                  '(SELECT order_id FROM partner_batches WHERE partner_id=?)', (item_id, partner_id))


def record_sale(partner_id, item_id, amount):
    if type(amount) is not int or not 0 <= amount < 10**12:
        raise ValueError('مبلغ دریافتی معتبر نیست')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT i.id FROM partner_items i JOIN partner_batches b ON b.order_id=i.order_id '
            'WHERE i.id=? AND b.partner_id=? AND i.applied=1', (item_id, partner_id)).fetchone()
        if not row:
            raise ValueError('اکانت تحویل‌شدهٔ متعلق به شما پیدا نشد')
        old = c.execute('SELECT amount FROM partner_retail_sales WHERE item_id=?', (item_id,)).fetchone()
        if old:
            if old[0] != amount:
                raise ValueError('این فروش قبلاً ثبت شده؛ برای اصلاح با پشتیبانی هماهنگ کنید')
            return False
        c.execute('INSERT INTO partner_retail_sales VALUES(?,?,?,?)', (item_id, partner_id, amount, int(time.time())))
        return True


def financials(partner_id):
    with db.connect() as c:
        r = c.execute('SELECT COUNT(i.id) accounts,COALESCE(SUM(i.cost),0) purchases, '
            'COALESCE(SUM(CASE WHEN i.applied=1 THEN i.retail-i.cost ELSE 0 END),0) expected_margin, '
            'COALESCE(SUM(s.amount),0) revenue, '
            'COALESCE(SUM(CASE WHEN s.item_id IS NOT NULL THEN s.amount-i.cost ELSE 0 END),0) gross_profit, '
            'COALESCE(SUM(CASE WHEN s.item_id IS NULL THEN i.cost ELSE 0 END),0) inventory '
            'FROM partner_items i JOIN partner_batches b ON b.order_id=i.order_id '
            "JOIN orders o ON o.id=i.order_id AND o.status='approved' "
            'LEFT JOIN partner_retail_sales s ON s.item_id=i.id WHERE b.partner_id=?', (partner_id,)).fetchone()
        wallet = c.execute('SELECT balance FROM customers WHERE tg_id=?', (partner_id,)).fetchone()
        reserved = c.execute("SELECT COALESCE(SUM(wallet_used),0) FROM orders WHERE tg_id=? "
                             "AND status IN ('waiting','pending')", (partner_id,)).fetchone()[0]
        return {**dict(r), 'purchase_wallet': wallet[0] if wallet else 0, 'wallet_reserved': reserved}


def ledger(partner_id, page=0):
    with db.connect() as c:
        return [dict(r) for r in c.execute("""
            SELECT * FROM (
             SELECT 'commission' kind, order_id ref,amount,credited_at at FROM partner_sales
              WHERE partner_id=? AND kind='commission' AND credited_at>0 AND amount>0
             UNION ALL SELECT 'purchase',id,final_price+wallet_used,decided_at FROM orders
              WHERE tg_id=? AND status='approved'
             UNION ALL SELECT 'retail',item_id,amount,at FROM partner_retail_sales WHERE partner_id=?
             UNION ALL SELECT 'reserve',id,amount,created_at FROM partner_withdrawals WHERE partner_id=?
             UNION ALL SELECT status,id,amount,decided_at FROM partner_withdrawals WHERE partner_id=? AND status!='pending'
            ) ORDER BY at DESC,kind,ref DESC LIMIT 10 OFFSET ?
            """, (partner_id, partner_id, partner_id, partner_id, partner_id, max(0, page)*10))]
