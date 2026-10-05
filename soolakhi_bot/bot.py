#!/usr/bin/env python3
"""
ربات تلگرام: اسکن مرحله‌ای سایت‌ها، ارسال تیتر + عکس ویدیوها، دانلود با دکمه، ذخیره علاقه‌مندی‌ها.
فایل ویدیو روی سرور نگه داشته نمی‌شود (فایل موقت بلافاصله بعد از ارسال پاک می‌شود).
"""
import asyncio, hashlib, io, json, logging, os, re, tempfile
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
CARD_HINTS: dict[str, dict] = {}  # page url -> {title, image} از کارت‌های صفحه لیست


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


def save_settings():
    save_json(SETTINGS_FILE, {"batch": BATCH, "concurrency": CONCURRENCY})


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
                title = title or d.get("name")
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
    title = title or meta("og:title") or meta("twitter:title")
    for cand in (meta("og:image"), meta("og:image:url"), meta("twitter:image"), meta("thumbnailUrl")):
        if not image and cand and not looks_generic(cand):
            image = cand
    for p in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        if meta(p): videos.add(J(meta(p)))

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
    for m in re.findall(r"""["'](?:file|src|source|url|video_url|mp4|hls)["']\s*:\s*["']([^"']+)["']""", html):
        if VIDEO_EXT.search(m): videos.add(J(m.replace("\\/", "/")))

    # 5) iframe پلیرها
    iframes = [J(u) for f in soup.find_all("iframe") for u in _attr_urls(f, ["src", "data-src"])
               if not re.search(r"(google|facebook|twitter|disqus|recaptcha|doubleclick|ads)", u, re.I)]

    # تیتر و عکس جایگزین
    if not title:
        h = soup.find("h1") or soup.find("h2")
        title = h.get_text(" ", strip=True) if h else (soup.title.get_text(strip=True) if soup.title else url)
    if not image:
        for img in soup.find_all("img"):
            for u in _attr_urls(img, IMG_ATTRS):
                if not re.search(r"(logo|icon|avatar|gravatar|sprite|banner|emoji|(?<![a-z])ads?[/_-])", u, re.I) \
                        and not (img.get("width", "999").isdigit() and int(img.get("width", "999")) < 100):
                    image = J(u); break
            if image: break
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
                if container is None: break
                img = container.find("img")
                if img: break
        if not img:
            continue
        thumb = next((J(x) for x in _attr_urls(img, IMG_ATTRS) if IMG_EXT.search(x) or "/" in x), None)
        t = a.get("title") or (a.get_text(" ", strip=True) if len(a.get_text(strip=True)) > 3 else "")
        if not t:
            box = a.parent
            for _ in range(3):  # عنوان کارت معمولاً در h2/h3 کنار عکس است
                if box is None: break
                h = box.find(["h1", "h2", "h3", "h4", "h5"])
                if h and len(h.get_text(strip=True)) > 3:
                    t = h.get_text(" ", strip=True); break
                box = box.parent
        if not t:
            alt = img.get("alt") or img.get("title") or ""
            t = alt if len(alt) > 3 else ""
        if thumb or t:
            old = CARD_HINTS.get(u, {})
            CARD_HINTS[u] = {"title": old.get("title") or clean_title(t or "", site_name), "image": old.get("image") or thumb}
    image = J(image) if image else None
    return clean_title(title, site_name)[:300], image, videos, iframes, links


def clean_title(t: str, site_name: str | None) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    parts = re.split(r"\s+[|\-–—»«:]\s+", t)
    if len(parts) > 1:
        sn = (site_name or "").strip().lower()
        keep = [p for p in parts if p.strip().lower() != sn and not (sn and sn in p.lower() and len(p) < len(sn) + 6)]
        t = max(keep or parts, key=len) if not sn else " - ".join(keep or parts)
    return t


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
    def __init__(self, start_url: str):
        self.domain = urlparse(start_url).netloc.removeprefix("www.")
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
            r = await c.get(url)
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
            while (self.hi or self.mid or self.lo) and self.pages < MAX_PAGES and len(found) < BATCH:
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
                    videos = {url}  # yt-dlp خودش از صفحه/iframe استخراج می‌کند
                    if not image:
                        t2, img2 = await ytdlp_info(url)
                        title, image = t2 or title, img2 or image
                hint = CARD_HINTS.pop(url, {})
                if hint.get("image") and (not image or looks_generic(image)):
                    image = hint["image"]
                if hint.get("title") and (not title or title == url or len(title) < 4):
                    title = hint["title"]
                ext = EXTERNAL.pop(url, [])
                if not videos and ext:
                    videos = {url}  # فقط لینک پلیر/فایل‌هاست دارد → کارت با دکمه‌های لینک
                if query_words and videos and not any(w in f"{title} {url}".lower() for w in query_words):
                    videos = set()  # نتیجه جستجو به کلمه ربطی ندارد
                for v in videos:
                    i = vid_id(v)
                    VIDEOS.setdefault(i, {"title": title, "image": image, "page": url, "video": v, "ext": ext})
                    if i not in sent:
                        sent.add(i)
                        found.append(i)
                return links


