"""
privacy.py — پاک‌سازی چت برای حریم خصوصی (برای کل ربات: سرراست و سولاخی).

  • آیدی پیام‌های ربات و پیام‌های کاربر ثبت می‌شود (فقط چت خصوصی).
  • «پاک شدن خودکار»: هر پیام بعد از مدت انتخابی (۱ / ۶ / ۲۴ ساعت) پاک می‌شود.
  • «همین الان پاک کن»: همهٔ پیام‌های ثبت‌شدهٔ ۴۸ ساعت اخیر یکجا پاک می‌شوند.
  • «تار کردن»: عکس‌ها و ویدیوها تار (اسپویلر) فرستاده می‌شوند و فقط با لمس دیده می‌شوند (پیش‌فرض روشن).

تلگرام به ربات اجازه می‌دهد فقط پیام‌های کمتر از ۴۸ ساعت را پاک کند؛
قدیمی‌ترها خودکار از لیست حذف می‌شوند.
"""

from __future__ import annotations

import json
import os
import threading
import time

MAX_AGE = 47 * 3600          # کمی کمتر از محدودیت ۴۸ ساعتهٔ تلگرام
MAX_PER_CHAT = 3000
DELAYS = [(0, "خاموش"), (3600, "۱ ساعت"), (6 * 3600, "۶ ساعت"), (24 * 3600, "۲۴ ساعت")]

_lock = threading.Lock()
_state: dict | None = None
_path: str | None = None


def init(data_dir: str) -> None:
    global _path
    _path = os.path.join(data_dir, "privacy.json")


def _load() -> dict:
    global _state
    if _state is None:
        try:
            with open(_path, encoding="utf-8") as f:
                _state = json.load(f)
        except Exception:
            _state = {}
        _state.setdefault("delay", {})   # chat_id -> ثانیه (۰ = خاموش)
        _state.setdefault("msgs", {})    # chat_id -> [[message_id, ts], ...]
        _state.setdefault("blur", {})    # chat_id -> bool (نبودن = روشن)
    return _state


def _save() -> None:
    if not _path:
        return
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_state, f)
    os.replace(tmp, _path)


def track(chat_id, message_id, chat_type: str = "private") -> None:
    """ثبت یک پیام (ربات یا کاربر) برای پاک‌سازی بعدی. فقط چت خصوصی."""
    if chat_type != "private" or not message_id:
        return
    now = time.time()
    with _lock:
        st = _load()
        lst = st["msgs"].setdefault(str(chat_id), [])
        lst.append([int(message_id), now])
        lst[:] = [m for m in lst if now - m[1] < MAX_AGE][-MAX_PER_CHAT:]
        _save()


def track_message(msg) -> None:
    try:
        track(msg.chat_id, msg.message_id, getattr(msg.chat, "type", "private"))
    except Exception:
        pass


def get_delay(chat_id) -> int:
    with _lock:
        return int(_load()["delay"].get(str(chat_id), 0))


def set_delay(chat_id, seconds: int) -> None:
    with _lock:
        st = _load()
        if seconds:
            st["delay"][str(chat_id)] = int(seconds)
        else:
            st["delay"].pop(str(chat_id), None)
        _save()


def get_blur(chat_id) -> bool:
    with _lock:
        return bool(_load()["blur"].get(str(chat_id), True))


def set_blur(chat_id, on: bool) -> None:
    with _lock:
        _load()["blur"][str(chat_id)] = bool(on)
        _save()


def delay_label(seconds: int) -> str:
    return dict(DELAYS).get(int(seconds), f"{seconds // 3600} ساعت")


def pop_due(now: float | None = None) -> dict[int, list[int]]:
    """پیام‌هایی که وقت پاک شدنشان رسیده (طبق تنظیم هر چت)؛ از لیست برداشته می‌شوند."""
    now = now or time.time()
    out: dict[int, list[int]] = {}
    with _lock:
        st = _load()
        changed = False
        for chat, lst in st["msgs"].items():
            delay = int(st["delay"].get(chat, 0))
            keep = []
            for mid, ts in lst:
                if now - ts >= MAX_AGE:
                    changed = True                       # دیگر قابل پاک کردن نیست
                elif delay and now - ts >= delay:
                    out.setdefault(int(chat), []).append(mid)
                    changed = True
                else:
                    keep.append([mid, ts])
            lst[:] = keep
        if changed:
            _save()
    return out


def pop_all(chat_id) -> list[int]:
    """همهٔ پیام‌های قابل پاک کردنِ یک چت (برای «همین الان پاک کن»)."""
    now = time.time()
    with _lock:
        st = _load()
        lst = st["msgs"].pop(str(chat_id), [])
        _save()
    return [mid for mid, ts in lst if now - ts < MAX_AGE]


async def delete_ids(bot, chat_id: int, ids: list[int]) -> int:
    """پاک کردن دسته‌ای (حداکثر ۱۰۰ تا در هر درخواست)؛ خطاها نادیده گرفته می‌شوند."""
    done = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
            done += len(chunk)
        except Exception:
            for mid in chunk:          # اگر دسته‌ای نشد، تک‌تک
                try:
                    await bot.delete_message(chat_id, mid)
                    done += 1
                except Exception:
                    pass
    return done
