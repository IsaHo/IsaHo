"""
bot.py — ربات لینک‌دهی و اطلاع‌رسانی بروزرسانی sarrast.com

این ربات دانلود نمی‌کند (نه عکس، نه PDF) تا رم اشغال نشود. فقط:
  • کل سایت را می‌خواند و کش می‌کند.
  • دوره‌ای چک می‌کند و داستان‌های جدید / قسمت‌های جدید را خبر می‌دهد.
  • برای هر داستان/قسمت، لینک تمیز (بدون ouo.io) می‌فرستد.

امکانات:
  ▶️ ادامهٔ خواندن      = با یک تپ، قسمت بعدیِ آخرین داستانی که خوندی
  🔎 جستجو / تایپ اسم   = سرچ در کل سایت
  🗂 دسته‌بندی‌ها        = تازه آپدیت‌شده‌ها / تازه اضافه‌شده‌ها / پرقسمت‌ترین‌ها
  📚 همهٔ داستان‌ها     = مرور کل فهرست سایت
  📖 دنبال‌شده‌ها       = داستان‌هایی که باز کردی
  کارت داستان          = کاور + خلاصه + دکمه‌های ادامه/آخرین قسمت/همهٔ لینک‌ها
  جستجوی inline        = در هر چتی بنویس @اسم_ربات و اسم داستان

اجرا:  python bot.py   (توکن از .env)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputFile,
    InputTextMessageContent,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import privacy
from scraper import SITE, Scraper, Series

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
# آیدی‌های داخل .env = مدیرها (قابل حذف نیستند و می‌توانند آیدی مدیریت کنند)
OWNER_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}

DATA_DIR = os.path.join(HERE, "data")
CATALOG_FILE = os.path.join(DATA_DIR, "catalog.json")
FOLLOWS_FILE = os.path.join(DATA_DIR, "follows.json")
ALLOWED_FILE = os.path.join(DATA_DIR, "allowed.json")
META_FILE = os.path.join(DATA_DIR, "meta.json")   # کاور/خلاصه/تعداد قسمت هر داستان
FAVS_FILE = os.path.join(DATA_DIR, "favs.json")   # علاقه‌مندی‌ها
SHELVES_FILE = os.path.join(DATA_DIR, "shelves.json")  # قفسه‌ها: دارم می‌خونم / تمومش کردم / بعداً
CATALOG_TTL = 6 * 3600
CATALOG_VERSION = 4               # با تغییر ساختار کاتالوگ، کش قدیمی باطل می‌شود
ENRICH_PAUSE = 3                  # مکث بین خواندن اطلاعات داستان‌ها در پس‌زمینه (ثانیه)
ENRICH_BATCH = 40                 # حداکثر داستان در هر دور
UPDATE_INTERVAL = 2 * 3600        # هر چند ثانیه سایت برای بروزرسانی چک شود
BROWSE_PER = 16
LINKS_PER_MSG = 40
URL_RE = re.compile(r"https?://[^\s]+")

BTN_CONTINUE = "▶️ ادامهٔ خواندن"
BTN_SEARCH = "🔎 جستجو"
BTN_CATS = "🗂 دسته‌بندی‌ها"
BTN_CAT_FRESH = "🔥 تازه آپدیت‌شده‌ها"
BTN_CAT_NEW = "✨ تازه اضافه‌شده‌ها"
BTN_CAT_LONG = "📈 پرقسمت‌ترین‌ها"
BTN_ALLSTORIES = "📚 همهٔ داستان‌ها"
BTN_ALLLINKS = "🔗 لینک همهٔ قسمت‌ها"
BTN_UPDATES = "🆕 بروزرسانی‌ها"
BTN_FOLLOWS = "📖 دنبال‌شده‌ها"
BTN_FAVS = "❤️ علاقه‌مندی‌ها"
BTN_SHELVES = "🗄 قفسه‌ها"
SHELVES = [("r", "📖 دارم می‌خونم"), ("d", "✅ تمومش کردم"), ("l", "🕒 بعداً می‌خونم")]
SHELF_LABEL = dict(SHELVES)                       # همین‌ها دکمه‌های منوی قفسه‌ها هم هستند
SHELF_BY_LABEL = {v: k for k, v in SHELVES}
SHELF_SHORT = {"r": "📖 می‌خونم", "d": "✅ تموم شد", "l": "🕒 بعداً"}
BTN_FOLLOW_EDIT = "🗑 حذف از دنبال‌شده‌ها"
BTN_IDS = "👤 مدیریت آیدی‌ها"
BTN_PRIVACY = "🧹 حریم خصوصی"  # منوی آن در لانچرِ مشترک (bot.py) است
BTN_HOME = "🏠 منوی اصلی"  # در لانچرِ مشترک (bot.py) برای برگشت به انتخاب سرراست/سولاخی
BTN_NEXT = "صفحهٔ بعد ▶️"
BTN_PREV = "◀️ صفحهٔ قبل"
BTN_BACK = "⬅️ بازگشت"

# همهٔ دکمه‌های کیبوردِ این بخش (لانچر از این برای تشخیص بخش استفاده می‌کند)
MENU_BUTTONS = {BTN_CONTINUE, BTN_SEARCH, BTN_CATS, BTN_CAT_FRESH, BTN_CAT_NEW, BTN_CAT_LONG,
                BTN_ALLSTORIES, BTN_ALLLINKS, BTN_UPDATES, BTN_FOLLOWS, BTN_FOLLOW_EDIT, BTN_IDS, BTN_FAVS,
                BTN_SHELVES, *SHELF_LABEL.values()}

scraper = Scraper()
_lock = threading.Lock()


def load_allowed() -> set:
    d = _read(ALLOWED_FILE) or {}
    return {int(x) for x in d.get("ids", [])}


def save_allowed(s):
    with _lock:
        _write(ALLOWED_FILE, {"ids": sorted(int(x) for x in s)})


def all_allowed() -> set:
    return OWNER_IDS | load_allowed()


def authorized(update: Update) -> bool:
    allowed = all_allowed()
    if not allowed:
        return True
    u = update.effective_user
    return bool(u and u.id in allowed)


def is_admin(uid) -> bool:
    return (not OWNER_IDS) or (uid in OWNER_IDS)


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
    """ذخیرهٔ کاتالوگ؛ زمان اولین دیده‌شدنِ هر داستان (first_seen) حفظ می‌شود."""
    with _lock:
        old = {it["slug"]: it for it in ((_read(CATALOG_FILE) or {}).get("items") or [])}
        now = time.time()
        for it in items:
            it["first_seen"] = old.get(it["slug"], {}).get("first_seen") or now
        _write(CATALOG_FILE, {"ts": time.time(), "v": CATALOG_VERSION, "items": items})


def slug_of(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def series_url_of(slug: str) -> str:
    return f"{SITE}/series/{slug}/"


# meta: {slug: {title, count, last, cover, summary, ts}}
def load_meta() -> dict:
    return _read(META_FILE) or {}


def update_meta(series) -> None:
    if not series.chapters:
        return
    with _lock:
        d = _read(META_FILE) or {}
        d[slug_of(series.url)] = {
            "title": series.title,
            "count": len(series.chapters),
            "last": series.chapters[-1].label,
            "cover": series.cover,
            "summary": (series.summary or "")[:700],
            "ts": time.time(),
        }
        _write(META_FILE, d)


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
            "ts": datetime.now().isoformat(timespec="microseconds"),
        }
        _write(FOLLOWS_FILE, d)


def list_follows(uid):
    items = list((_read(FOLLOWS_FILE) or {}).get(str(uid), {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", ""), reverse=True)
    return items


def del_follow(uid, url):
    with _lock:
        d = _read(FOLLOWS_FILE) or {}
        u = d.get(str(uid), {})
        if url in u:
            u.pop(url)
            d[str(uid)] = u
            _write(FOLLOWS_FILE, d)


# favs: {uid: {series_url: {title, ts}}}
def is_fav(uid, url) -> bool:
    return url in (_read(FAVS_FILE) or {}).get(str(uid), {})


def toggle_fav(uid, url, title) -> bool:
    """علاقه‌مندی را برعکس می‌کند؛ True = الان در علاقه‌مندی‌هاست."""
    with _lock:
        d = _read(FAVS_FILE) or {}
        u = d.setdefault(str(uid), {})
        if url in u:
            u.pop(url)
            now = False
        else:
            u[url] = {"title": title, "ts": time.time()}
            now = True
        _write(FAVS_FILE, d)
    return now


def list_favs(uid):
    items = list((_read(FAVS_FILE) or {}).get(str(uid), {}).items())
    items.sort(key=lambda kv: kv[1].get("ts", 0), reverse=True)
    return items


# shelves: {uid: {series_url: {shelf, title, ts}}}   shelf ∈ r / d / l
def get_shelf(uid, url):
    return ((_read(SHELVES_FILE) or {}).get(str(uid), {}).get(url) or {}).get("shelf")


def set_shelf(uid, url, title, shelf):
    with _lock:
        d = _read(SHELVES_FILE) or {}
        u = d.setdefault(str(uid), {})
        if shelf:
            u[url] = {"shelf": shelf, "title": title, "ts": time.time()}
        else:
            u.pop(url, None)
        _write(SHELVES_FILE, d)


def list_shelf(uid, shelf):
    items = [(u, i) for u, i in (_read(SHELVES_FILE) or {}).get(str(uid), {}).items() if i.get("shelf") == shelf]
    items.sort(key=lambda kv: kv[1].get("ts", 0), reverse=True)
    return items


# ----------------------------- لینک اختصاصی هر داستان -----------------------------

BOT_USERNAME = ""   # در start_background پر می‌شود


def deep_link(slug: str) -> str:
    """لینکی که کارت داستان را مستقیم در ربات باز می‌کند: t.me/<bot>?start=s_<slug>"""
    payload = f"s_{slug}"
    if BOT_USERNAME and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
        return f"https://t.me/{BOT_USERNAME}?start={payload}"
    return ""


def share_url(title: str, slug: str) -> str:
    dl = deep_link(slug)
    return f"https://t.me/share/url?url={quote(dl, safe='')}&text={quote('📖 ' + title)}" if dl else ""


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
        [[BTN_CONTINUE],
         [BTN_SEARCH, BTN_CATS],
         [BTN_FAVS, BTN_SHELVES, BTN_FOLLOWS],
         [BTN_ALLSTORIES, BTN_ALLLINKS],
         [BTN_UPDATES, BTN_IDS],
         [BTN_PRIVACY, BTN_HOME]],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="اسم داستان، شمارهٔ قسمت، یا لینک رو بفرست",
    )


async def show_ids_menu(context, chat_id, uid, edit_msg=None):
    owners = ", ".join(str(x) for x in sorted(OWNER_IDS)) or "—"
    extra = sorted(load_allowed() - OWNER_IDS)
    lines = [f"👤 آیدی خودت: `{uid}`", f"🔑 مدیرها: {owners}", "➕ اضافه‌شده‌ها:"]
    lines.append("\n".join(f"• {i}" for i in extra) if extra else "— (کسی اضافه نشده)")
    rows = [[InlineKeyboardButton(f"🗑 حذف {i}", callback_data=f"delid:{i}")] for i in extra]
    rows.append([InlineKeyboardButton("➕ افزودن آیدی", callback_data="addid")])
    kb = InlineKeyboardMarkup(rows)
    txt = "\n".join(lines)
    if edit_msg:
        try:
            await edit_msg.edit_text(txt, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, txt, reply_markup=kb, parse_mode="Markdown")


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
    head = ud.get("browse_title") or BTN_ALLSTORIES
    note = ud.get("browse_note") or ""
    await context.bot.send_message(
        chat_id, f"{head} ({total}) — صفحهٔ {pg+1}/{pages}\nیکی رو بزن، یا اسم داستان رو سرچ کن.{note}",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True))


def cats_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[BTN_CAT_FRESH], [BTN_CAT_NEW], [BTN_CAT_LONG], [BTN_BACK]],
                               resize_keyboard=True, is_persistent=True)


def category_items(kind: str, items: list, meta: dict) -> list:
    """آیتم‌های یک دسته، مرتب و با برچسب نمایشی."""
    out = []
    if kind == "fresh":      # ترتیب خود سایت (صفحهٔ اول = تازه‌ترین آپدیت‌ها)، «تازه»ها اول
        for it in sorted(items, key=lambda x: (not x.get("fresh"), x.get("rank", 0))):
            out.append({**it, "title": ("🔥 " if it.get("fresh") else "") + it["title"]})
    elif kind == "new":      # اولین باری که ربات داستان را در سایت دید
        out = sorted(items, key=lambda x: (-x.get("first_seen", 0), x.get("rank", 0)))
    elif kind == "long":     # بیشترین تعداد قسمت
        counted = []
        for it in items:
            n = (meta.get(it["slug"]) or {}).get("count") or it.get("latest")
            if n:
                counted.append((n, it))
        counted.sort(key=lambda x: -x[0])
        out = [{**it, "title": f"{it['title']} ({n} قسمت)"} for n, it in counted]
    return out


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
    rows.append([KeyboardButton(BTN_FOLLOW_EDIT)])
    rows.append([KeyboardButton(BTN_BACK)])
    ud["await_pick"] = True
    await context.bot.send_message(
        chat_id, "📖 داستان‌هایی که دنبال می‌کنی:\n(برای حذف، «🗑 حذف از دنبال‌شده‌ها» رو بزن)",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True))


async def show_favs(context, chat_id, ud):
    favs = list_favs(ud["uid"])
    if not favs:
        await context.bot.send_message(
            chat_id, "🤍 هنوز علاقه‌مندی نداری.\nروی کارت هر داستان «🤍 علاقه‌مندی» رو بزن.", reply_markup=main_kb())
        return
    follows = dict(list_follows(ud["uid"]))
    ud["pick_map"] = {}
    rows = []
    for url, info in favs:
        last = (follows.get(url) or {}).get("last_num")
        label = _uniq(f"❤️ {info['title']}" + (f" (تا قسمت {last})" if last else ""), ud["pick_map"])
        ud["pick_map"][label] = url
        rows.append([KeyboardButton(label)])
    rows.append([KeyboardButton(BTN_BACK)])
    ud["await_pick"] = True
    await context.bot.send_message(
        chat_id, f"❤️ علاقه‌مندی‌هات ({len(favs)}):\n(برای حذف، روی کارت داستان «❤️» رو بزن)",
        reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True))


async def show_follow_edit(context, chat_id, ud, edit_msg=None):
    items = list_follows(ud["uid"])
    if not items:
        txt = "📭 لیست دنبال‌شده‌ها خالیه."
        if edit_msg:
            try:
                await edit_msg.edit_text(txt)
                return
            except Exception:
                pass
        await context.bot.send_message(chat_id, txt, reply_markup=main_kb())
        return
    ud["unfollow_list"] = [u for u, _ in items]
    rows = [[InlineKeyboardButton(f"🗑 {info['title'][:45]}", callback_data=f"uf:{i}")]
            for i, (_, info) in enumerate(items)]
    txt = "🗑 روی هر داستان بزنی از دنبال‌شده‌ها حذف می‌شه (بقیه می‌مونن):"
    kb = InlineKeyboardMarkup(rows)
    if edit_msg:
        try:
            await edit_msg.edit_text(txt, reply_markup=kb)
            return
        except Exception:
            pass
    await context.bot.send_message(chat_id, txt, reply_markup=kb)


# ----------------------------- open series / links -----------------------------

def _cb(data: str, fallback: str) -> str:
    """callback_data حداکثر ۶۴ بایت است؛ اگر جا نشد، نسخهٔ کوتاه‌تر."""
    return data if len(data.encode()) <= 64 else fallback


def ch_cb(slug: str, idx: int) -> str:
    return _cb(f"s.ch:{slug}:{idx}", f"go:{idx}")


def _to_jpeg(data: bytes) -> io.BytesIO:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        buf.seek(0)
        return buf
    except Exception:
        return io.BytesIO(data)


async def send_with_cover(context, chat_id, cover, caption, kb):
    """کارت با کاور؛ اگر تلگرام عکس را از لینک نگرفت، خودمان دانلود و JPEG می‌کنیم؛ در بدترین حالت متن."""
    if cover:
        try:
            return await context.bot.send_photo(chat_id, cover, caption=caption, reply_markup=kb)
        except Exception as e:
            log.info("cover by url failed: %s", e)
        try:
            data, _ = await asyncio.to_thread(scraper.download_image, cover, SITE + "/")
            return await context.bot.send_photo(chat_id, _to_jpeg(data), caption=caption, reply_markup=kb)
        except Exception as e:
            log.info("cover fetch failed: %s", e)
    return await context.bot.send_message(chat_id, caption, reply_markup=kb, disable_web_page_preview=True)


def _next_idx(chapters, last_num):
    """اندیس قسمتِ بعد از last_num (None اگر همه خوانده شده)."""
    if last_num is None:
        return 0
    return next((i for i, c in enumerate(chapters) if c.num > float(last_num)), None)


def card_caption(series, last_num) -> str:
    n = len(series.chapters)
    lines = [f"📖 {series.title}",
             f"📚 {n} قسمت  •  آخرین: {series.chapters[-1].label}"]
    if last_num is not None:
        lines.append(f"🔖 تو تا قسمت {last_num} خوندی")
    if series.summary:
        summ = series.summary
        lines += ["", summ[:600] + ("…" if len(summ) > 600 else "")]
    return "\n".join(lines)[:1020]


def card_kb(series, last_num, fav: bool = False, shelf=None) -> InlineKeyboardMarkup:
    chapters, slug = series.chapters, slug_of(series.url)
    nxt = _next_idx(chapters, last_num)
    if nxt is None:
        i, read = 0, "🎉 همه رو خوندی — از اول"
    elif last_num is None:
        i, read = 0, f"▶️ شروع از {chapters[0].label}"
    else:
        i, read = nxt, f"▶️ ادامه از {chapters[nxt].label}"
    last_row = [InlineKeyboardButton("🌐 سایت", url=series.url)]
    share = share_url(series.title, slug)
    if share:
        last_row.append(InlineKeyboardButton("↗️ اشتراک‌گذاری", url=share))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(read, callback_data=ch_cb(slug, i))],
        [InlineKeyboardButton(f"🆕 آخرین ({chapters[-1].label})", callback_data=ch_cb(slug, len(chapters) - 1)),
         InlineKeyboardButton("🔗 همهٔ لینک‌ها", callback_data=_cb(f"s.all:{slug}", "s.all:"))],
        [InlineKeyboardButton(("✓ " if shelf == k else "") + SHELF_SHORT[k],
                              callback_data=_cb(f"s.sh:{k}:{slug}", f"s.sh:{k}:")) for k, _ in SHELVES],
        [InlineKeyboardButton("❤️ در علاقه‌مندی‌ها" if fav else "🤍 علاقه‌مندی",
                              callback_data=_cb(f"s.fav:{slug}", "s.fav:"))],
        last_row,
    ])


async def _refresh_card(q, ud):
    """بعد از تغییر علاقه‌مندی/قفسه، دکمه‌های همان کارت را بروز می‌کند."""
    uid, url = ud["uid"], ud["series_url"]
    last = dict(list_follows(uid)).get(url, {}).get("last_num")
    sr = Series(title=ud["title"], url=url, chapters=ud["chapters"])
    try:
        await q.message.edit_reply_markup(card_kb(sr, last, is_fav(uid, url), get_shelf(uid, url)))
    except Exception:
        pass


def _set_series(ud, series):
    ud.update({"chapters": series.chapters, "series_url": series.url,
               "title": series.title, "await_pick": False})


async def open_series(context, chat_id, ud, any_url, status_msg=None):
    """کارت داستان: کاور + اسم + تعداد قسمت + خلاصه + دکمه‌ها."""
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

    _set_series(ud, series)
    update_meta(series)
    prev = (_read(FOLLOWS_FILE) or {}).get(str(ud["uid"]), {}).get(series.url, {})
    last_num = prev.get("last_num")
    follow_story(ud["uid"], series.url, series.title, len(series.chapters))

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass
    await send_with_cover(context, chat_id, series.cover, card_caption(series, last_num),
                          card_kb(series, last_num, is_fav(ud["uid"], series.url),
                                  get_shelf(ud["uid"], series.url)))
    await context.bot.send_message(chat_id, "✏️ یا شمارهٔ قسمت رو بفرست.", reply_markup=main_kb())
    return True


async def ensure_series(context, chat_id, ud, slug) -> bool:
    """اگر داستانِ این slug الان باز نیست، بازش کن (بی‌صدا)."""
    url = series_url_of(slug)
    if ud.get("series_url") == url and ud.get("chapters"):
        return True
    try:
        series = await asyncio.to_thread(scraper.get_series, url)
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ خطا در خواندن سایت:\n{e}")
        return False
    if not series.chapters:
        await context.bot.send_message(chat_id, "❌ هیچ قسمتی پیدا نشد.")
        return False
    _set_series(ud, series)
    update_meta(series)
    return True


async def do_continue(context, chat_id, ud, url=None):
    """ادامهٔ خواندن: قسمتِ بعد از آخرین قسمتی که لینکش رو گرفتی."""
    follows = list_follows(ud["uid"])
    if url is None:
        cand = [kv for kv in follows if kv[1].get("last_num") is not None] or follows
        if not cand:
            await context.bot.send_message(
                chat_id, "📭 هنوز داستانی نخوندی. یه اسم سرچ کن یا 🗂 دسته‌بندی‌ها رو ببین.",
                reply_markup=main_kb())
            return
        url = cand[0][0]
    if not await ensure_series(context, chat_id, ud, slug_of(url)):
        return
    last = dict(follows).get(ud["series_url"], {}).get("last_num")
    idx = _next_idx(ud["chapters"], last)
    if idx is None:
        await context.bot.send_message(
            chat_id, f"🎉 «{ud['title']}» رو تا آخر ({ud['chapters'][-1].label}) خوندی.\n"
                     f"قسمت جدید که بیاد خبرت می‌کنم.", reply_markup=main_kb())
        return
    await send_chapter_link(context, chat_id, ud, idx)


def chapter_link_markup(idx, total, url, slug=""):
    rows = [[InlineKeyboardButton("🌐 باز کردن در مرورگر", url=url)]]
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("⬅️ قسمت قبل", callback_data=ch_cb(slug, idx - 1) if slug else f"go:{idx-1}"))
    if idx + 1 < total:
        nav.append(InlineKeyboardButton("قسمت بعد ➡️", callback_data=ch_cb(slug, idx + 1) if slug else f"go:{idx+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def send_chapter_link(context, chat_id, ud, idx):
    chapters = ud["chapters"]
    ch = chapters[idx]
    follow_story(ud["uid"], ud["series_url"], ud["title"], len(chapters),
                 last_num=int(ch.num) if ch.num == int(ch.num) else ch.num)
    if get_shelf(ud["uid"], ud["series_url"]) is None:      # اولین قسمت = «دارم می‌خونم»
        set_shelf(ud["uid"], ud["series_url"], ud["title"], "r")
    await context.bot.send_message(
        chat_id, f"🔗 {ch.label} از {len(chapters)} — «{ud['title']}»\n{ch.url}",
        reply_markup=chapter_link_markup(idx, len(chapters), ch.url, slug_of(ud["series_url"])),
        disable_web_page_preview=True)


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
    chapter_updates = {}  # uid -> [(series, old_count)]
    for uid, d in follows.items():
        for url, info in list(d.items()):
            try:
                sr = await asyncio.to_thread(scraper.get_series, url)
            except Exception:
                continue
            update_meta(sr)
            new_count = len(sr.chapters)
            if new_count > info.get("count", 0):
                chapter_updates.setdefault(uid, []).append((sr, info.get("count", 0)))
            info["count"] = new_count
            info["title"] = sr.title
    save_follows(follows)

    targets = [str(t) for t in all_allowed()] or list(follows.keys())

    ctx = _BotCtx(app.bot)
    # اطلاع‌رسانی داستان‌های جدید با کارت (در اجرای اول خبر نمی‌دهیم تا اسپم نشود)
    if new_series and not first_run:
        for t in targets:
            for it in new_series[:5]:
                cap = f"🆕 داستان جدید در سایت\n📖 {it['title']}" + (
                    f"\n📚 {it['latest']} قسمت" if it.get("latest") else "")
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("📖 باز کردن کارت داستان", callback_data=_cb(f"s.open:{it['slug']}", "s.open:"))],
                    [InlineKeyboardButton("🌐 صفحهٔ داستان", url=it["url"])]])
                try:
                    await send_with_cover(ctx, int(t), it.get("cover"), cap, kb)
                except Exception:
                    pass
            if len(new_series) > 5:
                rest = "\n".join(f"• {it['title']}\n{it['url']}" for it in new_series[5:25])
                try:
                    await app.bot.send_message(int(t), f"🆕 و {len(new_series) - 5} داستان جدید دیگه:\n{rest}",
                                               disable_web_page_preview=True)
                except Exception:
                    pass

    # اطلاع‌رسانی قسمت‌های جدید با کارت (کاور + دکمهٔ خواندن همان قسمت)
    for uid, ups in chapter_updates.items():
        for sr, old in ups[:8]:
            slug, n = slug_of(sr.url), len(sr.chapters)
            i = min(old, n - 1)
            cap = (("❤️ " if is_fav(uid, sr.url) else "") +
                   f"🔔 قسمت جدید!\n📖 {sr.title}\n📚 {n - old} قسمت جدید — حالا {n} قسمت")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"▶️ لینک {sr.chapters[i].label}", callback_data=ch_cb(slug, i)),
                 InlineKeyboardButton("🌐 باز کردن", url=sr.chapters[i].url)]])
            try:
                await send_with_cover(ctx, int(uid), sr.cover, cap, kb)
            except Exception:
                pass
        if len(ups) > 8:
            rest = "\n".join(f"• «{sr.title}»: حالا {len(sr.chapters)} قسمت" for sr, _ in ups[8:])
            try:
                await app.bot.send_message(int(uid), "📣 قسمت جدید در داستان‌های دیگه:\n" + rest)
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


class _BotCtx:
    """برای فراخوانی توابعی که context.bot می‌خواهند، از داخل حلقه‌های پس‌زمینه."""
    def __init__(self, bot):
        self.bot = bot


async def updater_loop(app):
    await asyncio.sleep(40)
    while True:
        try:
            await check_updates(app)
        except Exception as e:
            log.warning("updater: %s", e)
        await asyncio.sleep(UPDATE_INTERVAL)


async def enrich_loop(app):
    """در پس‌زمینه، آرام‌آرام اطلاعات هر داستان (کاور، خلاصه، تعداد قسمت) را جمع می‌کند
    تا کارت‌ها، «پرقسمت‌ترین‌ها» و جستجوی inline کامل باشند. دانلودی انجام نمی‌شود."""
    await asyncio.sleep(90)
    while True:
        did = 0
        try:
            items, meta, now = load_catalog_items(), load_meta(), time.time()

            def age(it):
                return now - (meta.get(it["slug"]) or {}).get("ts", 0)

            todo = [it for it in items if it["slug"] not in meta]
            todo += [it for it in items if it["slug"] in meta and it.get("fresh") and age(it) > 6 * 3600]
            todo += [it for it in items if it["slug"] in meta and age(it) > 3 * 86400]
            seen = set()
            for it in todo:
                if did >= ENRICH_BATCH:
                    break
                if it["slug"] in seen:
                    continue
                seen.add(it["slug"])
                try:
                    update_meta(await asyncio.to_thread(scraper.get_series, it["url"]))
                except Exception as e:
                    log.info("enrich %s: %s", it["slug"], e)
                did += 1
                await asyncio.sleep(ENRICH_PAUSE)
        except Exception as e:
            log.warning("enrich: %s", e)
        await asyncio.sleep(60 if did >= ENRICH_BATCH else 1800)


def start_background(app):
    global BOT_USERNAME
    BOT_USERNAME = app.bot.username or ""
    app.create_task(updater_loop(app))
    app.create_task(enrich_loop(app))


# ----------------------------- handlers -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    uid = update.effective_user.id
    context.user_data["uid"] = uid
    await update.message.reply_text(
        "📚 سرراست\n\n"
        "🔎 اسم داستان رو بفرست تا توی کل سایت بگردم\n"
        "▶️ «ادامهٔ خواندن» = قسمت بعدیِ آخرین داستانی که خوندی\n"
        "🗄 قفسه‌ها: دارم می‌خونم / تمومش کردم / بعداً\n"
        "↗️ هر داستان لینک اختصاصی داره (دکمهٔ اشتراک‌گذاری روی کارت)\n"
        "❤️ داستان‌های محبوبت رو توی علاقه‌مندی‌ها نگه دار\n"
        "🗂 «دسته‌بندی‌ها» = تازه آپدیت‌شده / تازه اضافه‌شده / پرقسمت‌ترین\n"
        "🔔 قسمت جدیدِ داستان‌هات و داستان‌های جدید سایت خودکار خبر داده می‌شه\n\n"
        "💡 توی هر چتی بنویس @" + (context.bot.username or "bot") + " و اسم داستان، تا سریع پیداش کنی.",
        reply_markup=main_kb(), disable_web_page_preview=True,
    )
    rows = []
    for url, info in list_follows(uid)[:3]:
        last = info.get("last_num")
        label = f"▶️ {info['title'][:28]} — " + (f"بعد از قسمت {last}" if last is not None else "شروع")
        cb = f"s.cont:{slug_of(url)}"
        if len(cb.encode()) <= 64:
            rows.append([InlineKeyboardButton(label, callback_data=cb)])
    if rows:
        await update.message.reply_text("📖 ادامهٔ خواندن:", reply_markup=InlineKeyboardMarkup(rows))


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    await update.message.reply_text("⏳ در حال چک کردن سایت...")
    await check_updates(context.application, notify_chat=update.effective_chat.id)


def _logo_candidates(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    cands = []
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        cands.append(og["content"])
    for l in soup.find_all("link"):
        rel = " ".join(l.get("rel") or []).lower()
        if any(k in rel for k in ("apple-touch-icon", "icon")) and l.get("href"):
            cands.append(l["href"])
    for img in soup.find_all("img"):
        blob = (img.get("src", "") + " " + " ".join(img.get("class") or []) +
                " " + (img.get("alt") or "")).lower()
        if "logo" in blob and img.get("src"):
            cands.append(img["src"])
    seen, out = set(), []
    for c in cands:
        u = urljoin(SITE + "/", c)
        if u not in seen:
            seen.add(u)
            out.append(u)
    # عکس‌های رسترِ بزرگ‌تر اول، SVG آخر
    out.sort(key=lambda x: (x.lower().endswith(".svg"), "apple-touch" not in x.lower()))
    return out


async def cmd_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ در حال گرفتن لوگوی سایت...")
    try:
        html = await asyncio.to_thread(lambda: scraper.get(SITE + "/").text)
    except Exception as e:
        await update.message.reply_text(f"❌ خطا: {e}")
        return
    cands = _logo_candidates(html)
    if not cands:
        await update.message.reply_text("لوگویی پیدا نشد.")
        return
    sent = 0
    for u in cands[:5]:
        try:
            data, ct = await asyncio.to_thread(scraper.download_image, u, SITE)
        except Exception:
            continue
        fn = (u.split("/")[-1].split("?")[0]) or "logo"
        try:
            await context.bot.send_document(
                chat_id, document=InputFile(io.BytesIO(data), filename=fn), caption=u)
            sent += 1
        except Exception:
            continue
        if not u.lower().endswith(".svg"):
            break
    if sent:
        await update.message.reply_text(
            "👆 فایل لوگو. برای گذاشتن روی بات:\n"
            "به @BotFather برو → /setuserpic → این بات رو انتخاب کن → همین عکس رو بفرست.\n"
            "(اگه SVG بود، توی گوشی به PNG مربع تبدیلش کن.)",
            reply_markup=main_kb())
    else:
        await update.message.reply_text("نشد لوگو رو دانلود کنم.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        await update.message.reply_text("⛔ این ربات خصوصی است.")
        return
    ud = context.user_data
    ud["uid"] = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    # در حال افزودن آیدی (فقط مدیر)
    if ud.get("await_addid"):
        mid = re.search(r"\d{4,}", text)
        if mid and is_admin(ud["uid"]):
            ud["await_addid"] = False
            s = load_allowed()
            s.add(int(mid.group(0)))
            save_allowed(s)
            await update.message.reply_text(f"✅ آیدی {mid.group(0)} اضافه شد.", reply_markup=main_kb())
            await show_ids_menu(context, chat_id, ud["uid"])
            return
        ud["await_addid"] = False  # متن عددی نبود → لغو و ادامهٔ عادی

    if text == BTN_IDS:
        if not is_admin(ud["uid"]):
            await update.message.reply_text("⛔ فقط مدیر می‌تونه آیدی‌ها رو مدیریت کنه.", reply_markup=main_kb())
            return
        await show_ids_menu(context, chat_id, ud["uid"])
        return

    m = URL_RE.search(text)
    if m:
        msg = await update.message.reply_text("⏳ در حال خواندن...")
        await open_series(context, chat_id, ud, m.group(0), status_msg=msg)
        return

    if text == BTN_SEARCH:
        await update.message.reply_text("🔎 اسم داستان (یا بخشی ازش) رو بفرست.", reply_markup=main_kb())
        return
    if text == BTN_CONTINUE:
        await do_continue(context, chat_id, ud)
        return
    if text == BTN_FAVS:
        await show_favs(context, chat_id, ud)
        return
    if text == BTN_SHELVES:
        ud["await_pick"] = False
        counts = "\n".join(f"{label}: {len(list_shelf(ud['uid'], k))}" for k, label in SHELVES)
        await update.message.reply_text(
            f"🗄 قفسه‌هات:\n{counts}\n\n(روی کارت هر داستان می‌تونی قفسه‌ش رو عوض کنی)",
            reply_markup=ReplyKeyboardMarkup([[label] for _, label in SHELVES] + [[BTN_BACK]],
                                             resize_keyboard=True, is_persistent=True))
        return
    if text in SHELF_BY_LABEL:
        k = SHELF_BY_LABEL[text]
        items = list_shelf(ud["uid"], k)
        if not items:
            await update.message.reply_text(f"{text}: خالیه.", reply_markup=main_kb())
            return
        follows = dict(list_follows(ud["uid"]))
        await show_picker(context, chat_id, ud, [
            {"title": info["title"] + (f" (تا قسمت {(follows.get(u) or {}).get('last_num')})"
                                       if (follows.get(u) or {}).get("last_num") else ""),
             "slug": slug_of(u), "url": u} for u, info in items], f"{text} ({len(items)}):")
        return
    if text == BTN_CATS:
        ud["await_pick"] = False
        await update.message.reply_text("🗂 کدوم دسته؟", reply_markup=cats_kb())
        return
    if text in (BTN_CAT_FRESH, BTN_CAT_NEW, BTN_CAT_LONG):
        items = await ensure_catalog(context, chat_id)
        if not items:
            await update.message.reply_text("نشد فهرست رو بگیرم، دوباره امتحان کن.", reply_markup=main_kb())
            return
        kind = {BTN_CAT_FRESH: "fresh", BTN_CAT_NEW: "new", BTN_CAT_LONG: "long"}[text]
        meta = load_meta()
        note = ""
        if kind == "long" and len(meta) < len(items):
            note = f"\n⏳ اطلاعات هنوز در حال تکمیله ({len(meta)} از {len(items)} داستان)."
        if kind == "new" and len({it.get("first_seen") for it in items}) <= 1:
            note = "\nℹ️ این دسته با گذشت زمان دقیق‌تر می‌شه (داستان‌هایی که بعداً اضافه بشن، اول میان)."
        ud["browse"], ud["bpage"] = category_items(kind, items, meta), 0
        ud["browse_title"], ud["browse_note"] = text, note
        await show_browse(context, chat_id, ud)
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
    if text == BTN_FOLLOW_EDIT:
        await show_follow_edit(context, chat_id, ud)
        return
    if text == BTN_ALLSTORIES:
        items = await ensure_catalog(context, chat_id)
        if not items:
            await update.message.reply_text("نشد فهرست رو بگیرم، دوباره امتحان کن.", reply_markup=main_kb())
            return
        ud["browse"], ud["bpage"] = sorted(items, key=lambda it: it["title"]), 0
        ud["browse_title"], ud["browse_note"] = BTN_ALLSTORIES, ""
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
        await send_chapter_link(context, chat_id, ud, idx)
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


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorized(update):
        await q.answer("⛔ دسترسی ندارید", show_alert=True)
        return
    data = q.data or ""
    ud = context.user_data
    ud["uid"] = update.effective_user.id

    if data == "addid":
        if not is_admin(ud["uid"]):
            await q.answer("⛔ فقط مدیر", show_alert=True)
            return
        ud["await_addid"] = True
        await q.answer()
        await context.bot.send_message(
            q.message.chat_id,
            "➕ آیدی عددی کاربر رو بفرست.\n(کاربر می‌تونه آیدی خودش رو از @userinfobot بگیره.)")
        return
    if data.startswith("delid:"):
        if not is_admin(ud["uid"]):
            await q.answer("⛔ فقط مدیر", show_alert=True)
            return
        rid = int(data[6:])
        s = load_allowed()
        s.discard(rid)
        save_allowed(s)
        await q.answer(f"آیدی {rid} حذف شد")
        await show_ids_menu(context, q.message.chat_id, ud["uid"], edit_msg=q.message)
        return

    if data.startswith("uf:"):
        lst = ud.get("unfollow_list") or []
        i = int(data[3:])
        if 0 <= i < len(lst):
            del_follow(ud["uid"], lst[i])
            await q.answer("حذف شد")
            await show_follow_edit(context, q.message.chat_id, ud, edit_msg=q.message)
        else:
            await q.answer("دوباره «🗑 حذف از دنبال‌شده‌ها» رو بزن", show_alert=True)
        return

    chat_id = q.message.chat_id
    if data.startswith(("s.ch:", "s.tg:")):   # s.tg = دکمه‌های قدیمیِ «داخل تلگرام» → لینک همان قسمت
        slug, _, idx = data[5:].rpartition(":")
        await q.answer()
        if await ensure_series(context, chat_id, ud, slug):
            idx = int(idx)
            if 0 <= idx < len(ud["chapters"]):
                await send_chapter_link(context, chat_id, ud, idx)
        return
    if data.startswith("s.fav:"):
        slug = data[6:] or slug_of(ud.get("series_url") or "")
        if not slug or not await ensure_series(context, chat_id, ud, slug):
            await q.answer("داستان رو دوباره باز کن.", show_alert=True)
            return
        now = toggle_fav(ud["uid"], ud["series_url"], ud["title"])
        await q.answer("❤️ به علاقه‌مندی‌ها اضافه شد" if now else "از علاقه‌مندی‌ها حذف شد")
        await _refresh_card(q, ud)
        return
    if data.startswith("s.sh:"):
        k, _, slug = data[5:].partition(":")
        slug = slug or slug_of(ud.get("series_url") or "")
        if k not in SHELF_LABEL or not slug or not await ensure_series(context, chat_id, ud, slug):
            await q.answer("داستان رو دوباره باز کن.", show_alert=True)
            return
        new = None if get_shelf(ud["uid"], ud["series_url"]) == k else k
        set_shelf(ud["uid"], ud["series_url"], ud["title"], new)
        await q.answer(f"🗄 {SHELF_LABEL[new]}" if new else "از قفسه برداشته شد")
        await _refresh_card(q, ud)
        return
    if data.startswith("s.open:"):
        await q.answer()
        if data[7:]:
            await open_series(context, chat_id, ud, series_url_of(data[7:]))
        return
    if data.startswith("s.all:"):
        await q.answer()
        slug = data[6:]
        if not slug or await ensure_series(context, chat_id, ud, slug):
            await send_all_links(context, chat_id, ud)
        return
    if data.startswith("s.cont:"):
        await q.answer()
        await do_continue(context, chat_id, ud, series_url_of(data[7:]))
        return

    if data.startswith("go:"):
        chapters = ud.get("chapters")
        if not chapters:
            await q.answer("لیست عوض شده؛ داستان رو دوباره باز کن.", show_alert=True)
            return
        idx = int(data[3:])
        if not (0 <= idx < len(chapters)):
            await q.answer("قسمت نامعتبر.", show_alert=True)
            return
        await q.answer()
        await send_chapter_link(context, q.message.chat_id, ud, idx)
        return
    await q.answer()


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """جستجوی inline: در هر چتی «@ربات اسم» → لیست داستان‌ها با کاور؛ انتخاب = کارت لینک."""
    iq = update.inline_query
    if not authorized(update):
        await iq.answer([], cache_time=10, is_personal=True)
        return
    items = load_catalog_items()
    q = (iq.query or "").strip()
    res = search_catalog(items, q) if q else sorted(items, key=lambda it: it.get("rank", 0))
    meta = load_meta()
    blur = privacy.get_blur(update.effective_user.id)   # حالت تار: کاورها نشان داده نمی‌شوند
    results = []
    for it in res[:50]:
        m = meta.get(it["slug"]) or {}
        n = m.get("count") or it.get("latest")
        desc = " • ".join(x for x in [f"{n} قسمت" if n else "", "🔥 تازه" if it.get("fresh") else ""] if x)
        summ = (m.get("summary") or "")[:90]
        results.append(InlineQueryResultArticle(
            id=hashlib.md5(it["slug"].encode()).hexdigest(),
            title=it["title"],
            description="\n".join(x for x in [desc, summ] if x) or it["url"],
            thumbnail_url=None if blur else ((m.get("cover") or it.get("cover")) or None),
            input_message_content=InputTextMessageContent(
                f"📖 {it['title']}" + (f"\n📚 {n} قسمت" if n else "") + f"\n{it['url']}"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌐 باز کردن داستان", url=it["url"])]]),
        ))
    try:
        await iq.answer(results, cache_time=60, is_personal=True)
    except Exception as e:
        log.warning("inline answer: %s", e)


async def post_init(app):
    start_background(app)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN تنظیم نشده (در .env یا متغیر محیطی).")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(CommandHandler("logo", cmd_logo))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("ربات روشن شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
