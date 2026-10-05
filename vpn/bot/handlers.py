"""Telegram bot handlers: admin panel and end-user self-service."""
import csv
import html
import io
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid as uuidlib

import psutil
import segno
from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

import db
import fmt
import links
import devices
import relays
import tunnels
import xray
from config import cfg

log = logging.getLogger(__name__)
router = Router()
PAGE = 10
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")


class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        return event.from_user is not None and event.from_user.id in db.admin_ids()


class IsOwner(Filter):
    async def __call__(self, event) -> bool:
        return event.from_user is not None and event.from_user.id in cfg.admin_ids


admin = IsAdmin()
owner = IsOwner()


class AddUser(StatesGroup):
    name = State()
    traffic = State()
    days = State()


class Edit(StatesGroup):
    days = State()
    traffic = State()
    note = State()
    search = State()
    cdn_ip = State()
    backup_chat = State()
    relays = State()
    broadcast = State()
    add_admin = State()
    bulk_days = State()
    ip_limit = State()
    ip_default = State()
    bulk_gb = State()
    support = State()
    reply = State()


# ---------- keyboards ----------

BTN_ADD = "➕ کاربر جدید"
BTN_USERS = "👥 کاربران"
BTN_SEARCH = "🔎 جستجو"
BTN_STATUS = "📊 وضعیت سرور"
BTN_SETTINGS = "⚙️ تنظیمات"
BTN_BACKUP = "💾 بکاپ"
BTN_BROADCAST = "📢 پیام همگانی"
BTN_CANCEL = "❌ لغو"
BTN_MY = "📊 حساب من"
BTN_DASH = "📈 داشبورد"
BTN_BULK = "🧰 عملیات گروهی"
BTN_HELP = "📱 آموزش اتصال"
BTN_SUPPORT = "💬 پشتیبانی"
BTN_BUY = "🛒 خرید اشتراک"
BTN_TEST = "🎁 اکانت تست"
BTN_RENEW = "🔄 تمدید"
BTN_INVITE = "👥 دعوت دوستان"
BTN_SHOP = "🛒 فروشگاه"

ADMIN_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_USERS)],
    [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_STATUS)],
    [KeyboardButton(text=BTN_DASH), KeyboardButton(text=BTN_BULK)],
    [KeyboardButton(text=BTN_SHOP)],
    [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_BACKUP)],
    [KeyboardButton(text=BTN_BROADCAST)],
])
CANCEL_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text=BTN_CANCEL)]])
USER_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_MY)],
    [KeyboardButton(text=BTN_RENEW), KeyboardButton(text=BTN_TEST)],
    [KeyboardButton(text=BTN_INVITE), KeyboardButton(text=BTN_HELP)],
    [KeyboardButton(text=BTN_SUPPORT)],
])


def ikb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def user_kb(u) -> InlineKeyboardMarkup:
    toggle = ("⛔ غیرفعال", f"off:{u.id}") if u.enabled else ("✅ فعال‌سازی", f"on:{u.id}")
    return ikb([
        [("🔗 لینک‌ها", f"lnk:{u.id}"), ("📱 QR", f"qr:{u.id}")],
        [("⏳ تمدید", f"ren:{u.id}"), ("📦 افزایش حجم", f"addt:{u.id}")],
        [("♻️ ریست مصرف", f"rst:{u.id}"), ("🔑 تغییر UUID", f"uuid:{u.id}")],
        [toggle, ("📝 یادداشت", f"note:{u.id}")],
        [("📱 محدودیت دستگاه", f"ipl:{u.id}")],
        [("🗑 حذف", f"del:{u.id}"), ("🔄 بروزرسانی", f"u:{u.id}")],
        [("🔙 لیست کاربران", "list:0")],
    ])


