"""Shop: plans, card-to-card payments with receipt approval, test accounts, self renewal,
discount codes, referral rewards and resellers. Customer-facing handlers run before the
main router so /start and the customer keyboard land here."""
import asyncio
import html
import logging
import re
import time

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import db
import fmt
import handlers as h
import links
import shopdb
import smspay
from config import cfg

log = logging.getLogger(__name__)
router = Router()
BOT = None  # set by main; used by the SMS webhook


class Buy(StatesGroup):
    name = State()
    discount = State()
    receipt = State()


class ShopAdmin(StatesGroup):
    plan = State()
    price = State()
    card = State()
    discount = State()
    test = State()
    referral = State()
    cost = State()
    reseller = State()


def toman(n: int) -> str:
    return f"{n:,} تومان"


def card_info() -> str:
    return db.get_setting("shop_card", "")


def shop_open() -> bool:
    return bool(card_info()) and bool(shopdb.plans())


def plan_line(p) -> str:
    gb = f"{p.gb:g} گیگ" if p.gb else "حجم نامحدود"
    days = f"{p.days} روز" if p.days else "بدون محدودیت زمان"
    return f"{p.title} — {gb}، {days}"


def owns(tg_id: int, u) -> bool:
    return any(a.id == u.id for a in db.owned_by(tg_id))


def reseller_price(c, price: int) -> int:
    return price * (100 - c.reseller_percent) // 100 if c.reseller_percent else price


async def notify_admins(bot: Bot, text: str, **kw) -> None:
    for admin_id in db.admin_ids():
        try:
            await bot.send_message(admin_id, text, **kw)
        except Exception:
            pass


# =====================================================================
# customer side
# =====================================================================

WELCOME = ("👋 به <b>{brand}</b> خوش آمدید!\n\n"
           "🛒 خرید اشتراک | 🎁 اکانت تست رایگان | 🔄 تمدید\n"
           "👥 دوستانتان را دعوت کنید و اعتبار هدیه بگیرید.")


@router.message(F.text == h.BTN_CANCEL)
async def cancel(msg: Message, state: FSMContext):
    """Registered first so it wins over the shop's own waiting states."""
    await state.clear()
    is_admin = msg.from_user.id in db.admin_ids()
    await msg.answer("لغو شد.", reply_markup=h.ADMIN_KB if is_admin else h.USER_KB)


@router.message(CommandStart(deep_link=True, magic=F.args.startswith("ref_")), ~h.admin)
async def start_ref(msg: Message, command: CommandObject):
    ref = command.args[4:]
    shopdb.customer(msg.from_user.id, msg.from_user.full_name or "", int(ref) if ref.isdigit() else None)
    await msg.answer(WELCOME.format(brand=html.escape(cfg.brand)), reply_markup=h.USER_KB)


@router.message(CommandStart(magic=F.args.is_(None)), ~h.admin)
async def start_plain(msg: Message, state: FSMContext):
    await state.clear()
    shopdb.customer(msg.from_user.id, msg.from_user.full_name or "")
    await msg.answer(WELCOME.format(brand=html.escape(cfg.brand)), reply_markup=h.USER_KB)


@router.message(F.text == h.BTN_MY)
async def my_accounts(msg: Message):
    accounts = db.owned_by(msg.from_user.id)
    if not accounts:
        await msg.answer("هنوز اشتراکی ندارید. از «🛒 خرید اشتراک» یا «🎁 اکانت تست» شروع کنید.",
                         reply_markup=h.USER_KB)
        return
    if len(accounts) == 1:
        await send_account(msg, accounts[0])
        return
    rows = [[(f"{fmt.status_icon(u)} {u.name}", f"acc:{u.id}")] for u in accounts[:50]]
    await msg.answer(f"📦 اکانت‌های شما ({len(accounts)}):", reply_markup=h.ikb(rows))


async def send_account(msg: Message, u) -> None:
    chart = fmt.day_chart(db.user_daily(u.name, 7))
    usage = ("\n\n📊 <b>مصرف ۷ روز اخیر</b>\n" + "\n".join(chart)) if chart else ""
    await msg.answer(fmt.user_card(u) + usage + "\n\n" + h.links_text(u).rsplit("\n🤖", 1)[0],
                     reply_markup=h.ikb([[("🔄 تمدید همین اکانت", f"rn:{u.id}")]]))


@router.callback_query(F.data.startswith("acc:"))
async def account_cb(cb: CallbackQuery):
    await cb.answer()
    u = db.get(int(cb.data[4:]))
    if u and owns(cb.from_user.id, u):
        await send_account(cb.message, u)


# ---------- buying ----------

def plans_kb(target: str):
    rows = [[(f"{p.title} — {toman(p.price)}", f"p:{p.id}:{target}")] for p in shopdb.plans()]
    return h.ikb(rows)


@router.message(F.text == h.BTN_BUY)
async def buy(msg: Message, state: FSMContext):
    await state.clear()
    if not shop_open():
        await msg.answer("فروش فعلاً بسته است. از «💬 پشتیبانی» پیام بدهید.")
        return
    c = shopdb.customer(msg.from_user.id, msg.from_user.full_name or "")
    text = "🛒 <b>پلن‌ها</b>\n\n" + "\n".join(f"• {html.escape(plan_line(p))}: <b>{toman(reseller_price(c, p.price))}</b>"
                                            for p in shopdb.plans())
    if c.reseller_percent:
        text += f"\n\n🤝 قیمت‌ها با {c.reseller_percent}٪ تخفیف نمایندگی است."
    await msg.answer(text + "\n\nیک پلن انتخاب کنید:", reply_markup=plans_kb("new"))


