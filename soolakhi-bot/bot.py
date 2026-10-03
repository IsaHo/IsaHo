"""Telegram bot (default, token-only): scan soolakhi.com, list videos with
thumbnails, and on demand download the chosen one and send it into Telegram —
deleting it from the server right after.

Works with just a BOT_TOKEN (no api_id/api_hash needed). Telegram limits file
sending with a plain bot token to ~50 MB; videos bigger than SEND_LIMIT_MB are
not downloaded — the bot sends their page link instead (so nothing big is ever
written to the server). For sending big files (up to ~2 GB) inside Telegram,
use bot_2gb.py.

Run:  python bot.py
"""
import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
import downloader
import scraper
import store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("soolakhi-bot")

config.validate()

# One download/upload at a time -> at most one file on disk, bounded CPU.
download_lock = asyncio.Lock()
SEND_LIMIT = config.SEND_LIMIT_MB * 1024 * 1024


def is_admin(user_id):
    return (not config.ADMIN_IDS) or (user_id in config.ADMIN_IDS)


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ شما اجازه‌ی استفاده از این بات را ندارید.")
        return
    await update.message.reply_text(
        "سلام 👋\n\n"
        "این بات ویدیوهای سایت را اسکن می‌کند و با عکس و عنوان اینجا نشان می‌دهد. "
        "روی هر کدام که دکمه‌ی «⬇️ دانلود» را بزنی، همان لحظه دانلود و برایت ارسال می‌شود "
        "و بعد از ارسال از روی سرور پاک می‌شود.\n\n"
        "دستورها:\n"
        "• /scan — اسکن صفحه‌ی اول\n"
        "• /scan 2 — اسکن صفحه‌ی شماره ۲ (و همینطور بقیه صفحه‌ها)\n"
        "• /help — همین راهنما"
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️ اجازه نداری.")
        return
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    await send_listing(context, update.effective_chat.id, page)


# --------------------------------------------------------------------------- #
#  Listing
# --------------------------------------------------------------------------- #
async def send_listing(context, chat_id, page):
    status = await context.bot.send_message(chat_id, f"🔎 در حال اسکن صفحه‌ی {page} …")
    try:
        items = await asyncio.to_thread(scraper.fetch_listing, page)
    except Exception as e:  # noqa: BLE001  (surface any scrape error to the user)
        await status.edit_text(f"❌ خطا در اسکن سایت:\n{e}")
        return

    if not items:
        await status.edit_text(
            "چیزی پیدا نشد 🤔\n"
            "احتمالاً سلکتورهای سایت فرق دارند. روی سرور این را اجرا کن تا تنظیمشان کنی:\n"
            "`python scraper.py --page 1`"
        )
        return

    items = items[: config.ITEMS_PER_SCAN]
    await status.edit_text(f"📄 صفحه‌ی {page} — {len(items)} ویدیو:")

    for it in items:
        vid = store.add(it["url"], it["title"], it["thumb"])
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬇️ دانلود", callback_data=f"dl:{vid}")]]
        )
        caption = f"🎬 {it['title']}"
        try:
            if it["thumb"]:
                await context.bot.send_photo(
                    chat_id, it["thumb"], caption=caption, reply_markup=kb
                )
            else:
                await context.bot.send_message(chat_id, caption, reply_markup=kb)
        except Exception:  # noqa: BLE001  (bad/blocked thumbnail -> text fallback)
            await context.bot.send_message(chat_id, caption, reply_markup=kb)
        await asyncio.sleep(0.4)  # be gentle with Telegram rate limits

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("➡️ صفحه‌ی بعد", callback_data=f"pg:{page + 1}")]]
    )
    await context.bot.send_message(chat_id, "برای ادامه:", reply_markup=kb)


# --------------------------------------------------------------------------- #
#  Callbacks
# --------------------------------------------------------------------------- #
async def cb_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not is_admin(cq.from_user.id):
        await cq.answer("اجازه نداری.", show_alert=True)
        return
    await cq.answer()
    page = int(cq.data.split(":")[1])
    await send_listing(context, cq.message.chat.id, page)


async def cb_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cq = update.callback_query
    if not is_admin(cq.from_user.id):
        await cq.answer("اجازه نداری.", show_alert=True)
        return
    vid = int(cq.data.split(":")[1])
    rec = store.get(vid)
    if not rec:
        await cq.answer("این مورد پیدا نشد.", show_alert=True)
        return

    await cq.answer("به صف اضافه شد ✅")
    chat_id = cq.message.chat.id
    title = rec["title"]
    status = await context.bot.send_message(chat_id, f"⏳ در صف دانلود:\n🎬 {title}")

    async with download_lock:
        workdir = tempfile.mkdtemp(dir=config.DOWNLOAD_DIR)
        try:
            await status.edit_text(f"⬇️ در حال دانلود…\n🎬 {title}")
            try:
                meta = await asyncio.to_thread(
                    downloader.download, rec["url"], workdir, SEND_LIMIT
                )
            except downloader.TooLarge:
                # Don't fill the disk with a file we can't send — hand over the link.
                await status.edit_text(
                    f"⚠️ این ویدیو بزرگ‌تر از {config.SEND_LIMIT_MB}MB است و تلگرام "
                    "اجازه‌ی ارسالش را با توکن ساده نمی‌دهد.\n\n"
                    f"🔗 لینک صفحه برای دانلود مستقیم:\n{rec['url']}\n\n"
                    "برای ارسال فایل‌های تا ۲ گیگ داخل بات، نسخه‌ی bot_2gb.py را اجرا کن "
                    "(نیاز به api_id/api_hash — راهنما در README)."
                )
                return

            await status.edit_text(f"⬆️ در حال آپلود در تلگرام…\n🎬 {title}")
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            await context.bot.send_video(
                chat_id,
                video=Path(meta["path"]),
                caption=f"🎬 {title}",
                duration=int(meta.get("duration") or 0),
                width=int(meta.get("width") or 0),
                height=int(meta.get("height") or 0),
                supports_streaming=True,
                read_timeout=120,
                write_timeout=600,
                connect_timeout=60,
                pool_timeout=120,
            )
            await status.delete()
        except Exception as e:  # noqa: BLE001  (report any failure to the user)
            log.exception("download/send failed")
            await status.edit_text(f"❌ خطا:\n{e}")
        finally:
            # delete the file no matter what -> nothing accumulates on disk
            shutil.rmtree(workdir, ignore_errors=True)


def _cleanup_tmp():
    for name in os.listdir(config.DOWNLOAD_DIR):
        shutil.rmtree(os.path.join(config.DOWNLOAD_DIR, name), ignore_errors=True)


def main():
    _cleanup_tmp()
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(cb_page, pattern=r"^pg:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^dl:\d+$"))
    log.info("Bot starting…")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
