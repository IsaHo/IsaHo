"""Native Telegram partner workspace; no automatic external money transfers."""
import html
import logging
import re
import secrets
from urllib.parse import quote

import db
import handlers as h
import partnerdb as store
import shopdb
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from config import cfg

router = Router()
log = logging.getLogger(__name__)
BUTTON = "🤝 پنل نمایندگی"


class Edit(StatesGroup):
    bind = State()
    bindconfirm = State()
    rate = State()
    amount = State()
    destination = State()
    confirm = State()
    decision = State()


def cash(n):
    return f"{n:,} تومان"


def integer(text):
    raw = (text or '').translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    raw = re.sub(r"[\s,٬]", '', raw)
    if not re.fullmatch(r"[0-9]{1,19}", raw):
        return None
    n = int(raw)
    return n if n <= 2**63-1 else None


def allowed(tg_id):
    p = store.profile(tg_id)
    # A disabled partner can still see past earnings and pending settlement.
    return p is not None


def customer_kb(tg_id):
    if not allowed(tg_id):
        return h.USER_KB
    return h.USER_KB.model_copy(update={"keyboard": [*h.USER_KB.keyboard,
                                      [h.KeyboardButton(text=BUTTON)]]})


def dashboard(tg_id):
    p, s = store.profile(tg_id), store.summary(tg_id)
    status = "فعال" if p["enabled"] else "متوقف؛ سوابق مالی محفوظ است"
    text = ("🤝 <b>ایستگاه نمایندگی</b>\n"
            f"{html.escape(cfg.brand)} • {status}\n\n"
            f"💎 قابل برداشت: <b>{cash(s['available'])}</b>\n"
            f"⏳ در انتظار تسویه: {cash(s['reserved'])}\n"
            f"✅ برداشت‌شده: {cash(s['paid'])}\n\n"
            f"👥 مشتری معرفی‌شده: {s['customers']}\n"
            f"🛍 فروش دارای پورسانت: {s['sales']}\n"
            f"📈 کل درآمد: {cash(s['earned'])}\n"
            f"🎯 پورسانت فروش مستقیم: <b>{p['percent']}٪</b>\n\n"
            "خرید، تمدید و حجم اضافهٔ مشتریِ معرفی‌شده، پس از تأیید پرداخت و تحویل سرویس، "
            "پورسانت دارد. مبنا فقط مبلغ پرداخت نقدی است؛ اعتبار هدیه شامل آن نمی‌شود.")
    rows = [[("🔗 لینک اختصاصی", "rp:link"), ("👥 مشتری‌ها", "rp:customers:0")],
            [("📒 ریز درآمد", "rp:sales:0"), ("🧾 برداشت‌های من", "rp:withdrawals:0")]]
    if s['available'] > 0:
        rows.append([("💸 درخواست برداشت", "rp:withdraw")])
    rows.append([("🔄 تازه‌سازی", "rp:home")])
    return text, h.ikb(rows)


