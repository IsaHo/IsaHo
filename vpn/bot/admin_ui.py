"""Compact, progressive admin navigation for the Telegram control panel."""

import html
import time

import db
import handlers as h
import health
import healthdb
import links
import nodes
import shop
import shopdb
import support
import supportdb
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from config import cfg

router = Router()


def _iran_health() -> tuple[int, int]:
    expected = {key for key in health._expected_paths() if key.startswith("relay:")}
    fresh = [
        row
        for row in health.current_checks()
        if row.origin == "iran" and time.time() - row.checked_at <= health.FRESH
    ]
    return sum(bool(row.ok) for row in fresh), len(expected)


def command_center() -> tuple[str, object]:
    users = db.all_users()
    active = sum(bool(user.enabled) for user in users)
    expiring = sum(
        bool(
            user.enabled
            and user.expire_at
            and user.expire_at - time.time() < 3 * db.DAY
        )
        for user in users
    )
    tickets = supportdb.counts()
    open_tickets = sum(tickets.get(key, 0) for key in supportdb.OPEN_STATUSES)
    healthy, paths = _iran_health()
    open_incidents = len(healthdb.incidents(100, active_only=True))
    path_text = f"{healthy}/{paths}" if paths else "در حال جمع‌آوری"
    text = (
        f"🏠 <b>مرکز فرماندهی {html.escape(cfg.brand)}</b>\n"
        "<i>تصویر کوتاه از همین لحظه</i>\n\n"
        f"👥 مشتریان: <b>{len(users)}</b> · فعال {active} · نزدیک پایان {expiring}\n"
        f"🛍 سفارش‌های منتظر: <b>{len(shopdb.pending_orders())}</b>\n"
        f"🎫 تیکت‌های باز: <b>{open_tickets}</b>\n"
        f"🇮🇷 مسیرهای سالم از ایران: <b>{path_text}</b>\n"
        f"🚨 رخدادهای باز: <b>{open_incidents}</b>\n\n"
        "یک بخش را انتخاب کنید؛ کارهای حساس قبل از اجرا تأیید می‌خواهند."
    )
    keyboard = h.ikb(
        [
            [("➕ کاربر جدید", "nav:add"), ("🔎 جستجوی سریع", "nav:search")],
            [("👥 مشتریان", "nav:customers"), ("🛍 فروش و درآمد", "nav:shop")],
            [("🧭 شبکه و سلامت", "nav:ops"), ("🎫 پشتیبانی", "nav:tickets")],
            [("⚙️ مدیریت", "nav:manage"), ("🔄 تازه‌سازی", "nav:home")],
        ]
    )
    return text, keyboard


def customers_view() -> tuple[str, object]:
    users = db.all_users()
    active = sum(bool(user.enabled) for user in users)
    return (
        "👥 <b>مرکز فرماندهی › مشتریان</b>\n\n"
        f"کل: <b>{len(users)}</b> · فعال: <b>{active}</b> · "
        f"غیرفعال: <b>{len(users) - active}</b>\n"
        "ساخت، پیدا کردن و نگهداری اکانت‌ها از اینجا انجام می‌شود.",
        h.ikb(
            [
                [("➕ ساخت کاربر", "nav:add"), ("🔎 جستجو", "nav:search")],
                [
                    ("👥 فهرست کاربران", "nav:user-list"),
                    ("🧰 عملیات گروهی", "nav:bulk"),
                ],
                [("📱 محدودیت دستگاه", "dev:menu")],
                [("🏠 مرکز فرماندهی", "nav:home")],
            ]
        ),
    )


def operations_view() -> tuple[str, object]:
    healthy, paths = _iran_health()
    return (
        "🧭 <b>مرکز فرماندهی › شبکه و سلامت</b>\n\n"
        f"🇮🇷 دید واقعی مشتری: <b>{healthy}/{paths}</b> مسیر سالم\n"
        f"🌍 نودهای خارج: <b>{len(nodes.all_nodes())}</b>\n"
        "پایش روزمره از عملیات حساس جدا شده تا خطای ناخواسته کمتر شود.",
        h.ikb(
            [
                [("🧭 سلامت مسیرها", "ph:menu"), ("🛡 مرکز تاب‌آوری", "rs:menu")],
                [("📊 وضعیت سرور", "nav:status"), ("📈 داشبورد مصرف", "nav:dashboard")],
                [("🛰 سرورهای ایران", "rl:menu"), ("🌍 سرورهای خارج", "nd:menu")],
                [("🏠 مرکز فرماندهی", "nav:home")],
            ]
        ),
    )


