"""Entry point: Telegram bot + subscription server + usage monitor."""
import asyncio
import html
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import db
import fmt
import handlers
import sub_server
import xray
from config import cfg

log = logging.getLogger("isaho")

WARN_TRAFFIC_RATIO = 0.9
WARN_DAYS = 2


async def notify(bot: Bot, user, text: str) -> None:
    targets = set(cfg.admin_ids)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.warning("cannot notify admin %s", chat_id)
    if user.tg_id and user.tg_id not in targets:
        try:
            await bot.send_message(user.tg_id, text)
        except Exception:
            pass


async def check_users(bot: Bot) -> None:
    now = time.time()
    for u in db.active_users():
        name = html.escape(u.name)
        reason = "expired" if u.expired else "traffic" if u.over_limit else ""
        if reason:
            db.update(u.id, enabled=0, disabled_reason=reason)
            await xray.sync_user(u, False)
            why = "تاریخ انقضا رسید" if reason == "expired" else "حجم تمام شد"
            await notify(bot, u, f"⛔ اکانت <b>{name}</b> غیرفعال شد: {why}.")
            continue
        low_traffic = u.traffic_limit and u.used >= u.traffic_limit * WARN_TRAFFIC_RATIO
        low_time = u.expire_at and u.expire_at - now < WARN_DAYS * db.DAY
        if (low_traffic or low_time) and not u.warned:
            db.update(u.id, warned=1)
            await notify(bot, u, f"⚠️ اکانت <b>{name}</b> رو به اتمام است.\n\n{fmt.user_card(u)}")


async def monitor(bot: Bot) -> None:
    while True:
        try:
            await xray.flush_stats()
            await check_users(bot)
            last = int(db.get_setting("last_backup", "0"))
            if time.time() - last > db.DAY:
                db.set_setting("last_backup", str(int(time.time())))
                for admin_id in cfg.admin_ids:
                    await handlers.send_backup(bot, admin_id)
        except Exception:
            log.exception("monitor iteration failed")
        await asyncio.sleep(cfg.stats_interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not cfg.bot_token or not cfg.admin_ids:
        raise SystemExit("BOT_TOKEN and ADMIN_IDS must be set in " + "/etc/isaho-vpn/vpn.env")
    db.init()
    await xray.apply_all()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    db.set_setting("bot_username", me.username)
    await bot.set_my_commands([BotCommand(command="start", description="منوی اصلی")])

    dp = Dispatcher()
    dp.include_router(handlers.router)

    runner = await sub_server.start()
    task = asyncio.create_task(monitor(bot))
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, "🚀 ربات و سرور VPN روشن شد. /start")
        except Exception:
            pass
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await runner.cleanup()
        await xray.flush_stats()


if __name__ == "__main__":
    asyncio.run(main())
