"""Guided diagnostics and a persistent support-ticket workflow."""

import html
import logging
import sqlite3
import time

import db
import devices
import fmt
import handlers as h
import links
import psutil
import shopdb
import supportdb
import tunnels
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

router = Router()
log = logging.getLogger(__name__)

CATEGORIES = {
    "connect": "اتصال برقرار نمی‌شود",
    "slow": "سرعت یا قطعی",
    "subscription": "آپدیت سابسکریپشن",
    "payment": "خرید و پرداخت",
    "account": "حجم، زمان یا دستگاه",
    "other": "موضوع دیگر",
}
STATUS = {
    "open": "جدید",
    "in_progress": "در حال بررسی",
    "waiting_customer": "منتظر پاسخ شما",
    "resolved": "حل‌شده",
    "closed": "بسته‌شده",
}
STATUS_ICON = {
    "open": "🔵",
    "in_progress": "🟣",
    "waiting_customer": "🟠",
    "resolved": "🟢",
    "closed": "⚪️",
}
ACCOUNT_CATEGORIES = {"connect", "slow", "subscription", "account"}


class SupportFlow(StatesGroup):
    customer_message = State()
    staff_reply = State()


def home_kb():
    return h.ikb(
        [
            [("🩺 بررسی هوشمند اتصال", "sup:diagnose")],
            [("✍️ ساخت تیکت جدید", "sup:new"), ("🎫 تیکت‌های من", "sup:mine")],
            [("📚 آموزش اتصال", "sup:help")],
        ]
    )


def category_kb(mode: str):
    return h.ikb(
        [
            [
                ("🚫 وصل نمی‌شوم", f"sup:issue:{mode}:connect"),
                ("🐢 سرعت پایین است", f"sup:issue:{mode}:slow"),
            ],
            [
                ("🔄 سابسکریپشن", f"sup:issue:{mode}:subscription"),
                ("💳 خرید و پرداخت", f"sup:issue:{mode}:payment"),
            ],
            [
                ("📊 حساب و دستگاه‌ها", f"sup:issue:{mode}:account"),
                ("💬 موضوع دیگر", f"sup:issue:{mode}:other"),
            ],
            [("↩️ بازگشت", "sup:home")],
        ]
    )


def account_kb(accounts, mode: str, category: str):
    rows = [
        [(f"{fmt.status_icon(u)} {u.name}", f"sup:acc:{mode}:{category}:{u.id}")]
        for u in accounts[:20]
    ]
    rows.append([("↩️ بازگشت", "sup:diagnose" if mode == "d" else "sup:new")])
    return h.ikb(rows)


def _owned(tg_id: int, user_id: int):
    return next((u for u in db.owned_by(tg_id) if u.id == user_id), None)


def _latest_order(tg_id: int):
    try:
        with db.connect() as c:
            return c.execute(
                "SELECT * FROM orders WHERE tg_id=? ORDER BY created_at DESC LIMIT 1",
                (tg_id,),
            ).fetchone()
    except sqlite3.Error:
        return None


