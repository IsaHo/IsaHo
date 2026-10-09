"""Announcement-channel membership, independent from account billing restrictions."""
import asyncio
import html
import logging

import db
import xray
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from config import cfg

router = Router()
log = logging.getLogger(__name__)
_lock = asyncio.Lock()
STATUS_TIMEOUT = 5
GATE_TIMEOUT = 8


def required() -> bool:
    return db.get_setting("membership_enabled") == "1" and bool(channel_id())


def channel_id():
    value = db.get_setting("membership_channel") or db.get_setting("status_chat")
    return int(value) if value and value.lstrip("-").isdigit() else None


def controller(user):
    # A linked end user takes precedence over the reseller who purchased it.
    return user.tg_id or user.owner_tg


def is_member(member) -> bool:
    return member.status in ("creator", "administrator", "member") or (
        member.status == "restricted" and bool(getattr(member, "is_member", False)))


def keyboard() -> InlineKeyboardMarkup:
    rows = []
    url = db.get_setting("membership_url")
    if url.startswith("https://t.me/"):
        rows.append([InlineKeyboardButton(text="📣 عضویت در کانال اطلاع‌رسانی", url=url)])
    rows.append([InlineKeyboardButton(text="✅ عضو شدم؛ بررسی کن", callback_data="membership:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


JOIN_TEXT = (
    "📣 <b>باخبر بمان؛ متصل بمان</b>\n\n"
    "وضعیت اتصال از ایران و اطلاعیه‌های سرویس در کانال EisaVPN منتشر می‌شود.\n"
    "برای دریافت تست، خرید و استفاده از اشتراک، عضو کانال شوید.\n\n"
    "۱. دکمهٔ عضویت را بزنید.\n۲. برگردید و «عضو شدم؛ بررسی کن» را بزنید.\n\n"
    "⏸ خروج از کانال دسترسی را موقتاً متوقف می‌کند؛ با عضویت دوباره، "
    "اشتراک معتبر باز می‌شود. حجم ریست نمی‌شود و تاریخ انقضا طبق روال ادامه دارد."
)


async def status(bot: Bot, tg_id: int):
    """None means unknown: never interpret an API/admin failure as a departure."""
    if not required() or tg_id in db.admin_ids():
        return True
    async def query():
        own = await bot.get_chat_member(channel_id(), bot.id)
        if own.status not in ("creator", "administrator"):
            return None
        return is_member(await bot.get_chat_member(channel_id(), tg_id))
    try:
        # One deadline covers BOTH Telegram calls, including DNS/connect waits.
        return await asyncio.wait_for(query(), timeout=STATUS_TIMEOUT)
    except (TelegramAPIError, TimeoutError, OSError, ValueError):
        log.warning("membership check unavailable (account state unchanged)")
        return None


async def reconcile_user(bot: Bot, tg_id: int, *, notify: bool = True):
    async with _lock:
        joined = await status(bot, tg_id) if tg_id else True
        changed = False
        for user in db.all_users():
            if controller(user) != tg_id:
                continue
            if joined is None and not user.channel_exempt:
                continue
            blocked = int(not joined and not user.channel_exempt)
            if user.channel_blocked != blocked:
                db.update(user.id, channel_blocked=blocked, channel_pending=1)
                changed = True
            current = db.get(user.id)
            if current.channel_pending:
                try:
                    await xray.sync_user(current, current.accessible, allow_restart=False)
                except (RuntimeError, OSError, ValueError):
                    log.warning("membership hot-update pending for account %s", user.id)
                else:
                    db.update(user.id, channel_pending=0)
        if changed and notify and joined is not None:
            try:
                text = ("✅ <b>عضویت تأیید شد</b>\nتعلیق عضویت برداشته شد؛ اکانت‌های دارای "
                        "اعتبار و حجم قابل استفاده‌اند." if joined else
                        "⏸ <b>عضویت در کانال لازم است</b>\nبرای بازشدن دسترسی، دوباره عضو شوید.\n\n" + JOIN_TEXT)
                await bot.send_message(tg_id, text, reply_markup=None if joined else keyboard())
            except (TelegramAPIError, TimeoutError, OSError):
                log.debug("membership notification unavailable")
        return joined


async def ensure(bot: Bot, tg_id: int, message) -> bool:
    try:
        # Includes queueing behind reconciliation and pending Xray hot updates.
        joined = await asyncio.wait_for(reconcile_user(bot, tg_id), timeout=GATE_TIMEOUT)
    except TimeoutError:
        joined = None
        log.warning("membership gate deadline reached (no access granted)")
    if joined is True:
        return True
    text = JOIN_TEXT if joined is False else (
        "⚠️ بررسی عضویت فعلاً ممکن نیست؛ چند لحظه بعد دوباره امتحان کنید. "
        "وضعیت فعلی اشتراک شما به علت این خطا تغییر نکرده است.")
    await message.answer(text, reply_markup=keyboard())
    return False


async def prepare_account(bot: Bot, user, *, new: bool = False):
    """Paid orders are fulfilled, but never activated before membership is verified."""
    user = db.get(user.id)
    who = controller(user)
    joined = True if user.channel_exempt else await status(bot, who) if who else True
    if required() and (joined is False or (new and joined is None)):
        db.update(user.id, channel_blocked=1, channel_pending=1)
    elif joined is True and user.channel_blocked:
        db.update(user.id, channel_blocked=0, channel_pending=1)
    return db.get(user.id)


@router.chat_member()
async def member_changed(event: ChatMemberUpdated, bot: Bot):
    if required() and event.chat.id == channel_id() and not event.new_chat_member.user.is_bot:
        # Requery live state: delayed leave events must not suspend someone who rejoined.
        tg_id = event.new_chat_member.user.id
        previous = getattr(event, "old_chat_member", None)
        entering = previous is not None and is_member(event.new_chat_member) and not is_member(previous)
        joined = await reconcile_user(bot, tg_id, notify=not entering)
        if entering and joined is True:
            with db.connect() as conn:
                known = conn.execute("SELECT 1 FROM customers WHERE tg_id=?", (tg_id,)).fetchone()
            accounts = [u for u in db.all_users() if controller(u) == tg_id]
            if known or accounts:
                pending = any(u.channel_pending for u in accounts)
                text = (
                    "🛡 <b>EisaVPN | عضویت شما تأیید شد</b>\n\n"
                    "به جمع همراهان کانال خوش آمدید 🌿\n"
                    + ("دسترسی اشتراک در حال همگام‌سازی است؛ لطفاً کمی صبر کنید.\n" if pending else
                       "تعلیق عضویت برداشته شد؛ اشتراک دارای اعتبار و حجم قابل استفاده است.\n")
                    + "🎁 برای دریافت تست یا خرید، به منوی اصلی ربات برگردید."
                )
                try:
                    await bot.send_message(tg_id, text)
                except (TelegramAPIError, TimeoutError, OSError):
                    log.debug("membership confirmation delivery unavailable")


@router.callback_query(F.data == "membership:check")
async def check_callback(cb: CallbackQuery, bot: Bot):
    await cb.answer("در حال بررسی عضویت…")
    if await ensure(bot, cb.from_user.id, cb.message):
        await cb.message.answer("✅ عضویت تأیید شد. اکنون تست یا خرید را از منوی اصلی انتخاب کنید.")


def settings_view():
    blocked = sum(bool(u.channel_blocked) for u in db.all_users())
    exempt = sum(bool(u.channel_exempt) for u in db.all_users())
    state = "فعال" if required() else "خاموش"
    rows = [[InlineKeyboardButton(text="⏸ خاموش‌کردن شرط عضویت" if required() else
                                 "🔐 فعال‌کردن شرط عضویت", callback_data="membership:toggle")],
            [InlineKeyboardButton(text=f"⏸ منتظر عضویت · {blocked}", callback_data="membership:waiting:0")],
            [InlineKeyboardButton(text="↩️ داده و اعلان‌ها", callback_data="nav:set:data")]]
    return ((f"📣 <b>عضویت کانال اطلاع‌رسانی</b>\n\nوضعیت: {state}\nاکانت‌های متوقف: {blocked}\nمعاف از عضویت: {exempt}\n\n"
            "تست و خرید نیازمند عضویت است. خروج، دسترسی را موقتاً متوقف می‌کند.\n"
            "با خاموش‌کردن این قابلیت فقط تعلیق عضویت برداشته می‌شود؛ سایر محدودیت‌ها باقی می‌ماند."),
            InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("membership:waiting:"))
async def waiting_callback(cb: CallbackQuery):
    if cb.from_user.id not in db.admin_ids():
        await cb.answer("فقط مدیر ربات", show_alert=True)
        return
    try:
        page = max(0, int(cb.data.rsplit(":", 1)[1]))
    except ValueError:
        await cb.answer("درخواست نامعتبر", show_alert=True)
        return
    users = [u for u in db.all_users() if u.channel_blocked]
    page = min(page, max(0, (len(users) - 1) // 8))
    rows = [[InlineKeyboardButton(text=f"⏸ {u.name}", callback_data=f"u:{u.id}")]
            for u in users[page * 8:(page + 1) * 8]]
    nav = []
    if page:
        nav.append(InlineKeyboardButton(text="قبلی", callback_data=f"membership:waiting:{page - 1}"))
    if (page + 1) * 8 < len(users):
        nav.append(InlineKeyboardButton(text="بعدی", callback_data=f"membership:waiting:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="↩️ تنظیمات عضویت", callback_data="membership:menu")])
    await cb.answer()
    await cb.message.edit_text(
        f"⏸ <b>منتظر عضویت · {len(users)} اکانت</b>\n\n"
        + ("اکانت را انتخاب کنید؛ مالک می‌تواند از کارت آن دسترسی بدون عضویت بدهد." if users else
           "هیچ اکانتی به علت عضویت متوقف نیست."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def set_exemption(bot: Bot, user_id: int, exempt: bool):
    """Explicit, serialized action; revocation requires a known live membership state."""
    async with _lock:
        user = db.get(user_id)
        if not user:
            raise ValueError("اکانت وجود ندارد")
        who = controller(user)
        joined = True if exempt or not who else await status(bot, who)
        if joined is None:
            raise ValueError("بررسی عضویت ممکن نیست؛ معافیت تغییر نکرد. دوباره تلاش کنید.")
        blocked = int(not joined)
        if user.channel_exempt != int(exempt) or user.channel_blocked != blocked:
            db.update(user_id, channel_exempt=int(exempt), channel_blocked=blocked, channel_pending=1)
        current = db.get(user_id)
        if current.channel_pending:
            try:
                await xray.sync_user(current, current.accessible, allow_restart=False)
            except (RuntimeError, OSError, ValueError):
                log.warning("membership exemption hot-update pending for account %s", user_id)
            else:
                db.update(user_id, channel_pending=0)
        return db.get(user_id)


@router.callback_query(F.data.startswith(("membership:exempt:", "membership:setexempt:")))
async def exemption_callback(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in cfg.admin_ids:
        await cb.answer("فقط مالک ربات می‌تواند معافیت بدهد", show_alert=True)
        return
    parts = cb.data.split(":")
    try:
        if len(parts) != (4 if parts[1] == "setexempt" else 3):
            raise ValueError
        user = db.get(int(parts[2]))
        if not user:
            raise ValueError
        if parts[1] == "setexempt":
            if parts[3] not in ("0", "1"):
                raise ValueError
            user = await set_exemption(bot, user.id, parts[3] == "1")
    except ValueError as exc:
        await cb.answer(str(exc) or "درخواست نامعتبر یا اکانت حذف شده", show_alert=True)
        return
    if parts[1] == "setexempt":
        from handlers import show_user
        await cb.answer("ذخیره شد؛ همگام‌سازی در انتظار تلاش مجدد" if user.channel_pending else "✅ تنظیم اعمال شد")
        await show_user(cb, user)
        return
    target = int(not user.channel_exempt)
    text = ("🔓 <b>دسترسی بدون عضویت</b>" if target else "🔐 <b>بازگرداندن شرط عضویت</b>")
    text += f"\n\nاکانت: <b>{html.escape(user.name)}</b>\n"
    text += ("فقط این اکانت از شرط کانال معاف می‌شود؛ خروج از کانال آن را قطع نمی‌کند.\n" if target else
             "اگر صاحب این اکانت عضو کانال نباشد، دسترسی آن متوقف می‌شود.\n")
    text += "انقضا، حجم، قطع دستی و محدودیت دستگاه تغییری نمی‌کنند."
    rows = [[InlineKeyboardButton(text="تأیید دسترسی بدون عضویت" if target else "تأیید الزام عضویت",
                                  callback_data=f"membership:setexempt:{user.id}:{target}")],
            [InlineKeyboardButton(text="↩️ برگشت به اکانت", callback_data=f"u:{user.id}")]]
    await cb.answer()
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "membership:menu")
async def settings_callback(cb: CallbackQuery):
    if cb.from_user.id not in db.admin_ids():
        await cb.answer("فقط مدیر ربات", show_alert=True)
        return
    await cb.answer()
    text, kb = settings_view()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "membership:toggle")
async def toggle_callback(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in cfg.admin_ids:
        await cb.answer("فقط مالک ربات", show_alert=True)
        return
    if not required():
        if not channel_id() or not db.get_setting("membership_url"):
            await cb.answer("ابتدا کانال و لینک عضویت تنظیم شود", show_alert=True)
            return
        try:
            own = await bot.get_chat_member(channel_id(), bot.id)
            if own.status not in ("creator", "administrator"):
                raise ValueError("not admin")
        except (TelegramAPIError, TimeoutError, OSError, ValueError):
            await cb.answer("دسترسی ادمین ربات به کانال تأیید نشد", show_alert=True)
            return
    db.set_setting("membership_enabled", "0" if required() else "1")
    await cb.answer("✅ تنظیم ذخیره شد؛ حداکثر یک دقیقه تا همگام‌سازی")
    text, kb = settings_view()
    await cb.message.edit_text(text, reply_markup=kb)


async def reconcile(bot: Bot):
    targets = {controller(u) for u in db.all_users()
               if controller(u) and (u.enabled or u.channel_blocked or u.channel_pending)}
    for tg_id in sorted(targets):
        await reconcile_user(bot, tg_id)
        await asyncio.sleep(0.1)
    if any(not controller(u) and u.channel_pending for u in db.all_users()):
        await reconcile_user(bot, None, notify=False)


async def monitor(bot: Bot):
    while True:
        try:
            await reconcile(bot)
        except Exception:
            log.exception("membership reconciliation failed")
        await asyncio.sleep(60)
