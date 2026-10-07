#!/usr/bin/env python3
"""
ربات تلگرام: اسکن مرحله‌ای سایت‌ها، ارسال تیتر + عکس ویدیوها، دانلود با دکمه، ذخیره علاقه‌مندی‌ها.
فایل ویدیو روی سرور نگه داشته نمی‌شود (فایل موقت بلافاصله بعد از ارسال پاک می‌شود).
"""
import asyncio, hashlib, html as htmlmod, io, json, logging, os, re, tempfile, time, unicodedata, zipfile
from collections import deque
from urllib.parse import quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x}
DEFAULT_URL = os.environ.get("START_URL", "https://www.soolakhi.com/")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5000"))
BATCH = int(os.environ.get("BATCH", "10"))  # تعداد ویدیو در هر صفحه
# اگر Local Bot API Server داری، آدرسش را بده تا محدودیت آپلود 50MB به 2GB برسد
LOCAL_API = os.environ.get("LOCAL_API")
# (پروکسی حذف شد) (مثلاً سایت‌هایی که IP کشور سرور را بسته‌اند): socks5://user:pass@host:port یا http://...
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAV_FILE = os.path.join(BASE_DIR, "favorites.json")
SITES_FILE = os.path.join(BASE_DIR, "sites.json")
BLOCK_FILE = os.path.join(BASE_DIR, "blocked.json")

