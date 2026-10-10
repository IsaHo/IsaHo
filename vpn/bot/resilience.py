"""Operational resilience center, incident timeline and guarded self-healing."""

import html
import logging
import time

import db
import fmt
import handlers as h
import health
import healthdb
import identity
import nodes
import pathdb
import relays
import xray
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message
from config import cfg

router = Router()
log = logging.getLogger(__name__)

MODES = {
    "observe": ("👁 فقط پایش", "هیچ اقدام عملیاتی اجرا نمی‌شود"),
    "approve": ("🪄 تأیید یک‌مرحله‌ای", "تعمیر فقط با دکمه مدیر اجرا می‌شود"),
    "auto": ("🤖 خودترمیم امن", "فقط ریستارت تانل رله پس از ۳ شکست متوالی"),
}
RECOVERY_COOLDOWN = 30 * 60


def mode() -> str:
    value = db.get_setting("resilience_mode", "approve")
    return value if value in MODES else "approve"


def _age(stamp: int) -> str:
    seconds = max(0, int(time.time()) - stamp)
    if seconds < 60:
        return "همین حالا"
    if seconds < 3600:
        return f"{seconds // 60} دقیقه پیش"
    return f"{seconds // 3600} ساعت پیش"


def _customer_state() -> tuple[int, int, int | None]:
    expected = {key for key in health._expected_paths() if key.startswith("relay:")}
    rows = [row for row in health.current_checks() if row.origin == "iran"]
    fresh = [row for row in rows if time.time() - row.checked_at <= health.FRESH]
    healthy = [row for row in fresh if row.ok]
    score = (
        round(sum(healthdb.path_score(row) for row in rows) / len(expected))
        if expected and rows
        else None
    )
    return len(healthy), len(expected), score


def overview() -> tuple[str, object]:
    healthy, total, score = _customer_state()
    current_mode = mode()
    incidents = healthdb.incidents(6, active_only=True)
    lines = [
        "🛡 <b>مرکز تاب‌آوری</b>",
        "",
        f"🇮🇷 سلامت واقعی مشتری: <b>{healthy} از {total}</b>",
        f"🎯 امتیاز ایران: <b>{score if score is not None else 'در حال جمع‌آوری'}</b>"
        + ("/100" if score is not None else ""),
        f"⚙️ حالت عملیات: <b>{MODES[current_mode][0]}</b>",
        f"└ {MODES[current_mode][1]}",
        "",
        "🧭 <b>کنترل‌پلین و نسخه‌های یدک</b>",
        f"🟢 فعال فعلی: <code>{cfg.server_ip}</code>",
    ]
    for node in nodes.all_nodes():
        report = nodes.reports.get(node["name"]) or {}
        standby_at = int(report.get("standby_at") or 0)
        fresh = standby_at and time.time() - standby_at < 10 * 60
        icon = (
            "🟢"
            if nodes.online(node) and fresh
            else "🟡"
            if nodes.online(node)
            else "🔴"
        )
        copy = _age(standby_at) if standby_at else "بدون نسخه"
        lines.append(f"{icon} {html.escape(node['name'])} · همگام‌سازی یدک: {copy}")
    lines += ["", "🚨 <b>رخدادهای باز</b>"]
    if incidents:
        for incident in incidents:
            action = f" · {html.escape(incident.action)}" if incident.action else ""
            lines.append(
                f"🔴 {html.escape(incident.label)} · {_age(incident.opened_at)}{action}"
            )
    else:
        lines.append("✅ رخداد بازی وجود ندارد")

    overdue = len(pathdb.due())
    buttons = [
        [("🩺 تست دوباره از ایران", "rs:probe"), ("🔄 تازه‌سازی", "rs:menu")],
        [("⚙️ تغییر حالت عملیات", "rs:modes"), ("📜 تاریخچه رخدادها", "rs:history")],
        [(f"🔄 چرخش آدرس{f' ({overdue})' if overdue else ''}", "rs:rot")],
        [("🚀 آمادگی انتقال کنترل‌پلین", "rs:standby")],
        [("🧭 مسیریابی خودترمیم‌شونده", "rt:menu")],
        [("↩️ شبکه و سلامت", "nav:ops"), ("🏠 خانه", "nav:home")],
    ]
    if incidents:
        buttons.append([("🪄 تعمیر رخدادهای قابل‌ترمیم", "rs:repair")])
    return "\n".join(lines), h.ikb(buttons)


