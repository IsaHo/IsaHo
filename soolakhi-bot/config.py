"""Configuration loaded from environment / .env file."""
import os
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


# --- Telegram ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()
}
# Only needed for the optional 2 GB version (bot_2gb.py) — leave empty otherwise.
API_ID = _int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()

# Telegram Bot API cap for sending files with a plain bot token (~50 MB).
SEND_LIMIT_MB = _int("SEND_LIMIT_MB", 48)

# --- Site ---
BASE_URL = os.getenv("BASE_URL", "https://www.soolakhi.com/").strip()
LISTING_URL_TEMPLATE = os.getenv(
    "LISTING_URL_TEMPLATE", "https://www.soolakhi.com/page/{page}/"
).strip()

ITEM_SELECTOR = os.getenv("ITEM_SELECTOR", "article").strip()
TITLE_SELECTOR = os.getenv(
    "TITLE_SELECTOR", 'h2 a, h3 a, .title a, .entry-title a, a[rel="bookmark"]'
).strip()
THUMB_SELECTOR = os.getenv("THUMB_SELECTOR", "img").strip()

# --- Download ---
MAX_FILESIZE_MB = _int("MAX_FILESIZE_MB", 1950)
YTDLP_FORMAT = os.getenv("YTDLP_FORMAT", "best[ext=mp4]/best").strip()
ITEMS_PER_SCAN = _int("ITEMS_PER_SCAN", 8)
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None

# --- Misc / paths ---
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)
REQUEST_TIMEOUT = _int("REQUEST_TIMEOUT", 25)

DATA_DIR = os.path.join(HERE, "data")
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_DIR = DATA_DIR
DB_PATH = os.path.join(DATA_DIR, "videos.sqlite3")

# Temp area for in-flight downloads. Each download gets its own subfolder and is
# deleted right after the upload, so only one file exists on disk at a time.
DOWNLOAD_DIR = os.path.join(DATA_DIR, "tmp")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def validate(need_api=False):
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if need_api:
        if not API_ID:
            missing.append("API_ID")
        if not API_HASH:
            missing.append("API_HASH")
    if missing:
        raise SystemExit(
            "تنظیمات ناقص است؛ این مقدارها را در فایل .env پر کن: " + ", ".join(missing)
        )
