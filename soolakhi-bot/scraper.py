"""Scrape the video listing from the target site.

The site HTML structure is unknown ahead of time, so this uses the CSS
selectors from .env with sane WordPress-style defaults, and falls back to
heuristics if the configured selectors find nothing. Run this file directly to
test what it extracts:

    python scraper.py --page 1
"""
import re
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


def _fetch(url):
    resp = _session.get(url, timeout=config.REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def _pick_image(img):
    """Return the best image URL from an <img>, honoring lazy-load attrs."""
    if img is None:
        return None
    for attr in ("data-src", "data-lazy-src", "data-original", "data-lazy", "src"):
        val = img.get(attr)
        if val and not val.startswith("data:"):
            return val
    srcset = img.get("data-srcset") or img.get("srcset")
    if srcset:
        # take the first URL in the srcset list
        first = srcset.split(",")[0].strip().split(" ")[0]
        if first:
            return first
    return None


def _looks_like_post(href, base):
    """Filter out category/tag/author/feed links; keep real post URLs."""
    if not href:
        return False
    href = href.strip()
    if href.startswith("#") or href.startswith("javascript:"):
        return False
    low = href.lower()
    bad = ("/category/", "/tag/", "/author/", "/feed", "wp-login", "/page/", "mailto:")
    if any(b in low for b in bad):
        return False
    # must be on the same host
    host = urlparse(urljoin(base, href)).netloc
    return host == urlparse(base).netloc


def _extract_item(node, base):
    # title + link
    title, link = None, None
    for sel in [s.strip() for s in config.TITLE_SELECTOR.split(",") if s.strip()]:
        a = node.select_one(sel)
        if a and a.get("href") and _looks_like_post(a.get("href"), base):
            title = a.get("title") or a.get_text(strip=True)
            link = urljoin(base, a["href"])
            break
    if not link:
        # fallback: first same-host anchor that looks like a post
        for a in node.find_all("a", href=True):
            if _looks_like_post(a["href"], base):
                link = urljoin(base, a["href"])
                title = (
                    a.get("title")
                    or a.get_text(strip=True)
                    or (node.find(["h1", "h2", "h3"]) or node).get_text(strip=True)
                )
                break
    if not link:
        return None

    title = re.sub(r"\s+", " ", (title or "بدون عنوان")).strip()[:300]

    # thumbnail
    thumb = None
    img = node.select_one(config.THUMB_SELECTOR) if config.THUMB_SELECTOR else node.find("img")
    thumb = _pick_image(img)
    if thumb:
        thumb = urljoin(base, thumb)

    return {"title": title, "url": link, "thumb": thumb}


def _heuristic_nodes(soup):
    """When the configured ITEM_SELECTOR matches nothing, guess containers that
    hold both a link and an image."""
    candidates = soup.select("article, li, .post, .item, .movie, .video, .entry")
    good = [c for c in candidates if c.find("a", href=True) and c.find("img")]
    return good


def fetch_listing(page=1):
    """Return a list of {title, url, thumb} for one listing page."""
    url = config.LISTING_URL_TEMPLATE.format(page=page)
    if page == 1:
        # /page/1/ often 404s on WordPress; use the base URL for the first page.
        url = config.BASE_URL
    html = _fetch(url)
    soup = BeautifulSoup(html, "lxml")

    nodes = soup.select(config.ITEM_SELECTOR) if config.ITEM_SELECTOR else []
    if not nodes:
        nodes = _heuristic_nodes(soup)

    items, seen = [], set()
    for node in nodes:
        item = _extract_item(node, config.BASE_URL)
        if item and item["url"] not in seen:
            seen.add(item["url"])
            items.append(item)
    return items


if __name__ == "__main__":
    # quick diagnostic: python scraper.py [--page N]
    page = 1
    if "--page" in sys.argv:
        try:
            page = int(sys.argv[sys.argv.index("--page") + 1])
        except (ValueError, IndexError):
            pass
    print(f"Fetching listing page {page} ...\n")
    found = fetch_listing(page)
    if not found:
        print("هیچ آیتمی پیدا نشد. سلکتورها را در .env تنظیم کن (ITEM_SELECTOR/TITLE_SELECTOR/THUMB_SELECTOR).")
    for i, it in enumerate(found, 1):
        print(f"{i:>2}. {it['title']}")
        print(f"    url  : {it['url']}")
        print(f"    thumb: {it['thumb']}")
    print(f"\nمجموع: {len(found)} آیتم")