@router.message(F.text == h.BTN_RENEW)
async def renew(msg: Message, state: FSMContext):
    await state.clear()
    accounts = db.owned_by(msg.from_user.id)
    if not accounts:
        await msg.answer("اکانتی برای تمدید ندارید. از «🛒 خرید اشتراک» شروع کنید.")
        return
    if len(accounts) == 1:
        await renew_pick(msg, accounts[0])
        return
    rows = [[(f"{fmt.status_icon(u)} {u.name}", f"rn:{u.id}")] for u in accounts[:50]]
    await msg.answer("کدام اکانت تمدید شود؟", reply_markup=h.ikb(rows))


async def renew_pick(msg: Message, u) -> None:
    if not shop_open():
        await msg.answer("فروش فعلاً بسته است. از «💬 پشتیبانی» پیام بدهید.")
        return
    await msg.answer(f"🔄 تمدید <b>{html.escape(u.name)}</b>\nبعد از تمدید، حجم پلن جدید جایگزین و "
                     "روزها به اعتبار فعلی اضافه می‌شود. پلن را انتخاب کنید:",
                     reply_markup=plans_kb(f"r{u.id}"))


@router.callback_query(F.data.startswith("rn:"))
async def renew_cb(cb: CallbackQuery):
    await cb.answer()
    u = db.get(int(cb.data[3:]))
    if u and owns(cb.from_user.id, u):
        await renew_pick(cb.message, u)