async def auto_recover(
    bot: Bot, row: healthdb.Check, incident: healthdb.Incident
) -> None:
    if mode() != "auto" or not row.path_key.endswith(":vpn"):
        return
    if healthdb.failures(row.path_key, 3) < 3:
        return
    parts = row.path_key.split(":")
    if len(parts) != 3 or parts[0] != "relay":
        return
    ip = parts[1]
    key = f"resilience-restart:{ip}"
    last = int(db.get_setting(key, "0") or 0)
    if time.time() - last < RECOVERY_COOLDOWN:
        return
    db.set_setting(key, str(int(time.time())))
    relays.queue(ip, "restart")
    action = "ریستارت امن تانل‌ها و HAProxy در صف قرار گرفت"
    healthdb.set_incident_action(incident.id, action)
    for admin_id in db.admin_ids():
        try:
            await bot.send_message(
                admin_id,
                f"🤖 <b>خودترمیم اجرا شد</b>\n{html.escape(row.label)}\n{action}",
            )
        except TelegramAPIError:
            log.warning("could not report auto recovery to %s", admin_id)


@router.message(F.text == h.BTN_RESILIENCE, h.admin)
async def resilience_menu(msg: Message):
    text, keyboard = overview()
    await msg.answer(text, reply_markup=keyboard)


@router.callback_query(F.data == "rs:menu", h.admin)
async def resilience_menu_cb(cb: CallbackQuery):
    await cb.answer()
    text, keyboard = overview()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "rs:probe", h.admin)
async def resilience_probe(cb: CallbackQuery):
    health.request_relay_checks()
    await cb.answer("تست‌های ایران در صف ایجنت‌ها قرار گرفت", show_alert=True)
    text, keyboard = overview()
    await cb.message.edit_text(
        text + "\n\n⏳ نتیجهٔ تازه حداکثر تا گزارش بعدی ایجنت ثبت می‌شود.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "rs:modes", h.admin)
async def resilience_modes(cb: CallbackQuery):
    await cb.answer()
    current = mode()
    rows = []
    for key, (title, description) in MODES.items():
        prefix = "✅ " if key == current else ""
        callback = "rs:auto:ask" if key == "auto" else f"rs:mode:{key}"
        rows.append([(prefix + title, callback)])
        rows.append([(f"└ {description[:45]}", "noop_a")])
    rows.append([("↩️ بازگشت", "rs:menu")])
    await cb.message.edit_text(
        "⚙️ <b>حالت عملیات</b>\n\nاقدامات حساس مثل Xray، فایروال و انتقال کنترل‌پلین همیشه دستی می‌مانند.",
        reply_markup=h.ikb(rows),
    )


@router.callback_query(F.data.startswith("rs:mode:"), h.owner)
async def resilience_set_mode(cb: CallbackQuery):
    value = cb.data.rsplit(":", 1)[1]
    if value not in {"observe", "approve"}:
        return
    db.set_setting("resilience_mode", value)
    await cb.answer("حالت عملیات تغییر کرد", show_alert=True)
    text, keyboard = overview()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data == "rs:auto:ask", h.owner)
async def resilience_auto_ask(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "🤖 <b>فعال‌سازی خودترمیم امن؟</b>\n\n"
        "فقط وقتی مسیر کامل یک رله در ۳ تست متوالی قطع باشد، ریستارت تانل‌ها و HAProxy همان رله در صف قرار می‌گیرد. "
        "Xray، فایروال و انتقال کنترل‌پلین خودکار تغییر نمی‌کنند.",
        reply_markup=h.ikb(
            [[("✅ فعال کن", "rs:auto:on"), ("❌ منصرف شدم", "rs:modes")]]
        ),
    )


@router.callback_query(F.data == "rs:auto:on", h.owner)
async def resilience_auto_on(cb: CallbackQuery):
    db.set_setting("resilience_mode", "auto")
    await cb.answer("خودترمیم امن فعال شد", show_alert=True)
    text, keyboard = overview()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(
    F.data.startswith("rs:mode:") | F.data.startswith("rs:auto:"), h.admin
)
async def resilience_mode_denied(cb: CallbackQuery):
    await cb.answer("فقط مالک اصلی می‌تواند حالت عملیات را تغییر دهد", show_alert=True)


