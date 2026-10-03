"""
bot.py — ربات تلگرام برای دانلود مانهوا از sarrast.com (و سایت‌های مشابه Madara)

کنترل همه‌چیز داخل خود ربات است:
  • لینک سری یا یک قسمت را برای ربات بفرست  ->  ربات لیست قسمت‌ها را می‌دهد.
  • روی هر قسمت بزن       ->  تمام صفحه‌های آن قسمت فرستاده می‌شود.
  • «⏬ از این قسمت تا آخر»  ->  همه‌ی قسمت‌ها از این‌جا به بعد پشت‌سرهم.
  • /cancel برای توقف دانلود دسته‌ای.
  • /mode photo|document برای تغییر نحوه‌ی ارسال عکس‌ها.

اجرا:
  export BOT_TOKEN="123456:ABC..."      # توکن از @BotFather
  python bot.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from scraper import Scraper

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("manhwa-bot")

def _load_env(path: str) -> None:
    """بارگذاری سادهٔ فایل .env (بدون نیاز به python-dotenv)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass


_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
# فقط این آیدی‌ها اجازهٔ استفاده دارند؛ خالی = همه مجازند
ALLOWED_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}
PER_PAGE = 8                    # تعداد قسمت در هر صفحه‌ی کیبورد
GROUP_SIZE = 10                 # تعداد عکس در هر آلبوم تلگرام (حداکثر ۱۰)
SLEEP_BETWEEN_GROUPS = 1.0      # مکث بین آلبوم‌ها (ضد محدودیت تلگرام)
URL_RE = re.compile(r"https?://[^\s]+")

scraper = Scraper()


