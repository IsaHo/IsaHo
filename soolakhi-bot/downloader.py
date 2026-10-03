"""Download one video with yt-dlp into a dedicated temp folder.

yt-dlp handles direct .mp4 links, HLS (.m3u8), DASH and embedded players, so we
just hand it the post-page URL (or a direct media URL) and let it figure things
out. Files are written to disk in chunks (low RAM); the caller is responsible
for deleting the folder afterwards so nothing accumulates on the server.
"""
import glob
import os

import yt_dlp

import config


class DownloadError(Exception):
    pass


class TooLarge(DownloadError):
    pass


def download(url, workdir):
    """Download `url` into `workdir`. Returns metadata incl. the local path."""
    ydl_opts = {
        "outtmpl": os.path.join(workdir, "%(title).70s.%(ext)s"),
        "format": config.YTDLP_FORMAT,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": config.MAX_FILESIZE_MB * 1024 * 1024,
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "http_headers": {
            "User-Agent": config.USER_AGENT,
            "Referer": config.BASE_URL,
        },
    }
    if config.COOKIES_FILE:
        ydl_opts["cookiefile"] = config.COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "max_filesize" in msg.lower() or "larger than" in msg.lower():
            raise TooLarge(msg)
        raise DownloadError(msg)

    files = [f for f in glob.glob(os.path.join(workdir, "*")) if os.path.isfile(f)]
    files = [f for f in files if not f.endswith((".part", ".ytdl"))]
    if not files:
        # yt-dlp skipped the format because it exceeded max_filesize
        raise TooLarge("فایل تولید نشد (احتمالاً از حداکثر حجم مجاز بزرگ‌تر بوده).")

    path = max(files, key=os.path.getsize)
    info = info if isinstance(info, dict) else {}
    return {
        "path": path,
        "title": info.get("title"),
        "duration": info.get("duration"),
        "width": info.get("width"),
        "height": info.get("height"),
        "size": os.path.getsize(path),
    }
