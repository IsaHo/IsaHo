"""
bot.py — ربات تلگرام دانلود مانهوا از sarrast.com

امکانات:
  • لینک سری/قسمت بفرست  ->  لیست قسمت‌ها با دکمه.
  • روی هر قسمت بزن       ->  همهٔ صفحه‌ها آفلاین فرستاده می‌شود.
  • «⏬ دانلود همه از اول» / «➡️ قسمت بعد» / «⏬ از این‌جا تا آخر».
  • پیشرفت خواندن ذخیره می‌شود: برای هر داستان یادش می‌ماند تا کدام قسمت رسیدی،
    بهت می‌گوید و دکمهٔ «ادامه» می‌دهد. لیست همه با /me یا دکمهٔ «📖 داستان‌های من».
  • /mode photo|document ، /cancel

اجرا:
  export BOT_TOKEN=...     (یا داخل فایل .env)
  python bot.py
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import threading
from datetime import datetime

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

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env(path: str) -> None:
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


_load_env(os.path.join(HERE, ".env"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ALLOWED_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}

PER_PAGE = 8
GROUP_SIZE = 10
SLEEP_BETWEEN_GROUPS = 1.0
SLEEP_BETWEEN_DOWNLOADS = 0.15
DATA_DIR = os.path.join(HERE, "data")
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")
URL_RE = re.compile(r"https?://[^\s]+")

scraper = Scraper()


def authorized(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    u = update.effective_user
    return bool(u and u.id in ALLOWED_IDS)


# ----------------------------- progress store -----------------------------

_plock = threading.Lock()


def _load_all() -> dict:
    try:
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_all(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PROGRESS_FILE)


def set_progress(uid, s_url, title, num, label, total) -> None:
    with _plock:
        d = _load_all()
        d.setdefault(str(uid), {})[s_url] = {
            "title": title,
            "num": num,
            "label": label,
            "total": total,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        _save_all(d)


def get_progress(uid, s_url):
    return _load_all().get(str(uid), {}).get(s_url)


def list_progress(uid):
    items = list(_load_all().get(str(uid), {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    return items  # [(s_url, info), ...]


# ----------------------------- keyboards -----------------------------

def chapters_keyboard(chapters, page, prog=None) -> InlineKeyboardMarkup:
    total = len(chapters)
    rows: list[list[InlineKeyboardButton]] = []
    if prog:
        rows.append([InlineKeyboardButton(
            f"➡️ ادامه (بعد از {prog['label']})", callback_data="resume")])
    rows.append([InlineKeyboardButton("⏬ دانلود همه از اول", callback_data="tail:0")])

    start, end = page * PER_PAGE, min(page * PER_PAGE + PER_PAGE, total)
    row: list[InlineKeyboardButton] = []
    for i in range(start, end):
        row.append(InlineKeyboardButton(chapters[i].label, callback_data=f"ch:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)

    pages = (total + PER_PAGE - 1) // PER_PAGE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"pg:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"pg:{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("📖 داستان‌های من", callback_data="me")])
    return InlineKeyboardMarkup(rows)


def after_chapter_keyboard(idx, total) -> InlineKeyboardMarkup:
    rows = []
    nav = []
    if idx + 1 < total:
        nav.append(InlineKeyboardButton("➡️ قسمت بعد", callback_data=f"ch:{idx+1}"))
    nav.append(InlineKeyboardButton("📚 لیست", callback_data="pg:0"))
    rows.append(nav)
    if idx + 1 < total:
        rows.append([InlineKeyboardButton("⏬ از این‌جا تا آخر", callback_data=f"tail:{idx+1}")])
    rows.append([InlineKeyboardButton("📖 داستان‌های من", callback_data="me")])
    return InlineKeyboardMarkup(rows)


# ----------------------------- sending -----------------------------

async def _safe(fn, *a, **kw):
    for _ in range(5):
        try:
            return await fn(*a, **kw)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            await asyncio.sleep(3)
    return await fn(*a, **kw)


async def _send_one(context, chat_id, data: bytes, fn: str, mode: str) -> bool:
    try:
        if mode == "photo":
            try:
                await _safe(context.bot.send_photo, chat_id,
                            photo=InputFile(io.BytesIO(data), filename=fn))
                return True
            except BadRequest:
                pass
        await _safe(context.bot.send_document, chat_id,
                    document=InputFile(io.BytesIO(data), filename=fn))
        return True
    except Exception as e:
        log.warning("ارسال ناموفق %s: %s", fn, e)
        return False


async def send_images(context, chat_id, items: list[tuple[bytes, str]], mode: str) -> int:
    sent, i, n = 0, 0, len(items)
    while i < n:
        batch = items[i:i + GROUP_SIZE]
        i += GROUP_SIZE
        ok = False
        if len(batch) >= 2:
            try:
                media = []
                for data, fn in batch:
                    f = InputFile(io.BytesIO(data), filename=fn)
                    media.append(InputMediaPhoto(media=f) if mode == "photo"
                                 else InputMediaDocument(media=f))
                await _safe(context.bot.send_media_group, chat_id, media=media)
                ok, sent = True, sent + len(batch)
            except Exception as e:
                log.warning("آلبوم ناموفق، تک‌به‌تک می‌فرستم: %s", e)
        if not ok:
            for data, fn in batch:
                if await _send_one(context, chat_id, data, fn, mode):
                    sent += 1
                await asyncio.sleep(0.3)
        await asyncio.sleep(SLEEP_BETWEEN_GROUPS)
    return sent


async def deliver_chapter(context, chat_id, chapter, mode: str) -> int:
    await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
    img_urls = await asyncio.to_thread(scraper.get_images, chapter.url)
    if not img_urls:
        await context.bot.send_message(chat_id, f"⚠️ برای {chapter.label} صفحه‌ای پیدا نشد.")
        return 0

    def ext_of(u):
        m = re.search(r"\.(jpe?g|png|webp|gif)(?:$|\?)", u, re.I)
        return m.group(1).lower() if m else "jpg"

    n_int = int(chapter.num) if chapter.num == int(chapter.num) else chapter.num
    items: list[tuple[bytes, str]] = []
    for idx, u in enumerate(img_urls, 1):
        try:
            data, _ = await asyncio.to_thread(scraper.download_image, u, chapter.url)
            items.append((data, f"{n_int}_{idx:03d}.{ext_of(u)}"))
        except Exception as e:
            log.warning("دانلود ناموفق %s: %s", u, e)
        await asyncio.sleep(SLEEP_BETWEEN_DOWNLOADS)

    if not items:
        await context.bot.send_message(chat_id, f"⚠️ دانلود صفحه‌های {chapter.label} ناموفق بود.")
        return 0
    return await send_images(context, chat_id, items, mode)


async def send_chapter_idx(context, chat_id, ud, idx, mode) -> int:
    chapters = ud["chapters"]
    ch = chapters[idx]
    await context.bot.send_message(chat_id, f"📥 در حال ارسال {ch.label} ...")
    n = await deliver_chapter(context, chat_id, ch, mode)
    if n:
        set_progress(ud["uid"], ud["series_url"], ud["title"], ch.num, ch.label, len(chapters))
        await context.bot.send_message(
            chat_id, f"✅ {ch.label} ({n} صفحه) ارسال شد.",
            reply_markup=after_chapter_keyboard(idx, len(chapters)),
        )
    return n


# ----------------------------- rendering -----------------------------

async def show_series(context, chat_id, ud, edit_msg=None):
    chapters = ud["chapters"]
    prog = get_progress(ud["uid"], ud["series_url"])
    text = f"✅ «{ud['title']}»\n{len(chapters)} قسمت. یکی رو انتخاب کن 👇"
    if prog:
        text += f"\n📖 آخرین‌بار تا {prog['label']} خوندی."
    kb = chapters_keyboard(chapters, ud.get("page", 0), prog)
    if edit_msg:
        try:
            await edit_msg.edit_text(text, reply_markup=kb)
            return
        except BadRequest:
            pass
    await context.bot.send_message(chat_id, text, reply_markup=kb)


async def show_my_series(context, chat_id, ud, edit_msg=None):
    items = list_progress(ud["uid"])
    if not items:
        txt = "📖 هنوز هیچ داستانی نخوندی. یه لینک بفرست تا شروع کنیم."
        if edit_msg:
            await edit_msg.edit_text(txt)
        else:
            await context.bot.send_message(chat_id, txt)
        return
    ud["my_series"] = [u for u, _ in items]
    rows = [[InlineKeyboardButton(f"{info['title']} — {info['label']}", callback_data=f"open:{i}")]
            for i, (_, info) in enumerate(items)]
    txt = "📖 داستان‌های تو (آخرین قسمتی که خوندی):"
    kb = InlineKeyboardMarkup(rows)
    if edit_msg:
        try:
            await edit_msg.edit_text(txt, reply_markup=kb)
            return
        except BadRequest:
            pass
    await context.bot.send_message(chat_id, txt, reply_markup=kb)


# ----------------------------- handlers -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک سری یا یکی از قسمت‌ها رو بفرست تا لیست قسمت‌ها رو بدم.\n"
        "نمونه: `https://sarrast.com/series/secret-class`\n\n"
        "ربات یادش می‌مونه هر داستان رو تا کجا خوندی.\n\n"
        "دستورها:\n"
        "/me — داستان‌هایی که خوندی + ادامه\n"
        "/mode photo|document — نحوهٔ ارسال عکس\n"
        "/cancel — توقف دانلود دسته‌ای",
        parse_mode="Markdown", disable_web_page_preview=True,
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


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data["uid"] = update.effective_user.id
    await show_my_series(context, update.effective_chat.id, context.user_data)


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("یک لینک معتبر بفرست.")
        return
    msg = await update.message.reply_text("⏳ در حال خواندن لیست قسمت‌ها...")
    try:
        series = await asyncio.to_thread(scraper.get_series, m.group(0))
    except Exception as e:
        await msg.edit_text(f"❌ خطا در خواندن سایت:\n{e}")
        return
    if not series.chapters:
        await msg.edit_text("❌ هیچ قسمتی پیدا نشد. لینک یا ساختار سایت رو چک کن.")
        return
    context.user_data.update({
        "chapters": series.chapters,
        "series_url": series.url,
        "title": series.title,
        "page": 0,
        "uid": update.effective_user.id,
    })
    await show_series(context, update.effective_chat.id, context.user_data, edit_msg=msg)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorized(update):
        await q.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    ud = context.user_data
    ud["uid"] = update.effective_user.id
    chat_id = q.message.chat_id
    mode = ud.get("mode", "photo")

    if data == "noop":
        return

    if data == "me":
        await show_my_series(context, chat_id, ud, edit_msg=q.message)
        return

    if data.startswith("open:"):
        i = int(data[5:])
        urls = ud.get("my_series") or []
        if not (0 <= i < len(urls)):
            return
        await q.message.edit_text("⏳ در حال باز کردن...")
        try:
            series = await asyncio.to_thread(scraper.get_series, urls[i])
        except Exception as e:
            await q.message.edit_text(f"❌ خطا: {e}")
            return
        ud.update({"chapters": series.chapters, "series_url": series.url,
                   "title": series.title, "page": 0})
        await show_series(context, chat_id, ud, edit_msg=q.message)
        return

    chapters = ud.get("chapters")
    if not chapters:
        await q.message.edit_text("لیست منقضی شده. دوباره لینک رو بفرست یا /me بزن.")
        return
    total = len(chapters)

    if data.startswith("pg:"):
        ud["page"] = max(0, min(int(data[3:]), (total - 1) // PER_PAGE))
        await show_series(context, chat_id, ud, edit_msg=q.message)
        return

    if data == "resume":
        prog = get_progress(ud["uid"], ud["series_url"])
        nxt = None
        if prog:
            nxt = next((i for i, c in enumerate(chapters) if c.num > prog["num"]), None)
        if nxt is None:
            await context.bot.send_message(chat_id, "🎉 به آخرین قسمت رسیدی!")
            return
        await send_chapter_idx(context, chat_id, ud, nxt, mode)
        return

    if data.startswith("ch:"):
        idx = int(data[3:])
        if 0 <= idx < total:
            try:
                await send_chapter_idx(context, chat_id, ud, idx, mode)
            except Exception as e:
                log.exception("ch failed")
                await context.bot.send_message(chat_id, f"❌ خطا: {e}")
        return

    if data.startswith("tail:"):
        start = int(data[5:])
        ud["cancel"] = False
        snapshot = dict(ud)  # کپی برای تسک پس‌زمینه
        context.application.create_task(batch_download(context, chat_id, snapshot, start, mode))
        return


async def batch_download(context, chat_id, ud, start, mode):
    chapters = ud["chapters"]
    total = len(chapters)
    await context.bot.send_message(
        chat_id, f"⏬ دانلود از {chapters[start].label} تا آخر ({total - start} قسمت)... /cancel برای توقف")
    for idx in range(start, total):
        if context.user_data.get("cancel"):
            await context.bot.send_message(chat_id, "⏹️ متوقف شد.")
            return
        ch = chapters[idx]
        await context.bot.send_message(chat_id, f"— {ch.label} ({idx+1}/{total})")
        try:
            n = await deliver_chapter(context, chat_id, ch, mode)
            if n:
                set_progress(ud["uid"], ud["series_url"], ud["title"], ch.num, ch.label, total)
        except Exception as e:
            log.exception("batch item failed")
            await context.bot.send_message(chat_id, f"❌ {ch.label} رد شد: {e}")
        await asyncio.sleep(1.5)
    await context.bot.send_message(chat_id, "🎉 تمام شد.")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN تنظیم نشده (در .env یا متغیر محیطی).")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))
    log.info("ربات روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