SEARCH_PATHS = ["?s={q}", "search/{q}/", "?q={q}", "search?q={q}", "videos/search?q={q}", "search/?query={q}"]


def norm_words(q: str):
    return [w for w in re.split(r"\s+", q.lower()) if len(w) > 1]


class SearchCrawler:
    """جستجو: اول صفحات نتیجه (داخل سایت‌ها یا موتور جستجو) را می‌گیرد، بعد فقط همان نتایج را باز می‌کند."""
    def __init__(self, query: str, sites: list[str]):
        self.query, self.sites = query, sites
        self.domain = ("، ".join(urlparse(u).netloc.removeprefix("www.") for u in self.sites[:3])
                                        + (" ..." if len(self.sites) > 3 else ""))
        self.queue, self.seen, self.sent, self.pages, self.ready = deque(), set(), set(), 0, False
        self.words = norm_words(query)

    async def _site_results(self, c, site: str):
        """صفحه جستجوی داخلی سایت را پیدا می‌کند (وردپرس ?s= و الگوهای رایج) و لینک نتایج را برمی‌گرداند."""
        base = site if site.endswith("/") else site + "/"
        dom = urlparse(base).netloc.removeprefix("www.")
        for path in SEARCH_PATHS:
            u = urljoin(base, path.format(q=quote_plus(self.query)))
            try:
                r = await c.get(u)
            except Exception:
                continue
            if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                continue
            before = set(CARD_HINTS)
            parse_page(str(r.url), r.text, dom)
            cards = [k for k in CARD_HINTS if k not in before and urlparse(k).netloc.removeprefix("www.") == dom]
            rel = [k for k in cards if any(w in (CARD_HINTS[k].get("title") or "").lower() + unquote(k).lower()
                                           for w in self.words)]
            if rel:
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
                    r = await c.get(url)
                    if "text/html" not in r.headers.get("content-type", ""):
                        return
                except Exception as e:
                    log.warning("fetch fail %s: %s", url, e)
                    return
                final = str(r.url)
                await process_page(final, r.text, urlparse(final).netloc.removeprefix("www."), self.sent, found)

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
    rows = [[InlineKeyboardButton("⬇️ دانلود", callback_data=f"dl:{i}"), fav]]
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
        r = await c.get(url)
        r.raise_for_status()
        if len(r.content) > 15 * 2**20:
            raise ValueError("image too big")
    im = Image.open(io.BytesIO(r.content))
    im = im.convert("RGB")
    im.thumbnail((1280, 1280))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    buf.seek(0)
    return buf


async def send_card(chat_id, i, v, ctx):
    cap = f"🎬 {v['title']}\n🌐 {urlparse(v.get('page', '')).netloc.removeprefix('www.')}"[:1000]
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
    task = asyncio.create_task(cr.next_batch())
    last = None
    while not task.done():
        await asyncio.wait({task}, timeout=3)
        if not task.done() and cr.pages != last and CRAWLERS.get(chat_id) is cr:
            last = cr.pages
            try:
                await msg.edit_text(f"🔄 در حال اسکن {cr.domain}\n📄 {cr.pages} صفحه بررسی شد ...", reply_markup=stop_kb)
            except Exception:
                pass
    found = task.result()
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
            await ctx.bot.send_message(chat_id, f"✅ تمام شد ({cr.pages} صفحه بررسی شد).")
    else:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ صفحه بعد", callback_data="next")]])
        await ctx.bot.send_message(chat_id, f"{len(found)} ویدیو. ({cr.pages} صفحه اسکن شد)", reply_markup=kb)


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
            f"🧠 ویدیوهای داخل حافظه: {len(VIDEOS)}\n🔄 اسکن فعال: {len(CRAWLERS)}\n"
            f"💾 فضای خالی دیسک: {disk.free // 2**30} GB\n⏱ مدت روشن بودن: {fmt_uptime()}\n"
            f"📤 حداکثر حجم ارسال: {'2 GB' if LOCAL_API else '50 MB'}\n\n"
            f"📦 ویدیو در هر صفحه: {BATCH}\n⚡ صفحات همزمان: {CONCURRENCY}")