def list_kb(users, page: int, prefix: str = "list") -> InlineKeyboardMarkup:
    chunk = users[page * PAGE:(page + 1) * PAGE]
    rows = []
    for i in range(0, len(chunk), 2):
        rows.append([(f"{fmt.status_icon(u)} {u.name}", f"u:{u.id}") for u in chunk[i:i + 2]])
    nav = []
    if page > 0:
        nav.append(("◀️ قبلی", f"{prefix}:{page - 1}"))
    if (page + 1) * PAGE < len(users):
        nav.append(("بعدی ▶️", f"{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    return ikb(rows)


# ---------- helpers ----------

def parse_number(text: str):
    text = (text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    try:
        v = float(text)
    except ValueError:
        return None
    return v if v >= 0 else None


def links_text(u) -> str:
    parts = [f"🔗 <b>کانفیگ‌های {html.escape(u.name)}</b>", "",
             "📥 <b>لینک سابسکریپشن</b> (پیشنهادی - خودکار آپدیت می‌شود):",
             f"<code>{html.escape(links.sub_url(u))}</code>", ""]
    for backup in links.backup_sub_urls(u):
        parts += ["📥 سابسکریپشن پشتیبان:", f"<code>{html.escape(backup)}</code>", ""]
    for link in links.all_links(u):
        if "-IR" in link.rsplit("#", 1)[-1]:
            label = "🇮🇷 از طریق سرور واسط ایران (پیشنهادی)"
        elif "security=reality" in link:
            label = "⚡ Reality (مستقیم)"
        else:
            label = "☁️ CDN (پشتیبان، ضد فیلتر)"
        parts += [f"<b>{label}</b>", f"<code>{html.escape(link)}</code>", ""]
    me_link = f"https://t.me/{db.get_setting('bot_username')}?start={u.sub_token}"
    parts.append(f"🤖 لینک اتصال کاربر به ربات (برای دیدن مصرف):\n<code>{me_link}</code>")
    return "\n".join(parts)


def qr_png(data: str) -> bytes:
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="png", scale=8, border=2)
    return buf.getvalue()


async def apply_user(u) -> None:
    await xray.sync_user(u, bool(u.enabled))


async def maybe_reactivate(u):
    """Re-enable a user disabled by the monitor once the reason no longer applies."""
    u = db.get(u.id)
    if not u.enabled and u.disabled_reason in ("expired", "traffic") and not u.expired and not u.over_limit:
        db.update(u.id, enabled=1, disabled_reason="", warned=0)
        u = db.get(u.id)
        await apply_user(u)
    return u


def device_line(u) -> str:
    if db.get_setting("real_ip") != "1":
        return ""
    limit = u.ip_limit or int(db.get_setting("ip_limit_default", "0") or 0)
    return f"\n📱 دستگاه‌های فعال: {devices.count(u.name)} | حد: {limit or 'نامحدود'}"


async def show_user(target, u, edit: bool = True):
    text = fmt.user_card(u) + device_line(u)
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=user_kb(u))
        except Exception:
            pass
        return
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(text, reply_markup=user_kb(u))


def user_from_cb(cb: CallbackQuery):
    return db.get(int(cb.data.split(":")[1]))


# ---------- common ----------

@router.message(CommandStart(deep_link=True), ~admin)
async def start_link(msg: Message, command: CommandObject):
    u = db.get_by_token(command.args or "")
    if not u:
        await msg.answer("❌ لینک نامعتبر است.")
        return
    db.update(u.id, tg_id=msg.from_user.id)
    await msg.answer(f"✅ اکانت <b>{html.escape(u.name)}</b> به تلگرام شما وصل شد.\n"
                     "از دکمه زیر وضعیت حسابتان را ببینید.", reply_markup=USER_KB)


@router.message(CommandStart(), admin)
@router.message(F.text == BTN_CANCEL, admin)
async def admin_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(f"👋 پنل مدیریت <b>{html.escape(cfg.brand)}</b>", reply_markup=ADMIN_KB)


@router.message(CommandStart())
@router.message(F.text == BTN_MY)
async def my_account(msg: Message):
    users = db.get_by_tg(msg.from_user.id)
    if not users:
        await msg.answer("سلام 👋\nبرای استفاده، لینک اختصاصی‌ای که مدیر برایتان فرستاده را باز کنید.")
        return
    for u in users:
        chart = fmt.day_chart(db.user_daily(u.name, 7))
        usage = ("\n\n📊 <b>مصرف ۷ روز اخیر</b>\n" + "\n".join(chart)) if chart else ""
        await msg.answer(fmt.user_card(u) + usage + "\n\n" + links_text(u).rsplit("\n🤖", 1)[0],
                         reply_markup=USER_KB)


# ---------- add user ----------

@router.message(F.text == BTN_ADD, admin)
async def add_start(msg: Message, state: FSMContext):
    await state.set_state(AddUser.name)
    await msg.answer("👤 نام کاربر را بفرستید (فقط حروف انگلیسی، عدد، _ . -):", reply_markup=CANCEL_KB)


@router.message(AddUser.name, admin)
async def add_name(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if not NAME_RE.match(name):
        await msg.answer("❌ نام نامعتبر است. ۲ تا ۳۲ کاراکتر انگلیسی/عدد/_.-")
        return
    if db.get_by_name(name):
        await msg.answer("❌ این نام قبلاً استفاده شده.")
        return
    await state.update_data(name=name)
    await state.set_state(AddUser.traffic)
    await msg.answer("📦 حجم (گیگابایت) را بفرستید یا انتخاب کنید (۰ = نامحدود):",
                     reply_markup=ikb([[("10", "t:10"), ("30", "t:30"), ("50", "t:50")],
                                       [("100", "t:100"), ("200", "t:200"), ("♾ نامحدود", "t:0")]]))


async def _add_traffic_value(target, state: FSMContext, gb: float):
    await state.update_data(traffic=gb)
    await state.set_state(AddUser.days)
    m = target.message if isinstance(target, CallbackQuery) else target
    await m.answer("⏳ مدت (روز) را بفرستید یا انتخاب کنید (۰ = نامحدود).\n"
                   "«از اولین اتصال» یعنی روزها از وقتی کاربر اولین بار وصل شود شمرده می‌شود.",
                   reply_markup=ikb([[("30", "d:30"), ("60", "d:60"), ("90", "d:90")],
                                     [("180", "d:180"), ("365", "d:365"), ("♾ نامحدود", "d:0")],
                                     [("30 از اولین اتصال", "d:f30"), ("60 از اولین اتصال", "d:f60")],
                                     [("90 از اولین اتصال", "d:f90")]]))


@router.callback_query(AddUser.traffic, F.data.startswith("t:"), admin)
async def add_traffic_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await _add_traffic_value(cb, state, float(cb.data[2:]))


@router.message(AddUser.traffic, admin)
async def add_traffic_msg(msg: Message, state: FSMContext):
    v = parse_number(msg.text)
    if v is None:
        await msg.answer("❌ یک عدد بفرستید.")
        return
    await _add_traffic_value(msg, state, v)


async def _finish_add(target, state: FSMContext, days: int, first_use: bool = False):
    data = await state.get_data()
    await state.clear()
    m = target.message if isinstance(target, CallbackQuery) else target
    try:
        u = db.create_user(data["name"], data["traffic"], days, first_use=first_use and days > 0)
        await apply_user(u)
    except Exception as e:
        log.exception("create user failed")
        await m.answer(f"❌ خطا: <code>{html.escape(str(e))}</code>", reply_markup=ADMIN_KB)
        return
    await m.answer("✅ کاربر ساخته شد.", reply_markup=ADMIN_KB)
    await m.answer(fmt.user_card(u), reply_markup=user_kb(u))
    await m.answer_photo(BufferedInputFile(qr_png(links.sub_url(u)), "qr.png"),
                         caption=links_text(u))


@router.callback_query(AddUser.days, F.data.startswith("d:"), admin)
async def add_days_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    value = cb.data[2:]
    await _finish_add(cb, state, int(value.lstrip("f")), first_use=value.startswith("f"))


@router.message(AddUser.days, admin)
async def add_days_msg(msg: Message, state: FSMContext):
    v = parse_number(msg.text)
    if v is None:
        await msg.answer("❌ یک عدد بفرستید.")
        return
    await _finish_add(msg, state, int(v))


# ---------- list / search / detail ----------

@router.message(F.text == BTN_USERS, admin)
async def users_list(msg: Message):
    users = db.all_users()
    if not users:
        await msg.answer("هنوز کاربری ندارید.")
        return
    active = sum(u.enabled for u in users)
    await msg.answer(f"👥 کاربران: {len(users)} (فعال: {active})\n🟢 فعال  🟡 رو به اتمام  🔴 غیرفعال",
                     reply_markup=list_kb(users, 0))


@router.callback_query(F.data.startswith("list:"), admin)
async def users_page(cb: CallbackQuery):
    await cb.answer()
    users = db.all_users()
    page = int(cb.data.split(":")[1])
    await cb.message.edit_text(f"👥 کاربران: {len(users)} (فعال: {sum(u.enabled for u in users)})",
                               reply_markup=list_kb(users, page))


@router.message(F.text == BTN_SEARCH, admin)
async def search_start(msg: Message, state: FSMContext):
    await state.set_state(Edit.search)
    await msg.answer("🔎 نام یا بخشی از UUID را بفرستید:", reply_markup=CANCEL_KB)


@router.message(Edit.search, admin)
async def search_do(msg: Message, state: FSMContext):
    await state.clear()
    found = db.search((msg.text or "").strip())
    if not found:
        await msg.answer("چیزی پیدا نشد.", reply_markup=ADMIN_KB)
        return
    await msg.answer(f"نتایج: {len(found)}", reply_markup=ADMIN_KB)
    if len(found) == 1:
        await show_user(msg, found[0], edit=False)
    else:
        await msg.answer("انتخاب کنید:", reply_markup=list_kb(found[:PAGE], 0))


@router.callback_query(F.data.startswith("u:"), admin)
async def user_detail(cb: CallbackQuery):
    await cb.answer()
    u = user_from_cb(cb)
    if not u:
        await cb.message.edit_text("کاربر وجود ندارد.")
        return
    await show_user(cb, u)


# ---------- user actions ----------

@router.callback_query(F.data.startswith("lnk:"), admin)
async def user_links(cb: CallbackQuery):
    await cb.answer()
    u = user_from_cb(cb)
    await cb.message.answer(links_text(u), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("qr:"), admin)
async def user_qr(cb: CallbackQuery):
    await cb.answer()
    u = user_from_cb(cb)
    await cb.message.answer_photo(BufferedInputFile(qr_png(links.sub_url(u)), "sub.png"),
                                  caption="📥 QR سابسکریپشن")
    for link in links.all_links(u):
        cap = "⚡ Reality" if "security=reality" in link else "☁️ CDN"
        await cb.message.answer_photo(BufferedInputFile(qr_png(link), "qr.png"), caption=cap)


@router.callback_query(F.data.startswith(("on:", "off:")), admin)
async def user_toggle(cb: CallbackQuery):
    u = user_from_cb(cb)
    enable = cb.data.startswith("on:")
    if enable:
        db.update(u.id, enabled=1, disabled_reason="", warned=0)
    else:
        db.update(u.id, enabled=0, disabled_reason="manual")
    u = db.get(u.id)
    try:
        await apply_user(u)
    except Exception as e:
        await cb.answer(f"خطا: {e}"[:190], show_alert=True)
        return
    await cb.answer("✅ فعال شد" if enable else "⛔ غیرفعال شد")
    if enable and (u.expired or u.over_limit):
        await cb.message.answer("⚠️ این کاربر منقضی شده یا حجمش تمام شده؛ "
                                "تا دقیقه‌ای دیگر دوباره غیرفعال می‌شود. اول تمدید یا افزایش حجم کنید.")
    await show_user(cb, u)


@router.callback_query(F.data.startswith("rst:"), admin)
async def user_reset(cb: CallbackQuery):
    u = user_from_cb(cb)
    db.update(u.id, up=0, down=0, warned=0)
    u = await maybe_reactivate(u)
    await cb.answer("♻️ مصرف ریست شد")
    await show_user(cb, u)


@router.callback_query(F.data.startswith("uuid:"), admin)
async def user_uuid(cb: CallbackQuery):
    u = user_from_cb(cb)
    db.update(u.id, uuid=str(uuidlib.uuid4()))
    u = db.get(u.id)
    if u.enabled:
        await apply_user(u)  # removes the old client by email, then adds the new UUID
    await cb.answer("🔑 UUID عوض شد؛ لینک‌های قبلی دیگر کار نمی‌کنند", show_alert=True)
    await show_user(cb, u)


@router.callback_query(F.data.startswith("del:"), admin)
async def user_del_ask(cb: CallbackQuery):
    await cb.answer()
    u = user_from_cb(cb)
    await cb.message.edit_text(f"🗑 کاربر <b>{html.escape(u.name)}</b> حذف شود؟",
                               reply_markup=ikb([[("✅ بله، حذف کن", f"delok:{u.id}"),
                                                  ("❌ نه", f"u:{u.id}")]]))


@router.callback_query(F.data.startswith("delok:"), admin)
async def user_del(cb: CallbackQuery):
    u = user_from_cb(cb)
    if u:
        db.delete(u.id)
        await xray.sync_user(u, False)
    await cb.answer("حذف شد")
    await cb.message.edit_text(f"🗑 کاربر <b>{html.escape(u.name if u else '')}</b> حذف شد.")


@router.callback_query(F.data.startswith("ipl:"), admin)
async def ip_limit_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.ip_limit)
    await state.update_data(uid=int(cb.data[4:]))
    note = "" if db.get_setting("real_ip") == "1" else (
        "\n⚠️ «IP واقعی کاربران» خاموش است؛ تا روشنش نکنید این محدودیت اعمال نمی‌شود "
        "(⚙️ تنظیمات ← 📱 محدودیت دستگاه).")
    await cb.message.answer("📱 حداکثر چند دستگاه هم‌زمان؟ (۰ = طبق پیش‌فرض)" + note, reply_markup=CANCEL_KB)


@router.message(Edit.ip_limit, admin)
async def ip_limit_set(msg: Message, state: FSMContext):
    v = parse_number(msg.text)
    if v is None:
        await msg.answer("❌ یک عدد بفرستید.")
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    db.update(uid, ip_limit=int(v))
    await msg.answer("✅ ذخیره شد.", reply_markup=ADMIN_KB)
    await show_user(msg, db.get(uid), edit=False)


def devices_view():
    on = db.get_setting("real_ip") == "1"
    default = int(db.get_setting("ip_limit_default", "0") or 0)
    action = db.get_setting("ip_limit_action", "warn")
    text = (
        "📱 <b>محدودیت دستگاه</b>\n\n"
        f"IP واقعی کاربران: {'🟢 روشن' if on else '🔴 خاموش'}\n"
        f"حد پیش‌فرض: {default or 'نامحدود'} دستگاه\n"
        f"وقتی کاربر از حد رد شد: {'⛔ ۱۵ دقیقه قطع + پیام' if action == 'disable' else '⚠️ فقط هشدار'}\n\n"
        "دستگاه = IPهای متفاوتی که در ۳ دقیقه‌ی اخیر وصل شده‌اند. گوشی‌ای که بین وای‌فای و دیتا "
        "جابه‌جا می‌شود ممکن است چند دقیقه دو دستگاه حساب شود؛ برای همین حد ۲ برای یک نفر امن‌تر است.\n"
        "روشن کردن IP واقعی حدود یک دقیقه اتصال کاربران را قطع و وصل می‌کند.")
    kb = ikb([
        [(("🔴 خاموش کردن IP واقعی" if on else "🟢 روشن کردن IP واقعی"), "dev:toggle")],
        [("🔢 حد پیش‌فرض", "dev:default"),
         (("⚠️ فقط هشدار" if action == "disable" else "⛔ قطع موقت"), "dev:action")],
    ])
    return text, kb


@router.callback_query(F.data == "dev:menu", admin)
async def devices_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = devices_view()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "dev:toggle", admin)
async def devices_toggle(cb: CallbackQuery):
    turning_on = db.get_setting("real_ip") != "1"
    if turning_on:
        ok, reason = relays.ready_for_real_ip()
        if not ok:
            await cb.answer(f"❌ {reason}", show_alert=True)
            return
    db.set_setting("real_ip", "1" if turning_on else "")
    if turning_on and not xray.split_relay_inbound():
        db.set_setting("real_ip", "")
        await cb.answer("❌ IP عمومی سرور روی کارت شبکه نیست؛ این قابلیت روی این سرور ممکن نیست", show_alert=True)
        return
    try:
        await xray.apply_all()
    except Exception as e:
        db.set_setting("real_ip", "" if turning_on else "1")
        await xray.apply_all()
        await cb.answer(f"❌ {e}"[:190], show_alert=True)
        return
    await cb.answer("✅ انجام شد؛ سرورهای ایران ظرف یک دقیقه هماهنگ می‌شوند", show_alert=True)
    text, kb = devices_view()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "dev:action", admin)