@router.callback_query(F.data.startswith("p:"))
async def plan_chosen(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, plan_id, target = cb.data.split(":")
    p = shopdb.plan(int(plan_id))
    if not p or not p.active:
        await cb.message.answer("این پلن دیگر موجود نیست.")
        return
    user_id = None
    if target.startswith("r"):
        u = db.get(int(target[1:]))
        if not u or not owns(cb.from_user.id, u):
            return
        user_id = u.id
    await state.set_data({"plan_id": p.id, "user_id": user_id, "code": "", "name": ""})
    c = shopdb.customer(cb.from_user.id)
    if user_id is None and c.reseller_percent:
        await state.set_state(Buy.name)
        await cb.message.answer("🤝 نام اکانت مشتری را بفرستید (حروف انگلیسی، عدد، _ . -):", reply_markup=h.CANCEL_KB)
        return
    await checkout(cb.message, state, cb.from_user.id)


@router.message(Buy.name)
async def got_name(msg: Message, state: FSMContext):
    name = (msg.text or "").strip()
    if not h.NAME_RE.match(name) or db.get_by_name(name):
        await msg.answer("❌ نام نامعتبر یا تکراری است. یک نام دیگر بفرستید.")
        return
    await state.update_data(name=name)
    await checkout(msg, state, msg.from_user.id)


def quote(data: dict, tg_id: int) -> dict:
    p = shopdb.plan(data["plan_id"])
    c = shopdb.customer(tg_id)
    price = reseller_price(c, p.price)
    d = shopdb.discount(data["code"]) if data.get("code") else None
    after_code = price * (100 - d["percent"]) // 100 if d else price
    wallet = min(c.balance, after_code)
    return {"plan": p, "price": price, "discount": d, "wallet": wallet, "final": after_code - wallet}


async def checkout(msg: Message, state: FSMContext, tg_id: int) -> None:
    data = await state.get_data()
    q = quote(data, tg_id)
    p = q["plan"]
    lines = ["🧾 <b>فاکتور</b>", "", f"پلن: {html.escape(plan_line(p))}", f"قیمت: {toman(q['price'])}"]
    if data.get("name"):
        lines.insert(3, f"نام اکانت: <code>{html.escape(data['name'])}</code>")
    if data.get("user_id"):
        lines.insert(3, f"تمدید: <code>{html.escape(db.get(data['user_id']).name)}</code>")
    if q["discount"]:
        lines.append(f"🎟 کد {q['discount']['code']}: {q['discount']['percent']}٪ تخفیف")
    if q["wallet"]:
        lines.append(f"👛 از کیف پول: {toman(q['wallet'])}")
    lines.append(f"\n💰 مبلغ قابل پرداخت: <b>{toman(q['final'])}</b>")
    await state.set_state(None)
    await msg.answer("\n".join(lines), reply_markup=h.ikb([
        [("✅ پرداخت", "pay")],
        [("🎟 کد تخفیف دارم", "code"), ("❌ انصراف", "paycancel")],
    ]))


@router.callback_query(F.data == "code")
async def ask_code(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    if not (await state.get_data()).get("plan_id"):
        return
    await state.set_state(Buy.discount)
    await cb.message.answer("🎟 کد تخفیف را بفرستید:", reply_markup=h.CANCEL_KB)


@router.message(Buy.discount)
async def got_code(msg: Message, state: FSMContext):
    code = (msg.text or "").strip().upper()
    if not shopdb.discount(code):
        await msg.answer("❌ کد نامعتبر یا منقضی است. دوباره بفرستید یا «❌ لغو» را بزنید.")
        return
    await state.update_data(code=code)
    await msg.answer("✅ کد اعمال شد.", reply_markup=h.USER_KB)
    await checkout(msg, state, msg.from_user.id)


@router.callback_query(F.data == "paycancel")
async def pay_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer("لغو شد")
    await cb.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "pay")
async def pay(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    if not data.get("plan_id"):
        await cb.answer("فاکتور منقضی شده؛ دوباره پلن را انتخاب کنید", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    q = quote(data, cb.from_user.id)
    if q["final"]:
        q["final"] = smspay.unique_amount(q["final"])  # last digits identify this order's deposit
    if q["wallet"]:
        shopdb.add_balance(cb.from_user.id, -q["wallet"])
    o = shopdb.create_order(
        tg_id=cb.from_user.id, plan_id=q["plan"].id, kind="renew" if data.get("user_id") else "new",
        user_id=data.get("user_id"), account_name=data.get("name", ""), price=q["price"],
        discount_code=q["discount"]["code"] if q["discount"] else "", wallet_used=q["wallet"],
        final_price=q["final"], status="pending" if q["final"] == 0 else "waiting")
    await state.clear()
    if q["final"] == 0:
        shopdb.decide_order(o.id, "approved", 0)
        await fulfill(bot, shopdb.order(o.id))
        return
    await state.set_state(Buy.receipt)
    await state.update_data(order_id=o.id)
    after = ("⚡ چند ثانیه بعد از واریز، اشتراک خودکار برایتان فرستاده می‌شود. "
             "اگر تا ۱۰ دقیقه نرسید، <b>عکس رسید</b> را همین‌جا بفرستید." if smspay.enabled()
             else "📸 بعد از واریز، <b>عکس رسید</b> را همین‌جا بفرستید.")
    await cb.message.answer(
        f"💳 لطفاً <b>دقیقاً {toman(q['final'])}</b> را به کارت زیر واریز کنید:\n\n{html.escape(card_info())}\n\n"
        f"⚠️ مبلغ را دقیق و با همین سه رقم آخر واریز کنید؛ سفارش شما با همین مبلغ شناسایی می‌شود.\n\n"
        f"{after}\n(سفارش #{o.id} — تا ۴۸ ساعت معتبر)", reply_markup=h.CANCEL_KB)


@router.message(Buy.receipt, F.photo | F.document)
async def got_receipt(msg: Message, state: FSMContext, bot: Bot):
    order_id = (await state.get_data()).get("order_id")
    await state.clear()
    o = shopdb.order(order_id) if order_id else None
    if o and o.status == "approved":
        await msg.answer("✅ پرداخت شما قبلاً خودکار تأیید و اشتراک ارسال شده است.", reply_markup=h.USER_KB)
        return
    if not o or o.status != "waiting":
        await msg.answer("سفارش پیدا نشد.", reply_markup=h.USER_KB)
        return
    file_id = msg.photo[-1].file_id if msg.photo else msg.document.file_id
    shopdb.update_order(o.id, status="pending", receipt=file_id)
    await msg.answer("✅ رسید دریافت شد. بعد از تأیید، اشتراک برایتان فرستاده می‌شود.", reply_markup=h.USER_KB)
    p = shopdb.plan(o.plan_id)
    who = html.escape(msg.from_user.full_name or "")
    target = f"تمدید {html.escape(db.get(o.user_id).name)}" if o.user_id else (
        f"اکانت جدید {html.escape(o.account_name)}" if o.account_name else "اکانت جدید")
    caption = (f"🧾 <b>سفارش #{o.id}</b>\nاز: {who} (<code>{msg.from_user.id}</code>)\n"
               f"{html.escape(plan_line(p))}\n{target}\n💰 {toman(o.final_price)}"
               + (f"\n🎟 {o.discount_code}" if o.discount_code else ""))
    kb = h.ikb([[("✅ تأیید", f"ord:ok:{o.id}"), ("❌ رد", f"ord:no:{o.id}")]])
    for admin_id in db.admin_ids():
        try:
            if msg.photo:
                await bot.send_photo(admin_id, file_id, caption=caption, reply_markup=kb)
            else:
                await bot.send_document(admin_id, file_id, caption=caption, reply_markup=kb)
        except Exception:
            pass


@router.message(Buy.receipt)
async def receipt_not_photo(msg: Message):
    await msg.answer("📸 لطفاً عکس رسید را بفرستید (یا «❌ لغو»).")


# ---------- fulfilment ----------

def unique_name(base: str) -> str:
    name, i = base, 1
    while db.get_by_name(name):
        i += 1
        name = f"{base}_{i}"
    return name


async def fulfill(bot: Bot, o) -> None:
    p = shopdb.plan(o.plan_id)
    buyer = shopdb.customer(o.tg_id)
    now = int(time.time())
    if o.kind == "new":
        u = db.create_user(unique_name(o.account_name or f"c{o.id}"), p.gb, p.days)
        if buyer.reseller_percent:
            db.update(u.id, owner_tg=o.tg_id)
        else:
            db.update(u.id, tg_id=o.tg_id)
        u = db.get(u.id)
        await h.apply_user(u)
        head = "🎉 خرید شما تأیید شد!"
    else:
        u = db.get(o.user_id)
        if u.pending_days:
            db.update(u.id, pending_days=u.pending_days + p.days)
        elif p.days:
            db.update(u.id, expire_at=max(u.expire_at, now) + p.days * db.DAY)
        db.update(u.id, traffic_limit=int(p.gb * db.GB), up=0, down=0, warned=0)
        u = await h.maybe_reactivate(db.get(u.id))
        if not u.enabled and u.disabled_reason in ("expired", "traffic"):
            db.update(u.id, enabled=1, disabled_reason="")
            u = db.get(u.id)
            await h.apply_user(u)
        head = "✅ تمدید شما انجام شد!"
    if o.discount_code:
        shopdb.use_discount(o.discount_code)
    # referral reward
    pct = int(db.get_setting("shop_ref_percent", "0") or 0)
    if pct and buyer.referrer and o.final_price:
        reward = o.final_price * pct // 100
        shopdb.add_balance(buyer.referrer, reward)
        try:
            await bot.send_message(buyer.referrer, f"🎁 یکی از دوستانی که دعوت کردید خرید کرد؛ "
                                                   f"{toman(reward)} به کیف پولتان اضافه شد.")
        except Exception:
            pass
    try:
        await bot.send_message(o.tg_id, f"{head}\n\n{fmt.user_card(u)}")
        await bot.send_photo(o.tg_id, BufferedInputFile(h.qr_png(links.sub_url(u)), "qr.png"),
                             caption=h.links_text(u).rsplit("\n🤖", 1)[0])
        await bot.send_message(o.tg_id, h.HELP_TEXT, reply_markup=h.USER_KB)
    except Exception:
        log.warning("could not deliver order %s", o.id)


# ---------- test account ----------

@router.message(F.text == h.BTN_TEST)
async def test_account(msg: Message, bot: Bot):
    raw = db.get_setting("shop_test", "")
    if not raw:
        await msg.answer("اکانت تست فعلاً فعال نیست.")
        return
    c = shopdb.customer(msg.from_user.id, msg.from_user.full_name or "")
    if c.test_used:
        await msg.answer("شما قبلاً از اکانت تست استفاده کرده‌اید. از «🛒 خرید اشتراک» اشتراک بگیرید.")
        return
    mb, hours = (int(x) for x in raw.split(":"))
    u = db.create_user(unique_name(f"test{msg.from_user.id}"), mb / 1024, 0)
    db.update(u.id, tg_id=msg.from_user.id, expire_at=int(time.time()) + hours * 3600, note="test")
    shopdb.update_customer(msg.from_user.id, test_used=1)
    u = db.get(u.id)
    await h.apply_user(u)
    await msg.answer(f"🎁 اکانت تست شما ({mb} مگابایت، {hours} ساعت):\n\n{fmt.user_card(u)}")
    await msg.answer_photo(BufferedInputFile(h.qr_png(links.sub_url(u)), "qr.png"),
                           caption=h.links_text(u).rsplit("\n🤖", 1)[0])
    await msg.answer(h.HELP_TEXT, reply_markup=h.USER_KB)
    await notify_admins(bot, f"🎁 اکانت تست ساخته شد برای {html.escape(msg.from_user.full_name or '')} "
                             f"(<code>{msg.from_user.id}</code>)")


# ---------- referrals ----------

@router.message(F.text == h.BTN_INVITE)
async def invite(msg: Message):
    c = shopdb.customer(msg.from_user.id, msg.from_user.full_name or "")
    pct = int(db.get_setting("shop_ref_percent", "0") or 0)
    link = f"https://t.me/{db.get_setting('bot_username')}?start=ref_{msg.from_user.id}"
    reward = (f"به ازای هر خرید دوستانتان <b>{pct}٪</b> مبلغ خرید به کیف پول شما اضافه می‌شود "
              "و در خریدهای بعدی خودکار کم می‌شود.") if pct else "پاداش دعوت فعلاً فعال نیست."
    await msg.answer(f"👥 <b>دعوت دوستان</b>\n\n{reward}\n\n"
                     f"🔗 لینک دعوت شما:\n<code>{link}</code>\n\n"
                     f"👤 دعوت‌شده‌ها: {shopdb.referral_count(msg.from_user.id)}\n"
                     f"👛 موجودی کیف پول: {toman(c.balance)}")


# =====================================================================
# admin side
# =====================================================================

@router.callback_query(F.data.startswith(("ord:ok:", "ord:no:")), h.admin)
async def decide(cb: CallbackQuery, bot: Bot):
    _, verdict, oid = cb.data.split(":")
    o = shopdb.order(int(oid))
    approve = verdict == "ok"
    if not o or not shopdb.decide_order(o.id, "approved" if approve else "rejected", cb.from_user.id):
        await cb.answer("این سفارش قبلاً بررسی شده", show_alert=True)
        return
    await cb.answer("✅ تأیید شد" if approve else "❌ رد شد")
    stamp = f"\n\n{'✅ تأیید' if approve else '❌ رد'} توسط {html.escape(cb.from_user.full_name or '')}"
    try:
        await cb.message.edit_caption(caption=(cb.message.caption or "") + stamp, reply_markup=None)
    except Exception:
        pass
    if approve:
        try:
            await fulfill(bot, shopdb.order(o.id))
        except Exception as e:
            log.exception("fulfil failed")
            await cb.message.answer(f"❌ خطا در ساخت اکانت سفارش #{o.id}: <code>{html.escape(str(e))}</code>")
    else:
        if o.wallet_used:
            shopdb.add_balance(o.tg_id, o.wallet_used)
        try:
            await bot.send_message(o.tg_id, f"❌ سفارش #{o.id} تأیید نشد. اگر واریز کرده‌اید از «💬 پشتیبانی» پیام بدهید.")
        except Exception:
            pass


@router.message(F.text == h.BTN_SHOP, h.admin)
async def shop_menu(msg: Message):
    await msg.answer(shop_admin_text(), reply_markup=shop_admin_kb())


def shop_admin_text() -> str:
    day = int(time.time()) - int(time.time()) % db.DAY
    month = int(time.mktime(time.strptime(time.strftime("%Y-%m-01"), "%Y-%m-%d")))
    n_day, s_day = shopdb.sales_since(day)
    n_month, s_month = shopdb.sales_since(month)
    test = db.get_setting("shop_test", "")
    test_text = f"{test.split(':')[0]} مگ / {test.split(':')[1]} ساعت" if test else "خاموش"
    return (
        "🛒 <b>فروشگاه</b>\n\n"
        f"وضعیت: {'🟢 باز' if shop_open() else '🔴 بسته (پلن و کارت لازم است)'}\n"
        f"📈 فروش امروز: {n_day} سفارش، {toman(s_day)}\n"
        f"📈 فروش این ماه: {n_month} سفارش، {toman(s_month)}\n"
        f"🧾 در انتظار بررسی: {len(shopdb.pending_orders())}\n"
        f"👥 مشتری‌ها: {shopdb.customer_count()}\n\n"
        f"📋 پلن‌ها: {len(shopdb.plans(False))} | 🎟 کدها: {len(shopdb.discounts())} | "
        f"🤝 نماینده‌ها: {len(shopdb.resellers())}\n"
        f"🎁 اکانت تست: {test_text}\n"
        f"👥 پاداش دعوت: {db.get_setting('shop_ref_percent', '0') or 0}٪\n"
        + profit_text(s_month)
    )


def profit_text(month_sales: int) -> str:
    cost = int(db.get_setting("shop_cost", "0") or 0)
    if not cost:
        return "💰 هزینه‌ی ماهانه‌ی سرورها ثبت نشده («💰 هزینه‌ها»)"
    profit = month_sales - cost
    return (f"💰 هزینه‌ی ماهانه: {toman(cost)} | "
            f"{'سود' if profit >= 0 else 'زیان'} این ماه: <b>{toman(abs(profit))}</b>")


def shop_admin_kb():
    return h.ikb([
        [("📋 پلن‌ها", "sa:plans"), ("💳 کارت", "sa:card")],
        [("🎟 کدهای تخفیف", "sa:codes"), ("🤝 نماینده‌ها", "sa:resellers")],
        [("🎁 اکانت تست", "sa:test"), ("👥 پاداش دعوت", "sa:ref")],
        [("🧾 سفارش‌های در انتظار", "sa:pending"), ("💰 هزینه‌ها", "sa:cost")],
        [("📲 تأیید خودکار پیامک", "sms:menu")],
        [("👁 نمای مشتری", "sa:preview")],
    ])


ADMIN_PROMPTS = {
    "card": (ShopAdmin.card, "💳 اطلاعات کارت را همان‌طور که مشتری باید ببیند بفرستید، مثلاً:\n"
                             "<code>6037-xxxx-xxxx-xxxx\nبه نام: علی رضایی\nبانک ملی</code>"),
    "test": (ShopAdmin.test, "🎁 اکانت تست: <code>مگابایت | ساعت</code> مثلاً <code>500 | 24</code>\n"
                             "برای خاموش کردن: <code>off</code>"),
    "ref": (ShopAdmin.referral, "👥 چند درصد از هر خرید به کیف پول دعوت‌کننده برود؟ (۰ = خاموش)"),
    "cost": (ShopAdmin.cost, "💰 جمع هزینه‌ی ماهانه‌ی همه‌ی سرورها (آلمان + ایران) به تومان؟\n"
                             "برای محاسبه‌ی سود ماهانه در صفحه‌ی فروشگاه."),
}


@router.callback_query(F.data.in_({"sa:card", "sa:test", "sa:ref", "sa:cost"}), h.admin)
async def shop_admin_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    st, prompt = ADMIN_PROMPTS[cb.data[3:]]
    await state.set_state(st)
    await cb.message.answer(prompt, reply_markup=h.CANCEL_KB)


@router.message(ShopAdmin.cost, h.admin)
async def set_cost(msg: Message, state: FSMContext):
    v = h.parse_number((msg.text or "").replace(",", "").replace("٬", ""))
    if v is None:
        await msg.answer("❌ یک عدد بفرستید، مثلاً <code>2500000</code>")
        return
    await state.clear()
    db.set_setting("shop_cost", str(int(v)))
    await msg.answer("✅ ذخیره شد.", reply_markup=h.ADMIN_KB)


@router.message(ShopAdmin.card, h.admin)
async def set_card(msg: Message, state: FSMContext):
    await state.clear()
    db.set_setting("shop_card", (msg.text or "").strip()[:500])
    await msg.answer("✅ ذخیره شد.", reply_markup=h.ADMIN_KB)


@router.message(ShopAdmin.test, h.admin)
async def set_test(msg: Message, state: FSMContext):
    text = (msg.text or "").strip()
    if text.lower() == "off":
        db.set_setting("shop_test", "")
    else:
        parts = [h.parse_number(x) for x in text.split("|")]
        if len(parts) != 2 or None in parts or not all(parts):
            await msg.answer("❌ قالب: <code>500 | 24</code>")
            return
        db.set_setting("shop_test", f"{int(parts[0])}:{int(parts[1])}")
    await state.clear()
    await msg.answer("✅ ذخیره شد.", reply_markup=h.ADMIN_KB)


@router.message(ShopAdmin.referral, h.admin)
async def set_ref(msg: Message, state: FSMContext):
    v = h.parse_number(msg.text)
    if v is None or v > 100:
        await msg.answer("❌ یک عدد بین ۰ تا ۱۰۰ بفرستید.")
        return
    await state.clear()
    db.set_setting("shop_ref_percent", str(int(v)))
    await msg.answer("✅ ذخیره شد.", reply_markup=h.ADMIN_KB)


# ---------- plans ----------

# starting points; prices are meant to be adjusted with ✏️
PRESETS = [
    ("🌱 اقتصادی ۲۰ گیگ", 20, 30, 150_000),
    ("⭐ استاندارد ۵۰ گیگ", 50, 30, 290_000),
    ("🔥 حرفه‌ای ۱۰۰ گیگ", 100, 30, 490_000),
    ("💎 ویژه ۲۰۰ گیگ", 200, 30, 850_000),
    ("📅 سه‌ماهه ۱۵۰ گیگ", 150, 90, 690_000),
    ("👨‍👩‍👧 خانوادگی ۳۰۰ گیگ", 300, 60, 1_250_000),
]
OLD_PRESET_PRICES = {"🌱 اقتصادی ۲۰ گیگ": 90_000, "⭐ استاندارد ۵۰ گیگ": 180_000,
                     "🔥 حرفه‌ای ۱۰۰ گیگ": 300_000, "💎 ویژه ۲۰۰ گیگ": 500_000,
                     "📅 سه‌ماهه ۱۵۰ گیگ": 420_000, "👨‍👩‍👧 خانوادگی ۳۰۰ گیگ": 750_000}


def apply_defaults() -> None:
    """Recommended starting settings, applied once on a fresh shop; admins can change them all later."""
    if db.get_setting("shop_seeded") and not db.get_setting("shop_prices_v2"):
        # raise first-version preset prices that the admin has not edited
        new = {t: p for t, _, _, p in PRESETS}
        for p in shopdb.plans(False):
            if OLD_PRESET_PRICES.get(p.title) == p.price:
                shopdb.set_price(p.id, new[p.title])
        db.set_setting("shop_prices_v2", "1")
    if db.get_setting("shop_seeded"):
        return
    if not shopdb.plans(False):
        for title, gb, days, price in PRESETS:
            shopdb.add_plan(title, gb, days, price)
    db.set_setting("shop_prices_v2", "1")
    defaults = {"shop_test": "500:24", "shop_ref_percent": "10", "shop_cost": "5000000",
                "ip_limit_default": "2", "ip_limit_action": "warn"}
    for key, value in defaults.items():
        if not db.get_setting(key):
            db.set_setting(key, value)
    db.set_setting("shop_seeded", "1")


def plans_admin():
    rows = [[(f"{'🟢' if p.active else '⚪️'} {p.title} — {toman(p.price)}", f"sp:t:{p.id}"),
             ("✏️", f"sp:e:{p.id}"), ("🗑", f"sp:d:{p.id}")] for p in shopdb.plans(False)]
    rows.append([("➕ پلن جدید", "sp:add"), ("✨ پلن‌های پیشنهادی", "sp:presets")])
    return ("📋 <b>پلن‌ها</b>\nروی هر پلن بزنید تا فعال/غیرفعال شود؛ ✏️ تغییر قیمت، 🗑 حذف.", h.ikb(rows))


@router.callback_query(F.data == "sp:presets", h.admin)
async def plan_presets(cb: CallbackQuery):
    existing = {p.title for p in shopdb.plans(False)}
    added = 0
    for title, gb, days, price in PRESETS:
        if title not in existing:
            shopdb.add_plan(title, gb, days, price)
            added += 1
    await cb.answer(f"✅ {added} پلن اضافه شد؛ قیمت‌ها را با ✏️ تنظیم کنید", show_alert=True)
    text, kb = plans_admin()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("sp:e:"), h.admin)
async def plan_price_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    p = shopdb.plan(int(cb.data[5:]))
    if not p:
        return
    await state.set_state(ShopAdmin.price)
    await state.update_data(plan_id=p.id)
    await cb.message.answer(f"✏️ قیمت جدید «{html.escape(p.title)}» (فعلی: {toman(p.price)}) را به تومان بفرستید:",
                            reply_markup=h.CANCEL_KB)


@router.message(ShopAdmin.price, h.admin)
async def plan_price_set(msg: Message, state: FSMContext):
    v = h.parse_number((msg.text or "").replace(",", "").replace("٬", ""))
    if v is None:
        await msg.answer("❌ یک عدد بفرستید، مثلاً <code>150000</code>")
        return
    shopdb.set_price((await state.get_data())["plan_id"], int(v))
    await state.clear()
    await msg.answer("✅ قیمت ذخیره شد.", reply_markup=h.ADMIN_KB)
    text, kb = plans_admin()
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == "sa:plans", h.admin)
async def plans_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = plans_admin()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.regexp(r"^sp:[td]:\d+$"), h.admin)
async def plan_edit(cb: CallbackQuery):
    _, op, pid = cb.data.split(":")
    (shopdb.toggle_plan if op == "t" else shopdb.delete_plan)(int(pid))
    await cb.answer("✅")
    text, kb = plans_admin()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "sp:add", h.admin)
