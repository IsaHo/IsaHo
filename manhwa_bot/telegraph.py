"""نشر قسمت‌های سرراست روی telegra.ph برای خواندن داخل خودِ تلگرام.
Telegraph بومی Instant View تلگرام را پشتیبانی می‌کند؛ همهٔ عکس‌ها به‌صورت URL می‌روند
(sarrast عکس‌ها را هات‌لینک می‌دهد، پس بات هیچ عکسی دانلود نمی‌کند = مصرف RAM صفر).

کش: data/telegraph.json → {access_token, pages: {chapter_url: telegra_url}}
یک بار account ساخته می‌شود؛ بعد هر قسمت فقط یک createPage.
"""
from __future__ import annotations

import json
import logging
import os
import threading

import requests

log = logging.getLogger("telegraph")
API = "https://api.telegra.ph"
MAX_IMAGES_PER_PAGE = 200  # Telegraph محدودیت ~64KB روی content دارد
TITLE_MAX = 256

_lock = threading.Lock()


def _state_path(data_dir: str) -> str:
    return os.path.join(data_dir, "telegraph.json")


def _load(data_dir: str) -> dict:
    try:
        with open(_state_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data_dir: str, state: dict) -> None:
    path = _state_path(data_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def _ensure_token(data_dir: str) -> tuple[str, dict]:
    state = _load(data_dir)
    tok = state.get("access_token")
    if tok:
        return tok, state
    r = requests.post(
        f"{API}/createAccount",
        data={"short_name": "sarrast", "author_name": "سرراست",
              "author_url": "https://sarrast.com/"},
        timeout=20,
    ).json()
    if not r.get("ok"):
        raise RuntimeError(f"createAccount failed: {r}")
    tok = r["result"]["access_token"]
    state["access_token"] = tok
    _save(data_dir, state)
    return tok, state


def _title(series_title: str, ch_label: str) -> str:
    return f"{series_title} — {ch_label}"[:TITLE_MAX]


def get_cached(data_dir: str, chapter_url: str) -> str | None:
    """URL تلگراف را از کش برگردان بدون ساختن اکانت یا تماس شبکه."""
    with _lock:
        return _load(data_dir).get("pages", {}).get(chapter_url)


def publish(data_dir: str, chapter_url: str, series_title: str,
            ch_label: str, image_urls: list[str],
            source_url: str | None = None) -> str:
    """ایجاد (یا بازگرداندن از کش) صفحهٔ تلگراف برای یک قسمت.
    بار اول حدود ۲۰۰–۵۰۰ms طول می‌کشد؛ دفعات بعد از کش فوری است."""
    if not image_urls:
        raise ValueError("no images")
    with _lock:
        tok, state = _ensure_token(data_dir)
        pages = state.setdefault("pages", {})
        cached = pages.get(chapter_url)
        if cached:
            return cached
    imgs = image_urls[:MAX_IMAGES_PER_PAGE]
    content: list = [{"tag": "img", "attrs": {"src": s}} for s in imgs]
    if source_url:
        content.append({"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": source_url},
             "children": ["🔗 منبع در سرراست"]},
        ]})
    r = requests.post(
        f"{API}/createPage",
        data={
            "access_token": tok,
            "title": _title(series_title, ch_label),
            "content": json.dumps(content, ensure_ascii=False),
            "author_name": "سرراست",
            "author_url": source_url or "https://sarrast.com/",
        },
        timeout=25,
    ).json()
    if not r.get("ok"):
        raise RuntimeError(f"createPage failed: {r}")
    url = r["result"]["url"]
    with _lock:
        state.setdefault("pages", {})[chapter_url] = url
        _save(data_dir, state)
    log.info("telegraph page created: %s (%d imgs) for %s", url, len(imgs), chapter_url)
    return url
