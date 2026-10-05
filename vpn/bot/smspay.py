"""Automatic card-to-card verification from forwarded bank SMS.

Each order gets a unique amount (last digits); the admin's phone forwards deposit SMS to
/pay/sms (through an Iranian relay) and an order whose amount appears in a deposit line is
approved. Bank SMS usually show rial, so both toman and rial amounts are matched."""
import hashlib
import random
import re
import time

import db
import shopdb

WINDOW = 2 * db.DAY            # only recent unpaid orders are matched
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_SKIP = ("مانده", "موجودی", "مانده:", "balance", "bal", "remain")


def enabled() -> bool:
    return db.get_setting("sms_key") != "" and db.get_setting("sms_on") == "1"


def key() -> str:
    k = db.get_setting("sms_key")
    if not k:
        k = hashlib.sha256(str(random.random()).encode()).hexdigest()[:24]
        db.set_setting("sms_key", k)
    return k


def unique_amount(base: int) -> int:
    """base + a 1..999 toman suffix that no other open order uses (so a deposit maps to one order)."""
    taken = {o.final_price for o in shopdb.open_orders(int(time.time()) - WINDOW)}
    for _ in range(200):
        amount = base + random.randint(1, 999)
        if amount not in taken:
            return amount
    return base + random.randint(1000, 9999)


def deposit_amounts(text: str) -> set:
    """Numbers on deposit lines of a bank SMS (balance lines and withdrawals are ignored)."""
    text = text.translate(_DIGITS)
    whole = text.replace(" ", "")
    if "برداشت" in whole and "واریز" not in whole:
        return set()
    found = set()
    for line in re.split(r"[\n\r]+", text):
        low = line.lower()
        if any(w in low for w in _SKIP):
            continue
        if "برداشت" in line and "واریز" not in line:
            continue
        for raw in re.findall(r"[+]?\d[\d,٬،.]*\d", line):
            if raw.count(".") and len(raw.split(".")[-1]) != 3:
                continue  # decimals / dates, not grouped amounts
            n = int(re.sub(r"[^\d]", "", raw))
            if 1000 <= n <= 10 ** 11:
                found.add(n)
    return found


def match(text: str) -> tuple:
    """(order or None, reason). Exactly one open order must match."""
    amounts = deposit_amounts(text)
    if not amounts:
        return None, "no deposit amount"
    candidates = [o for o in shopdb.open_orders(int(time.time()) - WINDOW)
                  if o.final_price in amounts or o.final_price * 10 in amounts]
    if not candidates:
        return None, f"no order for {sorted(amounts)}"
    if len(candidates) > 1:
        return None, f"ambiguous: orders {[o.id for o in candidates]}"
    return candidates[0], "ok"