@router.callback_query(F.data == "rs:history", h.admin)
async def resilience_history(cb: CallbackQuery):
    await cb.answer()
    rows = healthdb.incidents(12)
    lines = ["📜 <b>تاریخچه رخدادها</b>"]
    if not rows:
        lines.append("\nهنوز رخدادی ثبت نشده است.")
    for item in rows:
        icon = "🔴" if item.status == "open" else "✅"
        duration = (
            f" · {max(1, (item.closed_at - item.opened_at) // 60)} دقیقه"
            if item.closed_at
            else ""
        )
        action = f"\n└ {html.escape(item.action)}" if item.action else ""
        lines.append(
            f"\n{icon} {html.escape(item.label)} · {_age(item.opened_at)}{duration}{action}"
        )
    await cb.message.edit_text(
        "\n".join(lines), reply_markup=h.ikb([[("↩️ مرکز تاب‌آوری", "rs:menu")]])
    )


@router.callback_query(F.data == "rs:repair", h.admin)
async def resilience_repair(cb: CallbackQuery):
    incidents = healthdb.incidents(20, active_only=True)
    queued = set()
    for incident in incidents:
        parts = incident.path_key.split(":")
        if len(parts) == 3 and parts[0] == "relay" and parts[2] == "vpn":
            relays.queue(parts[1], "restart")
            queued.add(parts[1])
            healthdb.set_incident_action(
                incident.id, "ریستارت تانل‌ها و HAProxy با تأیید مدیر در صف قرار گرفت"
            )
    health.request_relay_checks()
    if queued:
        await cb.answer(f"تعمیر {len(queued)} رله در صف قرار گرفت", show_alert=True)
    else:
        await cb.answer(
            "نود خراب خودکار از HAProxy خارج است؛ تست مجدد درخواست شد",
            show_alert=True,
        )
    text, keyboard = overview()
    await cb.message.edit_text(text, reply_markup=keyboard)


PATH_LABELS = {"reality": "⚡ Reality مستقیم", "reality-relay": "🇮🇷 Reality از تانل رله",
               "cdn": "☁️ CDN"}


def _path_label(key: str) -> str:
    return PATH_LABELS.get(key, key)


def _path_address(key: str) -> str:
    """The address this path currently hands to customers, or "" when it is not a single one.

    Used only to offer parking it; an ambiguous path parks nothing rather than guessing."""
    import links
    if key == "reality":
        return cfg.server_ip or ""
    if key == "cdn":
        return links.cdn_address() or ""
    if key == "reality-relay":
        hosts = [host for host, _ in links.relays()]
        return hosts[0] if len(hosts) == 1 else ""
    return ""


def rotation_panel() -> tuple:
    rows_data = pathdb.rows()
    limit, max_age = pathdb.rotate_bytes(), pathdb.rotate_age()
    lines = [
        "🔄 <b>چرخش آدرس</b>",
        "",
        f"آستانه: <b>{fmt.size(limit)}</b> یا <b>{max_age // db.DAY} روز</b> از آخرین چرخش هر مسیر.",
        "",
    ]
    if not rows_data:
        lines.append("هنوز مصرفی ثبت نشده است. شمارش از اولین بازهٔ آماری شروع می‌شود.")
    buttons = []
    for row in rows_data:
        key = row["path_key"]
        icon = "🔴" if row["due"] else "🟢"
        reasons = []
        if row["by_bytes"]:
            reasons.append("حجم")
        if row["by_age"]:
            reasons.append("زمان")
        mark = f" · رسیده ({' و '.join(reasons)})" if reasons else ""
        lines.append(
            f"{icon} {html.escape(_path_label(key))} · {fmt.size(row['bytes'])}"
            f" · {row['age'] // db.DAY} روز{mark}"
        )
        lines.append(f"└ مجموع عمر: {fmt.size(row['total_bytes'])}")
        buttons.append([(f"✅ آدرس {_path_label(key)} را عوض کردم", f"rs:rot:ask:{key}")])
    parked = pathdb.parked()
    ready = sum(1 for item in parked if item["reusable"])
    lines += [
        "",
        "حجمی که یک مسیر از ایران می‌گیرد، سرعت بلاک‌شدن IPش را تعیین می‌کند؛ پس چرخاندن "
        "پیش از سوختن ارزان‌تر از بازیابی بعد از آن است.",
        "هیچ آدرسی خودکار عوض نمی‌شود — فقط شمارش و هشدار.",
    ]
    buttons += [
        [("🔑 کوهورت‌های shortId", "rs:sid")],
        [(f"🅿️ آدرس‌های پارک‌شده ({len(parked)}"
          + (f"، {ready} آزاد" if ready else "") + ")", "rs:burn")],
        [("↩️ مرکز تاب‌آوری", "rs:menu")],
    ]
    return "\n".join(lines), h.ikb(buttons)


