"""Operational resilience center, incident timeline and guarded self-healing."""

import html
import logging
import time

import db
import handlers as h
import health
import healthdb
import nodes
import relays
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

    buttons = [
        [("🩺 تست دوباره از ایران", "rs:probe"), ("🔄 تازه‌سازی", "rs:menu")],
        [("⚙️ تغییر حالت عملیات", "rs:modes"), ("📜 تاریخچه رخدادها", "rs:history")],
        [("🚀 آمادگی انتقال کنترل‌پلین", "rs:standby")],
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
