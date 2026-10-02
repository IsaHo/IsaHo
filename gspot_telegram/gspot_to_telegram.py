#!/usr/bin/env python3
"""Scrape GIFs/videos from a source site and send them to a Telegram chat.

Runs as a long-lived daemon: every POLL_INTERVAL seconds it scans the
configured page(s), finds media it has not seen before, and posts each one to
the Telegram chat. Already-sent items are remembered in a small JSON state file
so nothing is sent twice across restarts.

Configuration is via environment variables (see .env.example).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Configuration (all via environment variables)
# --------------------------------------------------------------------------- #

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Comma-separated list of pages to scan. Defaults to the site's home page.
SOURCE_URLS = [
    u.strip()
    for u in os.environ.get("SOURCE_URLS", "https://gspotwizard.com/").split(",")
    if u.strip()
]

# Seconds between scans.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "300"))

# Max media items to send per scan cycle (avoids flooding the chat on first run).
MAX_PER_CYCLE = int(os.environ.get("MAX_PER_CYCLE", "10"))

# Where the "already sent" state is persisted.
STATE_FILE = Path(os.environ.get("STATE_FILE", "sent_state.json"))

# Telegram bots can upload files up to 50 MB.
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "50"))

# Optional: a CSS selector + attribute to pull media URLs from, for when the
# generic extractor misses the site's markup. Example:
#   MEDIA_SELECTOR="a.gif-link"  MEDIA_ATTR="href"
MEDIA_SELECTOR = os.environ.get("MEDIA_SELECTOR", "").strip()
MEDIA_ATTR = os.environ.get("MEDIA_ATTR", "href").strip()

# Pretend to be a normal browser; some sites block default library UAs.
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
)

# Optional cookie header string, in case the site needs an age-gate / session
# cookie. Copy it from your browser's devtools if the scan comes back empty.
COOKIE = os.environ.get("COOKIE", "").strip()

MEDIA_EXT_RE = re.compile(r"\.(gif|mp4|webm)(?:[?#]|$)", re.IGNORECASE)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gspot")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(seen: set[str]) -> None:
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(seen)))
    tmp.replace(STATE_FILE)  # atomic


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    if COOKIE:
        s.headers["Cookie"] = COOKIE
    return s


def extract_media_urls(html: str, base_url: str) -> list[str]:
    """Pull candidate gif/mp4/webm URLs out of a page, in document order."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []

    def add(value: str | None) -> None:
        if value:
            candidates.append(urljoin(base_url, value.strip()))

    # 1) Explicit selector override, if the generic pass isn't enough.
    if MEDIA_SELECTOR:
        for el in soup.select(MEDIA_SELECTOR):
            add(el.get(MEDIA_ATTR))

    # 2) Common media-bearing tags/attributes.
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-original", "data-gif", "data-mp4"):
            add(img.get(attr))
    for src in soup.find_all("source"):
        add(src.get("src"))
    for video in soup.find_all("video"):
        add(video.get("src"))
    for a in soup.find_all("a"):
        add(a.get("href"))

    # 3) og:video / og:image meta tags.
    for meta in soup.find_all("meta"):
        if meta.get("property") in ("og:video", "og:image", "og:video:url"):
            add(meta.get("content"))

    # 4) Last-resort regex sweep over the raw HTML (catches JSON blobs etc.).
    for m in re.findall(r"""https?://[^\s"'<>\\]+?\.(?:gif|mp4|webm)""", html, re.IGNORECASE):
        candidates.append(m)

    # Keep only real media URLs, de-duplicated, order preserved.
    seen_local: set[str] = set()
    out: list[str] = []
    for url in candidates:
        if MEDIA_EXT_RE.search(url) and url not in seen_local:
            seen_local.add(url)
            out.append(url)
    return out


