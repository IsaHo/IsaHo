"""Entry point: Telegram bot + subscription server + usage monitor."""
import asyncio
import html
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from telegram_session import TelegramSession

import db
import admin_ui
import fmt
import handlers
import health
import healthdb
import shop
import shopdb
import support
import supportdb
import devices
import links
import membership
import relays
import reports
import resilience
import routing
import partners
import partnerwork
import sub_server
import tunnels
import xray
from config import cfg

log = logging.getLogger("isaho")

WARN_TRAFFIC_RATIO = 0.9
WARN_DAYS = 2


async def notify(bot: Bot, user, text: str) -> None:
    targets = db.admin_ids()
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
    for u in db.all_users():
        if not u.enabled:
            continue
        name = html.escape(u.name)
        reason = "expired" if u.expired else "traffic" if u.over_limit else ""
        if reason:
            db.update(u.id, enabled=0, disabled_reason=reason)
            await handlers.apply_user(db.get(u.id))
            why = "تاریخ انقضا رسید" if reason == "expired" else "حجم تمام شد"
            await notify(bot, u, f"⛔ اکانت <b>{name}</b> غیرفعال شد: {why}.")
            continue
        low_traffic = u.traffic_limit and u.used >= u.traffic_limit * WARN_TRAFFIC_RATIO
        low_time = u.expire_at and u.expire_at - now < WARN_DAYS * db.DAY
        if (low_traffic or low_time) and not u.warned:
            db.update(u.id, warned=1)
            await notify(bot, u, f"⚠️ اکانت <b>{name}</b> رو به اتمام است.\n\n{fmt.user_card(u)}")


_tunnel_seen = {}   # host -> True once we have seen it connected (NAT-style relays never are)
_tunnel_down = {}   # host -> consecutive checks with zero sessions
DOWN_AFTER = 2


async def check_tunnels(bot: Bot) -> None:
    for label, host, _, n in tunnels.status():
        if n:
            if _tunnel_down.get(host, 0) >= DOWN_AFTER:
                await notify_admins(bot, f"🟢 تانل {label} (<code>{host}</code>) دوباره وصل شد ({n} اتصال).")
                await notify_status(bot, f"✅ سرور {label} دوباره در دسترس است.")
            _tunnel_seen[host] = True
            _tunnel_down[host] = 0
            continue
        if not _tunnel_seen.get(host):
            continue
        _tunnel_down[host] = _tunnel_down.get(host, 0) + 1
        if _tunnel_down[host] == DOWN_AFTER:
            await notify_admins(bot, f"🔴 تانل {label} (<code>{host}</code>) قطع شد!\n"
                                     "کاربرانی که از این سرور واسط وصل‌اند الان قطع هستند.\n"
                                     "روی سرور ایران بررسی کنید: <code>systemctl status 'isaho-tunnel@*'</code>")
            others = [lb for lb, _, _, k in tunnels.status() if k and lb != label]
            await notify_status(bot, f"⚠️ سرور {label} موقتاً در دسترس نیست و در حال رفع مشکل است."
                                     + (f"\nلطفاً فعلاً از کانفیگ {' یا '.join(others)} استفاده کنید." if others else ""))


_device_warned = {}  # name -> last warning time
_device_over = {}    # name -> consecutive checks over the limit
DEVICE_CONFIRM = 5   # ~5 minutes
DEVICE_BAN = 15 * 60


async def auto_real_ip(bot: Bot) -> None:
    """Turn on real client IPs (needed for device limits) once, as soon as every relay can follow."""
    if db.get_setting("real_ip_auto") or db.get_setting("real_ip") == "1" or not links.relays():
        return
    ok, _ = relays.ready_for_real_ip()
    if not ok:
        return
    db.set_setting("real_ip", "1")
    if not xray.split_relay_inbound():
        db.set_setting("real_ip", "")
        db.set_setting("real_ip_auto", "unavailable")
        return
    try:
        await xray.apply_all()
    except Exception:
        log.exception("enabling real IPs failed")
        db.set_setting("real_ip", "")
        await xray.apply_all()
        db.set_setting("real_ip_auto", "failed")
        return
    db.set_setting("real_ip_auto", "done")
    await notify_admins(bot, "📱 محدودیت دستگاه فعال شد (حد پیش‌فرض ۲، فقط هشدار). "
                             "از ⚙️ تنظیمات ← 📱 محدودیت دستگاه قابل تغییر است.")


