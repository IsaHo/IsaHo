"""Subscription routing driven exclusively by fresh, independent Iranian probes."""
import html
import json
import logging
import time

import db
import healthdb
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

router = Router()
log = logging.getLogger(__name__)
FRESH = 15 * 60
CONFIRM = 3
MODES = {"off": "خاموش", "rank": "اولویت‌بندی", "hide": "کنارگذاری مسیر خراب"}


def mode():
    value = db.get_setting("routing_mode", "rank")
    return value if value in MODES else "rank"


def catalog():
    import links
    import nodes
    sources = [ip for ip, _ in links.relays()]
    routes = {f"relay:{ip}": (f"IR{i}", [f"relay:{ip}:vpn"])
              for i, ip in enumerate(sources, 1)}
    routes["main"] = ("Reality آلمان", [f"relay:{ip}:main" for ip in sources])
    routes["cdn"] = ("CDN آلمان", [f"relay:{ip}:cdn" for ip in sources])
    for node in nodes.public_nodes():
        name = node["name"]
        routes[f"node:{name}"] = (f"Reality {name}", [f"relay:{ip}:public:{name}" for ip in sources])
    for node in nodes.cdn_nodes():
        name = node["name"]
        routes[f"cdn:{name}"] = (f"CDN {name}", [f"relay:{ip}:cdn:{name}" for ip in sources])
    return routes


def refresh():
    """Persist hysteresis; repeated polling of the same sample never advances counters."""
    now = int(time.time())
    allowed = {key for _, keys in catalog().values() for key in keys}
    for row in healthdb.latest():
        if row.origin != "iran" or row.path_key not in allowed or not 0 <= now - row.checked_at <= FRESH:
            continue
        with db.connect() as c:
            old = c.execute("SELECT * FROM routing_sources WHERE path_key=?", (row.path_key,)).fetchone()
            if old and row.checked_at <= old["checked_at"]:
                continue
            # A long outage in monitoring breaks the consecutive-sample chain.
            continuous = old and row.checked_at - old["checked_at"] <= FRESH
            good = (old["good"] if continuous else 0) + 1 if row.ok else 0
            bad = (old["bad"] if continuous else 0) + 1 if not row.ok else 0
            state = old["state"] if continuous else "unknown"
            if good >= CONFIRM:
                state = "healthy"
            elif bad >= CONFIRM:
                state = "down"
            c.execute("INSERT INTO routing_sources VALUES (?,?,?,?,?,?,?) "
                      "ON CONFLICT(path_key) DO UPDATE SET state=excluded.state, "
                      "checked_at=excluded.checked_at,good=excluded.good,bad=excluded.bad, "
                      "latency_ms=excluded.latency_ms,sample_id=excluded.sample_id",
                      (row.path_key, state, row.checked_at, min(good, CONFIRM), min(bad, CONFIRM),
                       row.latency_ms, row.id))


def states():
    now = int(time.time())
    with db.connect() as c:
        exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='routing_sources'").fetchone()
        sources = {r["path_key"]: dict(r) for r in c.execute("SELECT * FROM routing_sources")} if exists else {}
    result = {}
    for key, (label, expected) in catalog().items():
        rows = [sources[p] for p in expected if p in sources and 0 <= now - sources[p]["checked_at"] <= FRESH]
        healthy = [r for r in rows if r["state"] == "healthy" and r["bad"] == 0]
        if healthy:
            state, latency = "healthy", min(r["latency_ms"] for r in healthy)
        elif expected and len(rows) == len(expected) and all(r["state"] == "down" for r in rows):
            state, latency = "down", 0
        else:
            state, latency = "unknown", 0
        if db.get_setting(f"routing-paused:{key}") == "1":
            state = "paused"
        result[key] = {"label": label, "state": state, "latency": latency}
    return result


def choose(entries):
    """entries: (stable route key, VLESS link); never return an empty enabled subscription."""
    if mode() == "off" or not entries:
        return entries
    current = states()
    rank = {"healthy": 0, "unknown": 1, "down": 2, "paused": 3}
    ordered = sorted(entries, key=lambda item: (
        rank[current.get(item[0], {}).get("state", "unknown")],
        current.get(item[0], {}).get("latency", 0)))
    if mode() == "hide":
        usable = [item for item in ordered if current.get(item[0], {}).get("state") not in {"down", "paused"}]
        return usable or ordered[:1]
    return ordered


def record_delivery(user_id, entries):
    with db.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='routing_subscribers'").fetchone():
            return
        c.execute("INSERT INTO routing_subscribers(user_id,route_keys,seen,notified_at) VALUES (?,?,?,0) "
                  "ON CONFLICT(user_id) DO UPDATE SET route_keys=excluded.route_keys,seen=excluded.seen",
                  (user_id, json.dumps([key for key, _ in entries]), int(time.time())))


