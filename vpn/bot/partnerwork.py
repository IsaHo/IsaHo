"""Partner workspace: private CRM, grouped wholesale checkout, QR delivery and accounts."""
import asyncio
import csv
import html
import io
import logging
import secrets
import time
import zipfile
from datetime import datetime, timezone

import db
import fmt
import handlers as h
import links
import membership
import partners
import shopdb
import workdb as store
import xray
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

router = Router()
log = logging.getLogger(__name__)
_fulfill_lock = asyncio.Lock()


class Edit(StatesGroup):
    attach = State()
    attachconfirm = State()
    contact = State()
    search = State()
    rows = State()
    preview = State()
    received = State()
    saleconfirm = State()


def back():
    return h.ikb([[("↩️ نمایندگی", "rp:home")]])


def allowed(cb):
    message = getattr(cb, 'message', None)
    return partners.allowed(cb.from_user.id) and message is not None and message.chat.type == 'private'


def package(partner_id, order_id):
    b = store.batch(partner_id, order_id)
    if not b or b['status'] != 'approved':
        raise ValueError('بستهٔ تأییدشدهٔ متعلق به شما پیدا نشد')
    rows = store.items(partner_id, order_id)
    if any(not r['applied'] for r in rows):
        raise ValueError('تحویل این بسته هنوز کامل نیست؛ «تکمیل تحویل» را بزنید')
    content = io.BytesIO()
    index = io.StringIO()
    manifest = csv.writer(index)
    manifest.writerow(['مشتری', 'فایل QR', 'فایل اتصال'])
    with zipfile.ZipFile(content, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for r in rows:
            u = db.get(r['user_id'])
            if not u or u.owner_tg != partner_id:
                raise ValueError('مالکیت یکی از اکانت‌ها عوض شده؛ صادرات متوقف شد')
            stem = f"account-{u.id}"
            manifest.writerow([csv_safe(r['label']), stem+'.png', stem+'.txt'])
            z.writestr(stem+'.png', h.qr_png(links.sub_url(u)))
            z.writestr(stem+'.txt', f"{r['label']}\n{u.name}\n{links.sub_url(u)}\n"
                       + '\n'.join(links.all_links(u)))
        z.writestr('customers.csv', '\ufeff' + index.getvalue())
        z.writestr('README.txt', 'بستهٔ محرمانهٔ تحویل: هر QR و فایل متنی فقط برای مشتری همان اکانت است.\n'
                   'لینک‌ها مانند رمز دسترسی هستند. کل بسته را عمومی یا برای یک مشتری ارسال نکنید.\n'
                   'مدت اشتراک از اولین استفاده شروع می‌شود. اشتراک پولی شرط عضویت کانال ندارد.')
    return content.getvalue()


async def fulfill_bulk(bot, order):
    async with _fulfill_lock:
        store.attempt(order.tg_id, order.id)
        rows = store.provision(order.tg_id, order.id)
        for row in rows:
            if row['applied']:
                continue
            u = await membership.prepare_account(bot, db.get(row['user_id']), new=True)
            if u.disabled_reason == 'provisioning':
                db.update(u.id, enabled=1, disabled_reason='')
            u = db.get(u.id)
            # A failed hot update leaves a retryable item; never restart the data plane.
            await xray.sync_user(u, bool(u.enabled), allow_restart=False)
            store.applied(order.tg_id, row['id'])
        data = package(order.tg_id, order.id)
        try:
            await bot.send_document(order.tg_id, BufferedInputFile(data, f'QR-order-{order.id}.zip'),
                caption=f"📦 سفارش گروهی #{order.id} آماده است • {len(rows)} اکانت\n"
                "هر فایل را فقط به مشتری خودش بدهید؛ بسته محرمانه است.",
                reply_markup=h.ikb([[("🗂 مشتری‌های من", "pw:contacts:0"), ("📊 سود و حسابداری", "pw:finance")]]))
        except TelegramAPIError:
            log.warning('bulk QR delivery failed; order=%s', order.id)
        if any(db.get(r['user_id']).channel_blocked for r in rows):
            try:
                await bot.send_message(order.tg_id, membership.JOIN_TEXT, reply_markup=membership.keyboard())
            except TelegramAPIError:
                log.warning('bulk membership notice failed; order=%s', order.id)


async def recover(bot):
    # At most one unfinished paid batch per monitoring pass; no external commands.
    with db.connect() as c:
        r = c.execute("SELECT o.id FROM orders o JOIN partner_batches b ON b.order_id=o.id "
            "WHERE o.status='approved' AND EXISTS(SELECT 1 FROM partner_items i WHERE i.order_id=o.id AND i.applied=0) "
            'AND b.last_attempt<? ORDER BY b.last_attempt,o.id LIMIT 1', (int(time.time())-300,)).fetchone()
    if r:
        try:
            await fulfill_bulk(bot, shopdb.order(r[0]))
        except (RuntimeError, OSError, ValueError, TelegramAPIError):
            log.warning('bulk fulfillment remains pending; order=%s', r[0])


def timestamp(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def csv_safe(text):
    text = str(text)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text


def financial_csv(partner_id):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['نوع', 'شناسه', 'مبلغ تومان', 'زمان UTC'])
    # Bounded export, scoped exactly like the paginated view; no credentials in CSV.
    for page in range(100):
        rows = store.ledger(partner_id, page)
        for r in rows:
            writer.writerow([r['kind'], r['ref'], r['amount'], timestamp(r['at'])])
        if len(rows) < 10:
            break
    return ('\ufeff' + out.getvalue()).encode('utf-8')


@router.callback_query(F.data.startswith('pw:'))
async def callback(cb: CallbackQuery, state: FSMContext, bot):
    if not allowed(cb):
        await cb.answer('پنل فقط در گفت‌وگوی خصوصی نماینده در دسترس است', show_alert=True)
        return
    await cb.answer()
    action = cb.data.split(':')[1]
    tg = cb.from_user.id
    raw = cb.data.split(':')[2] if len(cb.data.split(':')) > 2 else '0'
    number = partners.integer(raw)
    if number is None:
        return
    if action == 'contacts':
        rows = store.contacts(tg, min(number, 100000))
        buttons = [[(f"{r['label'][:35]} · {r['accounts']} اکانت", f"pw:contact:{r['id']}")] for r in rows]
        nav = []
        if number:
            nav.append(('‹ قبلی', f'pw:contacts:{number-1}'))
        if len(rows) == 10:
            nav.append(('بعدی ›', f'pw:contacts:{number+1}'))
        if nav:
            buttons.append(nav)
        buttons += [[('➕ مشتری', 'pw:add'), ('🔎 جست‌وجو', 'pw:search')],
                    [('👥 مشتری‌های معرفی‌شده', 'rp:customers:0')], [('↩️ نمایندگی', 'rp:home')]]
        await cb.message.answer('🗂 <b>دفتر مشتری‌های اختصاصی</b>\nنام و یادداشت این دفتر فقط برای خود شماست.\n'
                                'مشتری معرفی‌شده به بات، اکانت تحت مالکیت شما محسوب نمی‌شود.'
                                + ('\nهنوز مشتری ثبت نشده است.' if not rows else ''), reply_markup=h.ikb(buttons))
    elif action in ('add', 'search'):
        await state.clear()
        await state.set_state(Edit.contact if action == 'add' else Edit.search)
        await cb.message.answer('نام مشتری | یادداشت اختیاری (بدون رمز یا اطلاعات بانکی)' if action == 'add'
                                else 'بخشی از نام مشتری را بنویسید.', reply_markup=h.CANCEL_KB)
    elif action == 'contact':
        c = store.contact(tg, number)
        if not c:
            await cb.message.answer('مشتری پیدا نشد.')
            return
        extra = cb.data.split(':')
        page = min(partners.integer(extra[3]) or 0, 100000) if len(extra) > 3 else 0
        rows = store.items(tg, contact_id=number, page=page, limit=10)
        legacy = store.legacy_accounts(tg, number, page)
        lines = [f"🗂 <b>{html.escape(c['label'])}</b>", html.escape(c['note']), '']
        buttons = []
        for r in rows:
            u = db.get(r['user_id']) if r['user_id'] else None
            lines.append(f"سفارش #{r['order_id']} • {'تحویل‌شده' if r['applied'] else 'در انتظار تحویل'}")
            if u and u.owner_tg == tg and r['applied']:
                buttons.append([(f"📦 اکانت #{u.id}", f"pw:account:{r['id']}")])
        nav = []
        if page:
            nav.append(('‹ قبلی', f'pw:contact:{number}:{page-1}'))
        if len(rows) == 10:
            nav.append(('بعدی ›', f'pw:contact:{number}:{page+1}'))
        for u in legacy:
            buttons.append([(f"📦 اکانت قبلی {u['name'][:25]}", f"acc:{u['id']}")])
        if len(legacy) == 10 and len(rows) != 10:
            nav.append(('بعدی ›', f'pw:contact:{number}:{page+1}'))
        buttons.append([('🔗 اتصال اکانت قبلی', f'pw:attach:{number}')])
        await cb.message.answer('\n'.join(lines), reply_markup=h.ikb(buttons + ([nav] if nav else [])
                                + [[('↩️ مشتری‌ها', 'pw:contacts:0')]]))
    elif action == 'attach':
        if not store.contact(tg, number):
            return
        await state.clear()
        await state.set_state(Edit.attach)
        await state.update_data(contact_id=number)
        await cb.message.answer('شناسهٔ عددی اکانت قبلی را از «📊 حساب من» وارد کنید.\n'
                                'فقط اکانت خریداری‌شده توسط خودتان؛ اکانت معرفی‌شدهٔ مشتری قابل تصاحب نیست.',
                                reply_markup=h.CANCEL_KB)
    elif action == 'attachok':
        if await state.get_state() != Edit.attachconfirm.state:
            await cb.message.answer('فرم منقضی شده است.')
            return
        data = await state.get_data()
        try:
            created = store.link_account(tg, data['contact_id'], data['user_id'])
        except ValueError as e:
            await cb.message.answer(html.escape(str(e)))
            return
        await state.clear()
        await cb.message.answer('✅ اکانت قبلی متصل شد؛ سود گذشته حدس زده نمی‌شود.' if created else 'قبلاً همین اتصال ثبت شده است.',
                                reply_markup=h.ikb([[('↩️ مشتری', f"pw:contact:{data['contact_id']}")]]))
    elif action == 'account':
        rows = store.items(tg, item_id=number)
        r = rows[0] if rows else None
        u = db.get(r['user_id']) if r and r['user_id'] else None
        if not u or not r['applied'] or u.owner_tg != tg:
            await cb.message.answer('اکانت تحویل‌شدهٔ متعلق به شما پیدا نشد.')
            return
        buttons = [[('🔄 تمدید', f'rn:{u.id}'), ('➕ حجم اضافه', f'ad:{u.id}')],
                   [('🧾 ثبت مبلغ دریافتی', f"pw:sale:{r['id']}")], [('↩️ مشتری', f"pw:contact:{r['contact_id']}")]]
        await cb.message.answer(f"👤 {html.escape(r['label'])}\n\n" + fmt.user_card(u), reply_markup=h.ikb(buttons))
        await bot.send_photo(tg, BufferedInputFile(h.qr_png(links.sub_url(u)), f'account-{u.id}.png'),
                             caption=f"📱 QR اتصال • {html.escape(u.name)}\n"
                                     f"سابسکریپشن: <code>{html.escape(links.sub_url(u))}</code>")
    elif action == 'bulk':
        await state.clear()
        rows = [[(f'{p.title[:35]} · {partners.cash(shop.reseller_price(shopdb.customer(tg), p.price))}',
                  f'pw:plan:{p.id}')] for p in shopdb.plans() if p.price > 0]
        await cb.message.answer('📦 <b>ساخت گروهی</b>\nیک پلن برای همهٔ اعضای بسته انتخاب کنید؛ '
                                'قبل از پرداخت، هزینه و سود تخمینی نمایش داده می‌شود.', reply_markup=h.ikb(rows + [[('↩️ نمایندگی', 'rp:home')]]))
    elif action == 'plan':
        p = shopdb.plan(number)
        if not p or not p.active or p.kind != 'plan' or p.price <= 0:
            await cb.message.answer('پلن موجود نیست.')
            return
        await state.clear()
        await state.set_state(Edit.rows)
        await state.update_data(plan_id=p.id, unit=shop.reseller_price(shopdb.customer(tg), p.price),
                                workspace=True, terms=[p.gb, p.days], title=shop.plan_line(p), request_key=secrets.token_urlsafe(24))
        await cb.message.answer(f'{html.escape(shop.plan_line(p))}\n\nبرای هر مشتری یک سطر بنویسید:\n<code>نام مشتری | قیمت فروش تومان</code>\n'
                                'مثال:\n<code>علی | 150000\nسارا | 180000</code>\n'
                                f'حداکثر {store.MAX_BATCH} مشتری؛ قیمت فروش فقط تخمین است، نه دریافت پول.', reply_markup=h.CANCEL_KB)
    elif action == 'pay':
        if await state.get_state() != Edit.preview.state:
            await cb.message.answer('فرم منقضی شده؛ بسته‌های من را بررسی کنید.')
            return
        data = await state.get_data()
        try:
            o, created = store.checkout(tg, data['plan_id'], data['rows'], data['request_key'], data['unit'], data['terms'])
        except ValueError as e:
            await cb.message.answer(html.escape(str(e)))
            return
        await state.clear()
        if not created:
            await cb.message.answer(f'سفارش #{o.id} قبلاً ثبت شده؛ دوباره کسر نشد.', reply_markup=back())
        elif o.final_price == 0:
            if shopdb.decide_order(o.id, 'approved', 0):
                try:
                    await fulfill_bulk(bot, shopdb.order(o.id))
                except (RuntimeError, OSError, ValueError, TelegramAPIError):
                    await cb.message.answer(f'سفارش #{o.id} پرداخت شده؛ تحویل در صف تکمیل است. '
                                            'از «بسته‌های من ← تکمیل تحویل» پیگیری کنید. مبلغ دوباره کسر نمی‌شود.', reply_markup=back())
        else:
            await state.set_state(shop.Buy.receipt)
            await state.update_data(order_id=o.id)
            await cb.message.answer(f"💳 سفارش گروهی #{o.id} • {len(data['rows'])} اکانت\n"
                f"پرداخت نقدی: <b>{partners.cash(o.final_price)}</b>\nکسر از کیف پول: {partners.cash(o.wallet_used)}\n"
                f"مبلغ به ریال برای کپی: <code>{o.final_price*10}</code>\n\n{shop.card_html()}\n\n"
                'دقیقاً همین مبلغ را واریز کنید. در صورت فعال‌بودن تأیید پیامکی، خودکار تأیید می‌شود؛ '
                'در غیر این صورت عکس رسید را بفرستید. تا ۴۸ ساعت معتبر است.', reply_markup=h.CANCEL_KB)
    elif action == 'batches':
        records = store.batches(tg, min(number, 100000))
        rows = [[(f"#{b['order_id']} • {b['ready']}/{b['quantity']} آماده • {b['status']}", f"pw:batch:{b['order_id']}")] for b in records]
        nav = []
        if number:
            nav.append(('‹ قبلی', f'pw:batches:{number-1}'))
        if len(records) == 10:
            nav.append(('بعدی ›', f'pw:batches:{number+1}'))
        await cb.message.answer('📦 <b>بسته‌های من</b>' + ('\nبسته‌ای ندارید.' if not records else ''),
            reply_markup=h.ikb(rows + ([nav] if nav else []) + [[('↩️ نمایندگی', 'rp:home')]]))
    elif action in ('batch', 'zip', 'retry'):
        b = store.batch(tg, number)
        if not b:
            await cb.message.answer('بسته پیدا نشد.')
            return
        if action == 'zip':
            try:
                data = package(tg, number)
            except ValueError as e:
                await cb.message.answer(html.escape(str(e)))
                return
            await bot.send_document(tg, BufferedInputFile(data, f'QR-order-{number}.zip'), caption='🔐 بستهٔ محرمانهٔ QR؛ عمومی منتشر نکنید.')
        elif action == 'retry':
            if b['status'] != 'approved':
                await cb.message.answer('ابتدا پرداخت سفارش باید تأیید شود.')
                return
            try:
                await fulfill_bulk(bot, shopdb.order(number))
            except (RuntimeError, OSError, ValueError, TelegramAPIError):
                await cb.message.answer('تحویل کامل نشد؛ اکانت تکراری ساخته نشد و مبلغ دوباره کسر نشد. پشتیبانی را مطلع کنید.')
        else:
            count = sum(bool(r['applied']) for r in store.items(tg, number))
            await cb.message.answer(f"📦 سفارش #{number}\nوضعیت پرداخت: {b['status']}\n"
                f"تحویل آماده: {count}/{b['quantity']}\nهر اکانت از اولین استفاده شروع می‌شود.",
                reply_markup=h.ikb([[('🗜 دریافت بستهٔ QR', f'pw:zip:{number}')],
                    [('🛠 تکمیل تحویل بدون کسر مجدد', f'pw:retry:{number}')], [('↩️ بسته‌های من', 'pw:batches:0')]]))
    elif action == 'finance':
        s, cash = store.financials(tg), partners.store.summary(tg)
        await cb.message.answer('📊 <b>سود و حسابداری</b>\n\n'
            f"👛 کیف پول خرید: {partners.cash(s['purchase_wallet'])}\n"
            f"⏳ رزرو خریدهای منتظر پرداخت/بررسی: {partners.cash(s['wallet_reserved'])}\n\n"
            f"خرید گروهی تأییدشده: {partners.cash(s['purchases'])}\n"
            f"دریافتی ثبت‌شده از مشتری: {partners.cash(s['revenue'])}\n"
            f"سود ناخالص ثبت‌شده: <b>{partners.cash(s['gross_profit'])}</b>\n"
            f"بهای موجودی فروخته‌نشده: {partners.cash(s['inventory'])}\n"
            f"حاشیهٔ تخمینی بسته‌های آماده: {partners.cash(s['expected_margin'])}\n\n"
            f"💎 پورسانت قابل‌برداشت از بات: {partners.cash(cash['available'])}\n\n"
            'سود فروش بر اساس مبلغی است که خودتان ثبت می‌کنید؛ هزینه‌های جانبی کسر نشده‌اند. '
            'فروش خارج بات و موجودی خرید، قابل برداشت از ربات نیستند.',
            reply_markup=h.ikb([[('📒 گردش حساب', 'pw:ledger:0'), ('📥 خروجی CSV', 'pw:csv')],
                               [('💎 ریز پورسانت', 'rp:sales:0'), ('🧾 تسویه‌ها', 'rp:withdrawals:0')], [('↩️ نمایندگی', 'rp:home')]]))
    elif action == 'ledger':
        rows = store.ledger(tg, min(number, 100000))
        labels = {'commission': '💎 پورسانت +', 'purchase': '🛒 خرید −', 'retail': '💵 دریافتی شخصی +',
                  'reserve': '⏳ رزرو برداشت', 'paid': '✅ تسویهٔ رزرو', 'rejected': '↩️ آزادسازی رزرو'}
        text = ['📒 <b>گردش حساب تفکیک‌شده</b>', 'این رویدادها از حساب‌های متفاوت‌اند؛ رزرو و تسویه دوبار هزینه نیستند.', '']
        for r in rows:
            text.append(f"{labels[r['kind']]} {partners.cash(r['amount'])} • #{r['ref']}\n{timestamp(r['at'])}")
        await cb.message.answer('\n'.join(text) if rows else 'هنوز گردش ثبت نشده است.',
            reply_markup=h.ikb(([[("‹ قبلی", f'pw:ledger:{number-1}')]] if number else [])
                + ([[("بعدی ›", f'pw:ledger:{number+1}')]] if len(rows) == 10 else []) + [[('↩️ حسابداری', 'pw:finance')]]))
    elif action == 'csv':
        await bot.send_document(tg, BufferedInputFile(financial_csv(tg), 'partner-ledger.csv'),
                                caption='گردش حداکثر ۱۰۰۰ رویداد اخیر؛ مبلغ‌ها به تومان و حساب‌ها تفکیک‌شده‌اند. بدون کانفیگ مشتری.')
    elif action == 'sale':
        rows = store.items(tg, item_id=number)
        row = rows[0] if rows and rows[0]['applied'] else None
        if not row:
            return
        await state.clear()
        await state.set_state(Edit.received)
        await state.update_data(item_id=number)
        await cb.message.answer('مبلغی را که واقعاً از مشتری دریافت کرده‌اید، به تومان وارد کنید.\n'
                                'این ثبت حسابداری است، انتقال وجه یا افزایش موجودی قابل‌برداشت نیست.', reply_markup=h.CANCEL_KB)
    elif action == 'saleok':
        if await state.get_state() != Edit.saleconfirm.state:
            await cb.message.answer('فرم منقضی شده است.')
            return
        data = await state.get_data()
        try:
            created = store.record_sale(tg, data['item_id'], data['received'])
        except ValueError as e:
            await cb.message.answer(html.escape(str(e)))
            return
        await state.clear()
        await cb.message.answer('✅ دریافتی ثبت شد.' if created else 'قبلاً ثبت شده؛ دوباره محاسبه نشد.', reply_markup=back())


@router.message(Edit.contact)
@router.message(Edit.attach)
@router.message(Edit.search)
@router.message(Edit.rows)
@router.message(Edit.received)
async def inputs(msg: Message, state: FSMContext):
    if not partners.allowed(msg.from_user.id) or msg.chat.type != 'private':
        return
    if msg.text == h.BTN_CANCEL:
        await state.clear()
        await msg.answer('لغو شد.', reply_markup=partners.customer_kb(msg.from_user.id))
        return
    stage, data = await state.get_state(), await state.get_data()
    if stage == Edit.attach.state:
        n = partners.integer(msg.text)
        u = db.get(n) if n else None
        if not u or u.owner_tg != msg.from_user.id:
            await msg.answer('اکانت خریداری‌شدهٔ متعلق به شما پیدا نشد.')
            return
        await state.update_data(user_id=u.id)
        await state.set_state(Edit.attachconfirm)
        await msg.answer(f'اکانت <b>{html.escape(u.name)}</b> به این مشتری در دفتر اختصاصی متصل شود؟\n'
                         'مالکیت یا الزام عضویت کانال تغییر نمی‌کند.', reply_markup=h.ikb([
                             [('✅ تأیید اتصال', 'pw:attachok')], [('↩️ انصراف', 'rp:home')]]))
    elif stage == Edit.contact.state:
        parts = (msg.text or '').split('|', 1)
        try:
            store.add_contact(msg.from_user.id, parts[0], parts[1] if len(parts) == 2 else '')
        except ValueError as e:
            await msg.answer(html.escape(str(e)))
            return
        await state.clear()
        await msg.answer('✅ مشتری در دفتر خصوصی شما ثبت شد.', reply_markup=h.ikb([[('🗂 مشتری‌ها', 'pw:contacts:0')]]))
    elif stage == Edit.search.state:
        rows = store.contacts(msg.from_user.id, search=(msg.text or '').strip())
        await state.clear()
        await msg.answer('🔎 نتایج جست‌وجو (حداکثر ۱۰ مورد)' if rows else 'مشتری پیدا نشد.',
            reply_markup=h.ikb([[(r['label'][:40], f"pw:contact:{r['id']}")] for r in rows] + [[('↩️ مشتری‌ها', 'pw:contacts:0')]]))
    elif stage == Edit.rows.state:
        try:
            rows = store.parse_rows(msg.text)
        except ValueError as e:
            await msg.answer(html.escape(str(e)))
            return
        total, retail = len(rows)*data['unit'], sum(price for _, price in rows)
        await state.update_data(rows=rows)
        await state.set_state(Edit.preview)
        await msg.answer(f"🧾 <b>پیش‌نمایش بسته</b>\n{html.escape(data['title'])}\n\n{len(rows)} مشتری\n"
            f"بهای هر اکانت: {partners.cash(data['unit'])}\nکل خرید: <b>{partners.cash(total)}</b>\n"
            f"فروش تخمینی: {partners.cash(retail)}\nحاشیهٔ تخمینی: {partners.cash(retail-total)}\n\n"
            'پس از تأیید پرداخت ساخته می‌شود. رقم شناسایی بانک ممکن است اندکی مبلغ نقدی را تغییر دهد. '
            'قیمت فروش واردشده، دریافت پول را تأیید نمی‌کند.',
            reply_markup=h.ikb([[('✅ ثبت سفارش و پرداخت', 'pw:pay')], [('↩️ انصراف', 'rp:home')]]))
    elif stage == Edit.received.state:
        n = partners.integer(msg.text)
        if n is None or n >= 10**12:
            await msg.answer('مبلغ باید عدد صحیح نامنفی به تومان باشد.')
            return
        await state.update_data(received=n)
        await state.set_state(Edit.saleconfirm)
        await msg.answer(f"دریافت واقعی {partners.cash(n)} برای این اکانت ثبت شود؟\n"
                         'هر اکانت فقط یک بار ثبت می‌شود؛ اصلاح بعدی نیازمند بررسی پشتیبانی است.',
                         reply_markup=h.ikb([[('✅ تأیید ثبت دریافتی', 'pw:saleok')], [('↩️ انصراف', 'rp:home')]]))


# Local import avoids a shop/partners/workspace import cycle at module initialization.
import shop
