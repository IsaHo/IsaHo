# ربات دانلود مانهوا (sarrast.com و سایت‌های مشابه Madara)

ربات تلگرامی که لینک یک سری/قسمت رو ازت می‌گیره، صفحه‌های سایت رو خودش دانلود می‌کنه و آفلاین داخل تلگرام برات می‌فرسته. کنترل قسمت‌ها و لینک کاملاً داخل خود ربات انجام می‌شه.

## نصب (روی سرور خودت)

```bash
cd manhwa_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## اجرا

```bash
export BOT_TOKEN="توکن‌ت‌از@BotFather"
python bot.py
```

## استفاده در تلگرام

1. به ربات `/start` بده.
2. لینک سری یا یکی از قسمت‌ها رو بفرست، مثلاً:
   `https://sarrast.com/series/free-porn-manhwa-sarrast/`
   (لینک یک قسمت مثل `.../34-cum-in-mouth` هم کار می‌کنه؛ خودش سری رو تشخیص می‌ده.)
3. ربات لیست همهٔ قسمت‌ها رو با دکمه نشون می‌ده:
   - روی هر «قسمت N» بزنی → تمام صفحه‌های همون قسمت فرستاده می‌شه.
   - «⏬ دانلود همه از قسمت ۱» → از اول تا آخر پشت‌سرهم.
   - بعد از هر قسمت دکمهٔ «➡️ قسمت بعد» و «⏬ از این‌جا تا آخر» میاد.

## دستورها

| دستور | کار |
|-------|-----|
| `/start` | راهنما |
| `/mode photo` | ارسال به‌صورت عکس (پیش‌فرض، پیش‌نمایش داخل چت) |
| `/mode document` | ارسال به‌صورت فایل (برای صفحه‌های خیلی بلند مطمئن‌تره، کیفیت اصلی) |
| `/cancel` | توقف دانلود دسته‌ای |

## نکته‌ها

- اگر سایت پشت **Cloudflare** باشه، پکیج `cloudscraper` (در requirements هست) خودکار استفاده می‌شه.
- دانلود عکس‌ها با هدر `Referer` انجام می‌شه تا محافظت hotlink سایت دور زده بشه.
- تلگرام برای عکس محدودیت ابعاد/حجم داره؛ اگه آلبومی رد بشه، ربات خودکار همون‌ها رو به‌صورت **فایل** می‌فرسته. برای وبتون‌های نواری خیلی بلند، `/mode document` پیشنهاد می‌شه.
- اجرای دائمی روی سرور: با `screen`/`tmux` یا یک سرویس `systemd` ران کن.

### نمونه سرویس systemd

```ini
# /etc/systemd/system/manhwa-bot.service
[Unit]
Description=Manhwa Telegram Bot
After=network.target

[Service]
WorkingDirectory=/path/to/manhwa_bot
Environment=BOT_TOKEN=توکن‌ت
ExecStart=/path/to/manhwa_bot/venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now manhwa-bot
```

## تست اسکرپر بدون تلگرام

```bash
python scraper.py "https://sarrast.com/series/free-porn-manhwa-sarrast/"
python scraper.py "https://sarrast.com/series/free-porn-manhwa-sarrast/" --images-of 1
```

اگه این دستور لیست قسمت‌ها/عکس‌ها رو درست نشون داد، یعنی سلکتورها با سایت جور هستن و ربات هم کار می‌کنه. اگه خالی برگشت، ساختار سایت فرق داره و باید سلکتورهای `CHAPTER_LINK_SELECTORS` / `READER_SELECTORS` داخل `scraper.py` رو تنظیم کنیم.

> توجه: محتوای سایت دارای کپی‌رایت است؛ این ابزار برای استفادهٔ شخصی/آفلاین در نظر گرفته شده.