async def plan_add_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(ShopAdmin.plan)
    await cb.message.answer("➕ پلن جدید را در یک خط بفرستید:\n"
                            "<code>عنوان | حجم گیگ | روز | قیمت تومان</code>\n"
                            "مثال: <code>یک ماهه ۳۰ گیگ | 30 | 30 | 150000</code>\n"
                            "(حجم یا روز ۰ = نامحدود)", reply_markup=h.CANCEL_KB)


@router.message(ShopAdmin.plan, h.admin)
async def plan_add(msg: Message, state: FSMContext):
    parts = [x.strip() for x in (msg.text or "").split("|")]
    nums = [h.parse_number(x.replace(",", "")) for x in parts[1:]]
    if len(parts) != 4 or not parts[0] or None in nums:
        await msg.answer("❌ قالب: <code>عنوان | حجم | روز | قیمت</code>")
        return
    shopdb.add_plan(parts[0][:60], nums[0], int(nums[1]), int(nums[2]))
    await state.clear()
    await msg.answer("✅ پلن اضافه شد.", reply_markup=h.ADMIN_KB)
    text, kb = plans_admin()
    await msg.answer(text, reply_markup=kb)


# ---------- discount codes ----------

def codes_admin():
    rows = []
    lines = ["🎟 <b>کدهای تخفیف</b>", ""]
    for d in shopdb.discounts():
        exp = time.strftime("%Y-%m-%d", time.localtime(d["expires_at"])) if d["expires_at"] else "بدون انقضا"
        uses = f"{d['used']}/{d['max_uses']}" if d["max_uses"] else f"{d['used']}/∞"
        lines.append(f"<code>{d['code']}</code> — {d['percent']}٪ | استفاده {uses} | {exp}")
        rows.append([(f"🗑 {d['code']}", f"sc:d:{d['code']}")])
    rows.append([("➕ کد جدید", "sc:add")])
    return "\n".join(lines), h.ikb(rows)