async def devices_action(cb: CallbackQuery):
    db.set_setting("ip_limit_action", "warn" if db.get_setting("ip_limit_action") == "disable" else "disable")
    await cb.answer("✅")
    text, kb = devices_view()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "dev:default", admin)
async def devices_default_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.ip_default)
    await cb.message.answer("🔢 حد پیش‌فرض دستگاه برای همه‌ی کاربران؟ (۰ = نامحدود)", reply_markup=CANCEL_KB)


@router.message(Edit.ip_default, admin)
async def devices_default_set(msg: Message, state: FSMContext):
    v = parse_number(msg.text)
    if v is None:
        await msg.answer("❌ یک عدد بفرستید.")
        return
    await state.clear()
    db.set_setting("ip_limit_default", str(int(v)))
    await msg.answer("✅ ذخیره شد.", reply_markup=ADMIN_KB)


@router.callback_query(F.data.startswith(("ren:", "addt:", "note:")), admin)
async def user_edit_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    kind, uid = cb.data.split(":")
    await state.update_data(uid=int(uid))
    if kind == "ren":
        await state.set_state(Edit.days)
        await cb.message.answer("⏳ چند روز اضافه شود؟ (از امروز یا از تاریخ انقضای فعلی، هرکدام دیرتر)\n"
                                "۰ = نامحدود", reply_markup=CANCEL_KB)
    elif kind == "addt":
        await state.set_state(Edit.traffic)
        await cb.message.answer("📦 چند گیگ اضافه شود؟ (۰ = نامحدود)", reply_markup=CANCEL_KB)
    else:
        await state.set_state(Edit.note)
        await cb.message.answer("📝 یادداشت را بفرستید (مثلاً شماره تماس یا قیمت):", reply_markup=CANCEL_KB)