def diagnostic_report(
    category: str, tg_id: int, user_id: int | None = None
) -> tuple[str, str]:
    """Return a customer-safe report and suggested ticket priority."""
    u = _owned(tg_id, user_id) if user_id else None
    lines = ["🩺 <b>نتیجه بررسی هوشمند</b>", ""]
    priority = "normal"

    if category == "payment":
        order = _latest_order(tg_id)
        if not order:
            lines += [
                "⚪️ سفارش قبلی برای این حساب تلگرام پیدا نشد.",
                "اگر مبلغی پرداخت کرده‌اید، تیکت بسازید و تصویر رسید را بفرستید.",
            ]
        else:
            labels = {
                "waiting": "منتظر پرداخت یا رسید",
                "pending": "در صف بررسی",
                "approved": "تأیید و تحویل‌شده",
                "rejected": "ردشده",
                "canceled": "لغوشده",
            }
            state = labels.get(order["status"], order["status"])
            lines += [
                f"🧾 آخرین سفارش: <b>#{order['id']}</b>",
                f"وضعیت: <b>{state}</b>",
            ]
            if order["status"] == "pending":
                lines.append("رسید شما ثبت شده و نیازی به ارسال دوباره نیست.")
        return "\n".join(lines), priority

    if not u:
        lines += [
            "⚪️ اشتراکی به این حساب تلگرام متصل نیست.",
            "اگر قبلاً خرید کرده‌اید، لینک اختصاصی اتصال به ربات را باز کنید یا تیکت بسازید.",
        ]
        return "\n".join(lines), priority

    lines.append(f"👤 حساب: <b>{html.escape(u.name)}</b>")
    if not u.enabled:
        reasons = {
            "expired": "زمان اشتراک تمام شده",
            "traffic": "حجم اشتراک تمام شده",
            "manual": "حساب توسط مدیر غیرفعال شده",
        }
        lines.append(f"🔴 وضعیت: <b>{reasons.get(u.disabled_reason, 'غیرفعال')}</b>")
        priority = (
            "urgent" if u.disabled_reason not in ("expired", "traffic") else "normal"
        )
    elif u.expired:
        lines.append("🔴 وضعیت: زمان اشتراک به پایان رسیده است.")
    elif u.over_limit:
        lines.append("🔴 وضعیت: حجم اشتراک تمام شده است.")
    else:
        lines.append("🟢 حساب فعال است و حجم/زمان آن تمام نشده.")
    limit = fmt.size(u.traffic_limit) if u.traffic_limit else "نامحدود"
    lines += [
        f"📦 مصرف: <b>{fmt.size(u.used)}</b> از <b>{limit}</b>",
        f"⏳ اعتبار: <b>{fmt.remaining_days(u)}</b>",
    ]

    if category in {"connect", "slow", "subscription"}:
        try:
            paths = tunnels.status()
        except psutil.Error:
            log.warning("could not inspect tunnel sessions during support diagnostic")
            paths = []
        healthy = sum(1 for *_, sessions in paths if sessions)
        if paths:
            lines.append(
                f"{'🟢' if healthy else '🔴'} مسیرهای ایران: <b>{healthy} از {len(paths)} مسیر آماده</b>"
            )
            if not healthy:
                priority = "urgent"
        config_count = len(links.all_links(u))
        lines.append(
            f"{'🟢' if config_count else '🔴'} کانفیگ‌های قابل دریافت: <b>{config_count}</b>"
        )
        if not config_count:
            priority = "urgent"

    if category in {"slow", "account"}:
        active = devices.count(u.name)
        limit = u.ip_limit or int(db.get_setting("ip_limit_default", "0") or 0)
        if limit:
            lines.append(f"📱 شبکه‌های فعال اخیر: <b>{active}</b> از سقف <b>{limit}</b>")
        else:
            lines.append(f"📱 شبکه‌های فعال اخیر: <b>{active}</b>")

    if category == "subscription":
        lines += [
            "",
            "برای آپدیت، VPN را خاموش کنید، داخل برنامه گزینه‌ی Update Subscription را بزنید و سپس دوباره وصل شوید.",
            "📥 لینک سابسکریپشن:",
            f"<code>{html.escape(links.sub_url(u))}</code>",
        ]
    elif category == "connect" and u.enabled and not u.expired and not u.over_limit:
        lines += [
            "",
            "پیشنهاد سریع:",
            "۱) VPN را خاموش کنید و سابسکریپشن را به‌روز کنید.",
            "۲) اینترنت را یک بار بین Wi‑Fi و دیتای موبایل جابه‌جا کنید.",
            "۳) کانفیگ دیگری از همان سابسکریپشن را امتحان کنید.",
        ]
    elif category == "slow":
        lines += [
            "",
            "برای مقایسه، یک‌بار با دیتای موبایل و یک‌بار با Wi‑Fi تست کنید و نتیجه را در تیکت بنویسید.",
        ]

    return "\n".join(lines), priority


