"""Telegram bot: scan soolakhi.com, list videos with thumbnails, download on
demand and forward into Telegram — deleting each file from the server right
after it is sent.

Run:  python bot.py
"""
import asyncio
import logging
import os
import shutil
import tempfile
import time

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import downloader
import scraper
import store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("soolakhi-bot")

config.validate()

app = Client(
    "soolakhi_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.SESSION_DIR,
)

# Only one download/upload at a time -> at most one file on disk, bounded CPU.
download_lock = asyncio.Lock()
_last_edit = {}


def is_admin(user_id):
    return (not config.ADMIN_IDS) or (user_id in config.ADMIN_IDS)


async def safe(coro_func, *args, **kwargs):
    """Call a Pyrogram method, retrying once on FloodWait.

    Takes the method and its args (not an already-created coroutine) so it can
    be retried — a coroutine object can only be awaited once.
    """
    try:
        return await coro_func(*args, **kwargs)
    except FloodWait as e:
        await asyncio.sleep(int(e.value) + 1)
        return await coro_func(*args, **kwargs)


# --------------------------------------------------------------------------- #
#  Commands
# --------------------------------------------------------------------------- #
@app.on_message(filters.command("start") | filters.command("help"))
async def cmd_start(client, message):
    if not is_admin(message.from_user.id):
        await message.reply_text("⛔️ شما اجازه‌ی استفاده از این بات را ندارید.")
        return
    await message.reply_text(
        "سلام 👋\n\n"
        "این بات ویدیوهای سایت را اسکن می‌کند و با عکس و عنوان اینجا نشان می‌دهد. "
        "روی هر کدام که دکمه‌ی «⬇️ دانلود» را بزنی، همان لحظه دانلود و برایت ارسال می‌شود "
        "و بعد از ارسال از روی سرور پاک می‌شود.\n\n"
        "دستورها:\n"
        "• /scan — اسکن صفحه‌ی اول\n"
        "• /scan 2 — اسکن صفحه‌ی شماره ۲ (و همینطور بقیه صفحه‌ها)\n"
        "• /help — همین راهنما"
    )


@app.on_message(filters.command("scan"))
async def cmd_scan(client, message):
    if not is_admin(message.from_user.id):
        await message.reply_text("⛔️ اجازه نداری.")
        return
    page = 1
    if len(message.command) > 1 and message.command[1].isdigit():
        page = int(message.command[1])
    await send_listing(client, message.chat.id, page)


# --------------------------------------------------------------------------- #
#  Listing
# --------------------------------------------------------------------------- #
async def send_listing(client, chat_id, page):
    status = await safe(client.send_message, chat_id, f"🔎 در حال اسکن صفحه‌ی {page} …")
    loop = asyncio.get_event_loop()
    try:
        items = await loop.run_in_executor(None, scraper.fetch_listing, page)
    except Exception as e:  # noqa: BLE001  (surface any scrape error to the user)
        await safe(status.edit_text, f"❌ خطا در اسکن سایت:\n{e}")
        return

    if not items:
        await safe(
            status.edit_text,
            "چیزی پیدا نشد 🤔\n"
            "احتمالاً سلکتورهای سایت فرق دارند. روی سرور این را اجرا کن تا تنظیمشان کنی:\n"
            "`python scraper.py --page 1`",
        )
        return

    items = items[: config.ITEMS_PER_SCAN]
    await safe(status.edit_text, f"📄 صفحه‌ی {page} — {len(items)} ویدیو:")

    for it in items:
        vid = store.add(it["url"], it["title"], it["thumb"])
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬇️ دانلود", callback_data=f"dl:{vid}")]]
        )
        caption = f"🎬 {it['title']}"
        sent = False
        if it["thumb"]:
            try:
                await safe(
                    client.send_photo, chat_id, it["thumb"], caption=caption, reply_markup=kb
                )
                sent = True
            except Exception:  # noqa: BLE001  (bad/blocked thumbnail -> fall back to text)
                sent = False
        if not sent:
            await safe(client.send_message, chat_id, caption, reply_markup=kb)
        await asyncio.sleep(0.4)  # be gentle with Telegram rate limits

    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("➡️ صفحه‌ی بعد", callback_data=f"pg:{page + 1}")]]
    )
    await safe(client.send_message, chat_id, "برای ادامه:", reply_markup=kb)