@router.message(Edit.days, admin)
@router.message(Edit.traffic, admin)
@router.message(Edit.note, admin)
async def user_edit_do(msg: Message, state: FSMContext):
    current = await state.get_state()
    data = await state.get_data()
    u = db.get(data["uid"])
    if current == Edit.note.state:
        db.update(u.id, note=(msg.text or "")[:200])
    else:
        v = parse_number(msg.text)
        if v is None:
            await msg.answer("❌ یک عدد بفرستید.")
            return
        if current == Edit.days.state and u.pending_days and v:
            db.update(u.id, pending_days=u.pending_days + int(v))
        elif current == Edit.days.state:
            base = max(u.expire_at, int(time.time())) if u.expire_at else int(time.time())
            db.update(u.id, expire_at=base + int(v) * db.DAY if v else 0, warned=0)
        else:
            db.update(u.id, traffic_limit=u.traffic_limit + int(v * db.GB) if v else 0, warned=0)
    await state.clear()
    u = await maybe_reactivate(u)
    await msg.answer("✅ انجام شد.", reply_markup=ADMIN_KB)
    await show_user(msg, u, edit=False)


# ---------- server ----------

@router.message(F.text == BTN_STATUS, admin)
async def server_status(msg: Message):
    await xray.flush_stats()
    users = db.all_users()
    vm, disk, net = psutil.virtual_memory(), psutil.disk_usage("/"), psutil.net_io_counters()
    up = int(time.time() - psutil.boot_time())
    load = os.getloadavg()
    text = (
        "📊 <b>وضعیت سرور</b>\n\n"
        f"Xray: {'🟢 فعال' if await xray.is_active() else '🔴 خاموش'}\n"
        f"🖥 CPU: {psutil.cpu_percent(interval=0.5)}% ({psutil.cpu_count()} هسته) | load {load[0]:.2f}\n"
        f"🧠 RAM: {fmt.size(vm.used)} / {fmt.size(vm.total)} ({vm.percent}%)\n"
        f"💽 دیسک: {fmt.size(disk.used)} / {fmt.size(disk.total)} ({disk.percent}%)\n"
        f"🌐 ترافیک کارت شبکه: ⬆️ {fmt.size(net.bytes_sent)} ⬇️ {fmt.size(net.bytes_recv)}\n"
        f"⏱ آپتایم: {up // 86400} روز {up % 86400 // 3600} ساعت\n\n"
        f"👥 کاربران: {len(users)} | فعال: {sum(u.enabled for u in users)}\n"
        f"📦 مصرف کل کاربران: {fmt.size(sum(u.used for u in users))}\n"
        f"⚡ ترافیک لحظه‌ای کاربران: {fmt.size(int(xray.last_rate))}/s\n\n"
        f"📡 <b>تانل‌ها</b>\n{tunnels.summary()}"
    )
    top = sorted(users, key=lambda u: u.used, reverse=True)[:5]
    if top and top[0].used:
        text += "\n\n🏆 <b>پرمصرف‌ها:</b>\n" + "\n".join(
            f"{i + 1}. {html.escape(u.name)} — {fmt.size(u.used)}" for i, u in enumerate(top) if u.used)
    await msg.answer(text)


@router.message(F.text == BTN_SETTINGS, admin)
async def settings_menu(msg: Message):
    text = (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"IP سرور: <code>{cfg.server_ip}</code>\n"
        f"دامنه: <code>{cfg.domain}</code>\n"
        f"Reality: پورت {cfg.reality_port} | SNI <code>{cfg.reality_sni}</code>\n"
        f"CDN (XHTTP): پورت کلادفلر {links.cdn_public_port()} → سرور {cfg.cdn_port} | path <code>{cfg.cdn_path}</code>\n"
        f"آدرس اتصال CDN: <code>{links.cdn_address()}</code>\n"
        f"سابسکریپشن: پورت {cfg.sub_port}\n"
        f"سرورهای واسط: <code>{', '.join(f'{h}:{p}' for h, p in links.relays()) or 'ندارد'}</code>"
    )
    await msg.answer(text, reply_markup=ikb([
        [("🔗 لینک‌های سابسکریپشن", "lt:menu"), ("👮 مدیران", "adm:menu")],
        [("🛰 سرورهای ایران", "rl:menu"), ("📦 کانال بکاپ", "set:bchat")],
        [("📱 محدودیت دستگاه", "dev:menu")],
        [("🇮🇷 سرور واسط", "set:relays"), ("🌐 تنظیم IP تمیز کلادفلر", "set:cdn")],
        [("🧪 دستور تست سرور واسط", "set:relaytest")],
        [("🔌 پورت CDN: 443", "set:port:443"), (f"🔌 پورت CDN: {cfg.cdn_port}", f"set:port:{cfg.cdn_port}")],
        [("🔄 ریستارت Xray", "set:restart"), ("🛠 بازسازی کانفیگ", "set:rebuild")],
    ]))


def link_types_kb() -> InlineKeyboardMarkup:
    types = links.enabled_types()
    return ikb([[(f"{'✅' if t in types else '❌'} {label}", f"lt:{t}")] for t, label in links.LINK_TYPES.items()])