def diagnostic_kb(category: str, user_id: int | None, u=None):
    rows = []
    if u and (u.expired or u.disabled_reason == "expired"):
        rows.append([("🔄 تمدید همین حساب", f"rn:{u.id}")])
    if u and (u.over_limit or u.disabled_reason == "traffic") and shopdb.addons():
        rows.append([("➕ خرید حجم اضافه", f"ad:{u.id}")])
    uid = user_id or 0
    rows += [
        [
            ("✅ مشکل حل شد", "sup:solved"),
            ("🎫 هنوز مشکل دارم", f"sup:escalate:{category}:{uid}"),
        ],
        [("🔁 بررسی موضوع دیگر", "sup:diagnose")],
    ]
    return h.ikb(rows)


def _age(ts: int) -> str:
    seconds = max(0, int(time.time()) - ts)
    if seconds < 3600:
        return f"{max(1, seconds // 60)} دقیقه پیش"
    if seconds < 86400:
        return f"{seconds // 3600} ساعت پیش"
    return f"{seconds // 86400} روز پیش"


def _message_summary(msg: supportdb.TicketMessage) -> str:
    who = "👤" if msg.sender_role == "customer" else "🎧"
    body = html.escape(msg.text or f"[{msg.kind}]")
    return f"{who} {body[:500]}"


def ticket_text(ticket: supportdb.Ticket, staff_view: bool = False) -> str:
    u = db.get(ticket.user_id) if ticket.user_id else None
    lines = [
        f"{STATUS_ICON.get(ticket.status, '⚪️')} <b>تیکت #{ticket.id}</b>",
        f"وضعیت: <b>{STATUS.get(ticket.status, ticket.status)}</b>",
        f"موضوع: {CATEGORIES.get(ticket.category, ticket.category)}",
        f"حساب: <b>{html.escape(u.name) if u else '—'}</b>",
        f"آخرین تغییر: {_age(ticket.updated_at)}",
    ]
    if staff_view:
        lines += [
            f"کاربر تلگرام: <code>{ticket.tg_id}</code>",
            f"مسئول: <code>{ticket.assigned_to or 'تعیین نشده'}</code>",
        ]
        if ticket.diagnostic:
            lines += [
                "",
                "<b>خلاصه بررسی خودکار</b>",
                html.escape(ticket.diagnostic[:1200]),
            ]
    history = supportdb.messages(ticket.id, 8)
    if history:
        lines += ["", "<b>آخرین پیام‌ها</b>", *[_message_summary(m) for m in history]]
    return "\n".join(lines)


def customer_ticket_kb(ticket: supportdb.Ticket):
    rows = []
    if ticket.status not in {"resolved", "closed"}:
        rows.append(
            [
                ("✍️ افزودن پیام", f"sup:reply:{ticket.id}"),
                ("✅ حل شد", f"sup:close:{ticket.id}"),
            ]
        )
    rows.append([("↩️ تیکت‌های من", "sup:mine")])
    return h.ikb(rows)


def staff_ticket_kb(ticket: supportdb.Ticket):
    rows = []
    if ticket.status not in {"resolved", "closed"}:
        rows.append(
            [
                ("🙋 رسیدگی با من", f"st:claim:{ticket.id}"),
                ("✍️ پاسخ", f"st:reply:{ticket.id}"),
            ]
        )
        rows.append(
            [
                ("⏳ منتظر کاربر", f"st:wait:{ticket.id}"),
                ("✅ بستن تیکت", f"st:close:{ticket.id}"),
            ]
        )
    rows.append([("↩️ صف تیکت‌ها", "st:list")])
    return h.ikb(rows)