VID_EXTS = "mp4|mkv|webm|mov|avi|m4v|m3u8|mpd|flv|wmv|3gp|ts|ogv|mpg|mpeg"
IMG_EXTS = "jpg|jpeg|png|gif|webp|avif|bmp|svg|jfif"
VIDEO_EXT = re.compile(rf"\.({VID_EXTS})(\?|$)", re.I)
IMG_EXT = re.compile(rf"\.({IMG_EXTS})(\?|$)", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

BTN_SITES, BTN_NEW, BTN_FAVS = "🌐 سایت‌های من", "➕ افزودن سایت", "⭐ ذخیره‌ها"
BTN_STOP, BTN_SEARCH, BTN_PANEL = "⏹ توقف", "🔎 جستجو", "⚙️ پنل"
MENU = ReplyKeyboardMarkup([[BTN_SEARCH], [BTN_SITES, BTN_NEW], [BTN_FAVS, BTN_PANEL], [BTN_STOP]],
                           resize_keyboard=True)
PAGE = 10  # تعداد آیتم در هر صفحه لیست‌ها
START_TIME = __import__("time").time()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

try:
    import lxml  # noqa: F401  — چند برابر سریع‌تر از html.parser
    PARSER = "lxml"
except ImportError:
    PARSER = "html.parser"

VIDEOS: dict[str, dict] = {}  # id -> {title, image, page, video}
EXTERNAL: dict[str, list] = {}  # page url -> لینک پلیرها/فایل‌هاست‌های خارجی
# فایل‌هاست‌ها: لینک .mp4 دارند ولی صفحه دانلود (کپچا/اشتراک) هستند نه فایل مستقیم
FILE_HOSTS = re.compile(r"(nitroflare|rapidgator|uploaded|katfile|ddownload|turbobit|filefactory|mega\.nz|"
                        r"1fichier|uptobox|k2s|keep2share|fboom|alfafile|hitfile|mexa|clicknupload)\.", re.I)
# کوتاه‌کننده‌های تبلیغاتی با کپچا/انتظار: دور زدنشان انجام نمی‌شود
CAPTCHA_HOSTS = re.compile(r"(ouo\.(io|press)|shrinkme|shrink\.|adf\.ly|linkvertise|exe\.io|exey\.io|"
                           r"shorte\.st|bc\.vc|clk\.sh|cuty\.io|gplinks|droplink|za\.gl|fc\.lc)", re.I)
PLAYER_HINT = re.compile(r"(player|vid|embed|stream|watch|play|tube|dood|filemoon|voe|streamtape)", re.I)
# پلیرهای embed شناخته‌شده: برای اینها http_download کارساز نیست → مستقیم yt-dlp
KNOWN_PLAYERS = re.compile(
    r"(dood(?:stream)?|streamtape|filemoon|voe\.sx?|upstream|mixdrop|fembed|"
    r"streamlare|vidcloud|mcloud|streamhub|vupload|streamzz|supervideo|"
    r"dailymotion|ok\.ru|vimeo\.com|youtu\.?be|vidhide|vidmoly|"
    r"gofile\.io|sendvid|odnoklassniki|aparat\.com|nazar)\.", re.I
)
CARD_HINTS: dict[str, dict] = {}  # page url -> {title, image, dur, q} از کارت‌های صفحه لیست
PAGE_META: dict[str, dict] = {}  # page url -> {dur, q}: مدت (ثانیه) و کیفیت ویدیوی صفحه


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


FAVS: dict[str, dict] = load_json(FAV_FILE, {})  # فقط متن (تیتر/لینک)؛ حجمش ناچیز است
# سایت‌های ذخیره‌شده: id -> {name, url}
SITES: dict[str, dict] = load_json(SITES_FILE, None)
if SITES is None:
    SITES = {}
    SITES[hashlib.md5(DEFAULT_URL.encode()).hexdigest()[:12]] = {"name": urlparse(DEFAULT_URL).netloc, "url": DEFAULT_URL}
    save_json(SITES_FILE, SITES)


BLOCKED: list[str] = load_json(BLOCK_FILE, [])  # دامنه‌هایی که در جستجو نمی‌آیند


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.").removeprefix("m.")


def is_blocked(url: str) -> bool:
    h = host_of(url)
    return any(h == b or h.endswith("." + b) for b in BLOCKED)


SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
_settings = load_json(SETTINGS_FILE, {})
BATCH = int(_settings.get("batch", BATCH))


WATCH = {"on": bool(_settings.get("watch_on", False)), "hours": int(_settings.get("watch_hours", 6)),
         "chat": _settings.get("watch_chat"), "last": _settings.get("watch_last", 0)}
SEEN_FILE = os.path.join(BASE_DIR, "seen.json")
SEEN: dict[str, list] = load_json(SEEN_FILE, {})  # site_id -> idهای ویدیوهای دیده‌شده
WATCH_PAGES = int(os.environ.get("WATCH_PAGES", "40"))  # صفحات بررسی‌شده هر سایت در هر دور
WATCH_MAX_SEND = 15  # حداکثر ویدیوی جدید ارسالی برای هر سایت در هر دور


# 👁 دیده‌شده‌ها: id -> {"k": "d" (دانلود شده) | "s" (علامت دیدم), "t": زمان}
VIEWED_FILE = os.path.join(BASE_DIR, "viewed.json")
VIEWED: dict[str, dict] = load_json(VIEWED_FILE, {})
VIEWED_MAX = 20000
HIDE_SEEN = bool(_settings.get("hide_seen", False))       # دیده‌شده‌ها در اسکن‌های بعدی نیایند
DUR_FILTER = _settings.get("dur_filter", "all")           # all / short / long
DUR_SPLIT = 10 * 60                                        # مرز کوتاه/بلند: ۱۰ دقیقه
DUR_LABEL = {"all": "همه", "short": "کوتاه ≤۱۰ دقیقه", "long": "بلند >۱۰ دقیقه"}

# 🗂 پوشه‌های ذخیره‌ها: {"next": n, "names": {fid: name}}؛ پوشهٔ هر ویدیو در FAVS[i]["folder"]
FOLDERS_FILE = os.path.join(BASE_DIR, "folders.json")
FOLDERS: dict = load_json(FOLDERS_FILE, {})
FOLDERS.setdefault("next", 1)
FOLDERS.setdefault("names", {})


def save_settings():
    save_json(SETTINGS_FILE, {"batch": BATCH, "concurrency": CONCURRENCY, "watch_on": WATCH["on"],
                              "watch_hours": WATCH["hours"], "watch_chat": WATCH["chat"], "watch_last": WATCH["last"],
                              "hide_seen": HIDE_SEEN, "dur_filter": DUR_FILTER})


def mark_viewed(i: str, kind: str = "s"):
    import time
    old = VIEWED.get(i, {})
    VIEWED[i] = {"k": "d" if "d" in (kind, old.get("k")) else "s", "t": time.time()}
    if len(VIEWED) > VIEWED_MAX:
        for k, _ in sorted(VIEWED.items(), key=lambda kv: kv[1].get("t", 0))[:len(VIEWED) - VIEWED_MAX]:
            VIEWED.pop(k, None)
    save_json(VIEWED_FILE, VIEWED)


def unmark_viewed(i: str):
    if VIEWED.pop(i, None) is not None:
        save_json(VIEWED_FILE, VIEWED)


def parse_dur(val) -> int | None:
    """مدت ویدیو به ثانیه از «PT12M30S»، «12:30»، «1:02:03» یا عدد ثانیه."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        sec = int(val)
    else:
        t = str(val).strip()
        m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", t, re.I)
        if m and any(m.groups()):
            d, h, mi, se = (float(x or 0) for x in m.groups())
            sec = int(d * 86400 + h * 3600 + mi * 60 + se)
        elif re.fullmatch(r"\d{1,2}(:\d{2}){1,2}", t):
            sec = 0
            for part in t.split(":"):
                sec = sec * 60 + int(part)
        elif re.fullmatch(r"\d+(\.\d+)?", t):
            sec = int(float(t))
        else:
            return None
    return sec if 0 < sec < 24 * 3600 else None


def fmt_dur(sec) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s_ = divmod(rem, 60)
    return f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"


QUALITY_RE = re.compile(r"[_\-/.](2160|1440|1080|720|480|360|240)p?(?=[_\-/.]|$)", re.I)   # داخل مسیر لینک
CARD_Q_RE = re.compile(r"(?<![\d])(2160|1440|1080|720|480|360)p(?![\d])|\b(4K|UHD|FHD|HD)\b", re.I)  # برچسب کارت


def quality_of(urls, height=None) -> str | None:
    """کیفیت از ارتفاع ویدیو (متاتگ/JSON-LD) یا از عدد داخل لینک فایل (مثل video_720p.mp4)."""
    try:
        h = int(str(height).strip()) if height else 0
    except ValueError:
        h = 0
    if not h:
        found = [int(m.group(1)) for u in urls for m in QUALITY_RE.finditer(urlparse(u).path)]
        h = max(found, default=0)
    return ("4K" if h >= 2160 else f"{h}p") if h >= 240 else None


def visible(i: str) -> bool:
    """فیلترهای کاربر: مخفی کردن دیده‌شده‌ها و فیلتر مدت (ویدیوهای بی‌مدت همیشه نمایش داده می‌شوند)."""
    if HIDE_SEEN and i in VIEWED:
        return False
    d = (VIDEOS.get(i) or {}).get("dur")
    if DUR_FILTER != "all" and d:
        return d <= DUR_SPLIT if DUR_FILTER == "short" else d > DUR_SPLIT
    return True


def save_folders():
    save_json(FOLDERS_FILE, FOLDERS)


def folder_name(fid) -> str | None:
    return FOLDERS["names"].get(str(fid)) if fid else None


# ----------------------------- جستجو: یکسان‌سازی حروف -----------------------------
# ي↔ی ، ك↔ک ، همزه‌ها، و حذف نیم‌فاصله/اعراب تا جستجو به شکل نوشتن حساس نباشد.
NORM_MAP = str.maketrans({"\u064a": "\u06cc", "\u0643": "\u06a9", "\u0629": "\u0647", "\u06c0": "\u0647",
                          "\u0623": "\u0627", "\u0625": "\u0627", "\u0622": "\u0627", "\u0671": "\u0627",
                          "\u0624": "\u0648", "\u0626": "\u06cc", "\u200c": " ", "\u200f": "", "\u200e": "",
                          "\u0640": ""})


def norm(t: str) -> str:
    t = unicodedata.normalize("NFKC", (t or "").translate(NORM_MAP)).lower()
    t = re.sub(r"[\u064b-\u0652]", "", t)              # اعراب عربی
    t = re.sub(r"[^0-9a-z\u0600-\u06ff]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def search_words(q: str):
    """کلمه‌ها را به مثبت و منفی (با پیشوند -) تقسیم می‌کند؛ هر دو یکسان‌سازی‌شده."""
    pos, neg = [], []
    for raw in re.split(r"\s+", (q or "").strip()):
        if len(raw) > 1 and raw[0] == "-":
            neg += [w for w in norm(raw[1:]).split() if len(w) > 1]
        else:
            pos += [w for w in norm(raw).split() if len(w) > 1]
    return pos, neg


def match_score(text_norm: str, pos, neg) -> int:
    """امتیاز = تعداد کلمه‌های مثبتی که (حتی به‌صورت بخشی از کلمه) پیدا شدند؛ کلمهٔ منفی = رد."""
    if not pos or any(n in text_norm for n in neg):
        return 0
    return sum(1 for p in pos if p in text_norm)


# ----------------------------- 🗃 فهرست محلی ویدیوها -----------------------------
# هر ویدیویی که در اسکن/بررسی دیده می‌شود این‌جا (فقط متن و لینک) ذخیره می‌شود تا جستجو
# فوری و بدون باز کردن سایت انجام شود. حجمش ناچیز است و قدیمی‌ترها خودکار حذف می‌شوند.
INDEX_FILE = os.path.join(BASE_DIR, "index.json")
INDEX: dict[str, dict] = load_json(INDEX_FILE, {})
INDEX_MAX = 40000
_index_dirty = 0
_INDEX_KEYS = ("title", "image", "page", "video", "ext", "dur", "q")


def index_add(i: str, card: dict):
    global _index_dirty
    INDEX[i] = {k: card.get(k) for k in _INDEX_KEYS}
    INDEX[i]["n"] = norm((card.get("title") or "") + " " + unquote(card.get("page") or ""))
    INDEX[i]["ts"] = time.time()
    _index_dirty += 1


def flush_index():
    global _index_dirty
    if not _index_dirty:
        return
    if len(INDEX) > INDEX_MAX:
        for k in sorted(INDEX, key=lambda k: INDEX[k].get("ts", 0))[:len(INDEX) - INDEX_MAX]:
            INDEX.pop(k, None)
    save_json(INDEX_FILE, INDEX)
    _index_dirty = 0


def index_search(query: str, limit: int = 120) -> list[str]:
    pos, neg = search_words(query)
    if not pos:
        return []
    scored = []
    for i, v in INDEX.items():
        sc = match_score(v.get("n") or norm(v.get("title", "")), pos, neg)
        if sc:
            scored.append((sc, v.get("ts", 0), i))
    scored.sort(key=lambda x: (-x[0], -x[1]))        # بیشترین کلمهٔ جورشده، بعد تازه‌ترین
    return [i for _, _, i in scored[:limit]]


def index_card(i: str) -> dict | None:
    v = VIDEOS.get(i) or INDEX.get(i) or FAVS.get(i)
    if v and i not in VIDEOS:
        VIDEOS[i] = {k: val for k, val in v.items() if k not in ("n", "ts")}
    return VIDEOS.get(i)


RESULTS_PER = 10


def build_results(ud, page=0):
    r = ud.get("res") or {}
    ids, query = r.get("ids") or [], r.get("q", "")
    pages = max(1, -(-len(ids) // RESULTS_PER))
    page = max(0, min(page, pages - 1))
    chunk = ids[page * RESULTS_PER:(page + 1) * RESULTS_PER]
    lines = [f"🔎 «{query}» — {len(ids)} نتیجه از فهرست محلی:", ""]
    nums = []
    for off, i in enumerate(chunk):
        n = page * RESULTS_PER + off + 1
        v = INDEX.get(i) or VIDEOS.get(i) or FAVS.get(i) or {}
        info = " • ".join(x for x in (fmt_dur(v["dur"]) if v.get("dur") else None, v.get("q")) if x)
        mark = ("📥" if (VIEWED.get(i) or {}).get("k") == "d" else "👁") if i in VIEWED else ""
        host = urlparse(v.get("page", "")).netloc.removeprefix("www.")
        lines.append(f"{n}. {mark}{(v.get('title') or '?')[:72]}" + (f"\n    ⏱ {info}" if info else "")
                     + (f"  · {host}" if host else ""))
        nums.append(InlineKeyboardButton(str(n), callback_data=f"pick:{i}"))
    rows = [nums[k:k + 5] for k in range(0, len(nums), 5)]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"rpg:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop:x"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"rpg:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔎 جستجوی زنده در سایت‌ها (نتایج بیشتر)", callback_data="rlive:x")])
    lines.append("\n👇 عدد هر ویدیو رو بزن تا کارت کاملش باز شه.")
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(rows)


def save_favs():
    save_json(FAV_FILE, FAVS)


def site_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def vid_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]  # callback_data حداکثر 64 بایت


def _attr_urls(tag, names):
    for n in names:
        val = tag.get(n)
        if isinstance(val, list):
            val = " ".join(val)
        if val and not val.strip().startswith("data:"):
            for part in val.split(","):  # srcset: "a.jpg 1x, b.jpg 2x"
                part = part.strip().split(" ")[0]
                if part and not part.startswith("data:"):
                    yield part


PLACEHOLDER = re.compile(r"(blank|spacer|pixel|placeholder|lazy|loading|loader|transparent|1x1|dummy|"
                         r"no-?image|noimg|no-?thumb|default)[^/]*\.(gif|png|svg|jpe?g|webp)", re.I)
LAZY_ATTRS = ["data-src", "data-lazy-src", "data-original", "data-lazy", "data-thumb_url", "data-thumb", "data-thumbnail",
              "data-preview", "data-image", "data-webp", "data-echo", "data-hi-res-src", "data-poster", "data-bg",
              "data-srcset", "srcset", "src", "poster"]
CARD_JUNK = re.compile(r"(\b\d{1,2}:\d{2}(:\d{2})?\b|\b(2160|1440|1080|720|480|360)p\b|\b(4K|UHD|FHD|HD)\b|"
                       r"\b[\d.,]+\s*[KkMm]?\s*(views?|بازدید)\b|\b\d+\s*(seconds?|minutes?|hours?|days?|weeks?|months?|years?)"
                       r"\s+ago\b|\b\d+%|\bnew\b)", re.I)
GENERIC_TITLE = re.compile(r"^(home|homepage|index|video|videos|watch|play|untitled|page not found|404.*|not found|"
                           r"just a moment.*|attention required.*|access denied.*|خانه|صفحه اصلی|ویدیو|فیلم|thumbnail|"
                           r"image|photo|poster|preview|cover)$", re.I)


def _pick_src(val: str, is_set: bool = True) -> str | None:
    """از src یا srcset، بزرگ‌ترین عکس واقعی (نه placeholder یا data:)."""
    val = val.strip()
    if val.startswith("data:"):
        return None
    best, best_w = None, -1
    for part in (val.split(",") if is_set else [val]):
        bits = part.strip().split()
        if not bits or bits[0].startswith("data:") or PLACEHOLDER.search(bits[0]):
            continue
        w = 0
        if len(bits) > 1 and re.fullmatch(r"\d+(\.\d+)?[wx]", bits[1]):
            w = float(bits[1][:-1])
        if w > best_w:
            best, best_w = bits[0], w
    return best


def best_img(tag, J) -> str | None:
    """عکس واقعیِ یک تگ img/picture/div با در نظر گرفتن lazy-load و srcset و background-image."""
    if tag is None:
        return None
    cands = [tag]
    pic = tag.find_parent("picture") if tag.name == "img" else None
    if pic is not None:
        cands = [tag] + pic.find_all("source")
    for t in cands:
        for n in LAZY_ATTRS:
            val = t.get(n)
            if isinstance(val, list):
                val = " ".join(val)
            if val and isinstance(val, str):
                u = _pick_src(val, "srcset" in n)
                if u and not looks_generic(u):
                    return J(u)
    m = re.search(r"url\(['\"]?([^'\")]+)['\"]?\)", tag.get("style") or "")
    if m and not m.group(1).startswith("data:") and not PLACEHOLDER.search(m.group(1)):
        return J(m.group(1))
    return None


def _many_cards(box, u, base) -> bool:
    """آیا این بخش از صفحه بیش از یک کارت را در بر دارد؟ (تا تیتر/عکس کارت کناری برداشته نشود)"""
    for a in box.find_all("a", href=True, limit=40):
        h = urljoin(base, a["href"]).split("#")[0]
        if h != u and h != base and urlparse(h).netloc == urlparse(u).netloc and a.find("img") is not None:
            return True
    return False


def bad_title(t: str | None, site_name: str | None = None) -> bool:
    t = (t or "").strip()
    if len(t) < 4 or t.startswith("http") or GENERIC_TITLE.match(t):
        return True
    return bool(site_name) and t.lower() == site_name.strip().lower()


def title_from_url(url: str) -> str:
    slug = unquote(urlparse(url).path.rstrip("/").split("/")[-1] or "")
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    slug = re.sub(r"^\d+[-_]|[-_]\d{4,}$", "", slug)
    return re.sub(r"[-_+]+", " ", slug).strip()[:200]


def clean_card_title(t: str) -> str:
    t = CARD_JUNK.sub(" ", t or "")
    return re.sub(r"\s+", " ", t).strip(" -|•·")


SRC_ATTRS = ["src", "data-src", "data-lazy-src", "data-original", "data-url", "data-video", "data-video-src",
             "data-mp4", "data-file", "data-hls", "data-stream", "srcset", "data-srcset", "href", "content"]
IMG_ATTRS = ["poster", "data-poster", "data-thumb", "data-thumbnail", "data-preview", "data-image", "data-bg",
             "src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"]


def parse_page(url: str, html: str, domain: str):
    soup = BeautifulSoup(html, PARSER)
    site_name = (soup.find("meta", property="og:site_name") or {}).get("content")
    meta = lambda p: (soup.find("meta", property=p) or soup.find("meta", attrs={"name": p}) or {}).get("content")
    J = lambda u: urljoin(url, u.strip())

    title = image = None
    videos = set()
    dur = height = None

    # 1) JSON-LD (VideoObject) — دقیق‌ترین منبع تیتر/عکس/ویدیو
    for sc in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d); continue
            if not isinstance(d, dict):
                continue
            stack.extend(v for v in d.values() if isinstance(v, (dict, list)))
            if "VideoObject" in str(d.get("@type")):
                title = title or (htmlmod.unescape(d["name"]) if isinstance(d.get("name"), str) else None)
                dur = dur or parse_dur(d.get("duration"))
                height = height or d.get("height") or d.get("videoQuality")
                th = d.get("thumbnailUrl") or d.get("thumbnail")
                if isinstance(th, list): th = th[0] if th else None
                if isinstance(th, dict): th = th.get("url") or th.get("contentUrl")
                if isinstance(th, str) and not looks_generic(th): image = image or J(th)
                for k in ("contentUrl", "embedUrl"):
                    u = d.get(k)
                    # embedUrl گاهی خود همین صفحه است → ویدیو حساب نشود
                    if isinstance(u, str) and J(u).rstrip("/") != url.rstrip("/") and \
                            (VIDEO_EXT.search(u) or urlparse(J(u)).netloc.removeprefix("www.") != domain):
                        videos.add(J(u))

    # 2) متاتگ‌ها
    for cand in (meta("og:title"), meta("twitter:title")):
        if bad_title(title, site_name) and cand:
            title = cand
    for cand in (meta("og:image"), meta("og:image:url"), meta("twitter:image"), meta("thumbnailUrl")):
        if not image and cand and not looks_generic(cand):
            image = cand
    for p in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        if meta(p): videos.add(J(meta(p)))
    dur = dur or parse_dur(meta("video:duration") or meta("og:video:duration"))
    if not dur:
        it = soup.find(attrs={"itemprop": "duration"})
        dur = parse_dur(it.get("content") or it.get_text(strip=True)) if it else None
    height = height or meta("og:video:height")

    # 3) تگ‌های video/source/a و هر attribute با پسوند ویدیو
    for v in soup.find_all(["video", "source", "track", "embed", "object"]):
        for u in _attr_urls(v, SRC_ATTRS + ["data"]):
            if v.name in ("video", "source") or VIDEO_EXT.search(u): videos.add(J(u))
        if not image:
            for u in _attr_urls(v, ["poster", "data-poster", "data-thumb"]):
                image = J(u); break
    for t in soup.find_all(True):
        for n, val in t.attrs.items():
            if isinstance(val, str) and VIDEO_EXT.search(val) and not val.startswith("data:"):
                videos.add(J(val.split(",")[0].split(" ")[0]))
    # 4) لینک‌های ویدیو داخل جاوااسکریپت/JSON (مثل jwplayer, videojs, "file": "...")
    for m in re.findall(rf"""(https?:)?(\\?/\\?/[^"'\s<>]+?\.(?:{VID_EXTS})(?:\?[^"'\s<>]*)?)["'\s]""", html, re.I):
        videos.add(J((m[0] or "https:") + m[1].replace("\\/", "/")))
    for m in re.findall(r"""["'](?:file|src|source|url|video_url|mp4|hls|stream|m3u8)["']\s*:\s*["']([^"']+)["']""", html):
        if VIDEO_EXT.search(m): videos.add(J(m.replace("\\/", "/")))
    # الگوهای رایج پلیرهای سفارشی (takcdn، آپارات، و مشابه)
    for m in re.finditer(
            r"""(?:sources?|playlist|videoUrl|videoSrc|hlsUrl|file)\s*[:=]\s*['\"]([^'"]{10,}\.(?:m3u8|mp4|mpd)[^'\"]*)['\"]""",
            html, re.I):
        videos.add(J(m.group(1).replace("\\/", "/")))

    # 5) iframe پلیرها
    iframes = [J(u) for f in soup.find_all("iframe") for u in _attr_urls(f, ["src", "data-src"])
               if not re.search(r"(google|facebook|twitter|disqus|recaptcha|doubleclick|ads)", u, re.I)]

    # تیتر و عکس جایگزین
    if bad_title(title, site_name):
        h = soup.find("h1") or soup.find("h2")
        h = h.get_text(" ", strip=True) if h else ""
        page_t = soup.title.get_text(strip=True) if soup.title else ""
        title = next((x for x in (h, page_t) if not bad_title(clean_title(x, site_name), site_name)), title or url)
    if not image:
        for img in soup.find_all("img"):
            u = best_img(img, J)
            if u and not re.search(r"(logo|icon|avatar|gravatar|sprite|banner|emoji|(?<![a-z])ads?[/_-])", u, re.I) \
                    and not (img.get("width", "999").isdigit() and int(img.get("width", "999")) < 100):
                image = u
                break
    if not image:  # background-image: url(...)
        m = re.search(rf"""url\(["']?([^"')]+\.(?:{IMG_EXTS})[^"')]*)["']?\)""", html, re.I)
        if m: image = J(m.group(1))

    links = set()
    for a in soup.find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0]
        if urlparse(u).netloc.removeprefix("www.") == domain and not VIDEO_EXT.search(u) and not IMG_EXT.search(u) \
                and not re.search(r"\.(pdf|zip|rar|css|js|xml|json)(\?|$)", u, re.I):
            links.add(u)
    videos = {v for v in videos if v.startswith("http")}
    ext = [v for v in videos if FILE_HOSTS.search(v)]
    videos -= set(ext)
    for a in soup.find_all("a", href=True):  # دکمه‌های «پخش/دانلود» به سایت‌های دیگر
        u = J(a["href"])
        host = urlparse(u).netloc.removeprefix("www.")
        if u.startswith("http") and host and host != domain and u not in ext and \
                (FILE_HOSTS.search(u) or PLAYER_HINT.search(host) or
                 re.search(r"(download|direct|player|play|دانلود|پخش)", " ".join(a.get("class", [])) + a.get_text(), re.I)):
            if not re.search(r"(t\.me|telegram|instagram|twitter|x\.com|facebook|whatsapp|google)", host, re.I):
                ext.append(u)
    EXTERNAL[url] = ext[:6]

    # کارت‌های صفحه لیست: لینک + عکس بندانگشتی + عنوان هر ویدیو
    for a in soup.find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0]
        if u not in links or u == url:
            continue
        img = a.find("img")
        container = a
        if not img:  # گاهی عکس و لینک عنوان جدا هستند ولی در یک کارت مشترک
            for _ in range(3):
                container = container.parent
                if container is None or _many_cards(container, u, url): break
                img = container.find("img")
                if img: break
        if not img:
            continue
        thumb = best_img(img, J)
        # عنوان کارت به ترتیب اطمینان: title لینک، تیتر h داخل کارت، alt عکس، متن لینک (بدون مدت/بازدید/HD)
        t = a.get("title") or ""
        if bad_title(t):
            box = a
            for _ in range(3):  # عنوان کارت معمولاً در h2/h3 کنار عکس است
                if box is None or _many_cards(box, u, url): break
                h = box.find(["h1", "h2", "h3", "h4", "h5", "h6"]) or box.find(class_=re.compile(r"title|caption", re.I))
                if h and not bad_title(clean_card_title(h.get_text(" ", strip=True))):
                    t = clean_card_title(h.get_text(" ", strip=True)); break
                box = box.parent
        if bad_title(t):
            alt = img.get("alt") or img.get("title") or ""
            t = alt if not bad_title(alt) and not IMG_EXT.search(alt) else ""
        if bad_title(t):
            t = clean_card_title(a.get_text(" ", strip=True))
            t = t if not bad_title(t) else ""
        if thumb or t:
            old = CARD_HINTS.get(u, {})
            # مدت/کیفیت روی خود کارت (مثل «12:30» و «HD»)؛ فقط از متن کوتاه همان کارت
            box_txt = a.get_text(" ", strip=True)
            if a.parent is not None and len(a.parent.get_text(" ", strip=True)) < 300:
                box_txt = a.parent.get_text(" ", strip=True)
            mdur = re.search(r"(?<![\d:])(\d{1,2}:\d{2}(?::\d{2})?)(?![\d:])", box_txt)
            mq = CARD_Q_RE.search(box_txt)
            CARD_HINTS[u] = {"title": old.get("title") or clean_title(t or "", site_name), "image": old.get("image") or thumb,
                             "dur": old.get("dur") or (parse_dur(mdur.group(1)) if mdur else None),
                             "q": old.get("q") or ((f"{mq.group(1)}p" if mq.group(1) else mq.group(2).upper()) if mq else None)}
    image = J(image) if image else None
    PAGE_META[url] = {"dur": dur, "q": quality_of(videos, height)}
    return clean_title(title, site_name)[:300], image, videos, iframes, links


def clean_title(t: str, site_name: str | None) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    parts = re.split(r"\s+[|\-–—»«:]\s+", t)
    if len(parts) > 1:
        sn = (site_name or "").strip().lower()
        keep = [p for p in parts if p.strip().lower() != sn and not (sn and sn in p.lower() and len(p) < len(sn) + 6)]
        t = max(keep or parts, key=len) if not sn else " - ".join(keep or parts)
    return t


# ----------------------------- دریافت هوشمند صفحه -----------------------------
# بعضی سایت‌ها جلوی ربات‌ها را می‌گیرند (Cloudflare، DDoS-Guard، صفحهٔ «Just a moment» یا 403).
# در این حالت همان صفحه با شبیه‌سازی کامل مرورگر کروم (curl_cffi) دوباره گرفته می‌شود و
# سایت به خاطر سپرده می‌شود تا دفعه‌های بعد مستقیم همین روش استفاده شود.
BLOCK_SIGNS = re.compile(r"(cf-chl|challenge-platform|cf-browser-verification|Just a moment\.\.\.|Attention Required!|"
                         r"DDoS-Guard|ddos-guard|Checking your browser|enable JavaScript and cookies to continue|"
                         r"sucuri_cloudproxy|Access denied \||_Incapsula_Resource|bot verification)", re.I)
IMPERSONATE: set[str] = set()         # دامنه‌هایی که فقط با شبیه‌سازی مرورگر جواب می‌دهند
FETCH_STATS: dict[str, dict] = {}     # domain -> {ok, bypass, blocked, err, codes}
_CURL = None


def _curl():
    global _CURL
    if _CURL is None:
        from curl_cffi.requests import AsyncSession
        _CURL = AsyncSession(impersonate="chrome124", timeout=30, allow_redirects=True, max_clients=16)
    return _CURL


def looks_blocked(status: int, text: str) -> bool:
    if status in (403, 429, 503) or 520 <= status <= 530:
        return True
    return status == 200 and len(text) < 60000 and bool(BLOCK_SIGNS.search(text[:60000]))


def _stat(domain, key, code=None):
    st = FETCH_STATS.setdefault(domain, {"ok": 0, "bypass": 0, "blocked": 0, "err": 0, "codes": {}})
    st[key] += 1
    if code:
        st["codes"][str(code)] = st["codes"].get(str(code), 0) + 1


async def _curl_get(url, referer=None):
    from types import SimpleNamespace
    r = await _curl().get(url, headers={"Referer": referer} if referer else None)
    return SimpleNamespace(status_code=r.status_code, text=r.text, content=r.content, url=str(r.url),
                           headers={k.lower(): v for k, v in r.headers.items()})


async def smart_get(c, url, referer=None):
    """صفحه را می‌گیرد؛ اگر سایت جلوی ربات را گرفت، با شبیه‌سازی مرورگر دوباره امتحان می‌کند."""
    dom = host_of(url)
    if dom not in IMPERSONATE:
        try:
            r = await c.get(url, headers={"Referer": referer} if referer else None)
            if not looks_blocked(r.status_code, r.text if "html" in r.headers.get("content-type", "") else ""):
                _stat(dom, "ok")
                return r
            code = r.status_code
        except Exception as e:
            code = type(e).__name__
    else:
        code = "remembered"
    try:
        r2 = await _curl_get(url, referer)
    except Exception as e:
        _stat(dom, "err", type(e).__name__)
        if code == "remembered":
            raise
        raise RuntimeError(f"blocked ({code}); browser mode failed: {type(e).__name__}") from e
    if looks_blocked(r2.status_code, r2.text if "html" in r2.headers.get("content-type", "") else ""):
        _stat(dom, "blocked", r2.status_code)
        return r2
    if code != "remembered":
        log.info("browser mode works for %s (was %s)", dom, code)
    IMPERSONATE.add(dom)
    _stat(dom, "bypass")
    return r2


def looks_generic(img: str) -> bool:
    return bool(re.search(r"(logo|icon|favicon|default|placeholder|no-?image|share|og-image|gravatar|avatar)", img, re.I))


async def ytdlp_info(url: str):
    """برای صفحاتی که ویدیو داخل iframe/پلیر جاوااسکریپتی است: تیتر و عکس را yt-dlp استخراج کند."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "-J", "--no-playlist", "--skip-download", "--no-warnings", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
        except asyncio.TimeoutError:
            proc.kill()
            return None, None
        if proc.returncode == 0:
            d = json.loads(out)
            pm = PAGE_META.setdefault(url, {})
            pm["dur"] = pm.get("dur") or parse_dur(d.get("duration"))
            pm["q"] = pm.get("q") or quality_of([], d.get("height"))
            return d.get("title"), d.get("thumbnail")
    except Exception:
        pass
    return None, None


CONCURRENCY = int(_settings.get("concurrency", os.environ.get("CONCURRENCY", "8")))  # صفحات همزمان
# صفحاتی که معمولاً ویدیو ندارند (اول بقیه بررسی می‌شوند، این‌ها آخر صف)
LOW_PRIORITY = re.compile(r"/(tag|tags|category|categories|author|page|archive|date|search|label|cat|actors?|"
                          r"models?|pornstars?|channels?)(/|\?|$)|[?&](page|paged|p)=\d", re.I)
# صفحاتی که اصلاً ارزش باز کردن ندارند
SKIP_URL = re.compile(r"(/wp-(admin|login|json)|/feed/?$|/xmlrpc|/cart|/checkout|/my-account|/login|/register|"
                      r"/signup|/contact|/privacy|/terms|/dmca|/about|replytocom=|/comment-page-|\?share=|"
                      r"/cdn-cgi/|/amp/?$|\.(rss|atom)$)", re.I)


class Crawler:
    """اسکن مرحله‌ای و همزمان: هر بار تا پیدا شدن BATCH ویدیو جلو می‌رود.
    صفحات احتمالاً ویدیویی (کارت‌های دارای عکس) جلوتر از صفحات دسته/تگ بررسی می‌شوند."""
    def __init__(self, start_url: str, max_pages: int | None = None, batch: int | None = None):
        self.domain = urlparse(start_url).netloc.removeprefix("www.")
        self.max_pages, self.batch = max_pages, batch
        self.seen, self.pages = set(), 0
        self.hi, self.mid, self.lo = deque([start_url]), deque(), deque()
        self.sent: set[str] = set()

    def _push(self, links):
        for l in links:
            if l in self.seen or SKIP_URL.search(l):
                continue
            self.seen.add(l)
            if l in CARD_HINTS:
                self.hi.append(l)       # کارت با عکس → به احتمال زیاد صفحه ویدیو
            elif LOW_PRIORITY.search(l):
                self.lo.append(l)
            else:
                self.mid.append(l)

    def _pop(self):
        for q in (self.hi, self.mid, self.lo):
            if q:
                return q.popleft()

    async def _fetch(self, c, url, found):
        try:
            r = await smart_get(c, url)
            if "text/html" not in r.headers.get("content-type", ""):
                return []
        except Exception as e:
            log.warning("fetch fail %s: %s", url, e)
            return []
        return await process_page(url, r.text, self.domain, self.sent, found)

    async def next_batch(self):
        found = []
        self.seen.update(self.hi)
        limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20, limits=limits) as c:
            while (self.hi or self.mid or self.lo) and self.pages < (self.max_pages or MAX_PAGES) \
                    and len(found) < (self.batch or BATCH):
                batch = []
                while len(batch) < CONCURRENCY and (self.hi or self.mid or self.lo):
                    batch.append(self._pop())
                self.pages += len(batch)
                for links in await asyncio.gather(*(self._fetch(c, u, found) for u in batch)):
                    self._push(links)
        return found

    @property
    def done(self):
        return not (self.hi or self.mid or self.lo) or self.pages >= MAX_PAGES


async def process_page(url, html, domain, sent: set, found: list, query_words=None):
    """یک صفحه را پارس می‌کند، ویدیوها را در VIDEOS ثبت و idهای جدید را به found اضافه می‌کند."""
    if True:
        if True:
            if True:
                title, image, videos, iframes, links = await asyncio.to_thread(parse_page, url, html, domain)
                if not videos and iframes:
                    # پلیر embed شناخته‌شده اول؛ در غیر این صورت خود صفحه
                    player = next((u for u in iframes if KNOWN_PLAYERS.search(urlparse(u).netloc)), None)
                    target = player or url
                    videos = {target}
                    if not image:
                        t2, img2 = await ytdlp_info(target)
                        title, image = t2 or title, img2 or image
                hint = CARD_HINTS.pop(url, {})
                pm = PAGE_META.pop(url, {})
                dur, q = pm.get("dur") or hint.get("dur"), pm.get("q") or hint.get("q")
                if hint.get("image") and (not image or looks_generic(image)):
                    image = hint["image"]
                if hint.get("title") and bad_title(title):
                    title = hint["title"]
                if bad_title(title):
                    title = title_from_url(url) or title
                ext = EXTERNAL.pop(url, [])
                if not videos and ext:
                    videos = {url}  # فقط لینک پلیر/فایل‌هاست دارد → کارت با دکمه‌های لینک
                if query_words:
                    pos, neg = query_words
                    if videos and not match_score(norm(f"{title} {unquote(url)}"), pos, neg):
                        videos = set()  # نتیجه جستجو به کلمه ربطی ندارد
                if videos and urlparse(url).path.strip("/") == "" and len(links) >= 5:
                    videos = set()      # صفحهٔ اصلی سایت یک فهرست است، نه یک ویدیو
                if videos:
                    # هر صفحه = یک ویدیو؛ از بین فرمت‌ها/کیفیت‌ها یکی انتخاب می‌شود (نه چند کارت تکراری)
                    direct = [v for v in videos if VIDEO_EXT.search(v)]
                    mp4 = [v for v in direct if ".mp4" in v.lower()]
                    v = (mp4 or direct or sorted(videos))[0]
                    i = vid_id(url)
                    VIDEOS.setdefault(i, {"title": title, "image": image, "page": url, "video": v, "ext": ext,
                                          "dur": dur, "q": q})
                    index_add(i, VIDEOS[i])
                    if i not in sent:
                        sent.add(i)
                        found.append(i)
                return links


SEARCH_PATHS = ["?s={q}", "search/{q}/", "?q={q}", "search?q={q}", "videos/search?q={q}", "search/?query={q}"]
SPATH_FILE = os.path.join(BASE_DIR, "search_paths.json")
SPATHS: dict = load_json(SPATH_FILE, {})     # domain -> الگوی جستجویی که قبلاً جواب داد


class SearchCrawler:
    """جستجو: اول صفحات نتیجه (داخل سایت‌ها یا موتور جستجو) را می‌گیرد، بعد فقط همان نتایج را باز می‌کند."""
    def __init__(self, query: str, sites: list[str]):
        self.query, self.sites = query, sites
        self.domain = ("، ".join(urlparse(u).netloc.removeprefix("www.") for u in self.sites[:3])
                                        + (" ..." if len(self.sites) > 3 else ""))
        self.queue, self.seen, self.sent, self.pages, self.ready = deque(), set(), set(), 0, False
        self.pos, self.neg = search_words(query)

    async def _site_results(self, c, site: str):
        """صفحه جستجوی داخلی سایت را پیدا می‌کند (وردپرس ?s= و الگوهای رایج) و لینک نتایج را برمی‌گرداند."""
        base = site if site.endswith("/") else site + "/"
        dom = urlparse(base).netloc.removeprefix("www.")
        paths = SEARCH_PATHS
        if SPATHS.get(dom) in SEARCH_PATHS:          # الگوی به‌خاطرسپرده‌شده اول امتحان شود
            paths = [SPATHS[dom]] + [p for p in SEARCH_PATHS if p != SPATHS[dom]]
        for path in paths:
            u = urljoin(base, path.format(q=quote_plus(self.query)))
            try:
                r = await smart_get(c, u)
            except Exception:
                continue
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            before = set(CARD_HINTS)
            parse_page(str(r.url), r.text, dom)
            cards = [k for k in CARD_HINTS if k not in before and urlparse(k).netloc.removeprefix("www.") == dom]
            rel = [k for k in cards if match_score(norm((CARD_HINTS[k].get("title") or "") + " " + unquote(k)),
                                                   self.pos, self.neg)]
            if rel:
                if SPATHS.get(dom) != path:
                    SPATHS[dom] = path
                    save_json(SPATH_FILE, SPATHS)
                return rel
        return []

    async def _prepare(self, c):
        results = await asyncio.gather(*(self._site_results(c, s) for s in self.sites))
        for k in range(max((len(r) for r in results), default=0)):  # ترکیب نوبتی نتایج سایت‌ها
            for r in results:
                if k < len(r):
                    self.queue.append(r[k])
        self.ready = True

    async def next_batch(self):
        found = []
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as c:
            if not self.ready:
                await self._prepare(c)
            async def one(url):
                try:
                    r = await smart_get(c, url)
                    if "text/html" not in r.headers.get("content-type", ""):
                        return
                except Exception as e:
                    log.warning("fetch fail %s: %s", url, e)
                    return
                final = str(r.url)
                await process_page(final, r.text, urlparse(final).netloc.removeprefix("www."), self.sent, found,
                                   (self.pos, self.neg))

            while self.queue and len(found) < BATCH and self.pages < 150:
                batch = []
                while self.queue and len(batch) < CONCURRENCY:
                    url = self.queue.popleft()
                    if url not in self.seen and not is_blocked(url):
                        self.seen.add(url)
                        batch.append(url)
                self.pages += len(batch)
                await asyncio.gather(*(one(u) for u in batch))
        return found

    @property
    def done(self):
        return self.ready and (not self.queue or self.pages >= 150)


CRAWLERS: dict[int, Crawler] = {}  # chat_id -> وضعیت اسکن


def allowed(update: Update) -> bool:
    return not ALLOWED_USERS or update.effective_user.id in ALLOWED_USERS


def video_kb(i: str) -> InlineKeyboardMarkup:
    fav = InlineKeyboardButton("❌ حذف از ذخیره‌ها", callback_data=f"unfav:{i}") if i in FAVS \
        else InlineKeyboardButton("⭐ ذخیره", callback_data=f"fav:{i}")
    seen = InlineKeyboardButton("✅ دیده‌شده", callback_data=f"unseen:{i}") if i in VIEWED \
        else InlineKeyboardButton("👁 دیدمش", callback_data=f"seen:{i}")
    rows = [[InlineKeyboardButton("⬇️ دانلود", callback_data=f"dl:{i}"), fav, seen]]
    if i in FAVS:
        name = folder_name(FAVS[i].get("folder"))
        rows.append([InlineKeyboardButton(f"🗂 پوشه: {name}" if name else "🗂 گذاشتن در پوشه",
                                          callback_data=f"fold:{i}")])
    v = VIDEOS.get(i) or FAVS.get(i) or {}
    links = [InlineKeyboardButton(f"▶️ {urlparse(u).netloc.removeprefix('www.')[:20]}", url=u) for u in v.get("ext", [])]
    rows += [links[k:k + 2] for k in range(0, len(links), 2)]
    if v.get("page", "").startswith("http"):
        root = site_root(v["page"])
        if site_id(root) not in SITES:
            rows.append([InlineKeyboardButton(f"💾 ذخیره سایت {urlparse(root).netloc.removeprefix('www.')[:25]}",
                                              callback_data=f"savedom:{i}")])
        rows.append([InlineKeyboardButton(f"🚫 بلاک {host_of(root)[:25]}", callback_data=f"block:{i}")])
    return InlineKeyboardMarkup(rows)


def site_root(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/"


async def fetch_image(url: str, referer: str):
    """عکس را خودمان (با Referer) می‌گیریم و به JPEG تبدیل می‌کنیم؛ فقط در RAM و چند صد KB."""
    async with httpx.AsyncClient(headers={**HEADERS, "Referer": referer}, follow_redirects=True, timeout=20) as c:
        r = await smart_get(c, url, referer)
    if r.status_code >= 400 or not r.content or r.content[:15].lstrip().startswith((b"<", b"{")):
        r = await _curl_get(url, referer)        # hotlink بسته / صفحهٔ HTML به جای عکس → با مرورگر
        if r.status_code >= 400:
            raise ValueError(f"image HTTP {r.status_code}")
    if len(r.content) > 15 * 2**20:
        raise ValueError("image too big")
    im = Image.open(io.BytesIO(r.content))
    im = im.convert("RGB")
    im.thumbnail((1280, 1280))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    buf.seek(0)
    return buf


def folder_kb(i: str) -> InlineKeyboardMarkup:
    """انتخاب پوشه برای یک ویدیوی ذخیره‌شده (روی همان کارت)."""
    cur = str((FAVS.get(i) or {}).get("folder") or "")
    rows = [[InlineKeyboardButton(("✓ " if cur == fid else "") + f"📁 {name}", callback_data=f"mvf:{i}:{fid}")]
            for fid, name in FOLDERS["names"].items()]
    rows.append([InlineKeyboardButton(("✓ " if not cur else "") + "📂 بدون پوشه", callback_data=f"mvf:{i}:0")])
    rows.append([InlineKeyboardButton("➕ پوشهٔ جدید", callback_data=f"newfold:{i}"),
                 InlineKeyboardButton("⬅️ برگشت", callback_data=f"vkb:{i}")])
    return InlineKeyboardMarkup(rows)


def card_caption(i, v) -> str:
    info = " • ".join(x for x in (fmt_dur(v["dur"]) if v.get("dur") else None, v.get("q")) if x)
    seen = (VIEWED.get(i) or {}).get("k")
    lines = [f"🎬 {v['title']}"]
    if info:
        lines.append(f"⏱ {info}")
    if seen:
        lines.append("📥 قبلاً دانلود کردی" if seen == "d" else "👁 قبلاً دیدی")
    lines.append(f"🌐 {urlparse(v.get('page', '')).netloc.removeprefix('www.')}")
    return "\n".join(lines)[:1000]


async def send_card(chat_id, i, v, ctx):
    cap = card_caption(i, v)
    if v.get("image"):
        try:  # 1) تلگرام مستقیم از URL
            return await ctx.bot.send_photo(chat_id, v["image"], caption=cap, reply_markup=video_kb(i))
        except Exception as e:
            log.info("photo by URL failed (%s), fetching myself", e)
        try:  # 2) دانلود و تبدیل خودمان (برای سایت‌هایی که hotlink را می‌بندند یا فرمت webp/avif دارند)
            return await ctx.bot.send_photo(chat_id, await fetch_image(v["image"], v["page"]),
                                            caption=cap, reply_markup=video_kb(i))
        except Exception as e:
            log.warning("photo failed %s: %s", v["image"], e)
    await ctx.bot.send_message(chat_id, cap, reply_markup=video_kb(i))


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await update.message.reply_text("از دکمه‌های پایین استفاده کن، یا لینک هر سایتی رو بفرست تا اسکنش کنم.",
                                        reply_markup=MENU)


async def send_batch(chat_id, ctx: ContextTypes.DEFAULT_TYPE):
    cr = CRAWLERS.get(chat_id)
    if not cr:
        return
    stop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏹ توقف", callback_data="stop:x")]])
    msg = await ctx.bot.send_message(chat_id, f"🔄 در حال اسکن {cr.domain} ...", reply_markup=stop_kb)
    found, hidden, rounds, last = [], 0, 0, None
    while True:  # اگر فیلترها بیشتر نتایج را رد کردند، کمی جلوتر هم اسکن می‌شود
        task = asyncio.create_task(cr.next_batch())
        while not task.done():
            await asyncio.wait({task}, timeout=3)
            if not task.done() and cr.pages != last and CRAWLERS.get(chat_id) is cr:
                last = cr.pages
                try:
                    await msg.edit_text(f"🔄 در حال اسکن {cr.domain}\n📄 {cr.pages} صفحه بررسی شد ...",
                                        reply_markup=stop_kb)
                except Exception:
                    pass
        got = task.result()
        vis = [i for i in got if visible(i)]
        found += vis
        hidden += len(got) - len(vis)
        rounds += 1
        if len(found) >= max(1, BATCH // 2) or cr.done or rounds >= 4 or CRAWLERS.get(chat_id) is not cr:
            break
    flush_index()
    try:
        await msg.delete()
    except Exception:
        pass
    if CRAWLERS.get(chat_id) is not cr:
        return
    for i in found:
        if CRAWLERS.get(chat_id) is not cr:  # در این حین توقف یا اسکن جدید زده شده
            return
        await send_card(chat_id, i, VIDEOS[i], ctx)
        await asyncio.sleep(0.4)  # جلوگیری از flood limit تلگرام
    if cr.done:
        CRAWLERS.pop(chat_id, None)
        if isinstance(cr, SearchCrawler) and not cr.sent:
            await ctx.bot.send_message(chat_id, f"😕 برای «{cr.query}» ویدیویی پیدا نشد.")
        else:
            await ctx.bot.send_message(chat_id, f"✅ تمام شد ({cr.pages} صفحه بررسی شد)."
                                       + (f"\n🙈 {hidden} ویدیو با فیلترها رد شد (⚙️ پنل)" if hidden else ""))
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ صفحه بعد", callback_data="next")]])
        await ctx.bot.send_message(chat_id, f"{len(found)} ویدیو. ({cr.pages} صفحه اسکن شد)"
                                   + (f"\n🙈 {hidden} ویدیو با فیلترها رد شد (⚙️ پنل)" if hidden else ""),
                                   reply_markup=kb)


async def start_scan(chat_id, url, ctx):
    CRAWLERS[chat_id] = Crawler(url)
    await send_batch(chat_id, ctx)


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        url = ctx.args[0] if ctx.args else DEFAULT_URL
        await start_scan(update.effective_chat.id, url, ctx)


def pager(prefix, page, total):
    pages = max(1, -(-total // PAGE))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"{prefix}:{page - 1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop:x"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"{prefix}:{page + 1}"))
    return [nav] if nav else []


def sites_kb(page=0):
    items = list(SITES.items())
    page = max(0, min(page, (len(items) - 1) // PAGE if items else 0))
    rows = [[InlineKeyboardButton(f"🔍 {v['name']}", callback_data=f"scan:{i}"),
             InlineKeyboardButton("🗑", callback_data=f"delsite:{i}")] for i, v in items[page * PAGE:(page + 1) * PAGE]]
    rows += pager("sitespg", page, len(items))
    rows.append([InlineKeyboardButton(f"🚫 سایت‌های بلاک‌شده ({len(BLOCKED)})", callback_data="blocklist:x")])
    return InlineKeyboardMarkup(rows)


def blocked_kb():
    rows = [[InlineKeyboardButton(f"✅ آزاد کردن {d}", callback_data=f"unblock:{k}")] for k, d in enumerate(BLOCKED)]
    rows.append([InlineKeyboardButton("➕ بلاک دستی (اسم سایت رو بفرست)", callback_data="blockadd:x")])
    return InlineKeyboardMarkup(rows)


async def show_sites(chat_id, ctx):
    if not SITES:
        return await ctx.bot.send_message(chat_id, "هیچ سایتی ذخیره نشده. با «➕ افزودن سایت» اضافه کن.",
                                          reply_markup=sites_kb())
    await ctx.bot.send_message(chat_id, f"🌐 {len(SITES)} سایت ذخیره‌شده (برای اسکن بزن، 🗑 برای حذف):",
                               reply_markup=sites_kb())


def fmt_uptime():
    sec = int(__import__("time").time() - START_TIME)
    d, sec = divmod(sec, 86400); h, sec = divmod(sec, 3600); m = sec // 60
    return (f"{d} روز " if d else "") + f"{h} ساعت {m} دقیقه"


def panel_text():
    import shutil
    disk = shutil.disk_usage(BASE_DIR)
    return ("⚙️ پنل کنترل\n\n"
            f"🌐 سایت‌ها: {len(SITES)}\n⭐ ذخیره‌ها: {len(FAVS)}\n🚫 بلاک‌شده‌ها: {len(BLOCKED)}\n"
            f"🧠 ویدیوهای داخل حافظه: {len(VIDEOS)}\n🗃 فهرست جستجوی محلی: {len(INDEX)}\n🔄 اسکن فعال: {len(CRAWLERS)}\n"
            f"💾 فضای خالی دیسک: {disk.free // 2**30} GB\n⏱ مدت روشن بودن: {fmt_uptime()}\n"
            f"📤 حداکثر حجم ارسال: {'2 GB' if LOCAL_API else '50 MB'}\n\n"
            f"📦 ویدیو در هر صفحه: {BATCH}\n⚡ صفحات همزمان: {CONCURRENCY}\n\n"
            f"👁 دیده‌شده‌ها: {len(VIEWED)} — {'مخفی می‌شن' if HIDE_SEEN else 'با علامت نمایش داده می‌شن'}\n"
            f"⏱ فیلتر مدت: {DUR_LABEL.get(DUR_FILTER, 'همه')}\n\n"
            f"🔔 خبر ویدیوی جدید: {'روشن ✅' if WATCH['on'] else 'خاموش ❌'} (هر {WATCH['hours']} ساعت)\n"
            f"🕒 آخرین بررسی: {fmt_ago(WATCH['last'])}")


def fmt_ago(ts):
    if not ts:
        return "هنوز انجام نشده"
    m = int((__import__("time").time() - ts) // 60)
    return f"{m} دقیقه پیش" if m < 60 else f"{m // 60} ساعت پیش"


def panel_kb():
    mark = lambda cur, val: f"✅ {val}" if cur == val else str(val)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 ویدیو در صفحه:", callback_data="noop:x")] +
        [InlineKeyboardButton(mark(BATCH, n), callback_data=f"setbatch:{n}") for n in (5, 10, 20)],
        [InlineKeyboardButton("⚡ همزمانی:", callback_data="noop:x")] +
        [InlineKeyboardButton(mark(CONCURRENCY, n), callback_data=f"setconc:{n}") for n in (4, 8, 16)],
        [InlineKeyboardButton("👁 دیده‌شده‌ها: " + ("🙈 مخفی" if HIDE_SEEN else "✓ نمایش با علامت"),
                              callback_data="hideseen:x")],
        [InlineKeyboardButton("⏱ مدت:", callback_data="noop:x")] +
        [InlineKeyboardButton(("✅ " if DUR_FILTER == k else "") + lbl, callback_data=f"setdur:{k}")
         for k, lbl in (("all", "همه"), ("short", "کوتاه"), ("long", "بلند"))],
        [InlineKeyboardButton("🔔 خاموش کردن خبر" if WATCH["on"] else "🔔 روشن کردن خبر ویدیوی جدید",
                              callback_data="watch:toggle")],
        [InlineKeyboardButton("⏰ هر:", callback_data="noop:x")] +
        [InlineKeyboardButton(mark(WATCH["hours"], h) + "h", callback_data=f"watchh:{h}") for h in (1, 3, 6, 12, 24)],
        [InlineKeyboardButton("🔍 بررسی همین الان", callback_data="watch:now")],
        [InlineKeyboardButton("🩺 تست تک‌تک سایت‌ها (تیتر و عکس)", callback_data="doctor:x")],
        [InlineKeyboardButton("📤 پشتیبان (سایت‌ها و ذخیره‌ها)", callback_data="backup:x")],
        [InlineKeyboardButton("⬆️ آپدیت yt-dlp", callback_data="upytdlp:x"),
         InlineKeyboardButton("🧹 خالی کردن حافظه", callback_data="clearmem:x")],
        [InlineKeyboardButton("🧽 پاک کردن سابقهٔ دیده‌شده‌ها", callback_data="clrseen:x")],
        [InlineKeyboardButton("🔄 بروزرسانی آمار", callback_data="panel:x")],
    ])


def fav_menu_kb() -> InlineKeyboardMarkup:
    count = lambda fid: sum(1 for v in FAVS.values() if str(v.get("folder") or "") == fid)
    rows = [[InlineKeyboardButton(f"📂 همه ({len(FAVS)})", callback_data="fdir:all")]]
    rows += [[InlineKeyboardButton(f"📁 {name} ({count(fid)})", callback_data=f"fdir:{fid}")]
             for fid, name in FOLDERS["names"].items()]
    if FOLDERS["names"]:
        rows.append([InlineKeyboardButton(f"📂 بدون پوشه ({count('')})", callback_data="fdir:none")])
    rows.append([InlineKeyboardButton("➕ پوشهٔ جدید", callback_data="newfold:x")] +
                ([InlineKeyboardButton("🗑 حذف پوشه", callback_data="fmng:x")] if FOLDERS["names"] else []))
    return InlineKeyboardMarkup(rows)


async def show_fav_menu(chat_id, ctx):
    if not FAVS and not FOLDERS["names"]:
        return await ctx.bot.send_message(chat_id, "هنوز چیزی ذخیره نکردی. روی کارت هر ویدیو «⭐ ذخیره» رو بزن.")
    await ctx.bot.send_message(chat_id, f"⭐ ذخیره‌ها ({len(FAVS)} ویدیو) — کدوم پوشه؟", reply_markup=fav_menu_kb())


async def show_favs(chat_id, ctx, page=0, folder="all"):
    items = [(i, v) for i, v in list(FAVS.items())[::-1]  # جدیدترین اول
             if folder == "all" or str(v.get("folder") or "") == ("" if folder == "none" else folder)]
    name = {"all": "همه", "none": "بدون پوشه"}.get(folder) or folder_name(folder) or "?"
    if not items:
        return await ctx.bot.send_message(chat_id, f"📂 «{name}» خالیه.")
    chunk = items[page * PAGE:(page + 1) * PAGE]
    if page == 0:
        await ctx.bot.send_message(chat_id, f"⭐ {name}: {len(items)} ویدیو")
    for i, v in chunk:
        await send_card(chat_id, i, v, ctx)
        await asyncio.sleep(0.4)
    if (page + 1) * PAGE < len(items):
        await ctx.bot.send_message(chat_id, f"{(page + 1) * PAGE} از {len(items)}", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("➡️ ادامه ذخیره‌ها", callback_data=f"favpg:{folder}.{page + 1}")]]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id, text = update.effective_chat.id, update.message.text.strip()
    target = ctx.user_data.pop("newfolder", None)
    if target and text not in (BTN_SITES, BTN_NEW, BTN_FAVS, BTN_STOP, BTN_SEARCH, BTN_PANEL):
        name = re.sub(r"\s+", " ", text)[:30]
        fid = str(FOLDERS["next"])
        FOLDERS["next"] += 1
        FOLDERS["names"][fid] = name
        save_folders()
        if target in FAVS:
            FAVS[target]["folder"] = fid
            save_favs()
            return await update.message.reply_text(f"📁 پوشهٔ «{name}» ساخته شد و ویدیو داخلش رفت.")
        return await update.message.reply_text(f"📁 پوشهٔ «{name}» ساخته شد.", reply_markup=fav_menu_kb())
    if ctx.user_data.pop("blocking", None) and re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}", text, re.I):
        d = host_of(text if text.startswith("http") else "https://" + text)
        if d not in BLOCKED:
            BLOCKED.append(d)
            save_json(BLOCK_FILE, BLOCKED)
        return await update.message.reply_text(f"🚫 {d} بلاک شد.", reply_markup=blocked_kb())
    urls = re.findall(r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s,،]*)?", text, re.I)
    if len(urls) >= 2:  # چند سایت یک‌جا → همه ذخیره شوند
        ctx.user_data.pop("adding", None)
        added, dup = [], 0
        for u in urls:
            u = u if u.lower().startswith("http") else "https://" + u
            root = site_root(u)
            k = site_id(root)
            if k in SITES or any(host_of(v["url"]) == host_of(root) for v in SITES.values()):
                dup += 1
                continue
            SITES[k] = {"name": host_of(root), "url": root}
            added.append(host_of(root))
        save_json(SITES_FILE, SITES)
        txt = f"✅ {len(added)} سایت ذخیره شد" + (f" ({dup} تا تکراری بود)" if dup else "")
        if added:
            txt += ":\n" + "\n".join(f"• {d}" for d in added[:50])
        return await update.message.reply_text(txt[:4000], reply_markup=sites_kb())
    if text == BTN_SITES:
        ctx.user_data.pop("adding", None)
        await show_sites(chat_id, ctx)
    elif text == BTN_NEW:
        ctx.user_data["adding"] = True
        await update.message.reply_text("لینک سایت رو بفرست. اگه بخوای اسم هم بذاری، بعد از لینک با فاصله بنویس:\n"
                                        "https://example.com اسم دلخواه")
    elif text == BTN_FAVS:
        await show_fav_menu(chat_id, ctx)
    elif text == BTN_SEARCH:
        await update.message.reply_text("🔎 کلمه یا عبارت مورد نظرت رو بنویس:")
    elif text == BTN_PANEL:
        await update.message.reply_text(panel_text(), reply_markup=panel_kb())
    elif text == BTN_STOP:
        await update.message.reply_text("⏹ اسکن متوقف شد." if CRAWLERS.pop(chat_id, None) else "اسکنی در جریان نیست.")
    elif re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?(\s+.+)?$", text, re.I):
        link, _, name = text.partition(" ")
        url = link if link.startswith("http") else "https://" + link
        i = site_id(url)
        if ctx.user_data.pop("adding", None):
            SITES[i] = {"name": name.strip() or urlparse(url).netloc.removeprefix("www."), "url": url}
            save_json(SITES_FILE, SITES)
            await update.message.reply_text(f"✅ «{SITES[i]['name']}» ذخیره شد.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔍 اسکن الان", callback_data=f"scan:{i}")]]))
        else:
            if i not in SITES:
                ctx.bot_data.setdefault("tmp_sites", {})[i] = url
                await update.message.reply_text("می‌خوای این سایت ذخیره بشه؟", reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("💾 ذخیره سایت", callback_data=f"savesite:{i}")]]))
            await start_scan(chat_id, url, ctx)
    else:  # هر متن دیگری = جستجو: اول فهرست محلی (فوری)، بعد در صورت نیاز جستجوی زنده
        ctx.user_data["query"] = text[:100]
        ids = index_search(text)
        if ids:
            ctx.user_data["res"] = {"q": text[:100], "ids": ids}
            txt, kb = build_results(ctx.user_data, 0)
            await update.message.reply_text(txt, reply_markup=kb, disable_web_page_preview=True)
        else:
            await update.message.reply_text(
                f"🔎 توی فهرست محلی چیزی برای «{text[:100]}» نبود. جستجوی زنده کجا انجام بشه؟",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🌐 همه سایت‌های من", callback_data="sq:all")],
                    [InlineKeyboardButton("📌 انتخاب یک سایت", callback_data="sq:pick")]]))


CHUNK = 2 * 2**20  # هر تکه 2MB


async def http_download(url: str, referer: str, dest_dir: str, limit_mb: int, depth=0, prog=None):
    """دانلود مستقیم تکه‌تکه با Range (بعضی CDNها مثل takcdn اتصال طولانی را قطع می‌کنند
    ولی درخواست‌های کوچک Range را جواب می‌دهند). اگر HTML برگشت، لینک ویدیو را از آن درمی‌آورد."""
    hdr = {**HEADERS, "Referer": referer}
    async with httpx.AsyncClient(headers=hdr, follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as c:
        r = await c.get(url, headers={"Range": "bytes=0-1023"})
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").lower()
        if "text/html" in ctype:
            if depth:
                return None
            _, _, videos, _, _ = await asyncio.to_thread(parse_page, str(r.url), r.text, urlparse(str(r.url)).netloc.removeprefix("www."))
            for u in videos:
                if not re.search(r"\.(m3u8|mpd)(\?|$)", u, re.I):
                    p = await http_download(u, str(r.url), dest_dir, limit_mb, depth + 1, prog)
                    if p:
                        return p
            return None
        if "mpegurl" in ctype or "dash+xml" in ctype:
            return None  # استریم HLS/DASH → yt-dlp
        final = str(r.url)
        m = re.search(r"/(\d+)$", r.headers.get("content-range", ""))
        total = int(m.group(1)) if m else (int(r.headers.get("content-length") or 0) if r.status_code == 200 else 0)
        if total > limit_mb * 2**20:
            raise RuntimeError(f"حجم فایل {total // 2**20}MB بیشتر از محدودیت {limit_mb}MB تلگرام است")
        ext = (re.search(rf"\.({VID_EXTS})(\?|$)", final, re.I) or [None, "mp4"])[1]
        path = os.path.join(dest_dir, f"video.{ext}")
        if prog is not None:
            prog.update(done=0, total=total)

        if r.status_code == 200 and total and len(r.content) >= total:  # فایل کوچک، یکجا آمد
            with open(path, "wb") as f:
                f.write(r.content)
            return path

        done = 0
        with open(path, "wb") as f:  # روی دیسک موقت؛ RAM پر نمی‌شود
            while not total or done < total:
                end = done + CHUNK - 1
                if total:
                    end = min(end, total - 1)
                for attempt in range(6):
                    try:
                        cr = await c.get(final, headers={"Range": f"bytes={done}-{end}"})
                        if cr.status_code == 416:  # به انتهای فایل رسیدیم (وقتی total نامعلوم است)
                            return path
                        cr.raise_for_status()
                        data = cr.content
                        break
                    except Exception as e:
                        if attempt == 5:
                            raise RuntimeError(f"قطع در {done // 2**20}MB: {type(e).__name__}") from e
                        await asyncio.sleep(1 + attempt)
                if cr.status_code == 200:  # سرور Range را نادیده گرفت و کل فایل را داد
                    f.seek(0); f.truncate(); f.write(data)
                    return path
                f.write(data)
                done += len(data)
                if prog is not None:
                    prog["done"] = done
                if done > limit_mb * 2**20:
                    raise RuntimeError(f"حجم فایل بیشتر از محدودیت {limit_mb}MB تلگرام است")
                if not data or (not total and len(data) < end - (done - len(data)) + 1):
                    break
        return path


PROG_LINE = re.compile(rb"P ([\d.]+|NA) ([\d.]+|NA) ([\d.]+|NA)")


async def _run_ytdlp(args, prog):
    """yt-dlp را اجرا می‌کند و پیشرفت دانلود را (اگر prog داده شود) لحظه‌به‌لحظه در prog می‌نویسد."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    err = bytearray()

    async def read(stream, keep):
        async for line in stream:
            m = PROG_LINE.search(line)
            if m:
                if prog is not None:
                    d, t, te = (None if x == b"NA" else int(float(x)) for x in m.groups())
                    prog.update(done=d or 0, total=t or te or 0)
            elif keep:
                err.extend(line)
    try:
        await asyncio.gather(read(proc.stdout, False), read(proc.stderr, True))
        await proc.wait()
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode, bytes(err)


async def ytdlp_download(url: str, referer: str, dest_dir: str, prog=None):
    out = os.path.join(dest_dir, "video.%(ext)s")
    base = ["yt-dlp", "-q", "--progress", "--newline", "--progress-template",
            "download:P %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
            "--no-playlist", "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
            "--merge-output-format", "mp4", "--no-check-certificates",
            "--user-agent", HEADERS["User-Agent"], "--referer", referer,
            "--add-header", "Accept-Language:en-US,en;q=0.9",
            "--retries", "5", "--extractor-retries", "3", "-o", out]
    err = b""
    for extra in (["--impersonate", "chrome124"], ["--impersonate", "chrome"], []):  # chrome124 برای Cloudflare
        code, err = await _run_ytdlp([*base, *extra, url], prog)
        files = [f for f in os.listdir(dest_dir) if not f.endswith((".part", ".ytdl"))]
        if code == 0 and files:
            return os.path.join(dest_dir, files[0])
    raise RuntimeError(err.decode(errors="ignore")[-400:] or "دانلود ناموفق")


async def download_and_send(chat_id, v, ctx, prog=None):
    limit = 2000 if LOCAL_API else 50
    prog = prog if prog is not None else {}
    prog["phase"] = "dl"
    with tempfile.TemporaryDirectory() as tmp:
        path, errors = None, []
        # پلیر embed یا URL = صفحه → http_download کارساز نیست، مستقیم yt-dlp
        is_player = bool(KNOWN_PLAYERS.search(v["video"]) or
                         v["video"].rstrip("/") == v["page"].rstrip("/"))
        if not is_player:
            for attempt in range(2):  # 1) دانلود مستقیم
                try:
                    path = await http_download(v["video"], v["page"], tmp, limit, prog=prog)
                    break
                except RuntimeError as e:
                    if "محدودیت" in str(e):
                        raise
                    errors.append(f"direct: {e}")
                    break
                except Exception as e:
                    errors.append(f"direct#{attempt + 1}: {type(e).__name__}: {e}")
                    await asyncio.sleep(2)
        for target in (v["video"], v["page"]):  # 2) yt-dlp روی لینک ویدیو، بعد روی صفحه
            if path:
                break
            try:
                path = await ytdlp_download(target, v["page"], tmp, prog)
            except Exception as e:
                errors.append(f"yt-dlp: {e}")
        # 3) لینک‌های خارجی (ریدایرکت ساده یا صفحه دانلودی که لینک فایل داخلش هست).
        #    کوتاه‌کننده‌های کپچادار و فایل‌هاست‌های پولی رد می‌شوند.
        for u in v.get("ext", []):
            if path:
                break
            if CAPTCHA_HOSTS.search(u) or FILE_HOSTS.search(u):
                continue
            try:
                path = await http_download(u, v["page"], tmp, limit, prog=prog)
            except RuntimeError as e:
                if "محدودیت" in str(e):
                    raise
            except Exception as e:
                errors.append(f"{host_of(u)}: {type(e).__name__}")
        if not path:
            raise RuntimeError("\n".join(errors)[-700:] or "دانلود ناموفق")
        size = os.path.getsize(path)
        if size > limit * 2**20:
            raise RuntimeError(f"حجم فایل {size // 2**20}MB بیشتر از محدودیت {limit}MB تلگرام است")
        prog.update(phase="up", done=size, total=size)
        with open(path, "rb") as f:  # فایل استریم می‌شود، کل آن در RAM لود نمی‌شود
            await ctx.bot.send_video(chat_id, f, caption=v["title"][:1000], supports_streaming=True,
                                     read_timeout=1800, write_timeout=1800)
    # با خروج از with، پوشه موقت و فایل حذف شده‌اند


# ----------------------------- 📥 صف دانلود -----------------------------
# هر چت یک صف دارد؛ دانلودها پشت سر هم انجام می‌شوند و یک پیام وضعیت با درصد پیشرفت بروز می‌شود.
# در این مدت بقیهٔ ربات آزاد است (اسکن، جستجو، ذخیره‌ها ...).
DLQ: dict[int, dict] = {}  # chat_id -> {items: deque, cur, task, worker, msg, prog, text}


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _bar(pct: float) -> str:
    n = max(0, min(10, round(pct / 10)))
    return "▰" * n + "▱" * (10 - n)


def dlq_text(q) -> str:
    lines = ["📥 صف دانلود"]
    if q.get("cur"):
        v = VIDEOS.get(q["cur"]) or FAVS.get(q["cur"]) or {}
        p = q.get("prog") or {}
        lines.append(f"\n⏳ {v.get('title', '')[:60]}")
        if p.get("phase") == "up":
            lines.append("📤 در حال ارسال به تلگرام ...")
        elif p.get("total"):
            pct = 100 * p.get("done", 0) / p["total"]
            lines.append(f"{_bar(pct)} {pct:.0f}%  •  {p.get('done', 0) / 2**20:.1f} از {p['total'] / 2**20:.1f} MB")
        elif p.get("done"):
            lines.append(f"⬇️ {p['done'] / 2**20:.1f} MB دانلود شد ...")
        else:
            lines.append("🔎 در حال پیدا کردن فایل ...")
    if q["items"]:
        lines.append(f"\n🕒 در انتظار ({len(q['items'])}):")
        for n, i in enumerate(list(q["items"])[:8], 1):
            lines.append(f"{n}. {(VIDEOS.get(i) or FAVS.get(i) or {}).get('title', '')[:50]}")
        if len(q["items"]) > 8:
            lines.append(f"… و {len(q['items']) - 8} تای دیگه")
    lines.append("\n(در این مدت می‌تونی بقیهٔ ربات رو استفاده کنی)")
    return "\n".join(lines)


def dlq_kb(q) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("⏹ لغو دانلود فعلی", callback_data="dlq:cur")]
    if q["items"]:
        row.append(InlineKeyboardButton("🗑 خالی کردن صف", callback_data="dlq:clr"))
    return InlineKeyboardMarkup([row])


async def dlq_refresh(chat_id, bot, resend=False):
    q = DLQ.get(chat_id)
    if not q:
        return
    txt = dlq_text(q)
    if resend and q.get("msg"):  # پیام وضعیت بیاید پایین چت
        try:
            await q["msg"].delete()
        except Exception:
            pass
        q["msg"] = None
    if q.get("msg"):
        if txt == q.get("text"):
            return
        try:
            await q["msg"].edit_text(txt, reply_markup=dlq_kb(q))
            q["text"] = txt
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    try:
        q["msg"] = await bot.send_message(chat_id, txt, reply_markup=dlq_kb(q))
        q["text"] = txt
    except Exception:
        log.warning("queue status message failed")


async def dlq_add(chat_id, i, bot) -> int:
    """یک ویدیو را به صف اضافه می‌کند؛ جایگاهش در صف را برمی‌گرداند (۰ = تکراری)."""
    q = DLQ.setdefault(chat_id, {"items": deque(), "cur": None, "task": None, "worker": None,
                                 "msg": None, "prog": {}, "text": ""})
    if i == q["cur"] or i in q["items"]:
        return 0
    q["items"].append(i)
    if not q["worker"] or q["worker"].done():
        q["worker"] = asyncio.create_task(dlq_worker(chat_id, bot))
    await dlq_refresh(chat_id, bot, resend=True)
    return len(q["items"]) + (1 if q["cur"] else 0)


async def dlq_worker(chat_id, bot):
    q = DLQ[chat_id]
    try:
        while q["items"]:
            i = q["items"].popleft()
            v = VIDEOS.get(i) or FAVS.get(i)
            if not v:
                continue
            q["cur"], q["prog"] = i, {}
            q["task"] = asyncio.create_task(download_and_send(chat_id, v, _Ctx(bot), q["prog"]))
            while not q["task"].done():
                await asyncio.wait({q["task"]}, timeout=4)
                await dlq_refresh(chat_id, bot)
            try:
                q["task"].result()
                mark_viewed(i, "d")
            except asyncio.CancelledError:
                await bot.send_message(chat_id, f"⏹ دانلود لغو شد: {v['title'][:80]}")
            except Exception as e:
                log.warning("download failed: %s", e)
                await bot.send_message(chat_id, download_error_text(v, e)[:4000])
            q["cur"], q["task"] = None, None
            await dlq_refresh(chat_id, bot)
    finally:
        DLQ.pop(chat_id, None)
        if q.get("msg"):
            try:
                await q["msg"].delete()
            except Exception:
                pass


def download_error_text(v, e) -> str:
    if v.get("ext"):
        return (f"❌ «{v['title'][:60]}» فایل مستقیم نداره و فقط روی پلیر/فایل‌هاست خارجی هست.\n"
                "از دکمه‌های ▶️ زیر عکس ویدیو استفاده کن.")
    msg = str(e)
    if re.search(r"(Redirection detected|require login|geo|not available in your country|403)", msg, re.I):
        msg = "این سایت دانلود رو از IP سرور بسته یا لاگین می‌خواد.\n\n" + msg[-500:]
    return f"❌ دانلود «{v['title'][:60]}» نشد: {msg}"


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update):
        return await q.answer("دسترسی ندارید")
    chat_id = q.message.chat_id

    if q.data == "stop:x":
        CRAWLERS.pop(chat_id, None)
        await q.answer("⏹ متوقف شد")
        try:
            return await q.message.edit_text("⏹ اسکن متوقف شد.")
        except Exception:
            return
    if q.data == "next":
        await q.answer()
        if chat_id not in CRAWLERS:
            return await q.message.reply_text("اسکنی در جریان نیست؛ دوباره اسکن رو شروع کن.")
        await q.edit_message_reply_markup(None)  # جلوگیری از دوبار زدن
        return await send_batch(chat_id, ctx)

    action, i = q.data.split(":", 1)
    if action in ("sq", "sqs"):
        query = ctx.user_data.get("query")
        if not query:
            return await q.answer("دوباره کلمه رو بفرست", show_alert=True)
        await q.answer()
        if i == "pick":
            if not SITES:
                return await q.edit_message_text("هیچ سایتی ذخیره نشده.")
            return await q.edit_message_reply_markup(InlineKeyboardMarkup(
                [[InlineKeyboardButton(v["name"], callback_data=f"sqs:{k}")] for k, v in SITES.items()]))
        if action == "sqs":
            if i not in SITES:
                return await q.edit_message_text("این سایت حذف شده.")
            cr, where = SearchCrawler(query, [SITES[i]["url"]]), SITES[i]["name"]
        elif i == "all":
            if not SITES:
                return await q.edit_message_text("هیچ سایتی ذخیره نشده.")
            cr, where = SearchCrawler(query, [v["url"] for v in SITES.values()]), "همه سایت‌های من"
        else:
            return
        await q.edit_message_text(f"🔎 جستجوی «{query}» در {where} ...")
        CRAWLERS[chat_id] = cr
        return await send_batch(chat_id, ctx)
    global BATCH, CONCURRENCY, HIDE_SEEN, DUR_FILTER
    if action == "noop":
        return await q.answer()
    if action == "sitespg":
        await q.answer()
        return await q.edit_message_reply_markup(sites_kb(int(i)))
    if action == "favpg":
        await q.answer()
        await q.edit_message_reply_markup(None)
        folder, _, pg = i.rpartition(".")
        return await show_favs(chat_id, ctx, int(pg), folder or "all")
    if action == "doctor":
        await q.answer("🩺 تست شروع شد")
        asyncio.create_task(run_doctor(chat_id, ctx.bot))
        return
    if action == "pick":
        v = index_card(i)
        if not v:
            return await q.answer("این ویدیو دیگه توی فهرست نیست؛ دوباره اسکن کن", show_alert=True)
        await q.answer()
        return await send_card(chat_id, i, v, ctx)
    if action == "rpg":
        await q.answer()
        txt, kb = build_results(ctx.user_data, int(i))
        try:
            return await q.edit_message_text(txt, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            return
    if action == "rlive":
        query = ctx.user_data.get("query")
        if not query:
            return await q.answer("دوباره کلمه رو بفرست", show_alert=True)
        await q.answer()
        return await q.message.reply_text(
            f"🔎 جستجوی زندهٔ «{query}» کجا انجام بشه؟", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 همه سایت‌های من", callback_data="sq:all")],
                [InlineKeyboardButton("📌 انتخاب یک سایت", callback_data="sq:pick")]]))
    if action == "fdir":
        await q.answer()
        return await show_favs(chat_id, ctx, 0, i)
    if action == "newfold":
        ctx.user_data["newfolder"] = i
        await q.answer()
        return await q.message.reply_text("📁 اسم پوشهٔ جدید رو بفرست (مثلاً: «بهترین‌ها»):")
    if action == "fmng":
        await q.answer()
        rows = [[InlineKeyboardButton(f"🗑 {name}", callback_data=f"delfold:{fid}")]
                for fid, name in FOLDERS["names"].items()]
        rows.append([InlineKeyboardButton("⬅️ برگشت", callback_data="fmenu:x")])
        return await q.edit_message_text("کدوم پوشه حذف بشه؟ (ویدیوهاش پاک نمی‌شن، میرن «بدون پوشه»)",
                                         reply_markup=InlineKeyboardMarkup(rows))
    if action == "delfold":
        name = FOLDERS["names"].pop(i, None)
        save_folders()
        for v in FAVS.values():
            if str(v.get("folder") or "") == i:
                v.pop("folder", None)
        save_favs()
        await q.answer(f"🗑 «{name}» حذف شد" if name else "قبلاً حذف شده")
        return await q.edit_message_text(f"⭐ ذخیره‌ها ({len(FAVS)} ویدیو) — کدوم پوشه؟", reply_markup=fav_menu_kb())
    if action == "fmenu":
        await q.answer()
        return await q.edit_message_text(f"⭐ ذخیره‌ها ({len(FAVS)} ویدیو) — کدوم پوشه؟", reply_markup=fav_menu_kb())
    if action == "dlq":
        dq = DLQ.get(chat_id)
        if not dq:
            return await q.answer("صف دانلود خالیه")
        if i == "clr":
            dq["items"].clear()
            await q.answer("🗑 صف خالی شد")
        elif dq.get("task") and not dq["task"].done():
            dq["task"].cancel()
            await q.answer("⏹ لغو شد")
        else:
            await q.answer()
        return await dlq_refresh(chat_id, ctx.bot)
    if action in ("hideseen", "setdur", "clrseen"):
        if action == "hideseen":
            HIDE_SEEN = not HIDE_SEEN
        elif action == "setdur" and i in DUR_LABEL:
            DUR_FILTER = i
        elif action == "clrseen":
            VIEWED.clear()
            save_json(VIEWED_FILE, VIEWED)
        save_settings()
        await q.answer("✅ ذخیره شد")
        try:
            return await q.edit_message_text(panel_text(), reply_markup=panel_kb())
        except Exception:
            return
    if action == "watch" and i == "now":
        await q.answer("🔍 بررسی شروع شد")
        await q.message.reply_text(f"🔍 در حال بررسی {len(SITES)} سایت برای ویدیوی جدید... (ممکنه چند دقیقه طول بکشه)")
        return await check_new_videos(ctx.application, chat_id, manual=True)
    if action in ("watch", "watchh"):
        if action == "watch":
            WATCH["on"] = not WATCH["on"]
        else:
            WATCH["hours"] = int(i)
        WATCH["chat"] = chat_id
        save_settings()
        await q.answer("✅ ذخیره شد")
        if action == "watch" and WATCH["on"] and not SEEN:
            await q.message.reply_text("🔔 روشن شد. بار اول فقط ویدیوهای فعلی سایت‌ها ثبت میشن؛ "
                                       "از بررسی بعدی، فقط ویدیوهای جدید برات میاد.\n"
                                       "برای ثبت همین الان، «🔍 بررسی همین الان» رو بزن.")
        try:
            return await q.edit_message_text(panel_text(), reply_markup=panel_kb())
        except Exception:
            return
    if action in ("panel", "setbatch", "setconc", "clearmem"):
        if action == "setbatch":
            BATCH = int(i)
        elif action == "setconc":
            CONCURRENCY = int(i)
        elif action == "clearmem":
            keep = {k for k in VIDEOS if k in FAVS}
            for d in (VIDEOS, CARD_HINTS, EXTERNAL):
                for k in [k for k in d if k not in keep]:
                    d.pop(k, None)
        if action in ("setbatch", "setconc"):
            save_settings()
        await q.answer("✅ انجام شد" if action != "panel" else "")
        try:
            return await q.edit_message_text(panel_text(), reply_markup=panel_kb())
        except Exception:
            return
    if action == "backup":
        await q.answer()
        for path in (SITES_FILE, FAV_FILE, BLOCK_FILE, FOLDERS_FILE):
            if os.path.exists(path):
                with open(path, "rb") as f:
                    await ctx.bot.send_document(chat_id, f, filename=os.path.basename(path))
        return await q.message.reply_text("📥 برای بازگردانی، همین فایل‌ها رو برای بات بفرست.")
    if action == "upytdlp":
        await q.answer("در حال آپدیت...")
        import sys
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp",
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        ver = await asyncio.create_subprocess_exec("yt-dlp", "--version", stdout=asyncio.subprocess.PIPE)
        v_out, _ = await ver.communicate()
        return await q.message.reply_text(f"⬆️ yt-dlp: {v_out.decode().strip() or 'خطا'}"
                                          + (f"\n{out.decode()[-300:]}" if proc.returncode else ""))
    if action == "scan":
        await q.answer()
        if i not in SITES:
            return await q.message.reply_text("این سایت حذف شده.")
        return await start_scan(chat_id, SITES[i]["url"], ctx)
    if action == "delsite":
        site = SITES.pop(i, None)
        save_json(SITES_FILE, SITES)
        await q.answer(f"🗑 {site['name']} حذف شد" if site else "قبلاً حذف شده")
        return await q.edit_message_reply_markup(sites_kb())
    if action == "block":
        v = VIDEOS.get(i) or FAVS.get(i)
        if not v:
            return await q.answer("منقضی شده", show_alert=True)
        d = host_of(v["page"])
        if d not in BLOCKED:
            BLOCKED.append(d)
            save_json(BLOCK_FILE, BLOCKED)
        if isinstance(CRAWLERS.get(chat_id), SearchCrawler):  # از صف جستجوی فعلی هم حذف شود
            cr = CRAWLERS[chat_id]
            cr.queue = deque(u for u in cr.queue if not is_blocked(u))
        await q.answer(f"🚫 {d} بلاک شد و دیگه توی جستجو نمیاد", show_alert=True)
        try:
            return await q.message.delete()
        except Exception:
            return
    if action == "blocklist":
        await q.answer()
        return await q.message.reply_text("🚫 سایت‌های بلاک‌شده:" if BLOCKED else "هیچ سایتی بلاک نشده.",
                                          reply_markup=blocked_kb())
    if action == "unblock":
        k = int(i)
        if k < len(BLOCKED):
            d = BLOCKED.pop(k)
            save_json(BLOCK_FILE, BLOCKED)
            await q.answer(f"✅ {d} آزاد شد")
        return await q.edit_message_reply_markup(blocked_kb())
    if action == "blockadd":
        ctx.user_data["blocking"] = True
        await q.answer()
        return await q.message.reply_text("اسم یا لینک سایتی که می‌خوای بلاک بشه رو بفرست (مثلاً youtube.com):")
    if action == "savedom":
        v = VIDEOS.get(i) or FAVS.get(i)
        if not v:
            return await q.answer("منقضی شده", show_alert=True)
        root = site_root(v["page"])
        SITES[site_id(root)] = {"name": urlparse(root).netloc.removeprefix("www."), "url": root}
        save_json(SITES_FILE, SITES)
        await q.answer("💾 سایت به «سایت‌های من» اضافه شد")
        return await q.edit_message_reply_markup(video_kb(i))
    if action == "savesite":
        url = ctx.bot_data.get("tmp_sites", {}).get(i)
        if url:
            SITES[i] = {"name": urlparse(url).netloc.removeprefix("www."), "url": url}
            save_json(SITES_FILE, SITES)
        await q.answer("💾 ذخیره شد" if url else "منقضی شده")
        return await q.edit_message_text("✅ سایت ذخیره شد.")

    if action == "mvf":
        vid, _, fid = i.partition(":")
        if vid not in FAVS:
            return await q.answer("این ویدیو دیگه توی ذخیره‌ها نیست", show_alert=True)
        if fid != "0" and fid not in FOLDERS["names"]:
            return await q.answer("این پوشه حذف شده", show_alert=True)
        FAVS[vid]["folder"] = None if fid == "0" else fid
        save_favs()
        await q.answer(f"📁 رفت توی «{folder_name(fid)}»" if fid != "0" else "📂 بدون پوشه")
        return await q.edit_message_reply_markup(video_kb(vid))

    v = VIDEOS.get(i) or FAVS.get(i)
    if not v:
        return await q.answer("منقضی شده؛ دوباره اسکن کن", show_alert=True)

    if action == "fav":
        FAVS[i] = {**v, "folder": (FAVS.get(i) or {}).get("folder")}
        save_favs()
        await q.answer("⭐ ذخیره شد" + (" — با «🗂» بذارش توی پوشه" if FOLDERS["names"] else ""))
        return await q.edit_message_reply_markup(video_kb(i))
    if action == "unfav":
        FAVS.pop(i, None)
        save_favs()
        await q.answer("حذف شد")
        return await q.edit_message_reply_markup(video_kb(i))
    if action in ("seen", "unseen"):
        mark_viewed(i) if action == "seen" else unmark_viewed(i)
        await q.answer("👁 علامت خورد" + (" (از اسکن‌های بعدی مخفی می‌شه)" if HIDE_SEEN else "")
                       if action == "seen" else "علامت برداشته شد")
        return await q.edit_message_reply_markup(video_kb(i))
    if action == "fold":
        if i not in FAVS:
            return await q.answer("اول ⭐ ذخیره‌ش کن", show_alert=True)
        await q.answer()
        return await q.edit_message_reply_markup(folder_kb(i))
    if action == "vkb":
        await q.answer()
        return await q.edit_message_reply_markup(video_kb(i))

    pos = await dlq_add(chat_id, i, ctx.bot)
    await q.answer("این ویدیو الان توی صفه" if not pos else
                   "⬇️ دانلود شروع شد" if pos == 1 else f"📥 به صف اضافه شد (نفر {pos})")


