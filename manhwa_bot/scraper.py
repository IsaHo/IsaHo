"""
scraper.py — استخراج قسمت‌ها (chapters) و صفحه‌ها (images) از sarrast.com

ساختار سایت (کشف‌شده):
  • صفحه‌ی هر قسمت، لیست کامل همهٔ قسمت‌های سری را به صورت لینک <a> دارد.
  • بعضی لینک‌ها داخل لینک‌کوتاه‌کن ouo.io پیچیده شده‌اند؛ آدرس واقعی در پارامتر ?s= است.
  • عکس‌های هر قسمت: <img class="pg"> با مسیر /public/img/series/<series>/<chapter>/NNN.jpg

تست مستقل:
    python scraper.py "https://sarrast.com/series/free-porn-manhwa-sarrast/34-cum-in-mouth"
    python scraper.py "https://sarrast.com/series/free-porn-manhwa-sarrast/" --images-of 34
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin, parse_qs, unquote

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

IMG_ATTRS = ["data-src", "data-lazy-src", "data-original", "src"]


@dataclass
class Chapter:
    num: float
    title: str   # اسلاگ قسمت، مثل "34-cum-in-mouth"
    url: str

    @property
    def label(self) -> str:
        n = int(self.num) if self.num == int(self.num) else self.num
        return f"قسمت {n}"

    def __repr__(self) -> str:
        return f"<Chapter {self.num} {self.title!r}>"


class Scraper:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        try:
            import cloudscraper  # type: ignore

            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            self.session.headers.update(DEFAULT_HEADERS)
        except Exception:
            pass

    # ---------- http ----------

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        last = None
        for attempt in range(4):
            try:
                r = self.session.request(method, url, timeout=self.timeout, **kw)
                if r.status_code == 200:
                    return r
                last = RuntimeError(f"HTTP {r.status_code}")
            except requests.RequestException as e:
                last = e
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"درخواست ناموفق: {url} ({last})")

    def get(self, url: str, **kw) -> requests.Response:
        return self._request("GET", url, **kw)

    # ---------- url helpers ----------

    @staticmethod
    def _unwrap(href: str) -> str:
        """باز کردن لینک‌های پیچیده‌شده در ouo.io (آدرس واقعی در پارامتر s=)."""
        if href and "ouo.io" in href:
            q = parse_qs(urlparse(href).query)
            if q.get("s"):
                return unquote(q["s"][0])
        return href

    @staticmethod
    def series_url(any_url: str) -> str:
        """آدرس صفحه‌ی سری از روی هر لینک (قسمت یا خود سری).
        ساختار: scheme://host/<base>/<series-slug>/[chapter]
        """
        p = urlparse(any_url)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2:
            return f"{p.scheme}://{p.netloc}/{parts[0]}/{parts[1]}/"
        return any_url if any_url.endswith("/") else any_url + "/"

    # ---------- chapters ----------

    def _chapter_links(self, soup: BeautifulSoup, s_url: str) -> list[Chapter]:
        series_path = urlparse(s_url).path.rstrip("/")   # /series/<slug>
        out: dict[str, Chapter] = {}
        for a in soup.find_all("a"):
            href = self._unwrap((a.get("href") or "").strip())
            if not href:
                continue
            href = urljoin(s_url, href)
            path = urlparse(href).path.rstrip("/")
            prefix = series_path + "/"
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if not rest or "/" in rest:        # باید دقیقاً یک سطح پایین‌تر باشد
                continue
            m = re.match(r"(\d+(?:\.\d+)?)", rest)
            num = float(m.group(1)) if m else 0.0
            # آدرس را بدون کوئری نگه می‌داریم
            clean = f"{urlparse(href).scheme}://{urlparse(href).netloc}{path}"
            out[clean] = Chapter(num=num, title=rest, url=clean)
        return sorted(out.values(), key=lambda c: (c.num, c.title))

    def get_chapters(self, any_url: str) -> list[Chapter]:
        """لیست همهٔ قسمت‌های سری به ترتیب صعودی.

        صفحه‌ی هر قسمت کل لیست را دارد؛ اگر لینک ورودی خودِ صفحه‌ی سری باشد
        (که فقط لینک قسمت اول را دارد) اول آن قسمت را باز می‌کنیم تا لیست کامل بیاید.
        """
        s_url = self.series_url(any_url)
        soup = BeautifulSoup(self.get(any_url).text, "html.parser")
        chapters = self._chapter_links(soup, s_url)
        if len(chapters) < 2 and chapters:
            soup2 = BeautifulSoup(self.get(chapters[0].url).text, "html.parser")
            more = self._chapter_links(soup2, s_url)
            if len(more) > len(chapters):
                chapters = more
        return chapters

    # ---------- images ----------

    @staticmethod
    def _best_src(img, base_url: str) -> str | None:
        for attr in IMG_ATTRS:
            v = (img.get(attr) or "").strip()
            if v:
                return urljoin(base_url, v)
        ss = (img.get("data-srcset") or img.get("srcset") or "").strip()
        if ss:
            last = ss.split(",")[-1].strip().split(" ")[0]
            if last:
                return urljoin(base_url, last)
        return None

    def get_images(self, chapter_url: str) -> list[str]:
        """آدرس تمام صفحه‌های یک قسمت، به ترتیب شماره‌ی صفحه."""
        soup = BeautifulSoup(self.get(chapter_url).text, "html.parser")
        slug = urlparse(chapter_url).path.rstrip("/").split("/")[-1]

        imgs = soup.select("img.pg") or soup.find_all("img")
        urls: list[str] = []
        for img in imgs:
            src = self._best_src(img, chapter_url)
            if src and src.lower().startswith("http"):
                urls.append(src)

        # فقط عکس‌های همین قسمت (مسیر پوشه‌ی همین اسلاگ یا داخل public/img/series)
        filt = [u for u in urls if f"/{slug}/" in u] or \
               [u for u in urls if "/public/img/series" in u.lower()] or urls

        seen = set()
        filt = [u for u in filt if not (u in seen or seen.add(u))]

        def page_no(u: str) -> int:
            m = re.search(r"(\d+)\.(?:jpe?g|png|webp|gif)(?:$|\?)", u, re.I)
            return int(m.group(1)) if m else 0

        filt.sort(key=page_no)
        return filt

    def download_image(self, img_url: str, referer: str) -> tuple[bytes, str]:
        r = self.get(img_url, headers={"Referer": referer})
        return r.content, r.headers.get("Content-Type", "")


# ---------- CLI ----------

def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    url = argv[0]
    sc = Scraper()
    if "--images-of" in argv:
        want = float(argv[argv.index("--images-of") + 1])
        chs = sc.get_chapters(url)
        target = next((c for c in chs if c.num == want), None)
        if not target:
            print("قسمت پیدا نشد")
            return 1
        imgs = sc.get_images(target.url)
        print(f"{target.label}: {len(imgs)} صفحه")
        for u in imgs[:10]:
            print("  ", u)
        if len(imgs) > 10:
            print(f"   ... و {len(imgs) - 10} صفحه دیگر")
        return 0

    chs = sc.get_chapters(url)
    print(f"سری: {sc.series_url(url)}")
    print(f"تعداد قسمت‌ها: {len(chs)}")
    for c in chs[:25]:
        print(f"  {c.label:>10}  ({c.title})  ->  {c.url}")
    if len(chs) > 25:
        print(f"  ... و {len(chs) - 25} قسمت دیگر")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
