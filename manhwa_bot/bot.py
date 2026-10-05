"""
bot.py — لانچرِ مشترک: «سرراست» و «سولاخی» داخل یک ربات تلگرام.

  • منوی اصلی: 📚 سرراست  |  🎬 سولاخی
  • هر بخش منوی خودش را دارد؛ «🏠 منوی اصلی» برمی‌گرداند.
  • دکمه‌های شیشه‌ای (callback) بر اساس پیشوندشان به ربات درست می‌روند،
    پس پیام‌های قدیمی بعد از عوض‌کردن بخش هم کار می‌کنند.
  • دسترسی هر دو بخش با همان «👤 مدیریت آیدی‌ها»ی سرراست کنترل می‌شود.
  • اعلان‌های خودکار هر دو (بروزرسانی سرراست + ویدیوی جدید سولاخی) فعال‌اند.
  • 🧹 حریم خصوصی (برای کل چت): پاک شدن خودکار پیام‌ها + «همین الان پاک کن».

کد هر ربات دست‌نخورده در فایل خودش است:
  manhwa_bot/sarrast.py        ← ربات سرراست
  soolakhi_bot/bot.py          ← ربات سولاخی

اجرا:  python bot.py   (توکن از manhwa_bot/.env)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ExtBot,
    InlineQueryHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# سرراست اول لود می‌شود تا .env (BOT_TOKEN) را در محیط بگذارد؛ سولاخی هنگام import به آن نیاز دارد.
import sarrast  # noqa: E402
import privacy  # noqa: E402

privacy.init(sarrast.DATA_DIR)


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


soolakhi = _load_module("soolakhi", os.path.join(HERE, "..", "soolakhi_bot", "bot.py"))

log = logging.getLogger("launcher")

BTN_SARRAST = "📚 سرراست"
BTN_SOOLAKHI = "🎬 سولاخی"
BTN_HOME = sarrast.BTN_HOME
BTN_PRIVACY = sarrast.BTN_PRIVACY
ROOT_KB = ReplyKeyboardMarkup([[BTN_SARRAST, BTN_SOOLAKHI], [BTN_PRIVACY]], resize_keyboard=True, is_persistent=True)

# دسترسی یکپارچه: سولاخی هم از لیست آیدی‌های سرراست (مدیر + اضافه‌شده‌ها) پیروی کند
soolakhi.allowed = lambda update: sarrast.authorized(update)
# دکمهٔ برگشت به منوی اصلی در منوی سولاخی
soolakhi.MENU = ReplyKeyboardMarkup(
    [list(r) for r in soolakhi.MENU.keyboard] + [[BTN_PRIVACY, BTN_HOME]], resize_keyboard=True)

# دکمه‌هایی که فقط مال یک بخش‌اند (برای وقتی که بعد از ری‌استارت حالت معلوم نیست)
_SARRAST_BTNS = set(sarrast.MENU_BUTTONS)
_SOOLAKHI_BTNS = {soolakhi.BTN_SITES, soolakhi.BTN_NEW, soolakhi.BTN_FAVS, soolakhi.BTN_STOP,
                  soolakhi.BTN_SEARCH, soolakhi.BTN_PANEL}
_SHARED = _SARRAST_BTNS & _SOOLAKHI_BTNS
_SARRAST_BTNS -= _SHARED
_SOOLAKHI_BTNS -= _SHARED

# callbackهای سرراست؛ بقیه مال سولاخی است («s.» = همهٔ callbackهای جدید سرراست)
_SARRAST_CB_PREFIXES = ("s.", "go:", "uf:", "delid:")
_SARRAST_CB_EXACT = {"addid"}

# ---------- حالت فعلی هر کاربر (در فایل، تا بعد از ری‌استارت هم بماند) ----------

MODE_FILE = os.path.join(sarrast.DATA_DIR, "mode.json")


def _modes() -> dict:
    try:
        with open(MODE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_mode(uid) -> str | None:
    return _modes().get(str(uid))


def set_mode(uid, mode: str | None) -> None:
    d = _modes()
    if mode:
        d[str(uid)] = mode
    else:
        d.pop(str(uid), None)
    os.makedirs(sarrast.DATA_DIR, exist_ok=True)
    tmp = MODE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, MODE_FILE)


# ---------- ربات با ثبت پیام‌ها (برای پاک‌سازی حریم خصوصی) ----------

class TrackingBot(ExtBot):
    """همان ربات معمولی؛ فقط آیدی هر پیامی که می‌فرستد را برای پاک‌سازی بعدی ثبت می‌کند."""

    async def send_message(self, *a, **k):
        return _tracked(await super().send_message(*a, **k))

    async def send_photo(self, *a, **k):
        return _tracked(await super().send_photo(*a, **k))

    async def send_document(self, *a, **k):
        return _tracked(await super().send_document(*a, **k))

    async def send_video(self, *a, **k):
        return _tracked(await super().send_video(*a, **k))

    async def send_animation(self, *a, **k):
        return _tracked(await super().send_animation(*a, **k))

    async def send_audio(self, *a, **k):
        return _tracked(await super().send_audio(*a, **k))

    async def send_media_group(self, *a, **k):
        msgs = await super().send_media_group(*a, **k)
        for m in msgs:
            _tracked(m)
        return msgs


def _tracked(msg):
    privacy.track_message(msg)
    return msg


def privacy_text(chat_id) -> str:
    d = privacy.get_delay(chat_id)
    return ("🧹 حریم خصوصی\n\n"
            f"⏱ پاک شدن خودکار پیام‌ها: {privacy.delay_label(d)}\n"
            "(هم پیام‌های ربات، هم پیام‌هایی که تو فرستادی — در هر دو بخش)\n\n"
            "🧹 «همین الان پاک کن» همهٔ پیام‌های این چت از ۴۸ ساعت اخیر رو پاک می‌کنه.\n"
            "ℹ️ تلگرام به ربات اجازهٔ پاک کردن پیام‌های قدیمی‌تر از ۴۸ ساعت رو نمی‌ده.")


def privacy_kb(chat_id) -> InlineKeyboardMarkup:
    d = privacy.get_delay(chat_id)
    opts = [InlineKeyboardButton(("✅ " if sec == d else "") + label, callback_data=f"p.ad:{sec}")
            for sec, label in privacy.DELAYS]
    return InlineKeyboardMarkup([opts[:2], opts[2:], [InlineKeyboardButton("🧹 همین الان پاک کن", callback_data="p.clean")]])


async def on_privacy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not sarrast.authorized(update):
        return await q.answer("⛔ دسترسی ندارید", show_alert=True)
    chat_id = q.message.chat_id
    if q.data.startswith("p.ad:"):
        sec = int(q.data[5:])
        privacy.set_delay(chat_id, sec)
        await q.answer(f"⏱ پاک شدن خودکار: {privacy.delay_label(sec)}")
        try:
            await q.message.edit_text(privacy_text(chat_id), reply_markup=privacy_kb(chat_id))
        except Exception:
            pass
        return
    if q.data == "p.clean":
        await q.answer("🧹 در حال پاک کردن...")
        ids = privacy.pop_all(chat_id)
        if q.message.message_id not in ids:
            ids.append(q.message.message_id)
        n = await privacy.delete_ids(context.bot, chat_id, ids)
        mode = get_mode(update.effective_user.id)
        kb = sarrast.main_kb() if mode == "sarrast" else soolakhi.MENU if mode == "soolakhi" else ROOT_KB
        await context.bot.send_message(chat_id, f"🧹 چت پاک شد ({n} پیام).", reply_markup=kb)


async def autodelete_loop(app: Application):
    """هر ۳۰ ثانیه پیام‌هایی که وقت پاک شدنشان رسیده را پاک می‌کند."""
    while True:
        await asyncio.sleep(30)
        try:
            for chat_id, ids in privacy.pop_due().items():
                await privacy.delete_ids(app.bot, chat_id, ids)
        except Exception:
            log.exception("autodelete")


# ---------- handlers ----------

async def show_root(update: Update, text: str = None):
    await update.effective_message.reply_text(
        text or "سلام! 👋 کدوم بخش؟\n\n"
                "📚 سرراست\n"
                "مانهوا: کارت داستان، ادامهٔ خواندن، دسته‌بندی، لینک مستقیم قسمت‌ها، اعلان قسمت جدید\n\n"
                "🎬 سولاخی\n"
                "ویدیو: اسکن سایت‌ها، جستجو، ذخیره‌ها، دانلود، اعلان ویدیوی جدید",
        reply_markup=ROOT_KB)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not sarrast.authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    privacy.track_message(update.message)
    set_mode(update.effective_user.id, None)
    await show_root(update)


async def enter(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    set_mode(update.effective_user.id, mode)
    if mode == "sarrast":
        await sarrast.cmd_start(update, context)
    else:
        await soolakhi.cmd_start(update, context)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not sarrast.authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    uid = update.effective_user.id
    t = (update.message.text or "").strip()
    privacy.track_message(update.message)

    if t == BTN_PRIVACY:
        chat_id = update.effective_chat.id
        return await update.message.reply_text(privacy_text(chat_id), reply_markup=privacy_kb(chat_id))
    if t == BTN_SARRAST:
        return await enter(update, context, "sarrast")
    if t == BTN_SOOLAKHI:
        return await enter(update, context, "soolakhi")
    if t == BTN_HOME:
        set_mode(uid, None)
        return await show_root(update, "🏠 منوی اصلی — کدوم بخش؟")

    mode = get_mode(uid)
    if not mode:  # مثلاً بعد از ری‌استارت: از روی دکمه حدس بزن
        if t in _SARRAST_BTNS:
            mode = "sarrast"
        elif t in _SOOLAKHI_BTNS:
            mode = "soolakhi"
        if mode:
            set_mode(uid, mode)
        else:
            return await show_root(update, "اول بخش رو انتخاب کن 👇")

    if mode == "sarrast":
        return await sarrast.on_text(update, context)
    return await soolakhi.on_text(update, context)


async def route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = update.callback_query.data or ""
    if d.startswith("p."):
        return await on_privacy_callback(update, context)
    if d in _SARRAST_CB_EXACT or d.startswith(_SARRAST_CB_PREFIXES):
        return await sarrast.on_callback(update, context)
    return await soolakhi.on_button(update, context)


async def post_init(app: Application):
    sarrast.start_background(app)          # بروزرسانی‌ها + جمع‌کردن اطلاعات داستان‌ها
    app.create_task(soolakhi.watch_loop(app))
    app.create_task(autodelete_loop(app))
    for fn, txt in ((app.bot.set_my_short_description, "📚 سرراست + 🎬 سولاخی — مانهوا و ویدیو در یک ربات"),
                    (app.bot.set_my_description,
                     "📚 سرراست: جستجو، کارت داستان، ادامهٔ خواندن، دسته‌بندی و اعلان قسمت جدید\n"
                     "🎬 سولاخی: اسکن سایت‌های ویدیو، ذخیره‌ها و اعلان ویدیوی جدید")):
        try:
            await fn(txt)
        except Exception as e:
            log.info("description: %s", e)
    await app.bot.set_my_commands([
        BotCommand("start", "منوی اصلی (سرراست / سولاخی)"),
        BotCommand("update", "سرراست: چک بروزرسانی‌ها"),
        BotCommand("panel", "سولاخی: پنل کنترل"),
        BotCommand("debug", "سولاخی: بررسی یک صفحه"),
        BotCommand("logo", "سرراست: لوگوی سایت"),
    ])


def main():
    if not sarrast.BOT_TOKEN:
        raise SystemExit("BOT_TOKEN تنظیم نشده (در manhwa_bot/.env).")
    extra = {}
    if soolakhi.LOCAL_API:  # سرور Local Bot API برای آپلود تا 2GB (اختیاری)
        extra = {"base_url": soolakhi.LOCAL_API, "local_mode": True}
    bot = TrackingBot(sarrast.BOT_TOKEN,
                      request=HTTPXRequest(connection_pool_size=256),
                      get_updates_request=HTTPXRequest(connection_pool_size=1), **extra)
    app = Application.builder().bot(bot).concurrent_updates(True).post_init(post_init).build()

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    # سرراست
    app.add_handler(CommandHandler("update", sarrast.cmd_update))
    app.add_handler(CommandHandler("logo", sarrast.cmd_logo))
    # سولاخی
    app.add_handler(CommandHandler("scan", soolakhi.cmd_scan))
    app.add_handler(CommandHandler("debug", soolakhi.cmd_debug))
    app.add_handler(CommandHandler("panel", soolakhi.cmd_panel))
    app.add_handler(MessageHandler(filters.Document.ALL, soolakhi.on_document))
    # مسیردهی مشترک
    app.add_handler(CallbackQueryHandler(route_callback))
    app.add_handler(InlineQueryHandler(sarrast.on_inline))   # @ربات اسم‌داستان در هر چت
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))

    log.info("ربات مشترک (سرراست + سولاخی) روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