# ----------------------------- 🩺 تست سایت‌ها -----------------------------
DOCTOR = {"running": False}
DOCTOR_PAGES = 20      # صفحات بررسی‌شده از هر سایت
DOCTOR_VIDEOS = 6      # ویدیوهای نمونه از هر سایت


async def doctor_site(site) -> dict:
    """یک سایت را اسکن آزمایشی می‌کند: دسترسی، تیتر و عکس ویدیوها، و قابل نمایش بودن عکس‌ها."""
    dom = host_of(site["url"])
    FETCH_STATS.pop(dom, None)
    res = {"name": site["name"], "url": site["url"], "domain": dom, "error": None, "videos": [], "html": {}}
    cr = Crawler(site["url"], max_pages=DOCTOR_PAGES, batch=DOCTOR_VIDEOS)
    t0 = time.time()
    try:
        found = await asyncio.wait_for(cr.next_batch(), 180)
    except Exception as e:
        found, res["error"] = [], f"{type(e).__name__}: {e}"[:200]
    res["secs"], res["pages"] = int(time.time() - t0), cr.pages
    res["stats"] = FETCH_STATS.get(dom, {})
    res["browser"] = dom in IMPERSONATE
    for i in found[:DOCTOR_VIDEOS]:
        v = VIDEOS[i]
        item = {"title": v["title"], "page": v["page"], "image": v.get("image"), "dur": v.get("dur"), "q": v.get("q"),
                "title_ok": not bad_title(v["title"]), "img_ok": False, "img_err": None}
        if v.get("image"):
            try:
                await asyncio.wait_for(fetch_image(v["image"], v["page"]), 40)
                item["img_ok"] = True
            except Exception as e:
                item["img_err"] = f"{type(e).__name__}: {e}"[:120]
        res["videos"].append(item)
    # نمونهٔ HTML برای عیب‌یابی: صفحهٔ اول سایت + اولین صفحهٔ ویدیوی مشکل‌دار
    urls = [("home", site["url"])]
    bad = next((x for x in res["videos"] if not (x["title_ok"] and x["img_ok"])), None)
    if bad:
        urls.append(("video", bad["page"]))
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as c:
        for kind, u in urls:
            try:
                r = await smart_get(c, u)
                res["html"][kind] = r.text[:600_000]
            except Exception as e:
                res["html"][kind] = f"<!-- fetch failed: {type(e).__name__}: {e} -->"
    return res


