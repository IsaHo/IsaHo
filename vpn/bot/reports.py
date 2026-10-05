"""Weekly business report, win-back offers for lapsed customers, and capacity warnings."""
import html
import random
import time

import db
import fmt
import shopdb
import xray

WEEK = 7 * db.DAY


def toman(n: int) -> str:
    return f"{n:,} تومان"


def weekly_text() -> str:
    now = int(time.time())
    since = now - WEEK
    n_sales, s_sales = shopdb.sales_since(since)
    with db.connect() as c:
        kinds = dict(c.execute("SELECT kind, count(*) FROM orders WHERE status='approved' AND decided_at>=? "
                               "GROUP BY kind", (since,)).fetchall())
        new_customers = c.execute("SELECT count(*) FROM customers WHERE created_at>=?", (since,)).fetchone()[0]
    lapsed = [u for u in db.all_users()
              if not u.enabled and u.disabled_reason == "expired" and since <= u.expire_at <= now and u.note != "test"]
    day = time.strftime("%Y-%m-%d", time.localtime(since))
    usage = db.usage_since(day)
    top = db.top_usage_since(day, 5)
    cost = int(db.get_setting("shop_cost", "0") or 0)
    lines = [
        "📅 <b>گزارش هفتگی</b>", "",
        f"💰 فروش: {n_sales} سفارش، <b>{toman(s_sales)}</b>",
        f"   🆕 خرید جدید: {kinds.get('new', 0)} | 🔄 تمدید: {kinds.get('renew', 0)} | ➕ حجم اضافه: {kinds.get('addon', 0)}",
        f"👥 مشتری جدید در ربات: {new_customers}",
        f"⌛️ منقضی و تمدید نکرده: {len(lapsed)}",
        f"📦 مصرف کل: {fmt.size(usage)}",
    ]
    if cost:
        weekly_cost = cost * 7 // 30
        lines.append(f"📊 سود تقریبی هفته (هزینه‌ی {toman(weekly_cost)}): <b>{toman(s_sales - weekly_cost)}</b>")
    if top:
        lines += ["", "🏆 <b>پرمصرف‌ها</b>"] + [f"{i}. {html.escape(n)} — {fmt.size(b)}" for i, (n, b) in enumerate(top, 1)]
    if lapsed:
        lines += ["", "⌛️ <b>تمدید نکرده‌ها</b>", ", ".join(html.escape(u.name) for u in lapsed[:20])]
    return "\n".join(lines)


async def weekly_report(bot, notify) -> None:
    """Saturday mornings (server time), once a week."""
    lt = time.localtime()
    if lt.tm_wday != 5 or lt.tm_hour < 8:
        return
    if time.time() - int(db.get_setting("weekly_sent", "0") or 0) < 6 * db.DAY:
        return
    db.set_setting("weekly_sent", str(int(time.time())))
    await notify(bot, weekly_text())


async def winback(bot, ikb) -> None:
    """One offer to customers whose plan expired 14-30 days ago and who never renewed."""
    pct = int(db.get_setting("winback_discount", "15") or 0)
    if not pct or not db.get_setting("shop_card"):
        return
    now = time.time()
    for u in db.all_users():
        if u.enabled or u.disabled_reason != "expired" or u.note == "test":
            continue
        if not (14 * db.DAY <= now - u.expire_at <= 30 * db.DAY):
            continue
        to = u.owner_tg or u.tg_id
        if not to or db.get_setting(f"winback:{u.id}"):
            continue
        db.set_setting(f"winback:{u.id}", str(int(now)))
        code = f"WB{u.id}X{random.randint(1000, 9999)}"
        shopdb.add_discount(code, pct, 1, 7)
        try:
            await bot.send_message(
                to, f"👋 دلمان برایتان تنگ شده!\nاشتراک <b>{html.escape(u.name)}</b> چند وقتی است تمام شده. "
                    f"اگر تا ۷ روز تمدید کنید <b>{pct}٪ تخفیف</b> دارید (کد <code>{code}</code>).",
                reply_markup=ikb([[(f"🔄 تمدید با {pct}٪ تخفیف", f"rnc:{u.id}:{code}")]]))
        except Exception:
            pass


async def capacity(bot, notify) -> None:
    """Track each day's peak throughput; warn after 3 days in a row above the threshold."""
    today = time.strftime("%Y-%m-%d")
    peak = max(int(db.get_setting(f"peak:{today}", "0") or 0), int(xray.last_rate))
    db.set_setting(f"peak:{today}", str(peak))
    limit_mbps = int(db.get_setting("capacity_mbps", "150") or 0)
    if not limit_mbps or db.get_setting("capacity_warned") == today:
        return
    days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - i * db.DAY)) for i in range(3)]
    peaks = [int(db.get_setting(f"peak:{d}", "0") or 0) * 8 / 1e6 for d in days]
    if all(p >= limit_mbps * 0.85 for p in peaks):
        db.set_setting("capacity_warned", today)
        await notify(bot, f"📈 <b>هشدار ظرفیت</b>\nاوج ترافیک ۳ روز اخیر: "
                          f"{', '.join(f'{p:.0f}' for p in reversed(peaks))} مگابیت (سقف تنظیم‌شده: {limit_mbps}).\n"
                          "وقت اضافه کردن سرور ایران یا خارج جدید است، وگرنه شب‌ها سرعت افت می‌کند.")