@router.message(Command("partner"))
@router.message(F.text == BUTTON)
async def home(msg: Message, state: FSMContext):
    await state.clear()
    if not allowed(msg.from_user.id):
        await msg.answer("پنل ویژهٔ نمایندگان تأییدشده است؛ برای درخواست نمایندگی با پشتیبانی تماس بگیرید.")
        return
    text, kb = dashboard(msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


def paged(kind, page, has_next):
    row = []
    if page:
        row.append(("‹ قبلی", f"rp:{kind}:{page-1}"))
    if has_next:
        row.append(("بعدی ›", f"rp:{kind}:{page+1}"))
    return h.ikb(([row] if row else []) + [[("↩️ نمایندگی", "rp:home")]])


@router.callback_query(F.data.startswith("rp:"))
async def callback(cb: CallbackQuery, state: FSMContext, bot):
    if not allowed(cb.from_user.id):
        await cb.answer("نمایندگی ثبت نشده است", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]
    tg = cb.from_user.id
    await cb.answer()
    if action == "home":
        await state.clear()
        text, kb = dashboard(tg)
        await cb.message.answer(text, reply_markup=kb)
    elif action == "link":
        link = f"https://t.me/{db.get_setting('bot_username')}?start=ref_{tg}"
        share = "برای خرید و پشتیبانی VPN از لینک اختصاصی من وارد شوید؛ اتصال امن با پشتیبانی همراه شما."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 اشتراک با دوستان", url=f"https://t.me/share/url?url={quote(link)}&text={quote(share)}")],
            [InlineKeyboardButton(text="↩️ نمایندگی", callback_data="rp:home")]])
        await cb.message.answer("🔗 <b>ویترین اختصاصی شما</b>\n\n"
            f"<code>{html.escape(link)}</code>\n\n"
            "مشتری باید اولین ورودش به ربات از این لینک باشد. خریدهای بعدی هم به شما نسبت داده می‌شوند؛ "
            "لینک جدید معرف قبلی را عوض نمی‌کند. مشتری‌های قدیمی خودکار منتقل نمی‌شوند.", reply_markup=kb)
    elif action in ("sales", "customers", "withdrawals"):
        try:
            page = max(0, min(100000, int(parts[2])))
        except (ValueError, IndexError):
            return
        records = {"sales": store.history, "customers": store.customers, "withdrawals": store.withdrawals}[action](tg, page)
        lines = [{"sales": "📒 <b>ریز درآمد</b>", "customers": "👥 <b>مشتری‌های معرفی‌شده</b>",
                  "withdrawals": "🧾 <b>برداشت‌های من</b>"}[action], ""]
        for r in records:
            if action == "sales":
                lines.append(f"سفارش #{r['order_id']} • {r['percent']}٪ • +{cash(r['amount'])}")
            elif action == "customers":
                lines.append(f"• {html.escape(r['name'][:45] or 'مشتری')} — {r['purchases']} خرید تأییدشده")
            else:
                label = {"pending": "⏳ در انتظار", "paid": "✅ پرداخت‌شده", "rejected": "↩️ رد و آزادشده"}[r['status']]
                lines.append(f"#{r['id']} • {cash(r['amount'])} • {label}")
        if not records:
            lines.append("هنوز موردی ثبت نشده است.")
        await cb.message.answer("\n".join(lines), reply_markup=paged(action, page, len(records) == 10))
    elif action == "withdraw":
        if store.summary(tg)['available'] <= 0:
            await cb.message.answer("موجودی قابل برداشت ندارید.")
            return
        await state.clear()
        await state.set_state(Edit.amount)
        await cb.message.answer("💸 مبلغ برداشت را به <b>تومان</b> وارد کنید.\n"
                                f"قابل برداشت: {cash(store.summary(tg)['available'])}", reply_markup=h.CANCEL_KB)
    elif action == "submit":
        if await state.get_state() != Edit.confirm.state:
            await cb.message.answer("این فرم منقضی شده؛ برداشت‌های من را بررسی کنید.")
            return
        data = await state.get_data()
        try:
            r, created = store.request(tg, data['amount'], data['destination'], data['request_key'])
        except ValueError as e:
            await cb.message.answer(html.escape(str(e)))
            return
        await state.clear()
        await cb.message.answer(f"✅ درخواست #{r['id']} ثبت شد؛ {cash(r['amount'])} تا بررسی رزرو شده است.\n"
                                "انتقال بانکی خودکار نیست؛ پس از بررسی و واریز، تأیید دریافت می‌کنید.",
                                reply_markup=customer_kb(tg))
        if created:
            for owner in cfg.admin_ids:
                try:
                    await bot.send_message(owner, f"💸 برداشت نمایندگی #{r['id']} • {cash(r['amount'])}",
                        reply_markup=h.ikb([[("🧾 بررسی درخواست", f"pa:request:{r['id']}")]]))
                except TelegramAPIError:
                    log.warning("partner withdrawal notification failed; request=%s", r['id'])


