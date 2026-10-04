#!/usr/bin/env python3
"""
ربات تلگرام: اسکن سایت، ارسال تیتر + عکس ویدیوها، دانلود با دکمه.
هیچ فایلی روی سرور نگه داشته نمی‌شود (فایل موقت بلافاصله بعد از ارسال پاک می‌شود).
"""
import asyncio, hashlib, logging, os, re, tempfile
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x}
START_URL = os.environ.get("START_URL", "https://www.soolakhi.com/")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "300"))
# اگر Local Bot API Server داری، آدرسش را بده تا محدودیت آپلود 50MB به 2GB برسد
LOCAL_API = os.environ.get("LOCAL_API")  # e.g. http://127.0.0.1:8081/bot

VIDEO_EXT = re.compile(r"\.(mp4|mkv|webm|mov|avi|m4v|m3u8)(\?|$)", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
DOMAIN = urlparse(START_URL).netloc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")

VIDEOS: dict[str, dict] = {}  # id -> {title, image, page, video}


def vid_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:16]  # callback_data حداکثر 64 بایت


def parse_page(url: str, html: str):
    soup = BeautifulSoup(html, "html.parser")
    meta = lambda p: (soup.find("meta", property=p) or soup.find("meta", attrs={"name": p}) or {}).get("content")
    title = meta("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else url)
    image = meta("og:image") or meta("twitter:image")

    videos = set()
    for v in soup.find_all("video"):
        if v.get("src"):
            videos.add(urljoin(url, v["src"]))
        if not image and v.get("poster"):
            image = urljoin(url, v["poster"])
        for s in v.find_all("source"):
            if s.get("src"):
                videos.add(urljoin(url, s["src"]))
    for p in ("og:video", "og:video:url", "og:video:secure_url"):
        if meta(p):
            videos.add(urljoin(url, meta(p)))
    for a in soup.find_all("a", href=True):
        if VIDEO_EXT.search(a["href"]):
            videos.add(urljoin(url, a["href"]))
    for m in re.findall(r"""["'](https?://[^"'\s]+?\.(?:mp4|m3u8|webm)[^"'\s]*)["']""", html):
        videos.add(m)
    has_iframe = any(f.get("src") for f in soup.find_all("iframe"))

    links = set()
    for a in soup.find_all("a", href=True):
        u = urljoin(url, a["href"]).split("#")[0]
        if urlparse(u).netloc == DOMAIN and not VIDEO_EXT.search(u) and not re.search(r"\.(jpg|jpeg|png|gif|webp|pdf|zip|css|js)$", u, re.I):
            links.add(u)
    return title, (urljoin(url, image) if image else None), videos, has_iframe, links


async def crawl():
    found, seen, queue = [], set(), deque([START_URL])
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as c:
        while queue and len(seen) < MAX_PAGES:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            try:
                r = await c.get(url)
                if "text/html" not in r.headers.get("content-type", ""):
                    continue
            except Exception as e:
                log.warning("fetch fail %s: %s", url, e)
                continue
            title, image, videos, has_iframe, links = parse_page(url, r.text)
            if not videos and has_iframe:
                videos = {url}  # yt-dlp خودش از صفحه/iframe استخراج می‌کند
            for v in videos:
                i = vid_id(v)
                if i not in VIDEOS:
                    VIDEOS[i] = {"title": title, "image": image, "page": url, "video": v}
                    found.append(i)
            queue.extend(l for l in links if l not in seen)
            await asyncio.sleep(0.3)
    return found


def allowed(update: Update) -> bool:
    return not ALLOWED_USERS or update.effective_user.id in ALLOWED_USERS


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if allowed(update):
        await update.message.reply_text("/scan برای اسکن سایت")


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = await update.message.reply_text("در حال اسکن سایت...")
    found = await crawl()
    await msg.edit_text(f"{len(found)} ویدیو جدید پیدا شد.")
    for i in found:
        v = VIDEOS[i]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ دانلود", callback_data=f"dl:{i}")]])
        cap = f"🎬 {v['title']}\n{v['page']}"[:1000]
        try:
            if v["image"]:
                await update.message.reply_photo(v["image"], caption=cap, reply_markup=kb)
            else:
                await update.message.reply_text(cap, reply_markup=kb)
        except Exception:
            await update.message.reply_text(cap, reply_markup=kb)
        await asyncio.sleep(1)  # جلوگیری از flood limit تلگرام


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
    v = VIDEOS.get(q.data.split(":", 1)[1])
    if not v:
        return await q.answer("منقضی شده؛ دوباره /scan بزنید", show_alert=True)
    await q.answer("در حال دانلود...")
    status = await q.message.reply_text(f"⏳ دانلود: {v['title']}")
    try:
        await download_and_send(q.message.chat_id, v, ctx)
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
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^dl:"))
    app.run_polling()


if __name__ == "__main__":
    main()
