"""
scraper.py — استخراج قسمت‌ها و صفحه‌ها از sarrast.com

ساختار سایت:
  • صفحه‌ی هر قسمت، لیست کامل قسمت‌های سری را به صورت لینک <a> دارد.
  • بعضی لینک‌ها داخل ouo.io پیچیده شده‌اند؛ آدرس واقعی در پارامتر ?s= است.
  • اسلاگ قسمت یا با عدد شروع می‌شود (34-cum-in-mouth) یا episode-NNN-xxxx.
  • عکس‌های هر قسمت: /public/img/series/<series>/<chapter>/<N>.(jpg|webp)
    (بنر «ادامه دارد» با شماره‌ی غیرعادی مثل 9999999999999 تهش می‌آید و فیلتر می‌شود.)

تست:
    python scraper.py "https://sarrast.com/series/secret-class"
    python scraper.py "https://sarrast.com/series/secret-class" --images-of 1
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
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

SITE = "https://sarrast.com"
IMG_ATTRS = ["data-src", "data-lazy-src", "data-original", "src"]
MAX_PAGE_NO = 2000  # شماره‌ی صفحه‌ی بزرگ‌تر از این = آشغال (بنر ادامه دارد)

# الگوهای استخراج شماره‌ی قسمت از اسلاگ (به ترتیب اولویت)
_NUM_PATTERNS = [
    r"episode[-_ ]?(\d+(?:\.\d+)?)",
    r"chapter[-_ ]?(\d+(?:\.\d+)?)",
    r"\bep[-_ ]?(\d+(?:\.\d+)?)",
    r"part[-_ ]?(\d+(?:\.\d+)?)",
    r"قسمت[-_ ]?(\d+(?:\.\d+)?)",
    r"^(\d+(?:\.\d+)?)",
]


def extract_num(slug: str) -> float:
    for pat in _NUM_PATTERNS:
        m = re.search(pat, slug, re.I)
        if m:
            return float(m.group(1))
    m = re.search(r"(\d+)", slug)
    return float(m.group(1)) if m else 0.0


def _page_no(fname: str) -> int:
    """شمارهٔ صفحه را از نام فایل درمی‌آورد، برای هر سه حالت:
    '1-f37fa' -> 1 (عدد اول) ، '0' / '001' -> 0 ، '3532_Q2.._0' -> 0 (عدد آخر)."""
    m = re.match(r"^(\d+)-[0-9A-Za-z]+$", fname)   # <page>-<token>
    if m:
        return int(m.group(1))
    if fname.isdigit():
        return int(fname)
    m = re.search(r"(\d+)$", fname)
    return int(m.group(1)) if m else -1


@dataclass
class Chapter:
    num: float
    title: str   # اسلاگ، مثل "episode-001-L7PKh"
    url: str

    @property
    def label(self) -> str:
        n = int(self.num) if self.num == int(self.num) else self.num
        return f"قسمت {n}"

    def __repr__(self) -> str:
        return f"<Chapter {self.num} {self.title!r}>"


@dataclass
class Series:
    title: str
    url: str
    chapters: list = field(default_factory=list)


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

    # ---------- helpers ----------

    @staticmethod
    def _unwrap(href: str) -> str:
        if href and "ouo.io" in href:
            q = parse_qs(urlparse(href).query)
            if q.get("s"):
                return unquote(q["s"][0])
        return href

    @staticmethod
    def series_url(any_url: str) -> str:
        p = urlparse(any_url)
        parts = [x for x in p.path.split("/") if x]
        if len(parts) >= 2:
            return f"{p.scheme}://{p.netloc}/{parts[0]}/{parts[1]}/"
        return any_url if any_url.endswith("/") else any_url + "/"

    @staticmethod
    def _title(soup: BeautifulSoup, fallback: str) -> str:
        og = soup.find("meta", attrs={"property": "og:title"})
        raw = (og.get("content") if og and og.get("content") else None)
        if not raw and soup.title and soup.title.string:
            raw = soup.title.string
        if not raw:
            return fallback
        return raw.split("|")[0].split("-")[0].strip() or fallback

    # ---------- chapters ----------

    def _chapter_links(self, soup: BeautifulSoup, s_url: str) -> list[Chapter]:
        series_path = urlparse(s_url).path.rstrip("/")
        prefix = series_path + "/"
        out: dict[str, Chapter] = {}
        for a in soup.find_all("a"):
            href = self._unwrap((a.get("href") or "").strip())
            if not href:
                continue
            href = urljoin(s_url, href)
            path = urlparse(href).path.rstrip("/")
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if not rest or "/" in rest:
                continue
            clean = f"{urlparse(href).scheme}://{urlparse(href).netloc}{path}"
            out[clean] = Chapter(num=extract_num(rest), title=rest, url=clean)
        return sorted(out.values(), key=lambda c: (c.num, c.title))

    def get_series(self, any_url: str) -> Series:
        s_url = self.series_url(any_url)
        fallback = s_url.rstrip("/").split("/")[-1]
        soup = BeautifulSoup(self.get(any_url).text, "html.parser")
        title = self._title(soup, fallback)
        chapters = self._chapter_links(soup, s_url)
        if len(chapters) < 2 and chapters:
            soup2 = BeautifulSoup(self.get(chapters[0].url).text, "html.parser")
            more = self._chapter_links(soup2, s_url)
            if len(more) > len(chapters):
                chapters = more
        return Series(title=title, url=s_url, chapters=chapters)

    def get_chapters(self, any_url: str) -> list[Chapter]:
        return self.get_series(any_url).chapters

    # ---------- catalog ----------

    def get_catalog(self, max_pages: int = 60) -> list[dict]:
        """فهرست همهٔ داستان‌های سایت را با پیمایش صفحه‌های اصلی جمع می‌کند.
        هر آیتم: {'slug','title','url'}."""
        out: dict[str, str] = {}
        empty_streak = 0
        for pg in range(1, max_pages + 1):
            url = f"{SITE}/" if pg == 1 else f"{SITE}/?page={pg}"
            try:
                html = self.get(url).text
            except Exception:
                break
            soup = BeautifulSoup(html, "html.parser")
            before = len(out)
            for a in soup.find_all("a"):
                href = self._unwrap((a.get("href") or "").strip())
                parts = urlparse(urljoin(url, href)).path.strip("/").split("/")
                if len(parts) == 2 and parts[0] == "series":
                    slug = parts[1]
                    t = " ".join(a.get_text(strip=True).split())
                    out.setdefault(slug, "")
                    if t and t not in ("شروع خواندن", "ادامه") and not out[slug]:
                        out[slug] = t
            if len(out) == before:
                empty_streak += 1
                if empty_streak >= 2:
                    break
            else:
                empty_streak = 0
        return [{"slug": s, "title": t or s, "url": f"{SITE}/series/{s}"}
                for s, t in sorted(out.items(), key=lambda kv: kv[1] or kv[0])]

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
        soup = BeautifulSoup(self.get(chapter_url).text, "html.parser")
        slug = urlparse(chapter_url).path.rstrip("/").split("/")[-1]

        raw: list[str] = []
        for img in (soup.select("img.pg") or soup.find_all("img")):
            src = self._best_src(img, chapter_url)
            if src and src.lower().startswith("http"):
                raw.append(src)

        file_re = re.compile(r"/([^/]+)\.(?:jpe?g|png|webp|gif)(?:$|\?)", re.I)
        cand: list[tuple[int, str]] = []
        for u in raw:
            m = file_re.search(u)
            if not m:
                continue
            cand.append((_page_no(m.group(1)), u))

        # محدود به پوشه‌ی همین قسمت، در غیر این صورت هر عکسِ واقعیِ سایت
        in_slug = [c for c in cand if f"/{slug}/" in c[1]]
        base = in_slug or [c for c in cand if "/public/img/series" in c[1].lower()] or cand
        # فقط صفحه‌های واقعی؛ شماره‌ی غیرعادی (بنر «ادامه دارد» مثل 9999...) حذف می‌شود
        base = [c for c in base if 0 <= c[0] <= MAX_PAGE_NO]
        base.sort(key=lambda c: c[0])

        seen = set()
        urls = [u for _, u in base if not (u in seen or seen.add(u))]

        # اگر نسخهٔ فارسیِ ترجمه‌شده جدا وجود داشته باشد، همان را برگردان
        return self._prefer_translated(urls, html, slug)

    @staticmethod
    def _prefer_translated(urls: list[str], html: str, slug: str) -> list[str]:
        """سایت دو ست عکس دارد: src ثابت = نسخهٔ اصلی/انگلیسی، و توکن دیگر = فارسی.
        نام فایل به شکل <page>-<token>.<ext> است؛ توکنِ غیرِ src را (فارسی) جایگزین می‌کنیم."""
        tok_re = re.compile(r"/(\d+)-([0-9A-Za-z_]+)\.(?:jpe?g|png|webp|gif)\b", re.I)
        static_tokens = {m.group(2) for u in urls if (m := tok_re.search(u))}
        if not static_tokens:
            return urls  # این قسمت توکن زبانِ جدا ندارد
        all_tokens = set(re.findall(
            rf"/{re.escape(slug)}/\d+-([0-9A-Za-z_]+)\.(?:jpe?g|png|webp|gif)", html, re.I))
        persian = [t for t in all_tokens if t not in static_tokens]
        if not persian:
            return urls
        pt = persian[0]
        out = []
        for u in urls:
            m = tok_re.search(u)
            if m and m.group(2) in static_tokens:
                out.append(u[:m.start(2)] + pt + u[m.end(2):])
            else:
                out.append(u)
        return out

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
        s = sc.get_series(url)
        target = next((c for c in s.chapters if c.num == want), None)
        if not target:
            print("قسمت پیدا نشد")
            return 1
        imgs = sc.get_images(target.url)
        print(f"{target.label}: {len(imgs)} صفحه")
        for u in imgs[:8]:
            print("  ", u)
        if len(imgs) > 8:
            print(f"   ... و {len(imgs) - 8} صفحه دیگر")
        return 0

    s = sc.get_series(url)
    print(f"سری: {s.title}")
    print(f"آدرس: {s.url}")
    print(f"تعداد قسمت‌ها: {len(s.chapters)}")
    for c in s.chapters[:25]:
        print(f"  {c.label:>10}  ({c.title})")
    if len(s.chapters) > 25:
        print(f"  ... و {len(s.chapters) - 25} قسمت دیگر")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