async def check_devices(bot: Bot) -> None:
    now = time.time()
    # lift temporary device-limit suspensions
    for u in db.all_users():
        if not u.enabled and u.disabled_reason.startswith("iplimit:") and now >= int(u.disabled_reason[8:]):
            db.update(u.id, enabled=1, disabled_reason="")
            await handlers.apply_user(db.get(u.id))
    if db.get_setting("real_ip") != "1":
        return
    devices.scan()
    default = int(db.get_setting("ip_limit_default", "0") or 0)
    action = db.get_setting("ip_limit_action", "warn")
    for u in db.active_users():
        limit = u.ip_limit or default
        n = devices.count(u.name)
        if not limit or n <= limit:
            _device_over.pop(u.name, None)
            continue
        # act only when it persists, so a phone switching Wi-Fi/data is not punished
        _device_over[u.name] = _device_over.get(u.name, 0) + 1
        if _device_over[u.name] < DEVICE_CONFIRM:
            continue
        name = html.escape(u.name)
        nets = ", ".join(devices.networks(u.name)[:8])
        if action == "disable":
            db.update(u.id, enabled=0, disabled_reason=f"iplimit:{int(now + DEVICE_BAN)}")
            await handlers.apply_user(db.get(u.id))
            await notify(bot, u, f"⛔ اکانت <b>{name}</b> با {n} دستگاه هم‌زمان (حد: {limit}) "
                                 "استفاده شد و ۱۵ دقیقه قطع شد.")
        elif now - _device_warned.get(u.name, 0) > 6 * 3600:
            # warnings go to admins only; customers hear about it only if they are suspended
            _device_warned[u.name] = now
            await notify_admins(bot, f"⚠️ اکانت <b>{name}</b> از {n} شبکه‌ی مختلف هم‌زمان استفاده می‌شود "
                                     f"(حد: {limit}).\n<code>{nets}</code>")


async def notify_status(bot: Bot, text: str) -> None:
    """Customer-facing status updates to the public status channel, if set."""
    chat = db.get_setting("status_chat")
    if chat:
        try:
            await bot.send_message(int(chat), text)
        except Exception:
            log.warning("cannot post to status channel")


async def notify_admins(bot: Bot, text: str) -> None:
    for chat_id in db.admin_ids():
        try:
            await bot.send_message(chat_id, text)
        except Exception:
            log.warning("cannot notify admin %s", chat_id)


async def monitor(bot: Bot) -> None:
    while True:
        try:
            await xray.flush_stats()
            await check_users(bot)
            await check_tunnels(bot)
            shopdb.expire_waiting(int(time.time()) - 2 * db.DAY)
            await auto_real_ip(bot)
            await shop.renewal_reminders(bot)
            await reports.winback(bot, handlers.ikb)
            await reports.weekly_report(bot, notify_admins)
            await reports.capacity(bot, notify_admins)
            await check_devices(bot)
            await partnerwork.recover(bot)
            last = int(db.get_setting("last_backup", "0"))
            if time.time() - last > db.DAY:
                db.set_setting("last_backup", str(int(time.time())))
                targets = list(cfg.admin_ids)
                if db.get_setting("backup_chat"):
                    targets.append(int(db.get_setting("backup_chat")))
                for chat_id in targets:
                    try:
                        await handlers.send_backup(bot, chat_id)
                    except Exception:
                        log.warning("backup to %s failed", chat_id)
        except Exception:
            log.exception("monitor iteration failed")
        await asyncio.sleep(cfg.stats_interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not cfg.bot_token or not cfg.admin_ids:
        raise SystemExit("BOT_TOKEN and ADMIN_IDS must be set in " + "/etc/isaho-vpn/vpn.env")
    db.init()
    shopdb.init()
    supportdb.init()
    healthdb.init()
    shop.apply_defaults()
    await xray.ensure_started()

    bot = Bot(cfg.bot_token, session=TelegramSession(),
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    shop.BOT = bot
    me = await bot.get_me()
    db.set_setting("bot_username", me.username)
    await bot.set_my_commands([BotCommand(command="start", description="منوی اصلی")])

    dp = Dispatcher()
    dp.include_router(membership.router)
    dp.include_router(partnerwork.router)
    dp.include_router(partners.router)
    dp.include_router(shop.router)  # customer /start and shop states first
    dp.include_router(health.router)
    dp.include_router(resilience.router)
    dp.include_router(routing.router)
    dp.include_router(support.router)
    dp.include_router(admin_ui.router)
    dp.include_router(handlers.router)

    runner = await sub_server.start()
    task = asyncio.create_task(monitor(bot))
    health_task = asyncio.create_task(health.monitor(bot))
    membership_task = asyncio.create_task(membership.monitor(bot))
    for admin_id in cfg.admin_ids:
        try:
            await bot.send_message(admin_id, "🚀 ربات و سرور VPN روشن شد. /start")
        except Exception:
            pass
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        health_task.cancel()
        membership_task.cancel()
        await runner.cleanup()
        await xray.flush_stats()


if __name__ == "__main__":
    asyncio.run(main())
