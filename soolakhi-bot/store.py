"""Tiny sqlite store mapping a short numeric id <-> post URL.

Telegram callback_data is limited to 64 bytes, so we can't put full URLs in the
download buttons. We store each discovered video and reference it by its id.
"""
import sqlite3
import threading

import config

_lock = threading.Lock()
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.execute(
    """CREATE TABLE IF NOT EXISTS videos (
           id    INTEGER PRIMARY KEY AUTOINCREMENT,
           url   TEXT UNIQUE,
           title TEXT,
           thumb TEXT
       )"""
)
_conn.commit()


def add(url, title, thumb):
    """Insert (or reuse) a video row and return its id."""
    with _lock:
        _conn.execute(
            "INSERT OR IGNORE INTO videos(url, title, thumb) VALUES (?, ?, ?)",
            (url, title, thumb),
        )
        _conn.commit()
        row = _conn.execute("SELECT id FROM videos WHERE url = ?", (url,)).fetchone()
        return row[0]


def get(video_id):
    """Return dict(url, title, thumb) for an id, or None."""
    with _lock:
        row = _conn.execute(
            "SELECT url, title, thumb FROM videos WHERE id = ?", (video_id,)
        ).fetchone()
    if not row:
        return None
    return {"url": row[0], "title": row[1], "thumb": row[2]}