@router.message(Edit.amount)
async def amount(msg: Message, state: FSMContext):
    if msg.text == h.BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=customer_kb(msg.from_user.id))
        return
    n = integer(msg.text)
    if n is None or n != int(n) or not 0 < n <= store.summary(msg.from_user.id)['available']:
        await msg.answer("مبلغ باید عدد صحیح مثبت و حداکثر موجودی قابل برداشت باشد.")
        return
    await state.update_data(amount=int(n), request_key=secrets.token_urlsafe(24))
    await state.set_state(Edit.destination)
    await msg.answer("🏦 شماره شبای IR و ۲۴ رقم، سپس | و نام صاحب حساب را بفرستید.\n"
                     "مثال: <code>IR… | نام صاحب حساب</code>\nاطلاعات فقط در درخواست برداشت نگهداری می‌شود.")


@router.message(Edit.destination)
async def destination(msg: Message, state: FSMContext):
    if msg.text == h.BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=customer_kb(msg.from_user.id))
        return
    raw = (msg.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    parts = raw.split("|", 1)
    iban = re.sub(r"[\s-]", "", parts[0]).upper()
    name = parts[1].strip() if len(parts) == 2 else ""
    if not re.fullmatch(r"IR\d{24}", iban) or not 2 <= len(name) <= 80:
        await msg.answer("قالب: <code>IR و ۲۴ رقم | نام صاحب حساب</code>")
        return
    if int(iban[4:] + str(ord('I')-55) + str(ord('R')-55) + iban[2:4]) % 97 != 1:
        await msg.answer("شماره شبا از نظر رقم کنترل معتبر نیست؛ دوباره بررسی کنید.")
        return
    await state.update_data(destination=f"{iban} | {name}")
    await state.set_state(Edit.confirm)
    data = await state.get_data()
    await msg.answer(f"🧾 <b>پیش‌نمایش برداشت</b>\n\n{cash(data['amount'])}\n"
                     f"{html.escape(name)}\n<code>{iban}</code>\n\nپس از تأیید، مبلغ تا بررسی مالک رزرو می‌شود.",
                     reply_markup=h.ikb([[("✅ ثبت درخواست", "rp:submit")], [("↩️ انصراف", "rp:home")]]))


def admin_dashboard(page=0):
    ps = store.partners()
    pending = store.pending_count()
    rows = [[(f"{'🟢' if p['enabled'] else '⏸'} {p['name'][:24] or p['tg_id']} · {p['percent']}٪",
              f"pa:partner:{p['tg_id']}")] for p in ps[page*10:(page+1)*10]]
    pagination = []
    if page:
        pagination.append(("‹ قبلی", f"pa:list:{page-1}"))
    if len(ps) > (page+1)*10:
        pagination.append(("بعدی ›", f"pa:list:{page+1}"))
    if pagination:
        rows.append(pagination)
    rows += [[("➕ نمایندهٔ جدید", "sr:add"), ("💸 تسویه‌ها", "pa:pending")],
             [("↩️ رشد فروش", "sa:section:growth")]]
    return (("🤝 <b>مرکز همکاری و نمایندگی</b>\n\n"
            f"نمایندگان: {len(ps)} • درخواست‌های در انتظار: {pending}\n"
            "تخفیف خرید عمده و پورسانت فروش مستقیم مستقل‌اند. نرخ اولیهٔ پورسانت صفر است؛ "
            "از کارت هر نماینده تعیین کنید. تسویه فقط با تأیید مالک ثبت می‌شود."), h.ikb(rows))


@router.callback_query(F.data == "sa:resellers", h.admin)
async def admin_home(cb: CallbackQuery):
    await cb.answer()
    text, kb = admin_dashboard()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pa:"), h.owner)
async def admin_callback(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in cfg.admin_ids:
        await cb.answer("این بخش فقط برای مالک است", show_alert=True)
        return
    await cb.answer()
    parts = cb.data.split(":")
    action = parts[1]
    if action == "list":
        try:
            page = max(0, min(100000, int(parts[2])))
        except (ValueError, IndexError):
            return
        text, kb = admin_dashboard(page)
        await cb.message.answer(text, reply_markup=kb)
        return
    if action == "bindconfirm":
        if await state.get_state() != Edit.bindconfirm.state:
            await cb.message.answer("فرم منقضی شده؛ دوباره شروع کنید.")
            return
        data = await state.get_data()
        try:
            store.assign(data['partner_id'], data['customer_id'], cb.from_user.id)
        except ValueError as e:
            await cb.message.answer(html.escape(str(e)))
            return
        await state.clear()
        await cb.message.answer("✅ مشتری متصل شد؛ فقط سفارش‌های بعدی پورسانت دارند.", reply_markup=h.ADMIN_KB)
        return
    if action == "home":
        await state.clear()
        text, kb = admin_dashboard()
        await cb.message.answer(text, reply_markup=kb)
        return
    if action == "pending":
        rows = [[(f"#{r['id']} • {cash(r['amount'])}", f"pa:request:{r['id']}")] for r in store.withdrawals()]
        await cb.message.answer("💸 <b>صف تسویه</b>\nانتقال وجه را خارج از ربات انجام دهید؛ سپس شناسه انتقال را ثبت کنید."
                                + ("\nدرخواستی نیست." if not rows else ""),
                                reply_markup=h.ikb(rows + [[("↩️ نمایندگان", "pa:home")]]))
        return
    try:
        target = int(parts[2])
    except (IndexError, ValueError):
        return
    if action in ("partner", "rate", "pause", "pauseok", "resume", "bind"):
        p = store.profile(target)
        if not p:
            return
        if action == "bind":
            await state.set_state(Edit.bind)
            await state.update_data(partner_id=target)
            await cb.message.answer("آیدی عددی تلگرام مشتری قدیمی را وارد کنید.\n"
                                    "باید قبلاً /start کرده و معرف دیگری نداشته باشد.", reply_markup=h.CANCEL_KB)
            return
        if action == "rate":
            await state.set_state(Edit.rate)
            await state.update_data(partner_id=target)
            await cb.message.answer("🎯 درصد پورسانت فروش مستقیم را از ۰ تا ۱۰۰ وارد کنید.\n"
                                    "فقط سفارش‌های جدید مشمول نرخ جدید می‌شوند؛ تخفیف خرید تغییر نمی‌کند.", reply_markup=h.CANCEL_KB)
            return
        if action == "pause":
            await cb.message.answer("⏸ نمایندگی متوقف شود؟ پورسانت سفارش‌های جدید صفر می‌شود؛ "
                "طلب قبلی و درخواست‌های تسویه محفوظ‌اند.", reply_markup=h.ikb([
                    [("⏸ تأیید توقف", f"pa:pauseok:{target}")], [("↩️ بازگشت", f"pa:partner:{target}")]]))
            return
        if action in ("pauseok", "resume"):
            store.configure(target, p['percent'], action == "resume", cb.from_user.id)
            p = store.profile(target)
        s, c = store.summary(target), shopdb.customer(target)
        await cb.message.answer(f"🤝 <b>{html.escape(c.name or str(target))}</b>\n"
            f"پورسانت مستقیم: {p['percent']}٪ • تخفیف خرید: {c.reseller_percent}٪\n"
            f"مشتری: {s['customers']} • فروش: {s['sales']}\n"
            f"قابل برداشت: {cash(s['available'])}\nرزروشده: {cash(s['reserved'])}\nتسویه‌شده: {cash(s['paid'])}",
            reply_markup=h.ikb([[("🎯 تنظیم پورسانت", f"pa:rate:{target}")],
                [("🔗 اتصال مشتری قدیمی", f"pa:bind:{target}")],
                [("⏸ توقف" if p['enabled'] else "▶️ فعال‌سازی", f"pa:{'pause' if p['enabled'] else 'resume'}:{target}")],
                [("↩️ نمایندگان", "pa:home")]]))
    elif action in ("request", "pay", "reject"):
        r = store.withdrawal(target)
        if not r or r['status'] != 'pending':
            await cb.message.answer("این درخواست قبلاً بررسی شده یا وجود ندارد.")
            return
        if action != "request":
            await state.set_state(Edit.decision)
            await state.update_data(request_id=target, decision="paid" if action == "pay" else "rejected")
            await cb.message.answer("شناسهٔ انتقال بانکی انجام‌شده را وارد کنید؛ ثبت آن به معنی تأیید پرداخت است."
                                    if action == "pay" else "علت رد را بنویسید؛ مبلغ رزروشده آزاد می‌شود.",
                                    reply_markup=h.CANCEL_KB)
            return
        await cb.message.answer(f"💸 <b>برداشت #{target}</b>\nنماینده: <code>{r['partner_id']}</code>\n"
            f"{cash(r['amount'])}\n<code>{html.escape(r['destination'])}</code>\n\n"
            "ابتدا مبلغ را واریز کنید؛ دکمهٔ زیر انتقال بانکی انجام نمی‌دهد.",
            reply_markup=h.ikb([[("✅ واریز انجام شد", f"pa:pay:{target}")],
                [("↩️ رد و آزادسازی", f"pa:reject:{target}"), ("↩️ صف تسویه", "pa:pending")]]))


@router.message(Edit.rate, h.owner)
async def rate(msg: Message, state: FSMContext):
    if msg.text == h.BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=h.ADMIN_KB)
        return
    n = integer(msg.text)
    if n is None or n != int(n) or not 0 <= n <= 100:
        await msg.answer("درصد باید عدد صحیح از صفر تا صد باشد.")
        return
    data = await state.get_data()
    p = store.profile(data['partner_id'])
    store.configure(p['tg_id'], int(n), bool(p['enabled']), msg.from_user.id)
    await state.clear()
    await msg.answer("✅ نرخ سفارش‌های جدید ثبت شد.", reply_markup=h.ADMIN_KB)


@router.message(Edit.bind, h.owner)
async def bind(msg: Message, state: FSMContext):
    if msg.text == h.BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=h.ADMIN_KB)
        return
    n = integer(msg.text)
    if n is None or n != int(n) or n <= 0:
        await msg.answer("آیدی عددی مثبت وارد کنید.")
        return
    data = await state.get_data()
    await state.update_data(customer_id=int(n))
    await state.set_state(Edit.bindconfirm)
    await msg.answer(f"مشتری <code>{int(n)}</code> به نمایندهٔ <code>{data['partner_id']}</code> متصل شود؟\n"
                     "سفارش‌های گذشته تغییر نمی‌کنند و معرف قبلی قابل جایگزینی نیست.", reply_markup=h.ikb([
                         [("✅ تأیید اتصال", "pa:bindconfirm")], [("↩️ انصراف", "pa:home")]]))


