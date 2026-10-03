"""
bot.py — ربات لینک‌دهی و اطلاع‌رسانی بروزرسانی sarrast.com

این ربات دانلود نمی‌کند (نه عکس، نه PDF) تا رم اشغال نشود. فقط:
  • کل سایت را می‌خواند و کش می‌کند.
  • دوره‌ای چک می‌کند و داستان‌های جدید / قسمت‌های جدید را خبر می‌دهد.
  • برای هر داستان/قسمت، لینک تمیز (بدون ouo.io) می‌فرستد.

کیبورد پایین:
  🔎 جستجو            = اسم داستان رو بفرست تا سرچ کنم
  📚 همهٔ داستان‌ها     = مرور کل فهرست سایت
  🔗 لینک همهٔ قسمت‌ها  = همهٔ لینک‌های داستانِ بازشده
  🆕 بروزرسانی‌ها       = چک کردن تغییرات سایت همین حالا
  📖 دنبال‌شده‌ها       = داستان‌هایی که باز کردی + ادامه

اجرا:  python bot.py   (توکن از .env)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
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

DATA_DIR = os.path.join(HERE, "data")
CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")
FOLLOWS_FILE = os.path.join(DATA_DIR, "follows.json")
CATALOG_TTL = 6 * 3600
CATALOG_VERSION = 3               # با تغییر روش استخراج اسم، کش قدیمی باطل می‌شود
UPDATE_INTERVAL = 2 * 3600        # هر چند ثانیه سایت برای بروزرسانی چک شود
BROWSE_PER = 16
LINKS_PER_MSG = 40
URL_RE = re.compile(r"https?://[^\s]+")

BTN_SEARCH = "🔎 جستجو"
BTN_ALLSTORIES = "📚 همهٔ داستان‌ها"
BTN_ALLLINKS = "🔗 لینک همهٔ قسمت‌ها"
BTN_UPDATES = "🆕 بروزرسانی‌ها"
BTN_FOLLOWS = "📖 دنبال‌شده‌ها"
BTN_NEXT = "صفحهٔ بعد ▶️"
BTN_PREV = "◀️ صفحهٔ قبل"
BTN_BACK = "⬅️ بازگشت"

scraper = Scraper()
_lock = threading.Lock()


def authorized(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    u = update.effective_user
    return bool(u and u.id in ALLOWED_IDS)


# ----------------------------- json stores -----------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# catalog: {"ts":..., "v":..., "items":[{slug,title,url}, ...]}
def load_catalog_items():
    d = _read(CATALOG_FILE) or {}
    if d.get("v") != CATALOG_VERSION:
        return []
    return d.get("items") or []


def catalog_fresh():
    d = _read(CATALOG_FILE) or {}
    return (d.get("v") == CATALOG_VERSION and bool(d.get("items"))
            and time.time() - d.get("ts", 0) < CATALOG_TTL)


def save_catalog(items):
    with _lock:
        _write(CATALOG_FILE, {"ts": time.time(), "v": CATALOG_VERSION, "items": items})


# follows: {uid: {series_url: {title, count, last_num, ts}}}
def load_follows():
    return _read(FOLLOWS_FILE) or {}


def save_follows(d):
    with _lock:
        _write(FOLLOWS_FILE, d)


def follow_story(uid, url, title, count, last_num=None):
    with _lock:
        d = _read(FOLLOWS_FILE) or {}
        u = d.setdefault(str(uid), {})
        cur = u.get(url, {})
        u[url] = {
            "title": title,
            "count": count,
            "last_num": last_num if last_num is not None else cur.get("last_num"),
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        _write(FOLLOWS_FILE, d)


def list_follows(uid):
    items = list((_read(FOLLOWS_FILE) or {}).get(str(uid), {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    return items


# ----------------------------- catalog / search -----------------------------

async def ensure_catalog(context, chat_id):
    if catalog_fresh():
        return load_catalog_items()
    msg = await context.bot.send_message(
        chat_id, "⏳ در حال گرفتن فهرست همهٔ داستان‌های سایت... (یک‌بار، کمی طول می‌کشه)")
    items = await asyncio.to_thread(scraper.get_catalog)
    if items:
        save_catalog(items)
    try:
        await msg.delete()
    except Exception:
        pass
    return items or load_catalog_items()


def search_catalog(items, q):
    q = q.strip().lower()
    starts, contains = [], []
    for it in items:
        t = it["title"].lower()
        if t.startswith(q) or it["slug"].lower().startswith(q):
            starts.append(it)
        elif q in t or q in it["slug"].lower():
            contains.append(it)
    return starts + contains


# ----------------------------- keyboards -----------------------------

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BTN_SEARCH, BTN_ALLSTORIES],
         [BTN_ALLLINKS],
         [BTN_UPDATES, BTN_FOLLOWS]],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="اسم داستان، شمارهٔ قسمت، یا لینک رو بفرست",
    )


def _uniq(base, used, slug=""):
    label = (base or slug)[:55]
    while label in used:
        label += " ·"
    return label


async def show_picker(context, chat_id, ud, items, header):
    ud["pick_map"] = {}
    rows = []
    for it in items[:60]:
        label = _uniq(it["title"], ud["pick_map"], it["slug"])
        ud["pick_map"][label] = it["url"]
        rows.append([KeyboardButton(label)])
    rows.append([KeyboardButton(BTN_BACK)])
    ud["await_pick"] = True
    await context.bot.send_message(
        chat_id, header, reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True))


async def show_browse(context, chat_id, ud):
    items = ud.get("browse") or []
    total = len(items)
    pages = max(1, (total + BROWSE_PER - 1) // BROWSE_PER)
    pg = max(0, min(ud.get("bpage", 0), pages - 1))
    ud["bpage"] = pg
    chunk = items[pg * BROWSE_PER:(pg + 1) * BROWSE_PER]
    ud["pick_map"] = {}
    rows = []
    for it in chunk:
        label = _uniq(it["title"], ud["pick_map"], it["slug"])
        ud["pick_map"][label] = it["url"]
        rows.append([KeyboardButton(label)])
    nav = []
    if pg > 0:
        nav.append(KeyboardButton(BTN_PREV))
    nav.append(KeyboardButton(f"{pg+1}/{pages}"))
    if pg < pages - 1:
        nav.append(KeyboardButton(BTN_NEXT))
    rows.append(nav)
    rows.append([KeyboardButton(BTN_BACK)])
    ud["await_pick"] = True
    await context.bot.send_message(
        chat_id, f"📚 همهٔ داستان‌ها ({total}) — صفحهٔ {pg+1}/{pages}\nیکی رو بزن، یا اسم داستان رو سرچ کن.",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True))


async def show_follows(context, chat_id, ud):
    items = list_follows(ud["uid"])
    if not items:
        await context.bot.send_message(chat_id, "📖 هنوز داستانی باز نکردی. یه اسم سرچ کن یا 📚 همهٔ داستان‌ها رو بزن.",
                                        reply_markup=main_kb())
        return
    ud["pick_map"] = {}
    rows = []
    for url, info in items:
        last = f" (تا قسمت {info['last_num']})" if info.get("last_num") else ""
        label = _uniq(f"{info['title']}{last}", ud["pick_map"])
        ud["pick_map"][label] = url
        rows.append([KeyboardButton(label)])
    rows.append([KeyboardButton(BTN_BACK)])
    ud["await_pick"] = True
    await context.bot.send_message(
        chat_id, "📖 داستان‌هایی که دنبال می‌کنی:",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True))


# ----------------------------- open series / links -----------------------------

async def open_series(context, chat_id, ud, any_url, status_msg=None):
    try:
        series = await asyncio.to_thread(scraper.get_series, any_url)
    except Exception as e:
        txt = f"❌ خطا در خواندن سایت:\n{e}"
        await (status_msg.edit_text(txt) if status_msg else context.bot.send_message(chat_id, txt))
        return False
    if not series.chapters:
        txt = "❌ هیچ قسمتی پیدا نشد."
        await (status_msg.edit_text(txt) if status_msg else context.bot.send_message(chat_id, txt))
        return False

    ud.update({"chapters": series.chapters, "series_url": series.url,
               "title": series.title, "await_pick": False})
    prev = (_read(FOLLOWS_FILE) or {}).get(str(ud["uid"]), {}).get(series.url, {})
    follow_story(ud["uid"], series.url, series.title, len(series.chapters))

    first, last = series.chapters[0].label, series.chapters[-1].label
    info = (f"✅ «{series.title}»\n"
            f"{len(series.chapters)} قسمت ({first} تا {last}).\n"
            f"🔗 {series.url}")
    if prev.get("last_num"):
        info += f"\n📖 آخرین‌بار تا قسمت {prev['last_num']} رفته بودی."
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass
    await context.bot.send_message(
        chat_id, info,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌐 باز کردن صفحهٔ داستان", url=series.url)]]),
        disable_web_page_preview=True)
    await context.bot.send_message(
        chat_id, "👈 شمارهٔ قسمت رو بفرست تا لینک دکمه‌ایش رو بدم، یا «🔗 لینک همهٔ قسمت‌ها» رو بزن.",
        reply_markup=main_kb())
    return True


async def send_all_links(context, chat_id, ud):
    chapters = ud.get("chapters")
    if not chapters:
        await context.bot.send_message(chat_id, "اول یه داستان انتخاب کن (سرچ یا 📚 همهٔ داستان‌ها).",
                                        reply_markup=main_kb())
        return
    await context.bot.send_message(chat_id, f"🔗 لینک {len(chapters)} قسمت «{ud['title']}»:")
    lines = [f"{c.label}: {c.url}" for c in chapters]
    for i in range(0, len(lines), LINKS_PER_MSG):
        await context.bot.send_message(
            chat_id, "\n".join(lines[i:i + LINKS_PER_MSG]), disable_web_page_preview=True)
        await asyncio.sleep(0.4)
    await context.bot.send_message(chat_id, "تمام ✅", reply_markup=main_kb())


# ----------------------------- updates -----------------------------

async def check_updates(app, notify_chat=None):
    """کاتالوگ را تازه می‌کند؛ داستان‌های جدید و قسمت‌های جدیدِ دنبال‌شده‌ها را خبر می‌دهد.
    اگر notify_chat داده شود، گزارش دستی هم به همان چت می‌فرستد."""
    old_items = load_catalog_items()
    old_slugs = {it["slug"] for it in old_items}
    items = await asyncio.to_thread(scraper.get_catalog)
    if not items:
        if notify_chat:
            await app.bot.send_message(notify_chat, "نشد فهرست رو بگیرم.")
        return
    new_series = [it for it in items if it["slug"] not in old_slugs]
    save_catalog(items)
    first_run = not old_items

    # قسمت‌های جدیدِ داستان‌های دنبال‌شده
    follows = load_follows()
    chapter_updates = {}  # uid -> [(title, new_count, old_count, url)]
    for uid, d in follows.items():
        for url, info in list(d.items()):
            try:
                sr = await asyncio.to_thread(scraper.get_series, url)
            except Exception:
                continue
            new_count = len(sr.chapters)
            if new_count > info.get("count", 0):
                chapter_updates.setdefault(uid, []).append(
                    (sr.title, new_count, info.get("count", 0), url))
            info["count"] = new_count
            info["title"] = sr.title
    save_follows(follows)

    targets = [str(t) for t in ALLOWED_IDS] or list(follows.keys())

    # اطلاع‌رسانی داستان‌های جدید (در اجرای اول خبر نمی‌دهیم تا اسپم نشود)
    if new_series and not first_run:
        head = f"🆕 {len(new_series)} داستان جدید به سایت اضافه شد:"
        body = "\n".join(f"• {it['title']}\n{it['url']}" for it in new_series[:20])
        for t in targets:
            try:
                await app.bot.send_message(int(t), head + "\n" + body, disable_web_page_preview=True)
            except Exception:
                pass

    # اطلاع‌رسانی قسمت‌های جدید
    for uid, ups in chapter_updates.items():
        msg = "📣 قسمت‌های جدید:\n" + "\n".join(
            f"• «{t}»: {n - o} قسمت جدید (الان {n} قسمت)\n{u}" for t, n, o, u in ups)
        try:
            await app.bot.send_message(int(uid), msg, disable_web_page_preview=True)
        except Exception:
            pass

    if notify_chat:
        n_new = 0 if first_run else len(new_series)
        n_ch = sum(len(v) for v in chapter_updates.values())
        await app.bot.send_message(
            notify_chat,
            f"✅ چک شد. کل داستان‌ها: {len(items)} | داستان جدید: {n_new} | "
            f"داستان‌های دارای قسمت جدید: {n_ch}"
            + ("\n(اجرای اول بود؛ فهرست ذخیره شد و از این به بعد تغییرات خبر داده می‌شه.)" if first_run else ""),
            reply_markup=main_kb())


async def updater_loop(app):
    await asyncio.sleep(40)
    while True:
        try:
            await check_updates(app)
        except Exception as e:
            log.warning("updater: %s", e)
        await asyncio.sleep(UPDATE_INTERVAL)


# ----------------------------- handlers -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    context.user_data["uid"] = update.effective_user.id
    await update.message.reply_text(
        "سلام! 👋 این ربات لینک‌دهه (دانلود نمی‌کنه).\n\n"
        "• 🔎 اسم داستان رو بفرست تا توی کل سایت سرچ کنم.\n"
        "• 📚 «همهٔ داستان‌ها» = مرور کل فهرست سایت.\n"
        "• یه داستان رو باز کن، بعد شمارهٔ قسمت رو بفرست تا لینکش رو بدم،\n"
        "  یا «🔗 لینک همهٔ قسمت‌ها» رو بزن.\n"
        "• 🆕 «بروزرسانی‌ها» = چک تغییرات سایت. ربات خودش هم هر چند ساعت\n"
        "  چک می‌کنه و داستان/قسمت جدید رو بهت خبر می‌ده.\n"
        "• 📖 «دنبال‌شده‌ها» = داستان‌هایی که باز کردی.",
        reply_markup=main_kb(), disable_web_page_preview=True,
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text("⏳ در حال چک کردن سایت...")
    await check_updates(context.application, notify_chat=update.effective_chat.id)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    ud = context.user_data
    ud["uid"] = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    m = URL_RE.search(text)
    if m:
        msg = await update.message.reply_text("⏳ در حال خواندن...")
        await open_series(context, chat_id, ud, m.group(0), status_msg=msg)
        return

    if text == BTN_SEARCH:
        await update.message.reply_text("🔎 اسم داستان (یا بخشی ازش) رو بفرست.", reply_markup=main_kb())
        return
    if text == BTN_ALLLINKS:
        await send_all_links(context, chat_id, ud)
        return
    if text == BTN_UPDATES:
        await update.message.reply_text("⏳ در حال چک کردن سایت...")
        await check_updates(context.application, notify_chat=chat_id)
        return
    if text == BTN_FOLLOWS:
        await show_follows(context, chat_id, ud)
        return
    if text == BTN_ALLSTORIES:
        items = await ensure_catalog(context, chat_id)
        if not items:
            await update.message.reply_text("نشد فهرست رو بگیرم، دوباره امتحان کن.", reply_markup=main_kb())
            return
        ud["browse"], ud["bpage"] = items, 0
        await show_browse(context, chat_id, ud)
        return
    if text in (BTN_NEXT, BTN_PREV):
        if ud.get("browse"):
            ud["bpage"] = ud.get("bpage", 0) + (1 if text == BTN_NEXT else -1)
            await show_browse(context, chat_id, ud)
        return
    if re.fullmatch(r"\d+/\d+", text):
        return
    if text == BTN_BACK:
        ud["await_pick"] = False
        await update.message.reply_text("باشه.", reply_markup=main_kb())
        return

    if ud.get("await_pick") and text in (ud.get("pick_map") or {}):
        await update.message.reply_text("⏳ در حال باز کردن...")
        await open_series(context, chat_id, ud, ud["pick_map"][text])
        return

    # شمارهٔ قسمت -> لینک همون قسمت
    mnum = re.fullmatch(r"(?:قسمت\s*)?(\d+(?:\.\d+)?)", text)
    if mnum:
        chapters = ud.get("chapters")
        if not chapters:
            await update.message.reply_text("اول یه داستان انتخاب کن (سرچ یا 📚 همهٔ داستان‌ها).",
                                            reply_markup=main_kb())
            return
        want = float(mnum.group(1))
        idx = next((i for i, c in enumerate(chapters) if c.num == want), None)
        if idx is None and want == int(want) and 1 <= int(want) <= len(chapters):
            idx = int(want) - 1
        if idx is None:
            await update.message.reply_text(
                f"قسمت {int(want) if want==int(want) else want} پیدا نشد (۱ تا {len(chapters)}).",
                reply_markup=main_kb())
            return
        ch = chapters[idx]
        follow_story(ud["uid"], ud["series_url"], ud["title"], len(chapters), last_num=int(ch.num) if ch.num == int(ch.num) else ch.num)
        await update.message.reply_text(
            f"🔗 {ch.label} — «{ud['title']}»\n{ch.url}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌐 باز کردن در مرورگر", url=ch.url)]]),
            disable_web_page_preview=True)
        return

    # هر متن دیگر = جستجو
    items = await ensure_catalog(context, chat_id)
    res = search_catalog(items, text) if items else []
    if not res:
        await update.message.reply_text(
            f"🔎 «{text}» چیزی پیدا نشد. اسم دیگه‌ای امتحان کن یا 📚 همهٔ داستان‌ها رو بزن.",
            reply_markup=main_kb())
        return
    await show_picker(context, chat_id, ud, res, f"🔎 {len(res)} نتیجه برای «{text}». یکی رو انتخاب کن:")


async def post_init(app):
    app.create_task(updater_loop(app))


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN تنظیم نشده (در .env یا متغیر محیطی).")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("ربات روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