def management_view() -> tuple[str, object]:
    return (
        "⚙️ <b>مرکز فرماندهی › مدیریت</b>\n\n"
        "تنظیمات بر اساس نوع اثر دسته‌بندی شده‌اند. گزینه‌های عملیاتی خطرناک "
        "در بخش نگهداری قرار دارند.",
        h.ikb(
            [
                [
                    ("🧩 اتصال و شبکه", "nav:set:network"),
                    ("👮 دسترسی و دستگاه", "nav:set:access"),
                ],
                [
                    ("💾 داده و اعلان‌ها", "nav:set:data"),
                    ("🛠 نگهداری سیستم", "nav:set:system"),
                ],
                [("📢 پیام همگانی", "nav:broadcast")],
                [("🏠 مرکز فرماندهی", "nav:home")],
            ]
        ),
    )


def network_settings_view() -> tuple[str, object]:
    relays_text = ", ".join(host for host, _ in links.relays()) or "تعریف نشده"
    return (
        "🧩 <b>مدیریت › اتصال و شبکه</b>\n\n"
        f"دامنه: <code>{html.escape(cfg.domain)}</code>\n"
        f"سرورهای ایران: <code>{html.escape(relays_text)}</code>\n"
        f"پورت CDN: <b>{links.cdn_public_port()}</b>",
        h.ikb(
            [
                [("🔗 نوع لینک‌ها", "lt:menu"), ("🇮🇷 آدرس رله‌ها", "set:relays")],
                [("🛰 مدیریت ایران", "rl:menu"), ("🌍 مدیریت خارج", "nd:menu")],
                [("🌐 IP تمیز CDN", "set:cdn"), ("🧪 دستور تست رله", "set:relaytest")],
                [
                    ("پورت CDN · 443", "set:port:443"),
                    (f"پورت CDN · {cfg.cdn_port}", f"set:port:{cfg.cdn_port}"),
                ],
                [("↩️ مدیریت", "nav:manage"), ("🏠 خانه", "nav:home")],
            ]
        ),
    )


def access_settings_view() -> tuple[str, object]:
    return (
        "👮 <b>مدیریت › دسترسی و دستگاه</b>\n\n"
        "مدیران، تیم پشتیبانی و سیاست تعداد دستگاه‌ها را از این بخش کنترل کنید.",
        h.ikb(
            [
                [
                    ("👮 مدیران و پشتیبان‌ها", "adm:menu"),
                    ("📱 محدودیت دستگاه", "dev:menu"),
                ],
                [("↩️ مدیریت", "nav:manage"), ("🏠 خانه", "nav:home")],
            ]
        ),
    )


def data_settings_view() -> tuple[str, object]:
    backup = "تنظیم شده" if db.get_setting("backup_chat") else "تنظیم نشده"
    status = "تنظیم شده" if db.get_setting("status_chat") else "تنظیم نشده"
    return (
        "💾 <b>مدیریت › داده و اعلان‌ها</b>\n\n"
        f"کانال بکاپ: <b>{backup}</b>\nکانال وضعیت: <b>{status}</b>",
        h.ikb(
            [
                [("💾 بکاپ همین حالا", "nav:backup")],
                [("📦 کانال بکاپ", "set:bchat"), ("📣 کانال وضعیت", "set:schat")],
                [("↩️ مدیریت", "nav:manage"), ("🏠 خانه", "nav:home")],
            ]
        ),
    )


def system_settings_view() -> tuple[str, object]:
    return (
        "🛠 <b>مدیریت › نگهداری سیستم</b>\n\n"
        "این بخش روی سرویس زنده اثر می‌گذارد؛ قبل از هر اقدام حساس یک تأیید "
        "جداگانه نمایش داده می‌شود.",
        h.ikb(
            [
                [("📊 مشاهده وضعیت", "nav:status")],
                [
                    ("🔄 ری‌استارت Xray", "nav:restart:ask"),
                    ("🧱 بازسازی کانفیگ", "nav:rebuild:ask"),
                ],
                [("↩️ مدیریت", "nav:manage"), ("🏠 خانه", "nav:home")],
            ]
        ),
    )


async def _show(target: Message | CallbackQuery, view) -> None:
    text, keyboard = view()
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise
    else:
        await target.answer(text, reply_markup=keyboard)