@router.callback_query(F.data == "sa:codes", h.admin)
async def codes_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = codes_admin()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("sc:d:"), h.admin)
async def code_delete(cb: CallbackQuery):
    shopdb.delete_discount(cb.data[5:])
    await cb.answer("🗑")
    text, kb = codes_admin()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "sc:add", h.admin)
async def code_add_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(ShopAdmin.discount)
    await cb.message.answer("➕ کد تخفیف:\n<code>کد | درصد | تعداد استفاده | روز اعتبار</code>\n"
                            "مثال: <code>NOROOZ | 20 | 100 | 10</code> (۰ = نامحدود)", reply_markup=h.CANCEL_KB)


@router.message(ShopAdmin.discount, h.admin)
async def code_add(msg: Message, state: FSMContext):
    parts = [x.strip() for x in (msg.text or "").split("|")]
    nums = [h.parse_number(x) for x in parts[1:]]
    if len(parts) != 4 or not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", parts[0]) or None in nums or not 0 < nums[0] <= 100:
        await msg.answer("❌ قالب: <code>NOROOZ | 20 | 100 | 10</code> (کد فقط انگلیسی و عدد)")
        return
    shopdb.add_discount(parts[0], int(nums[0]), int(nums[1]), int(nums[2]))
    await state.clear()
    await msg.answer("✅ کد ساخته شد.", reply_markup=h.ADMIN_KB)
    text, kb = codes_admin()
    await msg.answer(text, reply_markup=kb)


