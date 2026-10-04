#!/usr/bin/env python3
"""
ربات تلگرام: اسکن مرحله‌ای سایت‌ها، ارسال تیتر + عکس ویدیوها، دانلود با دکمه، ذخیره علاقه‌مندی‌ها.
فایل ویدیو روی سرور نگه داشته نمی‌شود (فایل موقت بلافاصله بعد از ارسال پاک می‌شود).
"""
import asyncio, hashlib, io, json, logging, os, re, tempfile
from collections import deque
from urllib.parse import urljoin, urlparse

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
LOCAL_API = os.environ.get("LOCAL_API")  # e.g. http://127.0.0.1:8081/bot
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAV_FILE = os.path.join(BASE_DIR, "favorites.json")
SITES_FILE = os.path.join(BASE_DIR, "sites.json")

VID_EXTS = "mp4|mkv|webm|mov|avi|m4v|m3u8|mpd|flv|wmv|3gp|ts|ogv|mpg|mpeg"
IMG_EXTS = "jpg|jpeg|png|gif|webp|avif|bmp|svg|jfif"
VIDEO_EXT = re.compile(rf"\.({VID_EXTS})(\?|$)", re.I)
IMG_EXT = re.compile(rf"\.({IMG_EXTS})(\?|$)", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

BTN_SITES, BTN_NEW, BTN_FAVS = "🌐 سایت‌های من", "➕ افزودن سایت", "⭐ ذخیره‌ها"
BTN_STOP = "⏹ توقف اسکن"
MENU = ReplyKeyboardMarkup([[BTN_SITES, BTN_NEW], [BTN_FAVS, BTN_STOP]], resize_keyboard=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

VIDEOS: dict[str, dict] = {}  # id -> {title, image, page, video}
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


def save_favs():
    save_json(FAV_FILE, FAVS)


def site_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def vid_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]  # callback_data حداکثر 64 بایت


def _attr_urls(tag, names):
    for n in names:
        val = tag.get(n)
        if val:
            for part in val.split(","):  # srcset: "a.jpg 1x, b.jpg 2x"
                part = part.strip().split(" ")[0]
                if part and not part.startswith("data:"):
                    yield part


SRC_ATTRS = ["src", "data-src", "data-lazy-src", "data-original", "data-url", "data-video", "data-video-src",
             "data-mp4", "data-file", "data-hls", "data-stream", "srcset", "data-srcset", "href", "content"]
IMG_ATTRS = ["poster", "data-poster", "data-thumb", "data-thumbnail", "data-preview", "data-image", "data-bg",
             "src", "data-src", "data-lazy-src", "data-original", "srcset", "data-srcset"]


def parse_page(url: str, html: str, domain: str):
    soup = BeautifulSoup(html, "html.parser")
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
                if isinstance(th, str): image = image or J(th)
                for k in ("contentUrl", "embedUrl"):
                    if isinstance(d.get(k), str): videos.add(J(d[k]))

    # 2) متاتگ‌ها
    title = title or meta("og:title") or meta("twitter:title")
    image = image or meta("og:image") or meta("og:image:url") or meta("twitter:image") or meta("thumbnailUrl")
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
                if not re.search(r"(logo|icon|avatar|sprite|banner|ads?[/_-])", u, re.I):
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
    return bool(re.search(r"(logo|icon|favicon|default|placeholder|no-?image|share|og-image)", img, re.I))


async def ytdlp_info(url: str):
    """برای صفحاتی که ویدیو داخل iframe/پلیر جاوااسکریپتی است: تیتر و عکس را yt-dlp استخراج کند."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "-J", "--no-playlist", "--skip-download", "--no-warnings", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 40)
        if proc.returncode == 0:
            d = json.loads(out)
            return d.get("title"), d.get("thumbnail")
    except Exception:
        pass
    return None, None


class Crawler:
    """اسکن مرحله‌ای: هر بار فقط تا پیدا شدن BATCH ویدیو جدید جلو می‌رود."""
    def __init__(self, start_url: str):
        self.domain = urlparse(start_url).netloc.removeprefix("www.")
        self.seen, self.queue, self.pages = set(), deque([start_url]), 0
        self.sent: set[str] = set()

    async def next_batch(self):
        found = []
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as c:
            while self.queue and self.pages < MAX_PAGES and len(found) < BATCH:
                url = self.queue.popleft()
                if url in self.seen:
                    continue
                self.seen.add(url)
                self.pages += 1
                try:
                    r = await c.get(url)
                    if "text/html" not in r.headers.get("content-type", ""):
                        continue
                except Exception as e:
                    log.warning("fetch fail %s: %s", url, e)
                    continue
                title, image, videos, iframes, links = parse_page(url, r.text, self.domain)
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
                for v in videos:
                    i = vid_id(v)
                    VIDEOS.setdefault(i, {"title": title, "image": image, "page": url, "video": v})
                    if i not in self.sent:
                        self.sent.add(i)
                        found.append(i)
                self.queue.extend(l for l in links if l not in self.seen)
                await asyncio.sleep(0.3)
        return found

    @property
    def done(self):
        return not self.queue or self.pages >= MAX_PAGES


CRAWLERS: dict[int, Crawler] = {}  # chat_id -> وضعیت اسکن


def allowed(update: Update) -> bool:
    return not ALLOWED_USERS or update.effective_user.id in ALLOWED_USERS


def video_kb(i: str) -> InlineKeyboardMarkup:
    fav = InlineKeyboardButton("❌ حذف از ذخیره‌ها", callback_data=f"unfav:{i}") if i in FAVS \
        else InlineKeyboardButton("⭐ ذخیره", callback_data=f"fav:{i}")
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ دانلود", callback_data=f"dl:{i}"), fav]])


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
    cap = f"🎬 {v['title']}"[:1000]
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
    msg = await ctx.bot.send_message(chat_id, f"در حال اسکن {cr.domain} ...")
    found = await cr.next_batch()
    await msg.delete()
    for i in found:
        if CRAWLERS.get(chat_id) is not cr:  # در این حین توقف یا اسکن جدید زده شده
            return
        await send_card(chat_id, i, VIDEOS[i], ctx)
        await asyncio.sleep(1)  # جلوگیری از flood limit تلگرام
    if cr.done:
        CRAWLERS.pop(chat_id, None)
        await ctx.bot.send_message(chat_id, f"✅ اسکن {cr.domain} تمام شد ({cr.pages} صفحه).")
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


def sites_kb():
    rows = [[InlineKeyboardButton(f"🔍 {v['name']}", callback_data=f"scan:{i}"),
             InlineKeyboardButton("🗑", callback_data=f"delsite:{i}")] for i, v in SITES.items()]
    return InlineKeyboardMarkup(rows) if rows else None


async def show_sites(chat_id, ctx):
    if not SITES:
        return await ctx.bot.send_message(chat_id, "هیچ سایتی ذخیره نشده. با «➕ افزودن سایت» اضافه کن.")
    await ctx.bot.send_message(chat_id, "🌐 سایت‌های ذخیره‌شده (برای اسکن بزن، 🗑 برای حذف):", reply_markup=sites_kb())


async def show_favs(chat_id, ctx):
    if not FAVS:
        return await ctx.bot.send_message(chat_id, "هنوز چیزی ذخیره نکردی.")
    await ctx.bot.send_message(chat_id, f"⭐ {len(FAVS)} ویدیو ذخیره شده:")
    for i, v in list(FAVS.items()):
        await send_card(chat_id, i, v, ctx)
        await asyncio.sleep(0.5)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    chat_id, text = update.effective_chat.id, update.message.text.strip()
    if text == BTN_SITES:
        ctx.user_data.pop("adding", None)
        await show_sites(chat_id, ctx)
    elif text == BTN_NEW:
        ctx.user_data["adding"] = True
        await update.message.reply_text("لینک سایت رو بفرست. اگه بخوای اسم هم بذاری، بعد از لینک با فاصله بنویس:\n"
                                        "https://example.com اسم دلخواه")
    elif text == BTN_FAVS:
        await show_favs(chat_id, ctx)
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
    else:
        await update.message.reply_text("متوجه نشدم. لینک سایت بفرست یا از دکمه‌ها استفاده کن.", reply_markup=MENU)


async def http_download(url: str, referer: str, dest_dir: str, limit_mb: int, depth=0):
    """دانلود مستقیم با هدرهای مرورگر (خیلی از سایت‌ها yt-dlp را می‌بندند ولی مرورگر را نه).
    اگر به‌جای فایل، صفحه HTML برگشت، لینک ویدیو را از آن درمی‌آورد."""
    hdr = {**HEADERS, "Referer": referer, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
           "Origin": f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"}
    async with httpx.AsyncClient(headers=hdr, follow_redirects=True, timeout=httpx.Timeout(60, read=120)) as c:
        async with c.stream("GET", url) as r:
            r.raise_for_status()
            ctype = r.headers.get("content-type", "").lower()
            if "text/html" in ctype:
                if depth:
                    return None
                html = (await r.aread()).decode(errors="ignore")
                _, _, videos, iframes, _ = parse_page(str(r.url), html, urlparse(str(r.url)).netloc.removeprefix("www."))
                for u in sorted(videos, key=lambda x: (".m3u8" in x or ".mpd" in x)):
                    if not re.search(r"\.(m3u8|mpd)(\?|$)", u, re.I):
                        p = await http_download(u, str(r.url), dest_dir, limit_mb, depth + 1)
                        if p:
                            return p
                return None
            if "mpegurl" in ctype or "dash+xml" in ctype:
                return None  # استریم HLS/DASH → yt-dlp
            size = int(r.headers.get("content-length") or 0)
            if size > limit_mb * 2**20:
                raise RuntimeError(f"حجم فایل {size // 2**20}MB بیشتر از محدودیت {limit_mb}MB تلگرام است")
            ext = (re.search(rf"\.({VID_EXTS})(\?|$)", str(r.url), re.I) or [None, "mp4"])[1]
            path = os.path.join(dest_dir, f"video.{ext}")
            done = 0
            with open(path, "wb") as f:  # تکه‌تکه روی دیسک موقت؛ RAM پر نمی‌شود
                async for chunk in r.aiter_bytes(1 << 20):
                    done += len(chunk)
                    if done > limit_mb * 2**20:
                        raise RuntimeError(f"حجم فایل بیشتر از محدودیت {limit_mb}MB تلگرام است")
                    f.write(chunk)
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
        # 1) دانلود مستقیم با هدر مرورگر  2) yt-dlp روی لینک ویدیو  3) yt-dlp روی خود صفحه
        try:
            path = await http_download(v["video"], v["page"], tmp, limit)
        except RuntimeError:
            raise
        except Exception as e:
            errors.append(f"direct: {e}")
        for target in (v["video"], v["page"]):
            if path:
                break
            try:
                path = await ytdlp_download(target, v["page"], tmp)
            except Exception as e:
                errors.append(f"yt-dlp: {e}")
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

    if q.data == "next":
        await q.answer()
        if chat_id not in CRAWLERS:
            return await q.message.reply_text("اسکنی در جریان نیست؛ دوباره اسکن رو شروع کن.")
        await q.edit_message_reply_markup(None)  # جلوگیری از دوبار زدن
        return await send_batch(chat_id, ctx)

    action, i = q.data.split(":", 1)
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
        await status.edit_text(f"❌ خطا: {e}")


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


def main():
    b = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    if LOCAL_API:
        b = b.base_url(LOCAL_API).local_mode(True)
    app = b.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
