"""Telegram bot handlers: admin panel and end-user self-service."""
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
import xray
from config import cfg

log = logging.getLogger(__name__)
router = Router()
PAGE = 10
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")


class IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        return event.from_user is not None and event.from_user.id in cfg.admin_ids


admin = IsAdmin()


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
    broadcast = State()


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

ADMIN_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
    [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_USERS)],
    [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_STATUS)],
    [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_BACKUP)],
    [KeyboardButton(text=BTN_BROADCAST)],
])
CANCEL_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text=BTN_CANCEL)]])
USER_KB = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[[KeyboardButton(text=BTN_MY)]])


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
    for link in links.all_links(u):
        label = "⚡ Reality (سریع‌ترین)" if "security=reality" in link else "☁️ CDN (پشتیبان، ضد فیلتر)"
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


async def show_user(target, u, edit: bool = True):
    text = fmt.user_card(u)
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
        await msg.answer(fmt.user_card(u) + "\n\n" + links_text(u).rsplit("\n🤖", 1)[0],
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
    await m.answer("⏳ مدت (روز) را بفرستید یا انتخاب کنید (۰ = نامحدود):",
                   reply_markup=ikb([[("30", "d:30"), ("60", "d:60"), ("90", "d:90")],
                                     [("180", "d:180"), ("365", "d:365"), ("♾ نامحدود", "d:0")]]))


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


async def _finish_add(target, state: FSMContext, days: int):
    data = await state.get_data()
    await state.clear()
    m = target.message if isinstance(target, CallbackQuery) else target
    try:
        u = db.create_user(data["name"], data["traffic"], days)
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
    await _finish_add(cb, state, int(cb.data[2:]))


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
        if current == Edit.days.state:
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
        f"📦 مصرف کل کاربران: {fmt.size(sum(u.used for u in users))}"
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
        f"CDN (XHTTP): پورت {cfg.cdn_port} | path <code>{cfg.cdn_path}</code>\n"
        f"آدرس اتصال CDN: <code>{links.cdn_address()}</code>\n"
        f"سابسکریپشن: پورت {cfg.sub_port}"
    )
    await msg.answer(text, reply_markup=ikb([
        [("🌐 تنظیم IP تمیز کلادفلر", "set:cdn")],
        [("🔄 ریستارت Xray", "set:restart"), ("🛠 بازسازی کانفیگ", "set:rebuild")],
    ]))


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


@router.message(F.text == BTN_BACKUP, admin)
async def backup_cmd(msg: Message, bot: Bot):
    await send_backup(bot, msg.chat.id)


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
