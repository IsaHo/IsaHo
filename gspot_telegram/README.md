# gspot → Telegram

A small daemon that scans a source page for GIFs/videos (`.gif`, `.mp4`,
`.webm`, `.webp`) and posts any it hasn't seen before to a Telegram chat. It
remembers what it already sent, so restarts don't re-post.

`.webp` files (what gspotwizard serves) are converted before sending, because
Telegram can't play animated webp: animated webp → animated GIF, static webp →
PNG. This needs Pillow (in `requirements.txt`).

## Pagination

The daemon walks forward through the site's pages: it sends the new media on the
current page, and once a page has nothing new left it moves to the next page.
Already-sent items (`sent_state.json`) are never resent, and the current page
position is remembered across restarts.

By default it auto-detects the "next page" link in the HTML. If a scan logs
`no next page found … reached the end` even though more pages exist, the site's
pagination isn't a plain link — set a numbered template in `.env` instead:

```
PAGE_URL_TEMPLATE=https://gspotwizard.com/page/{page}/
START_PAGE=1
```

To start the whole walk over from the beginning, stop the service and delete
`sent_state.json`.

## Setup

```bash
cd gspot_telegram
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: put in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID at minimum
```

Get a bot token from [@BotFather](https://t.me/BotFather). For `TELEGRAM_CHAT_ID`:
- **Private chat:** message [@userinfobot](https://t.me/userinfobot).
- **Channel/group:** add your bot as an admin, then use `@channelusername` or the
  numeric `-100…` id.

## Run

```bash
set -a; source .env; set +a      # load env vars
python gspot_to_telegram.py
```

Logs go to stdout. First run sends up to `MAX_PER_CYCLE` items, then only new
ones each cycle.

## Run as a service (systemd)

```bash
sudo cp -r . /opt/gspot_telegram            # or clone/deploy there
sudo cp gspot-telegram.service /etc/systemd/system/
# edit the service file: User, paths, and make sure /opt/gspot_telegram/.env exists
sudo systemctl daemon-reload
sudo systemctl enable --now gspot-telegram
journalctl -u gspot-telegram -f             # follow logs
```

## If nothing is found

The extractor looks at `<img>`, `<source>`, `<video>`, `<a>`, `og:` meta tags,
and does a regex sweep for media URLs. If a scan reports `0 media url(s)` even
though the page has gifs, the site probably loads them via JavaScript or uses a
custom markup. Two options:

1. Point the scraper at the exact element in `.env`:
   ```
   MEDIA_SELECTOR=a.gif-link
   MEDIA_ATTR=href
   ```
   (Inspect the page in your browser's devtools to find the right selector/attr.)
2. If the page is JS-rendered (the media isn't in "View Source"), the right page
   to put in `SOURCE_URLS` is usually the underlying API/JSON endpoint the site
   calls — find it in the devtools Network tab. Tell me the structure and I'll
   adapt the parser.

If the site needs an age-gate cookie, copy your browser's `Cookie` header into
the `COOKIE` variable.

## Notes

- Telegram bots can upload files up to **50 MB**; larger media is skipped.
- Only send content you have the right to redistribute, and respect the source
  site's terms of service.