def panel_kb():
    mark = lambda cur, val: f"✅ {val}" if cur == val else str(val)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 ویدیو در صفحه:", callback_data="noop:x")] +
        [InlineKeyboardButton(mark(BATCH, n), callback_data=f"setbatch:{n}") for n in (5, 10, 20)],
        [InlineKeyboardButton("⚡ همزمانی:", callback_data="noop:x")] +
        [InlineKeyboardButton(mark(CONCURRENCY, n), callback_data=f"setconc:{n}") for n in (4, 8, 16)],
        [InlineKeyboardButton("📤 پشتیبان (سایت‌ها و ذخیره‌ها)", callback_data="backup:x")],
        [InlineKeyboardButton("⬆️ آپدیت yt-dlp", callback_data="upytdlp:x"),
         InlineKeyboardButton("🧹 خالی کردن حافظه", callback_data="clearmem:x")],
        [InlineKeyboardButton("🔄 بروزرسانی آمار", callback_data="panel:x")],
    ])


async def show_favs(chat_id, ctx, page=0):
    if not FAVS:
        return await ctx.bot.send_message(chat_id, "هنوز چیزی ذخیره نکردی.")
    items = list(FAVS.items())[::-1]  # جدیدترین اول
    chunk = items[page * PAGE:(page + 1) * PAGE]
    if page == 0:
        await ctx.bot.send_message(chat_id, f"⭐ {len(FAVS)} ویدیو ذخیره شده:")
    for i, v in chunk:
        await send_card(chat_id, i, v, ctx)
        await asyncio.sleep(0.4)
    if (page + 1) * PAGE < len(items):
        await ctx.bot.send_message(chat_id, f"{(page + 1) * PAGE} از {len(items)}", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("➡️ ادامه ذخیره‌ها", callback_data=f"favpg:{page + 1}")]]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id, text = update.effective_chat.id, update.message.text.strip()
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
        await show_favs(chat_id, ctx)
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
    else:  # هر متن دیگری = جستجو
        ctx.user_data["query"] = text[:100]
        await update.message.reply_text(f"🔎 جستجوی «{text[:100]}» کجا انجام بشه؟", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🌐 همه سایت‌های من", callback_data="sq:all")],
            [InlineKeyboardButton("📌 انتخاب یک سایت", callback_data="sq:pick")]]))


CHUNK = 2 * 2**20  # هر تکه 2MB


async def http_download(url: str, referer: str, dest_dir: str, limit_mb: int, depth=0):
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
            _, _, videos, _, _ = parse_page(str(r.url), r.text, urlparse(str(r.url)).netloc.removeprefix("www."))
            for u in videos:
                if not re.search(r"\.(m3u8|mpd)(\?|$)", u, re.I):
                    p = await http_download(u, str(r.url), dest_dir, limit_mb, depth + 1)
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
                if done > limit_mb * 2**20:
                    raise RuntimeError(f"حجم فایل بیشتر از محدودیت {limit_mb}MB تلگرام است")
                if not data or (not total and len(data) < end - (done - len(data)) + 1):
                    break
        return path