@router.message(F.text == h.BTN_SUPPORT)
async def support_home(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "🛟 <b>مرکز راهنمایی و پشتیبانی</b>\n\n"
        "اول می‌توانم حساب و مسیر اتصال را در چند ثانیه بررسی کنم. اگر حل نشد، نتیجه‌ی همین بررسی همراه تیکت برای پشتیبان فرستاده می‌شود.",
        reply_markup=home_kb(),
    )


@router.callback_query(F.data == "sup:home")
async def support_home_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(
        "🛟 <b>مرکز راهنمایی و پشتیبانی</b>\n\nاز کجا شروع کنیم؟",
        reply_markup=home_kb(),
    )


@router.callback_query(F.data == "sup:help")
async def support_help(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        h.HELP_TEXT, reply_markup=home_kb(), disable_web_page_preview=True
    )


@router.callback_query(F.data.in_({"sup:diagnose", "sup:new"}))
async def choose_issue(cb: CallbackQuery):
    await cb.answer()
    diagnosing = cb.data == "sup:diagnose"
    title = "🩺 چه چیزی را بررسی کنم؟" if diagnosing else "✍️ موضوع تیکت را انتخاب کنید"
    await cb.message.edit_text(
        title, reply_markup=category_kb("d" if diagnosing else "t")
    )