LINK_TYPES_TEXT = ("🔗 <b>لینک‌های داخل سابسکریپشن و پیام لینک‌ها</b>\n"
                   "روی هر مورد بزنید تا اضافه یا حذف شود. کاربران فقط سابسکریپشن را آپدیت کنند.")


@router.callback_query(F.data == "lt:menu", admin)
async def link_types_menu(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(LINK_TYPES_TEXT, reply_markup=link_types_kb())


@router.callback_query(F.data.startswith("lt:"), admin)
async def link_types_toggle(cb: CallbackQuery):
    t = cb.data[3:]
    if t not in links.LINK_TYPES:
        await cb.answer()
        return
    if t == "relay" and not links.relays() and t not in links.enabled_types():
        await cb.answer("اول از «🇮🇷 سرور واسط» یک سرور اضافه کنید", show_alert=True)
        return
    links.toggle_type(t)
    await cb.answer("✅ ذخیره شد")
    await cb.message.edit_text(LINK_TYPES_TEXT, reply_markup=link_types_kb())


def admins_view():
    rows = [[(f"👑 {a} (مالک)", "noop_a")] for a in sorted(cfg.admin_ids)]
    rows += [[(f"❌ حذف {a}", f"adm:del:{a}")] for a in db.extra_admins()]
    rows.append([("➕ افزودن مدیر", "adm:add")])
    text = ("👮 <b>مدیران ربات</b>\n"
            "مالک‌ها از فایل تنظیمات سرور هستند و از اینجا حذف نمی‌شوند.\n"
            "مدیرها به همه‌ی بخش‌ها جز مدیریت مدیران دسترسی دارند.")
    return text, ikb(rows)


@router.callback_query(F.data == "adm:menu", owner)
async def admins_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = admins_view()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "adm:menu", admin)
async def admins_menu_denied(cb: CallbackQuery):
    await cb.answer("فقط مالک ربات می‌تواند مدیران را تغییر دهد", show_alert=True)


@router.callback_query(F.data == "noop_a")
async def admins_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "adm:add", owner)
async def admins_add_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.add_admin)
    await cb.message.answer("آیدی عددی تلگرام مدیر جدید را بفرستید.\n"
                            "(می‌تواند آیدی‌اش را از ربات @userinfobot بگیرد.)", reply_markup=CANCEL_KB)


@router.message(Edit.add_admin, owner)
async def admins_add_do(msg: Message, state: FSMContext, bot: Bot):
    text = (msg.text or "").strip()
    if not text.isdigit():
        await msg.answer("❌ فقط آیدی عددی بفرستید، مثلاً <code>123456789</code>")
        return
    new_id = int(text)
    await state.clear()
    if new_id in db.admin_ids():
        await msg.answer("این شخص از قبل مدیر است.", reply_markup=ADMIN_KB)
        return
    db.set_setting("admins", ",".join(str(a) for a in db.extra_admins() + [new_id]))
    await msg.answer(f"✅ <code>{new_id}</code> مدیر شد.", reply_markup=ADMIN_KB)
    try:
        await bot.send_message(new_id, "👮 شما مدیر این ربات شدید. /start را بزنید.")
    except Exception:
        await msg.answer("ℹ️ نتوانستم به او پیام بدهم؛ باید یک بار خودش ربات را /start کند.")
    text, kb = admins_view()
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:del:"), owner)
async def admins_del(cb: CallbackQuery):
    target = int(cb.data.rsplit(":", 1)[1])
    db.set_setting("admins", ",".join(str(a) for a in db.extra_admins() if a != target))
    await cb.answer(f"🗑 {target} حذف شد")
    text, kb = admins_view()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "set:bchat", owner)
async def backup_chat_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.backup_chat)
    current = db.get_setting("backup_chat") or "ندارد"
    await cb.message.answer(
        f"📦 کانال بکاپ فعلی: <code>{current}</code>\n\n"
        "بکاپ روزانه علاوه بر شما به این کانال هم فرستاده می‌شود، تا اگر سرور از دست رفت، اطلاعات بماند.\n"
        "۱) یک کانال خصوصی بسازید و ربات را ادمین آن کنید.\n"
        "۲) یک پیام از کانال را برای این ربات فوروارد کنید، یا آیدی عددی کانال را بفرستید (مثل <code>-100123...</code>).\n"
        "برای حذف: <code>reset</code>", reply_markup=CANCEL_KB)


@router.callback_query(F.data == "set:bchat", admin)
async def backup_chat_denied(cb: CallbackQuery):
    await cb.answer("فقط مالک ربات می‌تواند کانال بکاپ را تغییر دهد", show_alert=True)


@router.message(Edit.backup_chat, owner)
async def backup_chat_set(msg: Message, state: FSMContext, bot: Bot):
    origin = getattr(msg, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    text = (msg.text or "").strip()
    if text.lower() == "reset":
        db.set_setting("backup_chat", "")
        await state.clear()
        await msg.answer("✅ کانال بکاپ حذف شد.", reply_markup=ADMIN_KB)
        return
    chat_id = chat.id if chat else (int(text) if re.fullmatch(r"-?\d+", text) else None)
    if chat_id is None:
        await msg.answer("❌ یک پیام از کانال فوروارد کنید یا آیدی عددی آن را بفرستید.")
        return
    try:
        await bot.send_message(chat_id, "✅ این کانال برای بکاپ‌های ربات تنظیم شد.")
    except Exception as e:
        await msg.answer(f"❌ نمی‌توانم در این کانال پیام بفرستم. ربات ادمین کانال است؟\n<code>{html.escape(str(e))[:200]}</code>")
        return
    db.set_setting("backup_chat", str(chat_id))
    await state.clear()
    await msg.answer("✅ کانال بکاپ تنظیم شد. یک بکاپ همین الان فرستاده می‌شود.", reply_markup=ADMIN_KB)
    await send_backup(bot, chat_id)


# ---------- Iranian relay servers (agents report through the tunnels) ----------

def relay_script_url() -> str:
    # pinned to this server's commit: same version everywhere, and no GitHub cache surprises
    return f"https://raw.githubusercontent.com/IsaHo/IsaHo/{relays.current_ref()}/vpn/relay.sh"


def relays_view():
    lines, rows = ["🛰 <b>سرورهای ایران</b>", ""], []
    known = {h for h, _ in links.relays()}
    relays.forget_stale(known)
    shown = sorted(known | set(relays.reports))
    if not shown:
        return "هیچ سرور ایرانی گزارشی نفرستاده است.", None
    for i, ip in enumerate(shown, 1):
        r = relays.reports.get(ip)
        if not r:
            lines.append(f"⚪️ <code>{ip}</code> — هنوز گزارشی نفرستاده (relay.sh را دوباره اجرا کنید)")
            continue
        ago = int(time.time() - r["seen"])
        icon = "🟢" if relays.online(ip) and r.get("tunnels_up") == r.get("tunnels_total") else (
            "🟡" if relays.online(ip) and r.get("tunnels_up") else "🔴")
        lines += [
            f"{icon} <b>{html.escape(str(r.get('hostname', ip)))}</b> <code>{ip}</code>",
            f"   تانل‌ها: {r.get('tunnels_up', '?')}/{r.get('tunnels_total', '?')} | "
            f"load {r.get('load', 0):.2f} | RAM {r.get('mem', 0)}%",
            f"   ⬇️ {fmt.size(int(r.get('rx_rate', 0)))}/s ⬆️ {fmt.size(int(r.get('tx_rate', 0)))}/s | "
            f"آخرین گزارش: {ago} ثانیه پیش",
            f"   نسخه: <code>{html.escape(str(r.get('version', '?'))[:10])}</code>",
        ]
        if r.get("last"):
            lines.append(f"   آخرین آپدیت: <code>{html.escape(str(r['last'])[-120:])}</code>")
        if ip in relays.pending:
            lines.append(f"   ⏳ در صف: {relays.pending[ip]['action']}")
        rows.append([(f"🔄 ریستارت {ip}", f"rl:restart:{ip}"), (f"⬆️ آپدیت {ip}", f"rl:update:{ip}")])
    rows.append([("⬆️ آپدیت همه", "rl:update:all"), ("🔃 بروزرسانی", "rl:menu")])
    lines += ["", f"نسخه‌ی سرور خارج: <code>{relays.current_ref()[:10]}</code>"]
    return "\n".join(lines), ikb(rows)


@router.callback_query(F.data == "rl:menu", admin)
async def relays_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = relays_view()
    if (cb.message.text or "").startswith("🛰"):
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass  # unchanged content
    else:
        await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith(("rl:restart:", "rl:update:")), admin)