def doctor_verdict(r) -> tuple[str, str]:
    vids, st = r["videos"], r.get("stats") or {}
    if not vids:
        if st.get("blocked") or (not st.get("ok") and not st.get("bypass")):
            codes = ", ".join(f"{k}×{v}" for k, v in (st.get("codes") or {}).items()) or r.get("error") or "جواب نداد"
            return "⛔", f"سایت جواب ربات رو نداد ({codes}) — احتمالاً IP سرور رو بسته یا کپچا داره"
        return "❌", "صفحه‌ها باز شدن ولی ویدیویی پیدا نشد (ساختار سایت باید بررسی بشه)"
    t_ok = sum(x["title_ok"] for x in vids)
    i_ok = sum(x["img_ok"] for x in vids)
    if t_ok == len(vids) and i_ok == len(vids):
        return "✅", "تیتر و عکس همه درسته"
    parts = []
    if t_ok < len(vids):
        parts.append(f"تیتر {len(vids) - t_ok} تا مشکل داره")
    if i_ok < len(vids):
        parts.append(f"عکس {len(vids) - i_ok} تا نیومد")
    return "⚠️", "، ".join(parts)


def doctor_text(r) -> str:
    icon, why = doctor_verdict(r)
    st = r.get("stats") or {}
    lines = [f"{icon} {r['name']} ({r['domain']})", f"   {why}",
             f"   📄 {r['pages']} صفحه در {r['secs']} ثانیه — عادی {st.get('ok', 0)}، "
             f"🛡 با شبیه‌سازی مرورگر {st.get('bypass', 0)}، ⛔ بسته {st.get('blocked', 0)}، خطا {st.get('err', 0)}"]
    for x in r["videos"][:4]:
        lines.append(f"   {'✓' if x['title_ok'] else '✗'}{'🖼' if x['img_ok'] else '⬜'} {x['title'][:60]}")
    return "\n".join(lines)


