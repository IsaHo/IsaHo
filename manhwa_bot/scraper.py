"""
scraper.py — استخراج قسمت‌ها (chapters) و صفحه‌ها (images) از سایت‌های
مبتنی بر قالب Madara وردپرس (مثل sarrast.com).

قابل استفاده مستقل برای تست:
    python scraper.py https://sarrast.com/series/free-porn-manhwa-sarrast/34-cum-in-mouth
    python scraper.py https://sarrast.com/series/free-porn-manhwa-sarrast/   --images-of 1
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
}

# ترتیب جست‌وجوی attribute ها برای پیدا کردن لینک واقعی عکس (به‌خاطر lazy-load)
IMG_ATTRS = ["data-src", "data-lazy-src", "data-cfsrc", "data-original", "src"]

# سلکتورهای احتمالی برای لیست قسمت‌ها
CHAPTER_LINK_SELECTORS = [
    "li.wp-manga-chapter > a",
    ".wp-manga-chapter a",
    ".listing-chapters_wrap a",
    ".version-chap li a",
]

# سلکتورهای احتمالی برای محفظه‌ی عکس‌های یک قسمت
READER_SELECTORS = [
    ".reading-content img",
    ".read-container img",
    ".page-break img",
    ".entry-content img",
]

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


@dataclass
class Chapter:
    num: float
    title: str
    url: str

    def __repr__(self) -> str:
        return f"<Chapter {self.num} {self.title!r}>"


class Scraper:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        # اگر cloudscraper نصب باشد، برای عبور از Cloudflare استفاده می‌شود
        try:
            import cloudscraper  # type: ignore

            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            self.session.headers.update(DEFAULT_HEADERS)
        except Exception:
            pass

    # ---------- helpers ----------

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        last_exc = None
        for attempt in range(4):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
                if r.status_code == 200:
                    return r
                last_exc = RuntimeError(f"HTTP {r.status_code} for {url}")
            except requests.RequestException as e:
                last_exc = e
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"درخواست ناموفق بود: {url} ({last_exc})")

    def get(self, url: str, **kw) -> requests.Response:
        return self._request("GET", url, **kw)

    def post(self, url: str, **kw) -> requests.Response:
        return self._request("POST", url, **kw)

    @staticmethod
    def series_url(any_url: str) -> str:
        """از روی لینک یک قسمت یا خود سری، آدرس صفحه‌ی اصلی سری را می‌سازد.

        ساختار Madara:  https://site.com/<base>/<series-slug>/<chapter-slug>
        صفحه‌ی سری:     https://site.com/<base>/<series-slug>/
        """
        p = urlparse(any_url)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2:
            base, slug = parts[0], parts[1]
            return f"{p.scheme}://{p.netloc}/{base}/{slug}/"
        return any_url if any_url.endswith("/") else any_url + "/"

    # ---------- chapters ----------

    def _parse_chapter_links(self, html: str, base_url: str) -> list[Chapter]:
        soup = BeautifulSoup(html, "html.parser")
        found: dict[str, Chapter] = {}
        series_path = urlparse(self.series_url(base_url)).path.rstrip("/")

        for sel in CHAPTER_LINK_SELECTORS:
            for a in soup.select(sel):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                href = urljoin(base_url, href)
                # فقط لینک‌هایی که زیرمجموعه‌ی همین سری هستند
                if series_path and series_path not in urlparse(href).path:
                    continue
                title = " ".join(a.get_text(strip=True).split()) or href.rstrip("/").split("/")[-1]
                m = _NUM_RE.search(title) or _NUM_RE.search(href.rstrip("/").split("/")[-1])
                num = float(m.group(1)) if m else 0.0
                found[href] = Chapter(num=num, title=title, url=href)
            if found:
                break

        chapters = list(found.values())
        chapters.sort(key=lambda c: c.num)
        return chapters

    def get_chapters(self, any_url: str) -> list[Chapter]:
        """لیست همهٔ قسمت‌های یک سری را به ترتیب صعودی (۱ تا آخر) برمی‌گرداند."""
        s_url = self.series_url(any_url)
        r = self.get(s_url)
        chapters = self._parse_chapter_links(r.text, s_url)
        if chapters:
            return chapters

        # --- fallback 1: endpoint جدید Madara ---
        try:
            ajax = s_url.rstrip("/") + "/ajax/chapters/"
            r2 = self.post(ajax, headers={"X-Requested-With": "XMLHttpRequest"})
            chapters = self._parse_chapter_links(r2.text, s_url)
            if chapters:
                return chapters
        except Exception:
            pass

        # --- fallback 2: admin-ajax با post id ---
        try:
            soup = BeautifulSoup(r.text, "html.parser")
            post_id = None
            holder = soup.select_one("#manga-chapters-holder[data-id]")
            if holder:
                post_id = holder.get("data-id")
            if not post_id:
                inp = soup.select_one("input.rating-post-id, .rating-post-id")
                if inp:
                    post_id = inp.get("value") or inp.get_text(strip=True)
            if not post_id:
                m = re.search(r'"manga_id"\s*:\s*"?(\d+)', r.text) or re.search(
                    r'postID\s*=\s*(\d+)', r.text
                )
                if m:
                    post_id = m.group(1)
            if post_id:
                p = urlparse(s_url)
                admin = f"{p.scheme}://{p.netloc}/wp-admin/admin-ajax.php"
                r3 = self.post(
                    admin,
                    data={"action": "manga_get_chapters", "manga": post_id},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                chapters = self._parse_chapter_links(r3.text, s_url)
                if chapters:
                    return chapters
        except Exception:
            pass

        return chapters  # ممکن است خالی باشد

    # ---------- images ----------

    @staticmethod
    def _best_src(img, base_url: str) -> str | None:
        for attr in IMG_ATTRS:
            val = (img.get(attr) or "").strip()
            if val:
                return urljoin(base_url, val)
        srcset = (img.get("data-srcset") or img.get("srcset") or "").strip()
        if srcset:
            # آخرین (بزرگ‌ترین) گزینه‌ی srcset
            last = srcset.split(",")[-1].strip().split(" ")[0]
            if last:
                return urljoin(base_url, last)
        return None

    def get_images(self, chapter_url: str) -> list[str]:
        """لیست آدرس تمام صفحه‌های (عکس‌های) یک قسمت را به ترتیب برمی‌گرداند."""
        r = self.get(chapter_url)
        soup = BeautifulSoup(r.text, "html.parser")
        for sel in READER_SELECTORS:
            imgs = soup.select(sel)
            urls: list[str] = []
            for img in imgs:
                src = self._best_src(img, chapter_url)
                if src and src.lower().startswith("http"):
                    # رد کردن آیکون/لوگو/آواتار
                    if re.search(r"(logo|icon|avatar|loading|placeholder)", src, re.I):
                        continue
                    urls.append(src)
            # حذف تکراری‌ها با حفظ ترتیب
            seen = set()
            urls = [u for u in urls if not (u in seen or seen.add(u))]
            if len(urls) >= 1:
                return urls
        return []

    def download_image(self, img_url: str, referer: str) -> tuple[bytes, str]:
        """عکس را با هدر Referer (برای عبور از hotlink protection) دانلود می‌کند."""
        r = self.get(img_url, headers={"Referer": referer})
        return r.content, r.headers.get("Content-Type", "")


# ---------- CLI test ----------

def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    url = argv[0]
    sc = Scraper()
    if "--images-of" in argv:
        # دانلود لیست عکس‌های قسمت شماره‌ی مشخص
        idx = argv.index("--images-of")
        want = float(argv[idx + 1])
        chs = sc.get_chapters(url)
        target = next((c for c in chs if c.num == want), None)
        if not target:
            print("قسمت پیدا نشد")
            return 1
        imgs = sc.get_images(target.url)
        print(f"{target}: {len(imgs)} صفحه")
        for u in imgs:
            print(" ", u)
        return 0

    chs = sc.get_chapters(url)
    print(f"سری: {sc.series_url(url)}")
    print(f"تعداد قسمت‌ها: {len(chs)}")
    for c in chs[:20]:
        print(f"  {c.num:>6}  {c.title}  ->  {c.url}")
    if len(chs) > 20:
        print(f"  ... و {len(chs) - 20} قسمت دیگر")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