async def ytdlp_download(url: str, referer: str, dest_dir: str):
    out = os.path.join(dest_dir, "video.%(ext)s")
    base = ["yt-dlp", "-q", "--no-playlist", "-f", "b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4",
            "--user-agent", HEADERS["User-Agent"], "--referer", referer,
            "--add-header", "Accept-Language:en-US,en;q=0.9", "--retries", "5", "-o", out]
    err = b""
    for extra in (["--impersonate", "chrome"], []):  # impersonate نیاز به curl_cffi دارد
        proc = await asyncio.create_subprocess_exec(*base, *extra, url, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        files = [f for f in os.listdir(dest_dir) if not f.endswith((".part", ".ytdl"))]
        if proc.returncode == 0 and files:
            return os.path.join(dest_dir, files[0])
    raise RuntimeError(err.decode(errors="ignore")[-400:] or "دانلود ناموفق")


async def download_and_send(chat_id, v, ctx):
    limit = 2000 if LOCAL_API else 50
    with tempfile.TemporaryDirectory() as tmp:
        path, errors = None, []
        for attempt in range(2):  # 1) دانلود مستقیم
            try:
                path = await http_download(v["video"], v["page"], tmp, limit)
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
                path = await ytdlp_download(target, v["page"], tmp)
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
                path = await http_download(u, v["page"], tmp, limit)
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
        with open(path, "rb") as f:  # فایل استریم می‌شود، کل آن در RAM لود نمی‌شود
            await ctx.bot.send_video(chat_id, f, caption=v["title"][:1000], supports_streaming=True,
                                     read_timeout=1800, write_timeout=1800)
    # با خروج از with، پوشه موقت و فایل حذف شده‌اند


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
    global BATCH, CONCURRENCY
    if action == "noop":
        return await q.answer()
    if action == "sitespg":
        await q.answer()
        return await q.edit_message_reply_markup(sites_kb(int(i)))
    if action == "favpg":
        await q.answer()
        await q.edit_message_reply_markup(None)
        return await show_favs(chat_id, ctx, int(i))
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
        for path in (SITES_FILE, FAV_FILE, BLOCK_FILE):
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

    v = VIDEOS.get(i) or FAVS.get(i)
    if not v:
        return await q.answer("منقضی شده؛ دوباره اسکن کن", show_alert=True)

    if action == "fav":
        FAVS[i] = v
        save_favs()
        await q.answer("⭐ ذخیره شد")
        return await q.edit_message_reply_markup(video_kb(i))
    if action == "unfav":
        FAVS.pop(i, None)
        save_favs()
        await q.answer("حذف شد")
        return await q.edit_message_reply_markup(video_kb(i))

    await q.answer("در حال دانلود...")
    status = await q.message.reply_text(f"⏳ دانلود: {v['title']}")
    try:
        await download_and_send(chat_id, v, ctx)
        await status.delete()
    except Exception as e:
        log.exception("download failed")
        if v.get("ext"):
            await status.edit_text("❌ این ویدیو فایل مستقیم نداره و فقط روی پلیر/فایل‌هاست خارجی هست.\n"
                                   "از دکمه‌های ▶️ زیر عکس ویدیو استفاده کن.")
        else:
            msg = str(e)
            if re.search(r"(Redirection detected|require login|geo|not available in your country|403)", msg, re.I):
                msg = "این سایت دانلود رو از IP سرور بسته یا لاگین می‌خواد.\n\n" + msg[-500:]
            await status.edit_text(f"❌ خطا: {msg}"[:4000])


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/debug <url> : نشان می‌دهد اسکنر از یک صفحه چه چیزی استخراج می‌کند."""
    if not allowed(update) or not ctx.args:
        return await update.message.reply_text("استفاده: /debug https://site.com/video-page")
    url = ctx.args[0]
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as c:
        r = await c.get(url)
    title, image, videos, iframes, links = parse_page(url, r.text, urlparse(url).netloc.removeprefix("www."))
    txt = (f"HTTP {r.status_code}\nTitle: {title}\nImage: {image}\nVideos ({len(videos)}):\n" + "\n".join(list(videos)[:8]) +
           f"\nIframes: {iframes[:3]}\nLinks: {len(links)}\nCards: " +
           "\n".join(f"{k} -> {v}" for k, v in list(CARD_HINTS.items())[:5]))
    await update.message.reply_text(txt[:4000], disable_web_page_preview=True)


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """بازگردانی پشتیبان: sites.json / favorites.json / blocked.json را ادغام می‌کند."""
    if not allowed(update):
        return
    doc = update.message.document
    targets = {"sites.json": (SITES, SITES_FILE), "favorites.json": (FAVS, FAV_FILE), "blocked.json": (BLOCKED, BLOCK_FILE)}
    if doc.file_name not in targets or doc.file_size > 5 * 2**20:
        return await update.message.reply_text("فقط فایل‌های پشتیبان sites.json، favorites.json یا blocked.json قبول میشه.")
    f = await doc.get_file()
    data = json.loads(bytes(await f.download_as_bytearray()).decode("utf-8"))
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


async def post_init(app):
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
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