# ---------- resellers ----------

def resellers_admin():
    lines, rows = ["🤝 <b>نماینده‌ها</b>",
                   "نماینده‌ها با تخفیف خودشان می‌خرند، برای هر خرید نام اکانت می‌دهند و اکانت‌هایشان را "
                   "از «📊 حساب من» مدیریت و تمدید می‌کنند.", ""], []
    for c in shopdb.resellers():
        n = len(db.owned_by(c.tg_id))
        lines.append(f"• {html.escape(c.name or str(c.tg_id))} (<code>{c.tg_id}</code>) — {c.reseller_percent}٪ | {n} اکانت")
        rows.append([(f"❌ حذف {c.tg_id}", f"sr:d:{c.tg_id}")])
    rows.append([("➕ نماینده‌ی جدید", "sr:add")])
    return "\n".join(lines), h.ikb(rows)


@router.callback_query(F.data == "sa:resellers", h.admin)
async def resellers_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = resellers_admin()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("sr:d:"), h.admin)
async def reseller_delete(cb: CallbackQuery):
    shopdb.update_customer(int(cb.data[5:]), reseller_percent=0)
    await cb.answer("🗑")
    text, kb = resellers_admin()
    await cb.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "sr:add", h.admin)
async def reseller_add_ask(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(ShopAdmin.reseller)
    await cb.message.answer("➕ نماینده: <code>آیدی عددی | درصد تخفیف</code> مثلاً <code>123456789 | 30</code>\n"
                            "(باید یک بار ربات را /start کرده باشد)", reply_markup=h.CANCEL_KB)


@router.message(ShopAdmin.reseller, h.admin)
async def reseller_add(msg: Message, state: FSMContext, bot: Bot):
    parts = [h.parse_number(x) for x in (msg.text or "").split("|")]
    if len(parts) != 2 or None in parts or not 0 < parts[1] < 100:
        await msg.answer("❌ قالب: <code>123456789 | 30</code>")
        return
    tg_id, pct = int(parts[0]), int(parts[1])
    shopdb.customer(tg_id)
    shopdb.update_customer(tg_id, reseller_percent=pct)
    await state.clear()
    await msg.answer("✅ نماینده اضافه شد.", reply_markup=h.ADMIN_KB)
    try:
        await bot.send_message(tg_id, f"🤝 شما نماینده‌ی {html.escape(cfg.brand)} شدید؛ همه‌ی پلن‌ها برای شما "
                                      f"{pct}٪ تخفیف دارند. /start")
    except Exception:
        await msg.answer("ℹ️ نتوانستم به او پیام بدهم؛ باید یک بار ربات را /start کند.")


# ---------- pending orders / preview ----------

@router.callback_query(F.data == "sa:pending", h.admin)
async def pending_menu(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    orders = shopdb.pending_orders()
    if not orders:
        await cb.message.answer("سفارشی در انتظار نیست.")
        return
    for o in orders[:10]:
        p = shopdb.plan(o.plan_id)
        caption = (f"🧾 <b>سفارش #{o.id}</b> از <code>{o.tg_id}</code>\n"
                   f"{html.escape(plan_line(p) if p else '?')}\n💰 {toman(o.final_price)}")
        kb = h.ikb([[("✅ تأیید", f"ord:ok:{o.id}"), ("❌ رد", f"ord:no:{o.id}")]])
        try:
            await bot.send_photo(cb.message.chat.id, o.receipt, caption=caption, reply_markup=kb)
        except Exception:
            await cb.message.answer(caption, reply_markup=kb)


@router.callback_query(F.data == "sa:preview", h.admin)
async def preview(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("👁 این منوی مشتری است. برای برگشت به پنل /start را بزنید.", reply_markup=h.USER_KB)



# =====================================================================
# automatic payment verification by bank SMS
# =====================================================================

async def handle_sms(text: str) -> str:
    text = (text or "").strip()
    if not text or not smspay.enabled():
        return "disabled"
    if not shopdb.log_sms(text):
        return "duplicate"
    o, reason = smspay.match(text)
    if not o or not shopdb.auto_approve(o.id):
        shopdb.sms_result(text, None, reason)
        return reason
    shopdb.sms_result(text, o.id, "approved")
    if BOT:
        # answer the phone right away; building the account can take a few seconds
        _tasks.add(t := asyncio.create_task(_auto_fulfil(o)))
        t.add_done_callback(_tasks.discard)
    return "approved"


_tasks = set()


async def _auto_fulfil(o) -> None:
    try:
        await fulfill(BOT, shopdb.order(o.id))
    except Exception as e:
        log.exception("auto fulfil failed")
        await notify_admins(BOT, f"❌ سفارش #{o.id} با پیامک تأیید شد ولی ساخت اکانت خطا داد: "
                                 f"<code>{html.escape(str(e))}</code>")
        return
    await notify_admins(BOT, f"⚡ سفارش #{o.id} خودکار با پیامک بانک تأیید شد ({toman(o.final_price)}).")


def sms_admin():
    on = smspay.enabled()
    from links import relays
    host = relays()[0][0] if relays() else cfg.domain
    url = f"http://{host}:2096/pay/sms?key={smspay.key()}"
    lines = [
        "📲 <b>تأیید خودکار با پیامک بانک</b>", "",
        f"وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}", "",
        "هر سفارش یک مبلغ یکتا (سه رقم آخر) می‌گیرد. پیامک واریز بانک از گوشی شما به ربات فرستاده "
        "می‌شود و سفارشی که مبلغش در پیامک باشد خودکار تأیید می‌شود. پیامک‌های برداشت و خط موجودی نادیده "
        "گرفته می‌شوند. اگر دو سفارش با یک پیامک جور شوند، تأیید دستی می‌ماند.", "",
        "🔗 آدرس (محرمانه؛ به کسی ندهید):", f"<code>{url}</code>", "",
        "<b>تنظیم در آیفون (iOS 17+)</b>",
        "۱. اپ Shortcuts ← Automation ← + ← <b>Message</b>",
        "۲. Sender: شماره یا نام پیامک‌های بانک | Message Contains: <code>واریز</code> | <b>Run Immediately</b>",
        "۳. Next ← New Blank Automation ← اکشن <b>Get Contents of URL</b>",
        "۴. URL: آدرس بالا | Method: <b>POST</b> | Request Body: <b>Form</b>",
        "۵. یک فیلد Text با نام <code>text</code> و مقدار: متغیر <b>Shortcut Input</b> (Content)",
        "۶. Done. برای تست یک مبلغ کوچک به کارت خودتان واریز کنید و «📜 پیامک‌های اخیر» را ببینید.",
    ]
    recent = shopdb.last_sms(5)
    if recent:
        lines += ["", "📜 <b>پیامک‌های اخیر</b>"]
        for r in recent:
            when = time.strftime("%m-%d %H:%M", time.localtime(r["at"]))
            lines.append(f"• {when} — {html.escape(r['result'] or '...')}"
                         + (f" (سفارش #{r['order_id']})" if r["order_id"] else ""))
    kb = h.ikb([
        [(("🔴 خاموش کردن" if on else "🟢 روشن کردن"), "sms:toggle"), ("🔑 کلید جدید", "sms:rekey")],
        [("📜 پیامک‌های اخیر", "sms:menu")],
    ])
    return "\n".join(lines), kb


@router.callback_query(F.data == "sms:menu", h.admin)
async def sms_menu(cb: CallbackQuery):
    await cb.answer()
    text, kb = sms_admin()
    await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.in_({"sms:toggle", "sms:rekey"}), h.owner)
async def sms_change(cb: CallbackQuery):
    if cb.data == "sms:toggle":
        smspay.key()
        db.set_setting("sms_on", "" if smspay.enabled() else "1")
    else:
        db.set_setting("sms_key", "")
        smspay.key()
    await cb.answer("✅ ذخیره شد" + ("؛ آدرس را در Shortcut هم عوض کنید" if cb.data == "sms:rekey" else ""),
                    show_alert=cb.data == "sms:rekey")
    text, kb = sms_admin()
    await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.in_({"sms:toggle", "sms:rekey"}), h.admin)
async def sms_change_denied(cb: CallbackQuery):
    await cb.answer("فقط مالک ربات می‌تواند این را تغییر دهد", show_alert=True)