@router.callback_query(F.data == "rs:rot", h.admin)
async def rotation_menu(cb: CallbackQuery):
    await cb.answer()
    text, keyboard = rotation_panel()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("rs:rot:ask:"), h.admin)
async def rotation_ask(cb: CallbackQuery):
    key = cb.data.split(":", 3)[3]
    if key not in {row["path_key"] for row in pathdb.rows()}:
        await cb.answer("این مسیر دیگر ثبت نشده است", show_alert=True)
        return
    await cb.answer()
    address = _path_address(key)
    note = (f"\n\nآدرس فعلی <code>{html.escape(address)}</code> پارک می‌شود و بعد از "
            f"{pathdb.cooldown_seconds() // db.DAY} روز به‌عنوان قابل‌استفاده علامت می‌خورد."
            if address else
            "\n\nاین مسیر یک آدرس مشخص ندارد، پس چیزی پارک نمی‌شود؛ فقط شمارش صفر می‌شود.")
    await cb.message.edit_text(
        f"🔄 <b>{html.escape(_path_label(key))}</b>\n\n"
        "این دکمه آدرس را عوض <i>نمی‌کند</i>؛ فقط ثبت می‌کند که شما عوضش کرده‌اید تا شمارش "
        "حجم از صفر شروع شود." + note
        + "\n\nاگر آدرس را واقعاً عوض نکرده‌اید، تأیید نکنید.",
        reply_markup=h.ikb([[("✅ تأیید", f"rs:rot:go:{key}")], [("↩️ برگشت", "rs:rot")]]))


@router.callback_query(F.data.startswith("rs:rot:go:"), h.admin)
async def rotation_done(cb: CallbackQuery):
    key = cb.data.split(":", 3)[3]
    if key not in {row["path_key"] for row in pathdb.rows()}:
        await cb.answer("این مسیر دیگر ثبت نشده است", show_alert=True)
        return
    address = _path_address(key)
    if address:
        pathdb.burn(address, kind=key, note=f"چرخش {_path_label(key)}")
    pathdb.rotate(key)
    await cb.answer("شمارش این مسیر صفر شد", show_alert=True)
    text, keyboard = rotation_panel()
    await cb.message.edit_text(text, reply_markup=keyboard)


def cohort_panel() -> tuple:
    values = identity.short_ids()
    counts = identity.members()
    lines = ["🔑 <b>کوهورت‌های shortId</b>", ""]
    if not values:
        lines.append("هیچ shortId معتبری تنظیم نشده است.")
    buttons = []
    for index, value in enumerate(values):
        lines.append(f"{index + 1}. <code>{html.escape(value[:4])}…</code> · "
                     f"{counts.get(index, 0)} اکانت")
        buttons.append([(f"♻️ چرخش کوهورت {index + 1}", f"rs:sid:ask:{index}")])
    lines += [
        "",
        "سرور همهٔ این مقادیر را می‌پذیرد، پس لینک‌های موجود تا وقتی مقدارشان در فهرست است کار می‌کنند.",
        "چرخش یک کوهورت فقط لینک همان اکانت‌ها را عوض می‌کند و با تازه‌سازی سابسکریپشن می‌رسد.",
        "⚠️ اعمال مقدار جدید روی سرور نیازمند بازنویسی کانفیگ و ری‌استارت Xray است؛ "
        "اتصال‌های فعلی لحظه‌ای قطع می‌شوند.",
    ]
    buttons.append([("↩️ چرخش آدرس", "rs:rot")])
    return "\n".join(lines), h.ikb(buttons)


@router.callback_query(F.data == "rs:sid", h.admin)
async def cohort_menu(cb: CallbackQuery):
    await cb.answer()
    text, keyboard = cohort_panel()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("rs:sid:ask:"), h.owner)
async def cohort_ask(cb: CallbackQuery):
    index = cb.data.rsplit(":", 1)[1]
    if not index.isdigit() or int(index) >= len(identity.short_ids()):
        await cb.answer("این کوهورت وجود ندارد", show_alert=True)
        return
    await cb.answer()
    members = identity.members().get(int(index), 0)
    await cb.message.edit_text(
        f"♻️ <b>چرخش کوهورت {int(index) + 1}</b>\n\n"
        f"{members} اکانت shortId جدید می‌گیرند و باید سابسکریپشن را تازه کنند.\n"
        "کانفیگ Xray بازنویسی و سرویس ری‌استارت می‌شود، پس اتصال‌های فعلی همهٔ کاربران "
        "لحظه‌ای قطع می‌شوند.\n\nمقدار قبلی دیگر پذیرفته نمی‌شود.",
        reply_markup=h.ikb([[("✅ تأیید و ری‌استارت", f"rs:sid:go:{index}")],
                            [("↩️ برگشت", "rs:sid")]]))