# --------------------------------------------------------------------------- #
#  Callbacks
# --------------------------------------------------------------------------- #
@app.on_callback_query(filters.regex(r"^pg:(\d+)$"))
async def cb_page(client, cq):
    if not is_admin(cq.from_user.id):
        await cq.answer("اجازه نداری.", show_alert=True)
        return
    await cq.answer()
    page = int(cq.matches[0].group(1))
    await send_listing(client, cq.message.chat.id, page)


async def _upload_progress(current, total, status, title):
    now = time.time()
    key = id(status)
    if not total:
        return
    if now - _last_edit.get(key, 0) >= 4 or current >= total:
        _last_edit[key] = now
        pct = int(current * 100 / total)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        try:
            await status.edit_text(f"⬆️ آپلود در تلگرام {pct}%\n{bar}\n🎬 {title}")
        except Exception:  # noqa: BLE001  (ignore 'message not modified' etc.)
            pass


@app.on_callback_query(filters.regex(r"^dl:(\d+)$"))
async def cb_download(client, cq):
    if not is_admin(cq.from_user.id):
        await cq.answer("اجازه نداری.", show_alert=True)
        return
    vid = int(cq.matches[0].group(1))
    rec = store.get(vid)
    if not rec:
        await cq.answer("این مورد پیدا نشد.", show_alert=True)
        return

    await cq.answer("به صف اضافه شد ✅")
    chat_id = cq.message.chat.id
    title = rec["title"]
    status = await safe(client.send_message, chat_id, f"⏳ در صف دانلود:\n🎬 {title}")

    async with download_lock:
        workdir = tempfile.mkdtemp(dir=config.DOWNLOAD_DIR)
        loop = asyncio.get_event_loop()
        try:
            await safe(status.edit_text, f"⬇️ در حال دانلود از سایت…\n🎬 {title}")
            meta = await loop.run_in_executor(
                None, downloader.download, rec["url"], workdir
            )
            await safe(status.edit_text, f"⬆️ در حال آپلود در تلگرام…\n🎬 {title}")
            await client.send_video(
                chat_id,
                video=meta["path"],
                caption=f"🎬 {title}",
                duration=int(meta.get("duration") or 0),
                width=int(meta.get("width") or 0),
                height=int(meta.get("height") or 0),
                supports_streaming=True,
                progress=_upload_progress,
                progress_args=(status, title),
            )
            await safe(status.delete)
        except downloader.TooLarge:
            await safe(
                status.edit_text,
                f"⚠️ این فایل از حد مجاز ({config.MAX_FILESIZE_MB}MB) بزرگ‌تر است.\n"
                "می‌توانی در فایل .env مقدار YTDLP_FORMAT را به کیفیت پایین‌تر تغییر دهی، مثلاً:\n"
                "`best[height<=720][ext=mp4]/best[height<=720]/best`",
            )
        except Exception as e:  # noqa: BLE001  (report any failure to the user)
            log.exception("download/send failed")
            await safe(status.edit_text, f"❌ خطا:\n{e}")
        finally:
            # delete the file no matter what -> nothing accumulates on disk
            shutil.rmtree(workdir, ignore_errors=True)


def _cleanup_tmp():
    for name in os.listdir(config.DOWNLOAD_DIR):
        shutil.rmtree(os.path.join(config.DOWNLOAD_DIR, name), ignore_errors=True)


if __name__ == "__main__":
    _cleanup_tmp()
    log.info("Bot starting…")
    app.run()