async def run_doctor(chat_id, bot):
    if DOCTOR["running"]:
        return await bot.send_message(chat_id, "🩺 یه تست در حال انجامه، صبر کن.")
    if not SITES:
        return await bot.send_message(chat_id, "هیچ سایتی ذخیره نشده.")
    DOCTOR["running"] = True
    results = []
    sites = list(SITES.values())
    msg = await bot.send_message(chat_id, f"🩺 تست تک‌تک {len(sites)} سایت شروع شد (هر سایت حدود ۱ تا ۳ دقیقه) ...")
    try:
        for n, site in enumerate(sites, 1):
            try:
                await msg.edit_text(f"🩺 تست سایت‌ها: {n} از {len(sites)}\n🔎 {site['name']} ...")
            except Exception:
                pass
            try:
                results.append(await doctor_site(site))
            except Exception as e:
                log.exception("doctor failed for %s", site["url"])
                results.append({"name": site["name"], "url": site["url"], "domain": host_of(site["url"]),
                                "error": str(e)[:200], "videos": [], "html": {}, "pages": 0, "secs": 0, "stats": {}})
        count = {k: sum(doctor_verdict(r)[0] == k for r in results) for k in ("✅", "⚠️", "❌", "⛔")}
        head = (f"🩺 نتیجهٔ تست {len(results)} سایت:\n✅ سالم {count['✅']} | ⚠️ ناقص {count['⚠️']} | "
                f"❌ ویدیو پیدا نشد {count['❌']} | ⛔ بسته {count['⛔']}\n"
                "(✓ تیتر درست، 🖼 عکس قابل نمایش)\n")
        order = {"⛔": 0, "❌": 1, "⚠️": 2, "✅": 3}
        results.sort(key=lambda r: order[doctor_verdict(r)[0]])
        chunk = head
        for r in results:
            block = "\n" + doctor_text(r) + "\n"
            if len(chunk) + len(block) > 3900:
                await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
                chunk = ""
            chunk += block
        if chunk.strip():
            await bot.send_message(chat_id, chunk, disable_web_page_preview=True)
        # فایل گزارش کامل + نمونهٔ HTML سایت‌های مشکل‌دار (برای اصلاح دقیق‌تر اسکنر)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("report.json", json.dumps([{k: v for k, v in r.items() if k != "html"} for r in results],
                                                 ensure_ascii=False, indent=1))
            for r in results:
                if doctor_verdict(r)[0] != "✅":
                    for kind, h in (r.get("html") or {}).items():
                        z.writestr(f"{r['domain']}_{kind}.html", h)
        buf.seek(0)
        await bot.send_document(chat_id, buf, filename="soolakhi_site_report.zip",
                                caption="📎 گزارش کامل تست سایت‌ها (برای عیب‌یابی سایت‌های ⚠️/❌)")
    finally:
        DOCTOR["running"] = False
        try:
            await msg.delete()
        except Exception:
            pass