@router.message(F.text == h.BTN_ADMIN_HOME, h.admin)
async def home_message(msg: Message):
    await _show(msg, command_center)


@router.message(F.text == h.BTN_CUSTOMERS, h.admin)
async def customers_message(msg: Message):
    await _show(msg, customers_view)


@router.message(F.text == h.BTN_OPERATIONS, h.admin)
async def operations_message(msg: Message):
    await _show(msg, operations_view)


@router.message(F.text == h.BTN_MANAGEMENT, h.admin)
async def management_message(msg: Message):
    await _show(msg, management_view)


@router.callback_query(F.data == "nav:home", h.admin)
async def home_callback(cb: CallbackQuery):
    await _show(cb, command_center)


@router.callback_query(F.data == "nav:customers", h.admin)
async def customers_callback(cb: CallbackQuery):
    await _show(cb, customers_view)


@router.callback_query(F.data == "nav:ops", h.admin)
async def operations_callback(cb: CallbackQuery):
    await _show(cb, operations_view)


@router.callback_query(F.data == "nav:manage", h.admin)
async def management_callback(cb: CallbackQuery):
    await _show(cb, management_view)


@router.callback_query(F.data == "nav:set:network", h.admin)
async def network_settings_callback(cb: CallbackQuery):
    await _show(cb, network_settings_view)


@router.callback_query(F.data == "nav:set:access", h.admin)
async def access_settings_callback(cb: CallbackQuery):
    await _show(cb, access_settings_view)


@router.callback_query(F.data == "nav:set:data", h.admin)
async def data_settings_callback(cb: CallbackQuery):
    await _show(cb, data_settings_view)


@router.callback_query(F.data == "nav:set:system", h.admin)
async def system_settings_callback(cb: CallbackQuery):
    await _show(cb, system_settings_view)


@router.callback_query(F.data == "nav:add", h.admin)
async def add_user_callback(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await h.add_start(cb.message, state)


@router.callback_query(F.data == "nav:search", h.admin)
async def search_callback(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await h.search_start(cb.message, state)


@router.callback_query(F.data == "nav:user-list", h.admin)
async def user_list_callback(cb: CallbackQuery):
    await cb.answer()
    await h.users_list(cb.message)


@router.callback_query(F.data == "nav:bulk", h.admin)
async def bulk_callback(cb: CallbackQuery):
    await cb.answer()
    await h.bulk_menu(cb.message)


@router.callback_query(F.data == "nav:status", h.admin)
async def status_callback(cb: CallbackQuery):
    await cb.answer("در حال دریافت وضعیت…")
    await h.server_status(cb.message)


@router.callback_query(F.data == "nav:dashboard", h.admin)
async def dashboard_callback(cb: CallbackQuery):
    await cb.answer("در حال آماده‌سازی داشبورد…")
    await h.dashboard(cb.message)


@router.callback_query(F.data == "nav:shop", h.admin)
async def shop_callback(cb: CallbackQuery):
    await cb.answer()
    await shop.shop_menu(cb.message)


@router.callback_query(F.data == "nav:tickets", h.staff)
async def tickets_callback(cb: CallbackQuery):
    await cb.answer()
    await support.show_staff_list(cb.message)


@router.callback_query(F.data == "nav:broadcast", h.admin)
async def broadcast_callback(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await h.broadcast_start(cb.message, state)


@router.callback_query(F.data == "nav:backup", h.admin)
async def backup_callback(cb: CallbackQuery, bot: Bot):
    await cb.answer("در حال ساخت بکاپ…")
    await h.backup_cmd(cb.message, bot)


@router.callback_query(F.data.in_({"nav:restart:ask", "nav:rebuild:ask"}), h.admin)
async def system_action_ask(cb: CallbackQuery):
    await cb.answer()
    rebuild = cb.data.endswith("rebuild:ask")
    action = "بازسازی کانفیگ Xray" if rebuild else "ری‌استارت Xray"
    callback = "set:rebuild" if rebuild else "set:restart"
    await cb.message.edit_text(
        f"⚠️ <b>{action}</b>\n\n"
        "این کار ممکن است اتصال کاربران را برای چند ثانیه قطع کند. ادامه می‌دهید؟",
        reply_markup=h.ikb(
            [[("✅ بله، اجرا کن", callback), ("❌ منصرف شدم", "nav:set:system")]]
        ),
    )
