#!/usr/bin/env python3
"""
ربات تلگرام: اسکن مرحله‌ای سایت‌ها، ارسال تیتر + عکس ویدیوها، دانلود با دکمه، ذخیره علاقه‌مندی‌ها.
فایل ویدیو روی سرور نگه داشته نمی‌شود (فایل موقت بلافاصله بعد از ارسال پاک می‌شود).
"""
import asyncio, hashlib, json, logging, os, re, tempfile
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
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
    return title.strip()[:300], image, videos, iframes, links


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


async def send_card(chat_id, i, v, ctx):
    cap = f"🎬 {v['title']}"[:1000]
    try:
        if v.get("image"):
            return await ctx.bot.send_photo(chat_id, v["image"], caption=cap, reply_markup=video_kb(i))
    except Exception:
        pass
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


async def download_and_send(chat_id, v, ctx):
    # 1) اول: تلگرام خودش از URL مستقیم بگیرد (هیچ چیزی روی سرور نمی‌آید)
    if re.search(r"\.(mp4)(\?|$)", v["video"], re.I):
        try:
            await ctx.bot.send_video(chat_id, v["video"], caption=v["title"][:1000], supports_streaming=True)
            return
        except Exception as e:
            log.info("send by URL failed, fallback to yt-dlp: %s", e)

    # 2) دانلود موقت با yt-dlp -> ارسال -> حذف فوری
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "video.%(ext)s")
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "-q", "--no-playlist", "-f", "b[ext=mp4]/bv*+ba/b",
            "--merge-output-format", "mp4", "--referer", v["page"], "-o", out, v["video"],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        files = os.listdir(tmp)
        if proc.returncode != 0 or not files:
            raise RuntimeError(err.decode(errors="ignore")[-400:] or "دانلود ناموفق")
        path = os.path.join(tmp, files[0])
        size = os.path.getsize(path)
        limit = 2000 if LOCAL_API else 50
        if size > limit * 1024 * 1024:
            raise RuntimeError(f"حجم فایل {size // 2**20}MB بیشتر از محدودیت {limit}MB تلگرام است")
        with open(path, "rb") as f:  # فایل استریم می‌شود، کل آن در RAM لود نمی‌شود
            await ctx.bot.send_video(chat_id, f, caption=v["title"][:1000], supports_streaming=True,
                                     read_timeout=600, write_timeout=600)
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


def main():
    b = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    if LOCAL_API:
        b = b.base_url(LOCAL_API).local_mode(True)
    app = b.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling()


if __name__ == "__main__":
    main()