async def cmd_doctor(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        asyncio.create_task(run_doctor(update.effective_chat.id, ctx.bot))


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/debug <url> : نشان می‌دهد اسکنر از یک صفحه چه چیزی استخراج می‌کند."""
    if not allowed(update) or not ctx.args:
        return await update.message.reply_text("استفاده: /debug https://site.com/video-page")
    url = ctx.args[0]
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as c:
        r = await smart_get(c, url)
    title, image, videos, iframes, links = parse_page(url, r.text, urlparse(url).netloc.removeprefix("www."))
    mode = "🛡 مرورگر" if host_of(url) in IMPERSONATE else "عادی"
    txt = (f"HTTP {r.status_code} ({mode})\nTitle: {title}\nImage: {image}\nVideos ({len(videos)}):\n" + "\n".join(list(videos)[:8]) +
           f"\nIframes: {iframes[:3]}\nLinks: {len(links)}\nCards: " +
           "\n".join(f"{k} -> {v}" for k, v in list(CARD_HINTS.items())[:5]))
    await update.message.reply_text(txt[:4000], disable_web_page_preview=True)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """بازگردانی پشتیبان: sites.json / favorites.json / blocked.json را ادغام می‌کند."""
    if not allowed(update):
        return
    doc = update.message.document
    targets = {"sites.json": (SITES, SITES_FILE), "favorites.json": (FAVS, FAV_FILE), "blocked.json": (BLOCKED, BLOCK_FILE)}
    if doc.file_name not in targets and doc.file_name != "folders.json" or doc.file_size > 5 * 2**20:
        return await update.message.reply_text(
            "فقط فایل‌های پشتیبان sites.json، favorites.json، blocked.json یا folders.json قبول میشه.")
    f = await doc.get_file()
    data = json.loads(bytes(await f.download_as_bytearray()).decode("utf-8"))
    if doc.file_name == "folders.json":
        if not isinstance(data, dict) or not isinstance(data.get("names"), dict):
            return await update.message.reply_text("فرمت فایل درست نیست.")
        before = len(FOLDERS["names"])
        FOLDERS["names"].update({str(k): str(n)[:30] for k, n in data["names"].items()})
        FOLDERS["next"] = max([FOLDERS["next"], int(data.get("next") or 1)] +
                              [int(k) + 1 for k in FOLDERS["names"] if k.isdigit()])
        save_folders()
        return await update.message.reply_text(f"✅ {len(FOLDERS['names']) - before} پوشهٔ جدید اضافه شد.")
    store, path = targets[doc.file_name]
    before = len(store)
    if isinstance(store, dict) and isinstance(data, dict):
        store.update(data)
    elif isinstance(store, list) and isinstance(data, list):
        store.extend(d for d in data if d not in store)
    else:
        return await update.message.reply_text("فرمت فایل درست نیست.")
    save_json(path, store)
    await update.message.reply_text(f"✅ {len(store) - before} مورد جدید از {doc.file_name} اضافه شد.")


async def check_new_videos(app, chat_id, manual=False):
    """همه سایت‌های ذخیره‌شده را چک می‌کند و فقط ویدیوهای جدید را می‌فرستد.
    بار اول هر سایت فقط وضعیت فعلی ثبت می‌شود (چیزی ارسال نمی‌شود) تا سیل پیام نیاید."""
    if WATCH.get("running"):
        if manual:
            await app.bot.send_message(chat_id, "⏳ یک بررسی در حال انجامه، صبر کن.")
        return
    WATCH["running"] = True
    total_new, baselined = 0, []
    try:
        for sid, site in list(SITES.items()):
            cr = Crawler(site["url"], max_pages=WATCH_PAGES, batch=10_000)
            try:
                found = await cr.next_batch()
            except Exception as e:
                log.warning("watch fail %s: %s", site["url"], e)
                continue
            known = set(SEEN.get(sid, []))
            first_time = sid not in SEEN
            new = [i for i in found if i not in known]
            SEEN[sid] = (SEEN.get(sid, []) + new)[-3000:]
            if first_time:
                baselined.append(site["name"])
                continue
            if not new:
                continue
            total_new += len(new)
            await app.bot.send_message(chat_id, f"🔔 {len(new)} ویدیوی جدید از {site['name']}"
                                       + (f" (نمایش {WATCH_MAX_SEND} تای اول)" if len(new) > WATCH_MAX_SEND else ""))
            for i in [i for i in new if visible(i)][:WATCH_MAX_SEND]:
                await send_card(chat_id, i, VIDEOS[i], app)
                await asyncio.sleep(0.4)
        save_json(SEEN_FILE, SEEN)
        flush_index()
        WATCH["last"] = __import__("time").time()
        save_settings()
        if manual or baselined:
            msg = f"✅ بررسی تمام شد. {total_new} ویدیوی جدید." if total_new or manual else ""
            if baselined:
                msg += (f"\n📌 {len(baselined)} سایت برای اولین بار ثبت شد"
                        " (از دور بعد ویدیوهای جدیدشون میاد).")
            if msg.strip():
                await app.bot.send_message(chat_id, msg.strip())
    finally:
        WATCH["running"] = False


async def watch_loop(app):
    """هر دقیقه چک می‌کند آیا وقت بررسی رسیده یا نه (با ری‌استارت هم زمان‌بندی حفظ می‌شود)."""
    import time
    while True:
        await asyncio.sleep(60)
        try:
            if WATCH["on"] and WATCH["chat"] and SITES and time.time() - WATCH["last"] >= WATCH["hours"] * 3600:
                await check_new_videos(app, WATCH["chat"])
        except Exception:
            log.exception("watch loop error")


async def post_init(app):
    asyncio.get_running_loop().create_task(watch_loop(app))
    from telegram import BotCommand
    await app.bot.set_my_commands([BotCommand("start", "منوی اصلی"), BotCommand("panel", "پنل کنترل"),
                                   BotCommand("debug", "بررسی یک صفحه")])


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await update.message.reply_text(panel_text(), reply_markup=panel_kb())


def main():
    b = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    if LOCAL_API:
        b = b.base_url(LOCAL_API).local_mode(True)
    app = b.post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("doctor", cmd_doctor))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
