"""
bot.py — ربات تلگرام دانلود مانهوا از sarrast.com

کنترل با کیبورد ثابتِ پایین صفحه (همیشه در دسترس، شناور نیست):
  ➡️ ادامه            = قسمت بعدی داستان فعلی (از جایی که موندی)
  ⏬ همه از اول        = دانلود کل قسمت‌ها از اول
  ⏹ توقف              = توقف دانلود دسته‌ای
  📖 داستان‌های من     = لیست داستان‌ها + ادامه
  🖼 حالت: عکس/فایل    = تغییر کیفیت (فایل = اندازهٔ اصلی، قابل بزرگ‌نمایی)

انتخاب قسمت مشخص: کافیست شمارهٔ قسمت را بفرستی (مثلاً 34).
لینک سری/قسمت را هم که بفرستی، لیست و اطلاعات داستان می‌آید.

اجرا:  python bot.py   (توکن از .env)
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
    InputFile,
    InputMediaDocument,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.ext import (
    Application,
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

GROUP_SIZE = 10
SLEEP_BETWEEN_GROUPS = 1.0
SLEEP_BETWEEN_DOWNLOADS = 0.15
DATA_DIR = os.path.join(HERE, "data")
PROGRESS_FILE = os.path.join(DATA_DIR, "progress.json")
URL_RE = re.compile(r"https?://[^\s]+")

# دکمه‌های کیبورد پایین
BTN_RESUME = "➡️ ادامه"
BTN_ALL = "⏬ همه از اول"
BTN_STOP = "⏹ توقف"
BTN_MINE = "📖 داستان‌های من"
BTN_MODE_PHOTO = "🖼 حالت: عکس"
BTN_MODE_DOC = "🖼 حالت: فایل"
BTN_BACK = "⬅️ بازگشت"

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
            "title": title, "num": num, "label": label, "total": total,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        _save_all(d)


def get_progress(uid, s_url):
    return _load_all().get(str(uid), {}).get(s_url)


def list_progress(uid):
    items = list(_load_all().get(str(uid), {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    return items


# ----------------------------- keyboards -----------------------------

def main_kb(mode: str) -> ReplyKeyboardMarkup:
    mode_btn = BTN_MODE_PHOTO if mode == "photo" else BTN_MODE_DOC
    return ReplyKeyboardMarkup(
        [[BTN_RESUME],
         [BTN_ALL, BTN_STOP],
         [BTN_MINE, mode_btn]],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="شمارهٔ قسمت یا لینک رو بفرست",
    )


def mine_kb(titles: list[str]) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(t)] for t in titles] + [[KeyboardButton(BTN_BACK)]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


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


async def send_images(context, chat_id, items, mode: str) -> int:
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
                log.warning("آلبوم ناموفق، تک‌به‌تک: %s", e)
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
    items = []
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
        ud["last_idx"] = idx
        set_progress(ud["uid"], ud["series_url"], ud["title"], ch.num, ch.label, len(chapters))
        nxt = "برای بعدی ➡️ ادامه بزن یا شمارهٔ قسمت رو بفرست." if idx + 1 < len(chapters) else "🎉 این آخرین قسمت بود."
        await context.bot.send_message(
            chat_id, f"✅ {ch.label} ({n} صفحه) ارسال شد.\n{nxt}",
            reply_markup=main_kb(mode))
    return n


# ----------------------------- helpers -----------------------------

async def open_series(context, chat_id, ud, any_url, status_msg=None):
    try:
        series = await asyncio.to_thread(scraper.get_series, any_url)
    except Exception as e:
        txt = f"❌ خطا در خواندن سایت:\n{e}"
        if status_msg:
            await status_msg.edit_text(txt)
        else:
            await context.bot.send_message(chat_id, txt)
        return False
    if not series.chapters:
        txt = "❌ هیچ قسمتی پیدا نشد. لینک یا ساختار سایت رو چک کن."
        if status_msg:
            await status_msg.edit_text(txt)
        else:
            await context.bot.send_message(chat_id, txt)
        return False

    ud.update({"chapters": series.chapters, "series_url": series.url,
               "title": series.title, "await_pick": False})
    prog = get_progress(ud["uid"], series.url)
    mode = ud.get("mode", "photo")
    first, last = series.chapters[0].label, series.chapters[-1].label
    text = (f"✅ «{series.title}»\n"
            f"{len(series.chapters)} قسمت ({first} تا {last}).\n"
            f"👈 شمارهٔ قسمت رو بفرست (مثلاً {int(series.chapters[0].num)}).")
    if prog:
        text += f"\n📖 آخرین‌بار تا {prog['label']} خوندی — «➡️ ادامه» بزن."
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass
    await context.bot.send_message(chat_id, text, reply_markup=main_kb(mode))
    return True


async def do_resume(context, chat_id, ud, mode):
    if not ud.get("chapters"):
        await context.bot.send_message(chat_id, "اول یه لینک داستان بفرست یا 📖 داستان‌های من رو بزن.",
                                        reply_markup=main_kb(mode))
        return
    prog = get_progress(ud["uid"], ud["series_url"])
    chapters = ud["chapters"]
    if prog:
        nxt = next((i for i, c in enumerate(chapters) if c.num > prog["num"]), None)
    else:
        nxt = 0  # هنوز چیزی نخونده؛ از اول
    if nxt is None:
        await context.bot.send_message(chat_id, "🎉 به آخرین قسمت رسیدی!", reply_markup=main_kb(mode))
        return
    await send_chapter_idx(context, chat_id, ud, nxt, mode)


async def show_mine(context, chat_id, ud):
    items = list_progress(ud["uid"])
    mode = ud.get("mode", "photo")
    if not items:
        await context.bot.send_message(chat_id, "📖 هنوز داستانی نخوندی. یه لینک بفرست.",
                                        reply_markup=main_kb(mode))
        return
    ud["pick_map"] = {}
    titles = []
    for u, info in items:
        label = f"{info['title']} — {info['label']}"
        ud["pick_map"][label] = u
        titles.append(label)
    ud["await_pick"] = True
    await context.bot.send_message(
        chat_id, "📖 یکی رو انتخاب کن تا ادامه بدی:", reply_markup=mine_kb(titles))


# ----------------------------- handlers -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    context.user_data["uid"] = update.effective_user.id
    mode = context.user_data.get("mode", "photo")
    await update.message.reply_text(
        "سلام! 👋\n\n"
        "لینک سری یا یکی از قسمت‌ها رو بفرست.\n"
        "نمونه: https://sarrast.com/series/secret-class\n\n"
        "• برای یه قسمت مشخص: شمارهٔ قسمت رو بفرست (مثلاً 34).\n"
        "• دکمه‌های پایین صفحه همیشه در دسترسن.\n"
        "• «🖼 حالت: فایل» عکس رو با اندازهٔ اصلی و قابل بزرگ‌نمایی می‌فرسته.\n"
        "• ربات یادش می‌مونه هر داستان رو تا کجا خوندی.",
        reply_markup=main_kb(mode), disable_web_page_preview=True,
    )


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("photo", "document"):
        context.user_data["mode"] = arg
    mode = context.user_data.get("mode", "photo")
    await update.message.reply_text(f"حالت ارسال: {mode}", reply_markup=main_kb(mode))


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data["cancel"] = True
    await update.message.reply_text("⏹️ توقف ثبت شد.", reply_markup=main_kb(context.user_data.get("mode", "photo")))


async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    context.user_data["uid"] = update.effective_user.id
    await show_mine(context, update.effective_chat.id, context.user_data)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    ud = context.user_data
    ud["uid"] = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    mode = ud.get("mode", "photo")

    # 1) لینک
    m = URL_RE.search(text)
    if m:
        msg = await update.message.reply_text("⏳ در حال خواندن...")
        await open_series(context, chat_id, ud, m.group(0), status_msg=msg)
        return

    # 2) دکمه‌های کیبورد
    if text == BTN_ALL:
        if not ud.get("chapters"):
            await update.message.reply_text("اول یه لینک بفرست.", reply_markup=main_kb(mode))
            return
        ud["cancel"] = False
        context.application.create_task(batch_download(context, chat_id, dict(ud), 0, mode))
        return
    if text == BTN_STOP:
        ud["cancel"] = True
        await update.message.reply_text("⏹️ توقف ثبت شد.", reply_markup=main_kb(mode))
        return
    if text == BTN_RESUME:
        await do_resume(context, chat_id, ud, mode)
        return
    if text == BTN_MINE:
        await show_mine(context, chat_id, ud)
        return
    if text == BTN_BACK:
        ud["await_pick"] = False
        await update.message.reply_text("باشه.", reply_markup=main_kb(mode))
        return
    if text.startswith("🖼 حالت"):
        ud["mode"] = "document" if mode == "photo" else "photo"
        mode = ud["mode"]
        human = "فایل (اندازهٔ اصلی، قابل بزرگ‌نمایی)" if mode == "document" else "عکس (فشرده، سبک‌تر)"
        await update.message.reply_text(f"✅ حالت: {human}", reply_markup=main_kb(mode))
        return

    # 3) انتخاب داستان از لیست «داستان‌های من»
    if ud.get("await_pick") and text in (ud.get("pick_map") or {}):
        await update.message.reply_text("⏳ در حال باز کردن...")
        await open_series(context, chat_id, ud, ud["pick_map"][text])
        return

    # 4) شمارهٔ قسمت
    mnum = re.fullmatch(r"(?:قسمت\s*)?(\d+(?:\.\d+)?)", text)
    if mnum:
        if not ud.get("chapters"):
            await update.message.reply_text("اول یه لینک داستان بفرست.", reply_markup=main_kb(mode))
            return
        want = float(mnum.group(1))
        idx = next((i for i, c in enumerate(ud["chapters"]) if c.num == want), None)
        if idx is None:
            last = ud["chapters"][-1].num
            await update.message.reply_text(
                f"قسمت {int(want) if want==int(want) else want} پیدا نشد (۱ تا {int(last)}).",
                reply_markup=main_kb(mode))
            return
        await send_chapter_idx(context, chat_id, ud, idx, mode)
        return

    await update.message.reply_text(
        "متوجه نشدم. یه لینک بفرست، یا شمارهٔ قسمت رو بزن، یا از دکمه‌های پایین استفاده کن.",
        reply_markup=main_kb(mode))


async def batch_download(context, chat_id, ud, start, mode):
    chapters = ud["chapters"]
    total = len(chapters)
    await context.bot.send_message(
        chat_id, f"⏬ دانلود از {chapters[start].label} تا آخر ({total - start} قسمت)... «⏹ توقف» برای ایست.")
    for idx in range(start, total):
        if context.user_data.get("cancel"):
            await context.bot.send_message(chat_id, "⏹️ متوقف شد.", reply_markup=main_kb(mode))
            return
        ch = chapters[idx]
        await context.bot.send_message(chat_id, f"— {ch.label} ({idx+1}/{total})")
        try:
            n = await deliver_chapter(context, chat_id, ch, mode)
            if n:
                context.user_data["last_idx"] = idx
                set_progress(ud["uid"], ud["series_url"], ud["title"], ch.num, ch.label, total)
        except Exception as e:
            log.exception("batch item failed")
            await context.bot.send_message(chat_id, f"❌ {ch.label} رد شد: {e}")
        await asyncio.sleep(1.5)
    await context.bot.send_message(chat_id, "🎉 تمام شد.", reply_markup=main_kb(mode))


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN تنظیم نشده (در .env یا متغیر محیطی).")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("ربات روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