async def relays_action(cb: CallbackQuery):
    _, action, target = cb.data.split(":", 2)
    targets = list(relays.reports) if target == "all" else [target]
    ref = relays.current_ref()
    for ip in targets:
        relays.queue(ip, action, ref if action == "update" else "")
    await cb.answer("⏳ در صف قرار گرفت؛ حداکثر تا یک دقیقه‌ی دیگر اجرا می‌شود", show_alert=True)
    text, kb = relays_view()
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass


@router.callback_query(F.data == "set:restart", admin)
async def settings_restart(cb: CallbackQuery):
    ok = await xray.restart()
    await cb.answer("✅ ریستارت شد" if ok else "❌ ریستارت ناموفق", show_alert=not ok)


@router.callback_query(F.data == "set:rebuild", admin)
async def settings_rebuild(cb: CallbackQuery):
    try:
        await xray.apply_all()
        await cb.answer("✅ کانفیگ بازسازی و اعمال شد")
    except Exception as e:
        await cb.answer(f"❌ {e}"[:190], show_alert=True)


@router.callback_query(F.data.startswith("set:port:"), admin)
async def settings_port(cb: CallbackQuery):
    port = cb.data.rsplit(":", 1)[1]
    db.set_setting("cdn_public_port", "" if port == str(cfg.cdn_port) else port)
    await cb.answer(f"✅ پورت CDN در لینک‌ها: {port}", show_alert=True)
    if port == "443":
        await cb.message.answer(
            "⚠️ برای پورت 443 باید در کلادفلر یک Origin Rule بسازید:\n"
            "Rules ← Origin Rules ← Create rule\n"
            f"• Custom filter: Hostname equals <code>{cfg.domain}</code> AND Server Port equals <code>443</code>\n"
            f"• Destination Port → Rewrite to <code>{cfg.cdn_port}</code>\n\n"
            "بعد کاربران فقط سابسکریپشن را آپدیت کنند. تست: <code>isaho doctor</code>")


@router.callback_query(F.data == "set:relaytest", admin)
async def settings_relaytest(cb: CallbackQuery):
    await cb.answer()
    users = db.active_users()
    if not users:
        await cb.message.answer("اول یک کاربر فعال بسازید.")
        return
    base = f"curl -fsSL {relay_script_url()} | bash -s test"
    reality = links.reality_link(users[0], cfg.server_ip, cfg.reality_port, "test")
    cmd1 = f"{base} '{reality}' {cfg.server_ip}"
    cmd2 = f"{base} '{links.cdn_link(users[0])}'"
    await cb.message.answer("روی هر دستور بزنید تا کپی شود و در ترمینال <b>سرور ایران</b> پیست کنید.\n\n"
                            f"۱) Reality مستقیم به سرور خارج:\n<code>{html.escape(cmd1)}</code>\n\n"
                            f"۲) از طریق کلادفلر (CDN):\n<code>{html.escape(cmd2)}</code>")


@router.callback_query(F.data == "set:relays", admin)
async def settings_relays(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.relays)
    await cb.message.answer(
        "🇮🇷 آدرس سرور(های) واسط ایران را به شکل <code>IP:پورت</code> بفرستید؛ چند تا را با کاما جدا کنید.\n"
        "مثال: <code>1.2.3.4:443</code>\n"
        "برای حذف همه: <code>reset</code>\n\n"
        "روی سرور ایران قبلش این را اجرا کنید:\n"
        f"<code>curl -fsSL {relay_script_url()} | bash -s ssh {cfg.server_ip} 3</code>\n"
        "و خط AUTHORIZED را روی همین سرور خارج اجرا کنید.", reply_markup=CANCEL_KB)


@router.message(Edit.relays, admin)
async def settings_relays_set(msg: Message, state: FSMContext):
    value = (msg.text or "").strip()
    if value.lower() == "reset":
        value = ""
    elif not re.match(r"^[A-Za-z0-9.\-]+(:\d{1,5})?(\s*,\s*[A-Za-z0-9.\-]+(:\d{1,5})?)*$", value):
        await msg.answer("❌ قالب نامعتبر است. مثال: <code>1.2.3.4:443</code>")
        return
    db.set_setting("relays", value.replace(" ", ""))
    await state.clear()
    await msg.answer(f"✅ سرورهای واسط: <code>{', '.join(f'{h}:{p}' for h, p in links.relays()) or 'ندارد'}</code>\n"
                     "کاربران فقط سابسکریپشن را آپدیت کنند.", reply_markup=ADMIN_KB)


@router.callback_query(F.data == "set:cdn", admin)
async def settings_cdn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.cdn_ip)
    await cb.message.answer(
        "🌐 یک IP یا دامنه تمیز کلادفلر بفرستید تا در لینک‌های CDN استفاده شود.\n"
        "برای برگشت به حالت پیش‌فرض (خود دامنه) کلمه <code>reset</code> را بفرستید.\n"
        "بعد از تغییر، کاربران فقط کافیست سابسکریپشن را آپدیت کنند.", reply_markup=CANCEL_KB)