@router.message(Edit.decision, h.owner)
async def decision(msg: Message, state: FSMContext, bot):
    if msg.text == h.BTN_CANCEL:
        await state.clear()
        await msg.answer("لغو شد.", reply_markup=h.ADMIN_KB)
        return
    text = (msg.text or '').strip()
    if not 3 <= len(text) <= 200:
        await msg.answer("شناسه انتقال یا علت رد باید ۳ تا ۲۰۰ نویسه باشد.")
        return
    data = await state.get_data()
    r = store.withdrawal(data['request_id'])
    changed = store.decide(r['id'], data['decision'], msg.from_user.id, text)
    await state.clear()
    await msg.answer("✅ ثبت شد." if changed else "قبلاً بررسی شده؛ دوباره ثبت نشد.", reply_markup=h.ADMIN_KB)
    if changed:
        label = "✅ واریز شد" if data['decision'] == 'paid' else "↩️ رد شد؛ موجودی آزاد شد"
        try:
            await bot.send_message(r['partner_id'], f"برداشت #{r['id']} • {cash(r['amount'])}\n{label}\n"
                                   f"{html.escape(text)}", reply_markup=h.ikb([[("🧾 برداشت‌های من", "rp:withdrawals:0")]]))
        except TelegramAPIError:
            log.warning("partner settlement notification failed; request=%s", r['id'])


@router.callback_query(F.data.startswith("pa:"))
async def owner_required(cb: CallbackQuery):
    await cb.answer("این بخش فقط برای مالک است", show_alert=True)