@router.callback_query(F.data.startswith("rs:sid:go:"), h.owner)
async def cohort_rotate(cb: CallbackQuery):
    index = cb.data.rsplit(":", 1)[1]
    if not index.isdigit() or int(index) >= len(identity.short_ids()):
        await cb.answer("این کوهورت وجود ندارد", show_alert=True)
        return
    previous = identity.short_ids()[int(index)]
    try:
        identity.rotate(int(index))
        await xray.apply_all()
    except Exception:
        log.exception("shortId rotation failed")
        # put the served value back so links already in customers' hands keep working
        try:
            values = identity.short_ids()
            values[int(index)] = previous
            identity.set_short_ids(values)
            await xray.apply_all()
        except Exception:
            log.exception("shortId rollback failed")
        await cb.answer("چرخش انجام نشد؛ مقدار قبلی برگشت", show_alert=True)
    else:
        await cb.answer("کوهورت چرخید و کانفیگ اعمال شد", show_alert=True)
    text, keyboard = cohort_panel()
    await cb.message.edit_text(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("rs:sid:"), h.admin)
async def cohort_denied(cb: CallbackQuery):
    await cb.answer("چرخش کوهورت فقط برای مالک اصلی", show_alert=True)


@router.callback_query(F.data == "rs:burn", h.admin)
async def burned_menu(cb: CallbackQuery):
    await cb.answer()
    parked = pathdb.parked()
    lines = ["🅿️ <b>آدرس‌های پارک‌شده</b>", ""]
    if not parked:
        lines.append("آدرسی پارک نشده است.")
    buttons = []
    for item in parked:
        if item["reusable"]:
            state = "🟢 آزاد برای استفادهٔ دوباره"
        else:
            state = f"⏳ {item['remaining'] // 3600 + 1} ساعت مانده"
        kind = f" · {html.escape(_path_label(item['kind']))}" if item["kind"] else ""
        lines.append(f"<code>{html.escape(item['address'])}</code>{kind}\n└ {state}")
        buttons.append([(f"🗑 حذف {item['address']}", f"rs:burn:del:{item['address']}")])
    lines += [
        "",
        f"دورهٔ خنک‌شدن: {pathdb.cooldown_seconds() // db.DAY} روز. "
        "گزارش‌های میدانی می‌گویند IP بلاک‌شده بعد از حدود یک هفته بی‌استفادگی برمی‌گردد؛ "
        "این یک تخمین است، نه تضمین.",
        "«آزاد» یعنی دورهٔ خنک‌شدن گذشته، نه اینکه آدرس آزمایش و تأیید شده است.",
    ]
    buttons.append([("↩️ چرخش آدرس", "rs:rot")])
    await cb.message.edit_text("\n".join(lines), reply_markup=h.ikb(buttons))


@router.callback_query(F.data.startswith("rs:burn:del:"), h.admin)
async def burned_release(cb: CallbackQuery):
    pathdb.release(cb.data.split(":", 3)[3])
    await cb.answer("از فهرست حذف شد", show_alert=True)
    await burned_menu(cb)


@router.callback_query(F.data == "rs:standby", h.admin)
async def resilience_standby(cb: CallbackQuery):
    await cb.answer()
    candidates = []
    for node in nodes.all_nodes():
        report = nodes.reports.get(node["name"]) or {}
        stamp = int(report.get("standby_at") or 0)
        ready = nodes.online(node) and stamp and time.time() - stamp < 10 * 60
        candidates.append(
            f"{'🟢' if ready else '🟡'} {html.escape(node['name'])} · "
            f"{'آماده' if ready else 'نیازمند همگام‌سازی'} · {_age(stamp) if stamp else 'بدون نسخه'}"
        )
    text = (
        "🚀 <b>آمادگی انتقال کنترل‌پلین</b>\n\n"
        + "\n".join(candidates)
        + "\n\nبرای جلوگیری از دو ربات و دو دیتابیس فعال، انتقال واقعی همیشه با مرحلهٔ تأیید و fencing انجام می‌شود."
    )
    await cb.message.edit_text(
        text, reply_markup=h.ikb([[("↩️ مرکز تاب‌آوری", "rs:menu")]])
    )