async def notify_changes(bot: Bot):
    with db.connect() as c:
        c.execute("DELETE FROM routing_subscribers WHERE seen<?", (int(time.time()) - 7 * db.DAY,))
    current = states()
    previous = json.loads(db.get_setting("routing_last_states", "{}"))
    db.set_setting("routing_last_states", json.dumps({key: item["state"] for key, item in current.items()}))
    if mode() == "off":
        return
    transitions = {key: item for key, item in current.items() if key in previous and
                   ((item["state"] == "down" and previous[key] != "down") or
                    (item["state"] == "healthy" and previous[key] == "down"))}
    if not transitions:
        return
    down_label = "⏸ کنارگذاری" if mode() == "hide" else "↘️ کاهش اولویت"
    text = "🧭 <b>مسیریابی هوشمند</b>\n" + "\n".join(
        f"{'✅ بازگشت' if item['state'] == 'healthy' else down_label} · {html.escape(item['label'])}"
        for item in transitions.values())
    recipients = set(db.admin_ids())
    # Opt-in notices only to owners of subscriptions that actually included the changed route.
    if db.get_setting("routing_customer_notices") == "1":
        now = int(time.time())
        with db.connect() as c:
            subscribers = c.execute("SELECT * FROM routing_subscribers WHERE seen>? AND notified_at<?",
                                    (now - db.DAY, now - 6 * 3600)).fetchall()
        import membership
        for row in subscribers:
            if not set(json.loads(row["route_keys"])) & transitions.keys():
                continue
            user = db.get(row["user_id"])
            if user and user.accessible and membership.controller(user):
                recipients.add(membership.controller(user))
                with db.connect() as c:
                    c.execute("UPDATE routing_subscribers SET notified_at=? WHERE user_id=?", (now, user.id))
    for tg_id in recipients:
        try:
            await bot.send_message(tg_id, text + "\nتغییر مسیر با تازه‌سازی سابسکریپشن دریافت می‌شود.")
        except (TelegramAPIError, OSError, TimeoutError):
            log.warning("routing notification unavailable")


def panel():
    import handlers as h
    icons = {"healthy": "🟢", "down": "🔴", "unknown": "⚪️", "paused": "⏸"}
    titles = {"healthy": "سالم", "down": "سه شکست تأییدشده", "unknown": "در انتظار تست تازه", "paused": "توقف دستی"}
    lines = ["🧭 <b>مسیریابی خودترمیم‌شونده</b>", "", f"حالت: {MODES[mode()]}",
             "۳ شکست مستقل ← کنارگذاری · ۳ موفقیت ← بازگشت", ""]
    rows = []
    for key, item in states().items():
        state = item["state"]
        lines.append(f"{icons[state]} {html.escape(item['label'])} · {titles[state]}")
        target = "0" if state == "paused" else "1"
        rows.append([("▶️ بازگرداندن " + item["label"] if target == "0" else "⏸ توقف " + item["label"],
                      f"rt:ask:{target}:{key}")])
    rows += [[("👁 فقط ترتیب", "rt:mode:rank"), ("🛡 حذف خراب‌ها", "rt:mode:hide")],
             [("⏹ خاموش", "rt:mode:off"), ("🔄 تازه‌سازی", "rt:menu")],
             [("🔕 اعلان مشتری" if db.get_setting("routing_customer_notices") == "1" else "🔔 اعلان مشتری",
               "rt:notice:0" if db.get_setting("routing_customer_notices") == "1" else "rt:notice:1")],
             [("↩️ مرکز تاب‌آوری", "rs:menu")]]
    lines += ["", "تصمیم‌ها از ایران گرفته می‌شوند. مسیر با نتیجهٔ قدیمی حذف نمی‌شود.",
              "اگر همهٔ مسیرها خراب باشند، یک مسیر اضطراری در فهرست می‌ماند؛ این به معنی سالم‌بودن آن نیست."]
    return "\n".join(lines), h.ikb(rows)


@router.callback_query(F.data.startswith("rt:"))
async def callback(cb: CallbackQuery):
    import handlers as h
    from config import cfg
    if cb.from_user.id not in db.admin_ids():
        await cb.answer("فقط مدیر ربات", show_alert=True)
        return
    if cb.data != "rt:menu" and cb.from_user.id not in cfg.admin_ids:
        await cb.answer("تنظیم مسیر فقط برای مالک ربات", show_alert=True)
        return
    parts = cb.data.split(":")
    if len(parts) == 3 and parts[1] == "mode" and parts[2] in MODES:
        db.set_setting("routing_mode", parts[2])
    elif len(parts) == 3 and parts[1] == "notice" and parts[2] in {"0", "1"}:
        db.set_setting("routing_customer_notices", parts[2])
    elif len(parts) >= 4 and parts[1] in {"ask", "pause"} and parts[2] in {"0", "1"}:
        key = ":".join(parts[3:])
        if key not in catalog():
            await cb.answer("مسیر دیگر وجود ندارد", show_alert=True)
            return
        if parts[1] == "ask":
            await cb.answer()
            await cb.message.edit_text(
                f"🧭 <b>{html.escape(catalog()[key][0])}</b>\n\n"
                + ("این مسیر در حالت حذف خراب‌ها کنار گذاشته می‌شود و در حالت ترتیب به انتها می‌رود." if parts[2] == "1" else
                   "توقف دستی برداشته می‌شود؛ سلامت مسیر همچنان از تست ایران تعیین می‌شود.")
                + "\nاتصال فعلی مشتری قطع نمی‌شود؛ تغییر با تازه‌سازی سابسکریپشن دریافت می‌شود.",
                reply_markup=h.ikb([[("✅ تأیید", f"rt:pause:{parts[2]}:{key}")], [("↩️ برگشت", "rt:menu")]]))
            return
        db.set_setting(f"routing-paused:{key}", parts[2])
    elif cb.data != "rt:menu":
        await cb.answer("درخواست نامعتبر", show_alert=True)
        return
    await cb.answer("✅")
    text, kb = panel()
    await cb.message.edit_text(text, reply_markup=kb)