@router.callback_query(F.data.startswith("sup:issue:"))
async def choose_issue_account(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, _, mode, category = cb.data.split(":", 3)
    if category not in CATEGORIES or mode not in {"d", "t"}:
        return
    accounts = db.owned_by(cb.from_user.id)
    if category in ACCOUNT_CATEGORIES and len(accounts) > 1:
        await cb.message.edit_text(
            "👤 کدام حساب را بررسی کنیم؟",
            reply_markup=account_kb(accounts, mode, category),
        )
        return
    user_id = accounts[0].id if category in ACCOUNT_CATEGORIES and accounts else None
    await _continue_issue(cb, state, mode, category, user_id)


@router.callback_query(F.data.startswith("sup:acc:"))
async def selected_account(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, _, mode, category, raw_id = cb.data.split(":", 4)
    user_id = int(raw_id)
    if not _owned(cb.from_user.id, user_id):
        await cb.message.edit_text(
            "❌ این حساب به تلگرام شما متصل نیست.", reply_markup=home_kb()
        )
        return
    await _continue_issue(cb, state, mode, category, user_id)


async def _continue_issue(
    cb: CallbackQuery, state: FSMContext, mode: str, category: str, user_id: int | None
) -> None:
    if mode == "d" and category != "other":
        report, _ = diagnostic_report(category, cb.from_user.id, user_id)
        u = _owned(cb.from_user.id, user_id) if user_id else None
        await cb.message.edit_text(
            report,
            reply_markup=diagnostic_kb(category, user_id, u),
            disable_web_page_preview=True,
        )
        return
    await ask_customer_message(cb.message, state, category, user_id)


@router.callback_query(F.data == "sup:solved")
async def solved(cb: CallbackQuery):
    await cb.answer("خوشحالیم که حل شد 🌱", show_alert=True)
    await cb.message.edit_text(
        "✅ عالی! اگر دوباره مشکلی پیش آمد، مرکز پشتیبانی همیشه در دسترس است.",
        reply_markup=home_kb(),
    )


@router.callback_query(F.data.startswith("sup:escalate:"))
async def escalate(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, _, category, raw_id = cb.data.split(":", 3)
    user_id = int(raw_id) or None
    if user_id and not _owned(cb.from_user.id, user_id):
        return
    report, priority = diagnostic_report(category, cb.from_user.id, user_id)
    await ask_customer_message(cb.message, state, category, user_id, report, priority)


async def ask_customer_message(
    message: Message,
    state: FSMContext,
    category: str,
    user_id: int | None,
    diagnostic: str = "",
    priority: str = "normal",
    ticket_id: int | None = None,
) -> None:
    await state.set_state(SupportFlow.customer_message)
    await state.update_data(
        category=category,
        user_id=user_id,
        diagnostic=diagnostic,
        priority=priority,
        ticket_id=ticket_id,
    )
    prompt = (
        "✍️ <b>یک توضیح کوتاه بفرستید</b>\n\n"
        "مثلاً نام برنامه، نوع اینترنت و متنی که هنگام اتصال می‌بینید. عکس خطا یا رسید را هم می‌توانید بفرستید."
    )
    if ticket_id:
        prompt = f"✍️ پیام جدید برای تیکت <b>#{ticket_id}</b> را بفرستید. عکس یا فایل هم قابل ارسال است."
    await message.answer(prompt, reply_markup=h.CANCEL_KB)


def _content(msg: Message) -> tuple[str, str, str]:
    text = msg.text or msg.caption or ""
    if msg.photo:
        return "photo", text, msg.photo[-1].file_id
    if msg.document:
        return "document", text or (msg.document.file_name or ""), msg.document.file_id
    if msg.video:
        return "video", text, msg.video.file_id
    if msg.voice:
        return "voice", text, msg.voice.file_id
    return "text", text, ""


@router.message(SupportFlow.customer_message)
async def customer_message(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    kind, text, file_id = _content(msg)
    if not text and not file_id:
        await msg.answer("لطفاً متن، عکس یا فایل مرتبط را بفرستید.")
        return
    ticket_id = data.get("ticket_id")
    is_reply = bool(ticket_id)
    if ticket_id:
        ticket = supportdb.get(ticket_id)
        if (
            not ticket
            or ticket.tg_id != msg.from_user.id
            or ticket.status in {"resolved", "closed"}
        ):
            await state.clear()
            await msg.answer(
                "این تیکت دیگر امکان دریافت پیام ندارد.", reply_markup=h.USER_KB
            )
            return
        supportdb.add_message(
            ticket.id, msg.from_user.id, "customer", text, kind, file_id
        )
        supportdb.update(ticket.id, status="in_progress")
    else:
        ticket = supportdb.create_ticket(
            msg.from_user.id,
            data.get("category", "other"),
            data.get("user_id"),
            data.get("diagnostic", ""),
            data.get("priority", "normal"),
        )
        supportdb.add_message(
            ticket.id, msg.from_user.id, "customer", text, kind, file_id
        )
    await state.clear()
    ticket = supportdb.get(ticket.id)
    await _notify_staff(bot, msg, ticket, is_reply)
    title = (
        f"پیام به تیکت #{ticket.id} اضافه شد"
        if is_reply
        else f"تیکت #{ticket.id} ثبت شد"
    )
    await msg.answer(
        f"✅ <b>{title}</b>\n"
        "پاسخ پشتیبانی همین‌جا برایتان می‌آید. نیازی نیست پیام را دوباره ارسال کنید.",
        reply_markup=customer_ticket_kb(ticket),
    )


async def _notify_staff(
    bot: Bot, source: Message, ticket: supportdb.Ticket, is_reply: bool
) -> None:
    u = db.get(ticket.user_id) if ticket.user_id else None
    header = (
        f"🎫 <b>{'پیام جدید · ' if is_reply else ''}تیکت "
        f"{'فوری ' if ticket.priority == 'urgent' else ''}#{ticket.id}</b>\n"
        f"موضوع: {CATEGORIES.get(ticket.category, ticket.category)}\n"
        f"کاربر: <code>{ticket.tg_id}</code> | حساب: <b>{html.escape(u.name) if u else '—'}</b>"
    )
    for staff_id in db.staff_ids():
        try:
            await bot.send_message(
                staff_id,
                header,
                reply_markup=h.ikb([[("🔎 باز کردن تیکت", f"st:view:{ticket.id}")]]),
            )
            await source.copy_to(staff_id)
        except TelegramAPIError:
            log.warning(
                "could not notify support staff %s about ticket %s", staff_id, ticket.id
            )


@router.callback_query(F.data == "sup:mine")
async def my_tickets(cb: CallbackQuery):
    await cb.answer()
    tickets = supportdb.list_for_customer(cb.from_user.id)
    if not tickets:
        await cb.message.edit_text("🎫 هنوز تیکتی ندارید.", reply_markup=home_kb())
        return
    rows = [
        [
            (
                f"{STATUS_ICON.get(t.status, '⚪️')} #{t.id} · {CATEGORIES.get(t.category, t.category)}",
                f"sup:view:{t.id}",
            )
        ]
        for t in tickets[:12]
    ]
    rows.append([("↩️ بازگشت", "sup:home")])
    await cb.message.edit_text(
        "🎫 <b>تیکت‌های من</b>\n\nبرای مشاهده، یکی را انتخاب کنید:",
        reply_markup=h.ikb(rows),
    )


@router.callback_query(F.data.startswith("sup:view:"))
async def customer_ticket_view(cb: CallbackQuery):
    await cb.answer()
    ticket = supportdb.get(int(cb.data.rsplit(":", 1)[1]))
    if not ticket or ticket.tg_id != cb.from_user.id:
        return
    await cb.message.edit_text(
        ticket_text(ticket), reply_markup=customer_ticket_kb(ticket)
    )


@router.callback_query(F.data.startswith("sup:reply:"))
async def customer_ticket_reply(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    ticket = supportdb.get(int(cb.data.rsplit(":", 1)[1]))
    if not ticket or ticket.tg_id != cb.from_user.id:
        return
    await ask_customer_message(
        cb.message, state, ticket.category, ticket.user_id, ticket_id=ticket.id
    )


@router.callback_query(F.data.startswith("sup:close:"))
async def customer_ticket_close(cb: CallbackQuery):
    ticket = supportdb.get(int(cb.data.rsplit(":", 1)[1]))
    if not ticket or ticket.tg_id != cb.from_user.id:
        await cb.answer()
        return
    ticket = supportdb.update(ticket.id, status="resolved")
    await cb.answer("تیکت حل‌شده ثبت شد", show_alert=True)
    await cb.message.edit_text(
        ticket_text(ticket), reply_markup=customer_ticket_kb(ticket)
    )


@router.message(F.text == h.BTN_TICKETS, h.staff)
async def staff_tickets(msg: Message):
    await show_staff_list(msg)


async def show_staff_list(target) -> None:
    tickets = supportdb.list_open()
    counts = supportdb.counts()
    text = (
        "🎧 <b>صف پشتیبانی</b>\n\n"
        f"🔵 جدید: {counts.get('open', 0)} | 🟣 در حال بررسی: {counts.get('in_progress', 0)} | "
        f"🟠 منتظر کاربر: {counts.get('waiting_customer', 0)}"
    )
    rows = [
        [
            (
                f"{'🔴 ' if t.priority == 'urgent' else ''}#{t.id} · {CATEGORIES.get(t.category, t.category)} · {_age(t.updated_at)}",
                f"st:view:{t.id}",
            )
        ]
        for t in tickets[:30]
    ]
    kb = h.ikb(rows) if rows else None
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(
            text + ("" if rows else "\n\n✅ تیکت بازی ندارید."), reply_markup=kb
        )
    else:
        await target.answer(
            text + ("" if rows else "\n\n✅ تیکت بازی ندارید."), reply_markup=kb
        )


@router.callback_query(F.data == "st:list", h.staff)
async def staff_list_cb(cb: CallbackQuery):
    await cb.answer()
    await show_staff_list(cb)


@router.callback_query(F.data.startswith("st:view:"), h.staff)
async def staff_ticket_view(cb: CallbackQuery):
    await cb.answer()
    ticket = supportdb.get(int(cb.data.rsplit(":", 1)[1]))
    if ticket:
        await cb.message.edit_text(
            ticket_text(ticket, True), reply_markup=staff_ticket_kb(ticket)
        )


@router.callback_query(F.data.startswith("st:claim:"), h.staff)
async def staff_claim(cb: CallbackQuery):
    ticket_id = int(cb.data.rsplit(":", 1)[1])
    ticket = supportdb.update(
        ticket_id, assigned_to=cb.from_user.id, status="in_progress"
    )
    await cb.answer("رسیدگی به نام شما ثبت شد")
    if ticket:
        await cb.message.edit_text(
            ticket_text(ticket, True), reply_markup=staff_ticket_kb(ticket)
        )


@router.callback_query(F.data.startswith("st:reply:"), h.staff)
async def staff_reply_ask(cb: CallbackQuery, state: FSMContext):
    ticket = supportdb.get(int(cb.data.rsplit(":", 1)[1]))
    if not ticket:
        await cb.answer("تیکت پیدا نشد", show_alert=True)
        return
    await cb.answer()
    await state.set_state(SupportFlow.staff_reply)
    await state.update_data(ticket_id=ticket.id)
    await cb.message.answer(
        f"✍️ پاسخ برای تیکت <b>#{ticket.id}</b> را بفرستید:", reply_markup=h.CANCEL_KB
    )


@router.message(SupportFlow.staff_reply, h.staff)
async def staff_reply_send(msg: Message, state: FSMContext, bot: Bot):
    ticket_id = (await state.get_data()).get("ticket_id")
    ticket = supportdb.get(ticket_id) if ticket_id else None
    kind, text, file_id = _content(msg)
    if not ticket or ticket.status in {"resolved", "closed"}:
        await state.clear()
        await msg.answer(
            "این تیکت بسته شده یا پیدا نشد.", reply_markup=h.staff_kb(msg.from_user.id)
        )
        return
    if not text and not file_id:
        await msg.answer("لطفاً متن، عکس یا فایل پاسخ را بفرستید.")
        return
    try:
        await bot.send_message(
            ticket.tg_id, f"🎧 <b>پاسخ پشتیبانی · تیکت #{ticket.id}</b>"
        )
        await msg.copy_to(ticket.tg_id)
    except TelegramAPIError:
        await msg.answer("❌ ارسال به کاربر ناموفق بود؛ شاید ربات را مسدود کرده باشد.")
        return
    supportdb.add_message(ticket.id, msg.from_user.id, "staff", text, kind, file_id)
    supportdb.update(ticket.id, assigned_to=msg.from_user.id, status="waiting_customer")
    await state.clear()
    await msg.answer(
        "✅ پاسخ ارسال شد و تیکت منتظر پاسخ کاربر است.",
        reply_markup=h.staff_kb(msg.from_user.id),
    )


@router.callback_query(F.data.startswith(("st:wait:", "st:close:")), h.staff)
async def staff_change_status(cb: CallbackQuery, bot: Bot):
    action, raw_id = cb.data.split(":")[1:]
    ticket = supportdb.get(int(raw_id))
    if not ticket:
        await cb.answer("تیکت پیدا نشد", show_alert=True)
        return
    status = "waiting_customer" if action == "wait" else "resolved"
    ticket = supportdb.update(ticket.id, assigned_to=cb.from_user.id, status=status)
    await cb.answer("وضعیت ذخیره شد")
    if status == "resolved":
        try:
            await bot.send_message(
                ticket.tg_id,
                f"✅ تیکت <b>#{ticket.id}</b> حل‌شده ثبت شد. اگر مشکل ادامه داشت، یک تیکت جدید بسازید.",
            )
        except TelegramAPIError:
            log.warning(
                "could not notify customer %s about resolved ticket %s",
                ticket.tg_id,
                ticket.id,
            )
    await cb.message.edit_text(
        ticket_text(ticket, True), reply_markup=staff_ticket_kb(ticket)
    )
