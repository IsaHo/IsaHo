"""
bot.py — لانچرِ مشترک: «سرراست» و «سولاخی» داخل یک ربات تلگرام.

  • منوی اصلی: 📚 سرراست  |  🎬 سولاخی
  • هر بخش منوی خودش را دارد؛ «🏠 منوی اصلی» برمی‌گرداند.
  • دکمه‌های شیشه‌ای (callback) بر اساس پیشوندشان به ربات درست می‌روند،
    پس پیام‌های قدیمی بعد از عوض‌کردن بخش هم کار می‌کنند.
  • دسترسی هر دو بخش با همان «👤 مدیریت آیدی‌ها»ی سرراست کنترل می‌شود.
  • اعلان‌های خودکار هر دو (بروزرسانی سرراست + ویدیوی جدید سولاخی) فعال‌اند.

کد هر ربات دست‌نخورده در فایل خودش است:
  manhwa_bot/sarrast.py        ← ربات سرراست
  soolakhi_bot/bot.py          ← ربات سولاخی

اجرا:  python bot.py   (توکن از manhwa_bot/.env)
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys

from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# سرراست اول لود می‌شود تا .env (BOT_TOKEN) را در محیط بگذارد؛ سولاخی هنگام import به آن نیاز دارد.
import sarrast  # noqa: E402


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
ROOT_KB = ReplyKeyboardMarkup([[BTN_SARRAST, BTN_SOOLAKHI]], resize_keyboard=True, is_persistent=True)

# دسترسی یکپارچه: سولاخی هم از لیست آیدی‌های سرراست (مدیر + اضافه‌شده‌ها) پیروی کند
soolakhi.allowed = lambda update: sarrast.authorized(update)
# دکمهٔ برگشت به منوی اصلی در منوی سولاخی
soolakhi.MENU = ReplyKeyboardMarkup(
    [list(r) for r in soolakhi.MENU.keyboard] + [[BTN_HOME]], resize_keyboard=True)

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
    if d in _SARRAST_CB_EXACT or d.startswith(_SARRAST_CB_PREFIXES):
        return await sarrast.on_callback(update, context)
    return await soolakhi.on_button(update, context)


async def post_init(app: Application):
    sarrast.start_background(app)          # بروزرسانی‌ها + جمع‌کردن اطلاعات داستان‌ها
    app.create_task(soolakhi.watch_loop(app))
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
    b = Application.builder().token(sarrast.BOT_TOKEN).concurrent_updates(True)
    if soolakhi.LOCAL_API:  # سرور Local Bot API برای آپلود تا 2GB (اختیاری)
        b = b.base_url(soolakhi.LOCAL_API).local_mode(True)
    app = b.post_init(post_init).build()

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
