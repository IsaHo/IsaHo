# بات تلگرام سولاخی 🎬

ویدیوهای سایت را اسکن می‌کند و با **عکس + عنوان** داخل بات تلگرام نشان می‌دهد.
روی هر ویدیو دکمه‌ی **«⬇️ دانلود»** بزنی، همان لحظه دانلود و برایت فرستاده می‌شود و
**بلافاصله از روی سرور پاک می‌شود** — پس رم و هارد سرور پر نمی‌شود.

## دو نسخه دارد

| نسخه | فایل | چه چیزی لازم دارد | محدودیت حجم |
|------|------|-------------------|-------------|
| **پیش‌فرض** (ساده) | `bot.py` | فقط `BOT_TOKEN` | تا ~۴۸ مگابایت؛ فایل بزرگ‌تر → بات فقط **لینک** را می‌فرستد |
| **۲ گیگی** (اختیاری) | `bot_2gb.py` | `BOT_TOKEN` + `API_ID` + `API_HASH` | تا ~۲ گیگابایت واقعاً فایل را می‌فرستد |

> اگر می‌خواهی فایل‌های بزرگ هم مثل یک ویدیوی واقعی داخل تلگرام بیایند، سراغ نسخه‌ی ۲ گیگی برو
> (پایین همین صفحه توضیح داده شده). برای شروع، نسخه‌ی پیش‌فرض کافی است.

---

## پیش‌نیازها

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg
```

## نصب (نسخه‌ی پیش‌فرض)

```bash
sudo git clone -b soolakhi-bot https://github.com/IsaHo/IsaHo.git /opt/isaho-src
sudo mv /opt/isaho-src/soolakhi-bot /opt/soolakhi-bot
sudo rm -rf /opt/isaho-src
cd /opt/soolakhi-bot

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## تنظیمات

اگر فایل `.env` آماده را گرفته‌ای، همان را در `/opt/soolakhi-bot/.env` بگذار. وگرنه:

```bash
cp .env.example .env
nano .env     # BOT_TOKEN و ADMIN_IDS را پر کن
```

- `BOT_TOKEN` از **@BotFather**
- `ADMIN_IDS` آیدی عددی خودت از **@userinfobot**

> ⚠️ بعدش حتماً یک‌بار برو سراغ **باتِ خودت** و دکمه‌ی **Start** را بزن؛ تلگرام اجازه نمی‌دهد
> بات به کسی که اول خودش شروع نکرده پیام بفرستد.

## تست اسکرپر (مهم)

```bash
source .venv/bin/activate
python scraper.py --page 1
```

اگر لیستِ **عنوان + لینک + عکس** درست چاپ شد، عالی. اگر نه، در `.env` این سه مقدار را تنظیم کن و
دوباره تست کن: `ITEM_SELECTOR` / `TITLE_SELECTOR` / `THUMB_SELECTOR`
(و در صورت نیاز `LISTING_URL_TEMPLATE`).

## اجرای تستی

```bash
source .venv/bin/activate
python bot.py
```

در تلگرام برو سراغ باتت و بزن `/scan`. برای قطع: `Ctrl+C`.

## اجرای دائمی (systemd)

```bash
sudo cp /opt/soolakhi-bot/soolakhi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now soolakhi-bot
sudo systemctl status soolakhi-bot        # باید active (running) باشد
journalctl -u soolakhi-bot -f             # دیدن لاگ‌ها
```

دستورهای مفید:

```bash
sudo systemctl restart soolakhi-bot       # بعد از تغییر .env
sudo systemctl stop soolakhi-bot          # خاموش موقت
cd /opt/soolakhi-bot && sudo git pull && sudo systemctl restart soolakhi-bot   # آپدیت
```

---

## نسخه‌ی ۲ گیگی (اختیاری — برای فایل‌های بزرگ)

۱) `API_ID` و `API_HASH` را از <https://my.telegram.org> بگیر
(**API development tools → Create new application**) و در `.env` پر کن.

۲) کتابخانه‌های اضافه را نصب کن:

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-2gb.txt
```

۳) به‌جای `bot.py`، این را اجرا کن:

```bash
python bot_2gb.py
```

برای systemd هم کافی است در فایل سرویس، `bot.py` را به `bot_2gb.py` تغییر دهی.

---

## تنظیم حجم/کیفیت

برای کم‌کردن حجم ویدیوها، در `.env`:

```
YTDLP_FORMAT=best[height<=720][ext=mp4]/best[height<=720]/best
```

## اگر سایت نیاز به ورود دارد

کوکی‌ها را به فرمت Netscape اکسپورت کن و مسیرش را بده:

```
COOKIES_FILE=/opt/soolakhi-bot/cookies.txt
```

---

## چند نکته‌ی مهم ⚠️

- این ابزار برای **استفاده‌ی شخصی** و محتوایی است که حق دانلودش را داری. رعایت کپی‌رایت،
  شرایط استفاده‌ی سایت و قوانین تلگرام بر عهده‌ی خودت است.
- `ADMIN_IDS` را حتماً پر کن تا بات عمومی نشود.
- فایل `.env` توکن دارد؛ جایی عمومی نگذارش (در `.gitignore` هست).