def authorized(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    u = update.effective_user
    return bool(u and u.id in ALLOWED_IDS)


# ----------------------------- UI helpers -----------------------------

def chapters_keyboard(chapters: list, page: int) -> InlineKeyboardMarkup:
    total = len(chapters)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton("⏬ دانلود همه از اولین قسمت", callback_data="tail:0")])

    start = page * PER_PAGE
    end = min(start + PER_PAGE, total)
    row: list[InlineKeyboardButton] = []
    for i in range(start, end):
        row.append(InlineKeyboardButton(chapters[i].label, callback_data=f"ch:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    pages = (total + PER_PAGE - 1) // PER_PAGE
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"pg:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"pg:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def after_chapter_keyboard(idx: int, total: int) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if idx + 1 < total:
        nav.append(InlineKeyboardButton("➡️ قسمت بعد", callback_data=f"ch:{idx + 1}"))
    nav.append(InlineKeyboardButton("📚 لیست قسمت‌ها", callback_data="pg:0"))
    rows.append(nav)
    if idx + 1 < total:
        rows.append([InlineKeyboardButton("⏬ از این‌جا تا آخر", callback_data=f"tail:{idx + 1}")])
    return InlineKeyboardMarkup(rows)


# ----------------------------- Telegram send helpers -----------------------------

async def _safe(coro_func, *args, **kwargs):
    """اجرای یک عملیات تلگرام با مدیریت RetryAfter / TimedOut."""
    for _ in range(5):
        try:
            return await coro_func(*args, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            await asyncio.sleep(3)
    return await coro_func(*args, **kwargs)


async def _send_one(context, chat_id, data: bytes, fn: str, mode: str):
    bio = io.BytesIO(data)
    bio.name = fn
    if mode == "photo":
        try:
            await _safe(context.bot.send_photo, chat_id, photo=InputFile(bio, filename=fn))
            return
        except BadRequest:
            bio.seek(0)
    await _safe(context.bot.send_document, chat_id, document=InputFile(bio, filename=fn))


async def send_images(context, chat_id, items: list[tuple[bytes, str]], mode: str):
    """ارسال عکس‌ها به‌صورت آلبوم (۱۰تایی) با fallback تک‌به‌تک."""
    i, n = 0, len(items)
    while i < n:
        batch = items[i : i + GROUP_SIZE]
        i += GROUP_SIZE
        if len(batch) == 1:
            await _send_one(context, chat_id, batch[0][0], batch[0][1], mode)
        else:
            media = []
            for data, fn in batch:
                bio = io.BytesIO(data)
                bio.name = fn
                if mode == "photo":
                    media.append(InputMediaPhoto(media=InputFile(bio, filename=fn)))
                else:
                    media.append(InputMediaDocument(media=InputFile(bio, filename=fn)))
            try:
                await _safe(context.bot.send_media_group, chat_id, media=media)
            except BadRequest:
                # اگر آلبوم رد شد، تک‌به‌تک بفرست
                for data, fn in batch:
                    await _send_one(context, chat_id, data, fn, mode)
        await asyncio.sleep(SLEEP_BETWEEN_GROUPS)


async def deliver_chapter(context, chat_id, chapter, mode: str) -> int:
    """دانلود و ارسال تمام صفحه‌های یک قسمت. تعداد صفحه‌های فرستاده‌شده را برمی‌گرداند."""
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
    img_urls = await asyncio.to_thread(scraper.get_images, chapter.url)
    if not img_urls:
        await context.bot.send_message(chat_id, f"⚠️ برای {chapter.label} صفحه‌ای پیدا نشد.")
        return 0

    items: list[tuple[bytes, str]] = []
    ext_from = lambda u: (re.search(r"\.(jpe?g|png|webp|gif)", u, re.I) or ["", "jpg"])[0].lstrip(".") or "jpg"
    for idx, u in enumerate(img_urls, 1):
        try:
            data, _ = await asyncio.to_thread(scraper.download_image, u, chapter.url)
            items.append((data, f"{int(chapter.num) if chapter.num==int(chapter.num) else chapter.num}_{idx:03d}.{ext_from(u)}"))
        except Exception as e:
            log.warning("دانلود عکس ناموفق %s: %s", u, e)

    if not items:
        await context.bot.send_message(chat_id, f"⚠️ دانلود صفحه‌های {chapter.label} ناموفق بود.")
        return 0

    await send_images(context, chat_id, items, mode)
    return len(items)


# ----------------------------- Handlers -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک سری یا یکی از قسمت‌ها رو برام بفرست تا لیست قسمت‌ها رو بدم.\n"
        "نمونه:\n"
        "`https://sarrast.com/series/free-porn-manhwa-sarrast/`\n\n"
        "دستورها:\n"
        "/mode photo|document — نحوهٔ ارسال عکس (پیش‌فرض photo)\n"
        "/cancel — توقف دانلود دسته‌ای",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("photo", "document"):
        cur = context.user_data.get("mode", "photo")
        await update.message.reply_text(f"حالت فعلی: {cur}\nاستفاده: /mode photo یا /mode document")
        return
    context.user_data["mode"] = arg
    await update.message.reply_text(f"✅ حالت ارسال روی «{arg}» تنظیم شد.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data["cancel"] = True
    await update.message.reply_text("⏹️ درخواست توقف ثبت شد؛ بعد از قسمت فعلی متوقف می‌شود.")


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("یک لینک معتبر بفرست.")
        return
    url = m.group(0)
    msg = await update.message.reply_text("⏳ در حال خواندن لیست قسمت‌ها...")
    try:
        chapters = await asyncio.to_thread(scraper.get_chapters, url)
    except Exception as e:
        await msg.edit_text(f"❌ خطا در خواندن سایت:\n{e}")
        return
    if not chapters:
        await msg.edit_text("❌ هیچ قسمتی پیدا نشد. ممکنه ساختار سایت فرق کنه یا لینک اشتباه باشه.")
        return

    context.user_data["chapters"] = chapters
    context.user_data["series"] = scraper.series_url(url)
    context.user_data["page"] = 0
    await msg.edit_text(
        f"✅ {len(chapters)} قسمت پیدا شد.\nیکی رو انتخاب کن 👇",
        reply_markup=chapters_keyboard(chapters, 0),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorized(update):
        await q.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    chapters = context.user_data.get("chapters")

    if data == "noop":
        return
    if not chapters:
        await q.edit_message_text("لیست منقضی شده. دوباره لینک رو بفرست.")
        return

    total = len(chapters)
    mode = context.user_data.get("mode", "photo")

    if data.startswith("pg:"):
        page = max(0, min(int(data[3:]), (total - 1) // PER_PAGE))
        context.user_data["page"] = page
        try:
            await q.edit_message_text(
                f"✅ {total} قسمت.\nیکی رو انتخاب کن 👇",
                reply_markup=chapters_keyboard(chapters, page),
            )
        except BadRequest:
            pass
        return

    if data.startswith("ch:"):
        idx = int(data[3:])
        if not (0 <= idx < total):
            return
        ch = chapters[idx]
        await context.bot.send_message(q.message.chat_id, f"📥 در حال ارسال {ch.label} ...")
        try:
            n = await deliver_chapter(context, q.message.chat_id, ch, mode)
            if n:
                await context.bot.send_message(
                    q.message.chat_id,
                    f"✅ {ch.label} ({n} صفحه) ارسال شد.",
                    reply_markup=after_chapter_keyboard(idx, total),
                )
        except Exception as e:
            log.exception("deliver_chapter failed")
            await context.bot.send_message(q.message.chat_id, f"❌ خطا: {e}")
        return

    if data.startswith("tail:"):
        start = int(data[5:])
        context.user_data["cancel"] = False
        # دانلود دسته‌ای در تسک جدا تا /cancel کار کند
        context.application.create_task(
            batch_download(context, q.message.chat_id, start, mode)
        )
        return


async def batch_download(context, chat_id, start: int, mode: str):
    chapters = context.user_data.get("chapters") or []
    total = len(chapters)
    await context.bot.send_message(
        chat_id, f"⏬ شروع دانلود از قسمت {start + 1} تا {total}... (برای توقف: /cancel)"
    )
    for idx in range(start, total):
        if context.user_data.get("cancel"):
            await context.bot.send_message(chat_id, "⏹️ متوقف شد.")
            return
        ch = chapters[idx]
        await context.bot.send_message(chat_id, f"— {ch.label} ({idx + 1}/{total})")
        try:
            await deliver_chapter(context, chat_id, ch, mode)
        except Exception as e:
            log.exception("batch item failed")
            await context.bot.send_message(chat_id, f"❌ قسمت {idx + 1} رد شد: {e}")
        await asyncio.sleep(1.5)
    await context.bot.send_message(chat_id, "🎉 تمام شد.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("متغیر محیطی BOT_TOKEN تنظیم نشده. مثال: export BOT_TOKEN='123:ABC'")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    log.info("ربات روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