def scan(session: requests.Session) -> list[str]:
    found: list[str] = []
    for page in SOURCE_URLS:
        try:
            resp = session.get(page, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("fetch failed for %s: %s", page, exc)
            continue
        urls = extract_media_urls(resp.text, page)
        log.info("scanned %s -> %d media url(s)", page, len(urls))
        found.extend(urls)
    # De-dup across pages, preserve order.
    seen_local: set[str] = set()
    ordered: list[str] = []
    for u in found:
        if u not in seen_local:
            seen_local.add(u)
            ordered.append(u)
    return ordered


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def download(session: requests.Session, url: str) -> tuple[Path, str] | None:
    """Download a media URL to a temp file. Returns (path, ext) or None."""
    try:
        with session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            size = int(resp.headers.get("Content-Length", "0"))
            if size and size > MAX_FILE_MB * 1024 * 1024:
                log.warning("skip (too big, %d bytes): %s", size, url)
                return None
            ext = Path(urlparse(url).path).suffix.lower().lstrip(".") or "bin"
            fd, tmp_path = tempfile.mkstemp(suffix="." + ext)
            total = 0
            limit = MAX_FILE_MB * 1024 * 1024
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > limit:
                        log.warning("skip (stream exceeded limit): %s", url)
                        f.close()
                        os.unlink(tmp_path)
                        return None
                    f.write(chunk)
            return Path(tmp_path), ext
    except requests.RequestException as exc:
        log.warning("download failed %s: %s", url, exc)
        return None


def send_media(path: Path, ext: str, caption: str) -> bool:
    """Send a downloaded file to Telegram, picking the right method by type."""
    # (method, telegram field name) — fall back to document if the first fails.
    if ext == "gif":
        attempts = [("sendAnimation", "animation")]
    elif ext == "mp4":
        attempts = [("sendVideo", "video"), ("sendAnimation", "animation")]
    else:  # webm or anything else
        attempts = [("sendVideo", "video")]
    attempts.append(("sendDocument", "document"))

    for method, field in attempts:
        try:
            with path.open("rb") as fh:
                resp = requests.post(
                    f"{TELEGRAM_API}/{method}",
                    data={"chat_id": CHAT_ID, "caption": caption[:1024]},
                    files={field: fh},
                    timeout=120,
                )
            payload = resp.json()
            if payload.get("ok"):
                return True
            # Rate limited -> honour retry_after and try the same method again.
            if resp.status_code == 429:
                wait = payload.get("parameters", {}).get("retry_after", 5)
                log.info("rate limited, sleeping %ss", wait)
                time.sleep(wait + 1)
                return send_media(path, ext, caption)
            log.warning("%s failed: %s", method, payload.get("description"))
        except (requests.RequestException, ValueError) as exc:
            log.warning("%s error: %s", method, exc)
    return False


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #

_running = True


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, shutting down", signum)
    _running = False


def check_config() -> None:
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", BOT_TOKEN), ("TELEGRAM_CHAT_ID", CHAT_ID)) if not v]
    if missing:
        log.error("missing required env var(s): %s", ", ".join(missing))
        sys.exit(1)
    # Verify the token up front with a getMe call.
    try:
        me = requests.get(f"{TELEGRAM_API}/getMe", timeout=15).json()
        if not me.get("ok"):
            log.error("Telegram getMe failed: %s", me.get("description"))
            sys.exit(1)
        log.info("authenticated as @%s", me["result"].get("username"))
    except (requests.RequestException, ValueError) as exc:
        log.error("cannot reach Telegram API: %s", exc)
        sys.exit(1)


def run_cycle(session: requests.Session, seen: set[str]) -> None:
    media = scan(session)
    new = [u for u in media if u not in seen]
    if not new:
        log.info("no new media this cycle")
        return
    log.info("%d new item(s); sending up to %d", len(new), MAX_PER_CYCLE)
    sent = 0
    for url in new:
        if sent >= MAX_PER_CYCLE:
            break
        downloaded = download(session, url)
        if downloaded is None:
            # Mark as seen so a permanently-broken URL isn't retried forever.
            seen.add(url)
            continue
        path, ext = downloaded
        try:
            if send_media(path, ext, caption=url):
                seen.add(url)
                sent += 1
                log.info("sent %s", url)
                save_state(seen)
                time.sleep(2)  # gentle pacing between messages
            else:
                log.warning("giving up on %s", url)
        finally:
            path.unlink(missing_ok=True)
    save_state(seen)


def main() -> None:
    check_config()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    session = make_session()
    seen = load_state()
    log.info(
        "starting daemon: %d source(s), interval %ss, state has %d seen",
        len(SOURCE_URLS), POLL_INTERVAL, len(seen),
    )

    while _running:
        try:
            run_cycle(session, seen)
        except Exception:  # keep the daemon alive on any unexpected error
            log.exception("cycle error")
        # Sleep in short slices so signals are handled promptly.
        slept = 0
        while _running and slept < POLL_INTERVAL:
            time.sleep(min(5, POLL_INTERVAL - slept))
            slept += 5
    log.info("stopped")


if __name__ == "__main__":
    main()