@router.message(Edit.cdn_ip, admin)
async def settings_cdn_set(msg: Message, state: FSMContext):
    value = (msg.text or "").strip()
    if not re.match(r"^[A-Za-z0-9.:\-\[\]]{3,253}$", value):
        await msg.answer("❌ مقدار نامعتبر است.")
        return
    db.set_setting("cdn_address", "" if value.lower() == "reset" else value)
    await state.clear()
    await msg.answer(f"✅ آدرس CDN: <code>{links.cdn_address()}</code>", reply_markup=ADMIN_KB)


# ---------- backup / restore ----------

async def send_backup(bot: Bot, chat_id: int) -> None:
    await xray.flush_stats()
    snapshot = os.path.join(cfg.data_dir, "backup.db")
    src, dst = sqlite3.connect(cfg.db_path), sqlite3.connect(snapshot)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    stamp = time.strftime("%Y-%m-%d_%H-%M")
    await bot.send_document(chat_id, FSInputFile(snapshot, filename=f"isaho_{stamp}.db"),
                            caption=f"💾 بکاپ {stamp}\nبرای بازگردانی، همین فایل را به ربات بفرستید.")


def users_csv() -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "enabled", "used_GB", "limit_GB", "expire", "days_left", "telegram", "note", "created"])
    now = time.time()
    for u in db.all_users():
        w.writerow([
            u.name, "yes" if u.enabled else f"no ({u.disabled_reason})",
            round(u.used / db.GB, 2), round(u.traffic_limit / db.GB, 2) if u.traffic_limit else "unlimited",
            time.strftime("%Y-%m-%d", time.localtime(u.expire_at)) if u.expire_at
            else ("first use" if u.pending_days else "never"),
            int((u.expire_at - now) // db.DAY) if u.expire_at else (u.pending_days or ""),
            u.tg_id or "", u.note, time.strftime("%Y-%m-%d", time.localtime(u.created_at)),
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM so Excel shows Persian correctly


@router.message(F.text == BTN_BACKUP, admin)
async def backup_cmd(msg: Message, bot: Bot):
    await send_backup(bot, msg.chat.id)
    if db.get_setting("backup_chat"):
        try:
            await send_backup(bot, int(db.get_setting("backup_chat")))
        except Exception:
            await msg.answer("⚠️ ارسال به کانال بکاپ ناموفق بود.")
    await msg.answer_document(BufferedInputFile(users_csv(), f"users_{time.strftime('%Y-%m-%d')}.csv"),
                              caption="📄 لیست کاربران (با Excel باز می‌شود)")


@router.message(F.document, admin)
async def restore_ask(msg: Message, state: FSMContext):
    if not (msg.document.file_name or "").endswith(".db"):
        return
    await state.update_data(restore_file=msg.document.file_id)
    await msg.answer("⚠️ دیتابیس فعلی با این فایل جایگزین شود؟",
                     reply_markup=ikb([[("✅ بله، بازگردانی کن", "restore"), ("❌ نه", "noop")]]))


@router.callback_query(F.data == "restore", admin)
async def restore_do(cb: CallbackQuery, state: FSMContext, bot: Bot):
    file_id = (await state.get_data()).get("restore_file")
    await state.clear()
    if not file_id:
        await cb.answer("فایل پیدا نشد", show_alert=True)
        return
    tmp = cfg.db_path + ".upload"
    await bot.download(file_id, destination=tmp)
    try:
        with sqlite3.connect(tmp) as c:
            c.execute("SELECT count(*) FROM users").fetchone()
    except sqlite3.Error:
        os.unlink(tmp)
        await cb.answer("❌ فایل دیتابیس معتبر نیست", show_alert=True)
        return
    shutil.copy(cfg.db_path, cfg.db_path + ".before-restore")
    os.replace(tmp, cfg.db_path)
    db.init()
    await xray.apply_all()
    await cb.answer()
    await cb.message.edit_text(f"✅ بازگردانی شد. کاربران: {len(db.all_users())}")


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer("لغو شد")
    await cb.message.delete()


# ---------- broadcast ----------

@router.message(F.text == BTN_BROADCAST, admin)
async def broadcast_start(msg: Message, state: FSMContext):
    linked = [u for u in db.all_users() if u.tg_id]
    await state.set_state(Edit.broadcast)
    await msg.answer(f"📢 پیام را بفرستید. برای {len({u.tg_id for u in linked})} کاربر متصل به ربات ارسال می‌شود.",
                     reply_markup=CANCEL_KB)


@router.message(Edit.broadcast, admin)
async def broadcast_do(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    sent = failed = 0
    for tg_id in {u.tg_id for u in db.all_users() if u.tg_id}:
        try:
            await msg.copy_to(tg_id)
            sent += 1
        except Exception:
            failed += 1
    await msg.answer(f"✅ ارسال شد: {sent} | ناموفق: {failed}", reply_markup=ADMIN_KB)


# ---------- dashboard ----------

@router.message(F.text == BTN_DASH, admin)
async def dashboard(msg: Message):
    await xray.flush_stats()
    users, now = db.all_users(), time.time()
    today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m-01")
    active = [u for u in users if u.enabled]
    expiring = sorted((u for u in active if u.expire_at and u.expire_at - now < 3 * db.DAY),
                      key=lambda u: u.expire_at)
    low = [u for u in active if u.traffic_limit and u.used >= u.traffic_limit * 0.9]
    lines = [
        "📈 <b>داشبورد</b>", "",
        f"👥 کاربران: {len(users)} | 🟢 فعال: {len(active)} | 🔴 غیرفعال: {len(users) - len(active)}",
        f"⏱ منتظر اولین اتصال: {sum(1 for u in users if u.pending_days)}",
        "",
        f"📦 مصرف امروز: <b>{fmt.size(db.usage_since(today))}</b>",
        f"📦 مصرف این ماه: <b>{fmt.size(db.usage_since(month))}</b>",
        f"⚡ لحظه‌ای: {fmt.size(int(xray.last_rate))}/s",
    ]
    chart = fmt.day_chart(db.daily_totals(7))
    if chart:
        lines += ["", "📊 <b>۷ روز اخیر</b>", *chart]
    top = db.top_usage_since(today)
    if top:
        lines += ["", "🏆 <b>پرمصرف‌های امروز</b>"]
        lines += [f"{i}. {html.escape(n)} — {fmt.size(b)}" for i, (n, b) in enumerate(top, 1)]
    if expiring:
        lines += ["", "⏳ <b>تا ۳ روز دیگر منقضی می‌شوند</b>"]
        lines += [f"• {html.escape(u.name)} — {fmt.remaining_days(u)}" for u in expiring[:15]]
    if low:
        lines += ["", "🔋 <b>حجمشان رو به اتمام است</b>"]
        lines += [f"• {html.escape(u.name)} — {fmt.size(u.used)} از {fmt.size(u.traffic_limit)}" for u in low[:15]]
    lines += ["", "📡 <b>تانل‌ها</b>", tunnels.summary()]
    await msg.answer("\n".join(lines))


# ---------- bulk operations ----------

@router.message(F.text == BTN_BULK, admin)
async def bulk_menu(msg: Message):
    await msg.answer("🧰 <b>عملیات گروهی</b>\nروی همه‌ی کاربران اعمال می‌شود (کاربرانی که دستی غیرفعال کرده‌اید جدا می‌مانند).",
                     reply_markup=ikb([
                         [("⏳ تمدید همه", "bulk:days")],
                         [("📦 افزودن حجم به همه", "bulk:gb")],
                         [("🗑 حذف کاربران منقضی/تمام‌شده", "bulk:purge")],
                     ]))


@router.callback_query(F.data.in_({"bulk:days", "bulk:gb"}), admin)
async def bulk_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if cb.data == "bulk:days":
        await state.set_state(Edit.bulk_days)
        await cb.message.answer("⏳ چند روز به همه‌ی کاربران زمان‌دار اضافه شود؟ (مثلاً برای جبران قطعی)",
                                reply_markup=CANCEL_KB)
    else:
        await state.set_state(Edit.bulk_gb)
        await cb.message.answer("📦 چند گیگ به همه‌ی کاربران حجم‌دار اضافه شود؟", reply_markup=CANCEL_KB)


@router.message(Edit.bulk_days, admin)
@router.message(Edit.bulk_gb, admin)
async def bulk_do(msg: Message, state: FSMContext):
    v = parse_number(msg.text)
    if not v:
        await msg.answer("❌ یک عدد بزرگ‌تر از صفر بفرستید.")
        return
    is_days = await state.get_state() == Edit.bulk_days.state
    await state.clear()
    changed = 0
    for u in db.all_users():
        if not u.enabled and u.disabled_reason == "manual":
            continue
        if is_days and u.pending_days:
            db.update(u.id, pending_days=u.pending_days + int(v))
        elif is_days and u.expire_at:
            db.update(u.id, expire_at=max(u.expire_at, int(time.time())) + int(v) * db.DAY, warned=0)
        elif not is_days and u.traffic_limit:
            db.update(u.id, traffic_limit=u.traffic_limit + int(v * db.GB), warned=0)
        else:
            continue
        changed += 1
        await maybe_reactivate(u)
    what = f"{int(v)} روز" if is_days else f"{v:g} گیگ"
    await msg.answer(f"✅ {what} به {changed} کاربر اضافه شد.", reply_markup=ADMIN_KB)


@router.callback_query(F.data == "bulk:purge", admin)
async def bulk_purge_ask(cb: CallbackQuery):
    await cb.answer()
    dead = [u for u in db.all_users() if not u.enabled and u.disabled_reason in ("expired", "traffic")]
    if not dead:
        await cb.message.answer("کاربر منقضی یا تمام‌شده‌ای وجود ندارد.")
        return
    names = ", ".join(html.escape(u.name) for u in dead[:30])
    await cb.message.answer(f"🗑 {len(dead)} کاربر حذف شوند؟\n{names}{' …' if len(dead) > 30 else ''}",
                            reply_markup=ikb([[("✅ بله، حذف کن", "bulk:purgeok"), ("❌ نه", "noop")]]))


@router.callback_query(F.data == "bulk:purgeok", admin)
async def bulk_purge(cb: CallbackQuery):
    dead = [u for u in db.all_users() if not u.enabled and u.disabled_reason in ("expired", "traffic")]
    for u in dead:
        db.delete(u.id)
    await cb.answer()
    await cb.message.edit_text(f"🗑 {len(dead)} کاربر حذف شد.")


# ---------- end-user help and support ----------

HELP_TEXT = """📱 <b>آموزش اتصال</b>

<b>iPhone</b>
1. یکی از اپ‌های <b>Streisand</b>، <b>Shadowrocket</b> یا <b>V2Box</b> را از App Store نصب کنید.
2. در همین ربات «📊 حساب من» را بزنید و روی لینک سابسکریپشن بزنید تا کپی شود.
3. در اپ، دکمه‌ی + را بزنید و از کلیپ‌بورد اضافه کنید.
4. کانفیگ را انتخاب و وصل شوید.

<b>Android</b>
1. اپ <b>v2rayNG</b> یا <b>Hiddify</b> را نصب کنید.
2. لینک سابسکریپشن را از «📊 حساب من» کپی کنید.
3. در v2rayNG: منوی ☰ ← Subscription group ← + ← لینک را پیست کنید، بعد «Update subscription».
4. کانفیگ را انتخاب و دکمه‌ی وصل شدن را بزنید.

<b>اگر وصل نشد</b>
• یک بار سابسکریپشن را آپدیت کنید (وقتی VPN خاموش است).
• اینترنت را یک بار قطع و وصل کنید.
• از «💬 پشتیبانی» به ما پیام بدهید."""


@router.message(F.text == BTN_HELP)
async def help_cmd(msg: Message):
    await msg.answer(HELP_TEXT, disable_web_page_preview=True)


@router.message(F.text == BTN_SUPPORT)
async def support_start(msg: Message, state: FSMContext):
    await state.set_state(Edit.support)
    await msg.answer("💬 پیامتان را بنویسید (می‌توانید عکس هم بفرستید):", reply_markup=CANCEL_KB)


@router.message(F.text == BTN_CANCEL)
async def user_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("لغو شد.", reply_markup=USER_KB)


@router.message(Edit.support)
async def support_send(msg: Message, state: FSMContext, bot: Bot):
    await state.clear()
    accounts = ", ".join(u.name for u in db.owned_by(msg.from_user.id)) or "ندارد"
    who = html.escape(msg.from_user.full_name or "")
    header = (f"💬 <b>پیام پشتیبانی</b>\nاز: {who} (<code>{msg.from_user.id}</code>)\n"
              f"اکانت: {html.escape(accounts)}")
    for admin_id in db.admin_ids():
        try:
            await bot.send_message(admin_id, header,
                                   reply_markup=ikb([[("↩️ پاسخ", f"rep:{msg.from_user.id}")]]))
            await msg.copy_to(admin_id)
        except Exception:
            pass
    await msg.answer("✅ پیامتان برای پشتیبانی فرستاده شد.", reply_markup=USER_KB)


@router.callback_query(F.data.startswith("rep:"), admin)
async def support_reply_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(Edit.reply)
    await state.update_data(reply_to=int(cb.data[4:]))
    await cb.message.answer("✍️ پاسخ را بنویسید:", reply_markup=CANCEL_KB)


@router.message(Edit.reply, admin)
async def support_reply_send(msg: Message, state: FSMContext, bot: Bot):
    target = (await state.get_data()).get("reply_to")
    await state.clear()
    try:
        await bot.send_message(target, "💬 <b>پاسخ پشتیبانی:</b>")
        await msg.copy_to(target)
        await msg.answer("✅ پاسخ فرستاده شد.", reply_markup=ADMIN_KB)
    except Exception:
        await msg.answer("❌ ارسال نشد (شاید کاربر ربات را بلاک کرده).", reply_markup=ADMIN_KB)
