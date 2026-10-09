import concurrent.futures
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import db
import partnerdb as store
import partners
import shop
import shopdb


class PartnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        shopdb.init()
        shopdb.customer(10, "Partner <unsafe>")
        store.configure(10, 20)
        shopdb.customer(20, "Customer", 10)
        shopdb.add_plan("Plan", 10, 30, 100000)
        self.owner = mock.patch("config.cfg", SimpleNamespace(admin_ids=[999], brand="Brand"))
        self.owner.start()

    def tearDown(self):
        self.owner.stop()
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def order(self, **kw):
        data = {"tg_id": 20, "plan_id": 1, "kind": "new", "user_id": None, "account_name": "account",
                "price": 100000, "final_price": 100000, "status": "pending"}
        data.update(kw)
        return shopdb.create_order(**data)

    def credit(self, amount=100000):
        o = self.order(final_price=amount)
        shopdb.decide_order(o.id, 'approved', 999)
        return o, store.settle(o.id)

    def test_cash_commission_separate_from_purchase_wallet(self):
        o, credit = self.credit()
        self.assertEqual(20000, credit['amount'])
        self.assertEqual(20000, store.summary(10)['available'])
        self.assertEqual(0, shopdb.customer(10).balance)
        self.assertEqual(o.id, store.history(10)[0]['order_id'])

    def test_pending_rejected_canceled_never_credit(self):
        for status in ('waiting', 'pending', 'rejected', 'canceled'):
            o = self.order(status=status)
            self.assertIsNone(store.settle(o.id))
        self.assertEqual(0, store.summary(10)['earned'])

    def test_duplicate_settlement_is_idempotent(self):
        o, _ = self.credit()
        self.assertIsNone(store.settle(o.id))
        self.assertEqual(20000, store.summary(10)['available'])

    def test_concurrent_settlement_credits_once(self):
        o = self.order(status='approved')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            rs = list(executor.map(lambda _: store.settle(o.id), range(4)))
        self.assertEqual(1, sum(r is not None for r in rs))
        self.assertEqual(20000, store.summary(10)['earned'])

    def test_percent_and_referrer_are_snapshotted(self):
        o = self.order()
        store.configure(10, 90)
        shopdb.update_customer(20, referrer=30)
        shopdb.decide_order(o.id, 'approved', 999)
        credit = store.settle(o.id)
        self.assertEqual((10, 20, 20000), (credit['partner_id'], credit['percent'], credit['amount']))

    def test_zero_cash_or_free_trial_earns_nothing(self):
        for kw in ({'final_price': 0, 'wallet_used': 100000}, {'kind': 'trial'}, {'kind': 'wallet'}):
            o = self.order(status='approved', **kw)
            self.assertIsNone(store.settle(o.id))
        self.assertEqual(0, store.summary(10)['earned'])

    def test_partial_wallet_cash_only(self):
        o = self.order(status='approved', final_price=50000, wallet_used=50000)
        self.assertEqual(10000, store.settle(o.id)['amount'])

    def test_all_service_purchase_kinds(self):
        for kind in ('new', 'renew', 'addon'):
            o = self.order(status='approved', kind=kind)
            self.assertEqual(20000, store.settle(o.id)['amount'])
        self.assertEqual(3, store.summary(10)['sales'])

    def test_legacy_approved_orders_not_retroactively_credited(self):
        o = self.order(status='approved')
        with db.connect() as c:
            c.execute('DELETE FROM partner_sales WHERE order_id=?', (o.id,))
        shopdb.init()
        self.assertIsNone(store.settle(o.id))

    def test_legacy_pending_order_preserves_generic_reward_not_cash(self):
        db.set_setting('shop_ref_percent', '10')
        o = self.order()
        with db.connect() as c:
            c.execute('DELETE FROM partner_sales WHERE order_id=?', (o.id,))
        shopdb.init()
        shopdb.decide_order(o.id, 'approved', 999)
        credit = store.settle(o.id)
        self.assertEqual('wallet', credit['kind'])
        self.assertEqual(10000, shopdb.customer(10).balance)
        self.assertEqual(0, store.summary(10)['earned'])

    def test_generic_referral_reward_not_paid_twice(self):
        shopdb.customer(30)
        shopdb.customer(40, referrer=30)
        db.set_setting('shop_ref_percent', '10')
        o = self.order(tg_id=40, status='approved')
        self.assertEqual('wallet', store.settle(o.id)['kind'])
        self.assertIsNone(store.settle(o.id))
        self.assertEqual(10000, shopdb.customer(30).balance)
        self.assertEqual(0, store.summary(30)['available'])

    def test_partner_never_gets_generic_reward_in_addition(self):
        db.set_setting('shop_ref_percent', '10')
        self.credit()
        self.assertEqual(0, shopdb.customer(10).balance)

    def test_self_referral_ignored(self):
        c = shopdb.customer(50, referrer=50)
        self.assertIsNone(c.referrer)

    def test_second_invite_never_steals_attribution(self):
        self.assertEqual(10, shopdb.customer(20, referrer=30).referrer)

    def test_paused_partner_new_sales_zero_old_snapshot_honored(self):
        o = self.order(status='approved')
        store.configure(10, 20, False)
        self.assertEqual(20000, store.settle(o.id)['amount'])
        self.assertIsNone(store.settle(self.order(status='approved').id))
        # Stopping future commissions cannot confiscate existing earnings.
        store.request(10, 10000, 'destination', 'paused')
        self.assertEqual(10000, store.summary(10)['available'])

    def test_withdrawal_reserves_and_reject_releases(self):
        self.credit()
        r, created = store.request(10, 15000, 'destination', 'key')
        self.assertTrue(created)
        self.assertEqual(5000, store.summary(10)['available'])
        self.assertTrue(store.decide(r['id'], 'rejected', 999, 'reason'))
        self.assertEqual(20000, store.summary(10)['available'])

    def test_paid_withdrawal_deducts_once(self):
        self.credit()
        r, _ = store.request(10, 20000, 'destination', 'key')
        self.assertTrue(store.decide(r['id'], 'paid', 999, 'transfer-123'))
        self.assertFalse(store.decide(r['id'], 'paid', 999, 'duplicate'))
        self.assertFalse(store.decide(r['id'], 'rejected', 999, 'late'))
        self.assertEqual(0, store.summary(10)['available'])
        self.assertEqual(20000, store.summary(10)['paid'])

    def test_repeat_request_returns_same_row(self):
        self.credit()
        r, _ = store.request(10, 20000, 'destination', 'key')
        repeat, created = store.request(10, 20000, 'destination', 'key')
        self.assertFalse(created)
        self.assertEqual(r['id'], repeat['id'])
        with self.assertRaises(ValueError):
            store.request(20, 20000, 'destination', 'key')

    def test_invalid_or_overdrawn_request(self):
        self.credit()
        for amount in (-1, 0, 1.5, 20001):
            with self.assertRaises(ValueError):
                store.request(10, amount, 'destination', str(amount))

    def test_concurrent_requests_cannot_overdraw(self):
        self.credit()
        def attempt(key):
            try:
                store.request(10, 15000, 'destination', key)
                return True
            except ValueError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            rs = list(executor.map(attempt, ['one', 'two']))
        self.assertEqual(1, sum(rs))
        self.assertEqual(5000, store.summary(10)['available'])

    def test_only_owner_can_settle_withdrawal(self):
        self.credit()
        r, _ = store.request(10, 10000, 'destination', 'key')
        with self.assertRaises(PermissionError):
            store.decide(r['id'], 'paid', 10, 'transfer-123')
        self.assertEqual('pending', store.withdrawal(r['id'])['status'])

    def test_transfer_reference_required(self):
        with self.assertRaises(ValueError):
            store.decide(1, 'paid', 999, '')

    def test_existing_reseller_migration_preserves_wallet_and_discount(self):
        shopdb.customer(70)
        shopdb.update_customer(70, reseller_percent=30, balance=50000)
        shopdb.init()
        self.assertEqual(0, store.profile(70)['percent'])
        self.assertEqual(30, shopdb.customer(70).reseller_percent)
        self.assertEqual(50000, shopdb.customer(70).balance)
        store.configure(70, 25, False)
        shopdb.init()
        self.assertEqual(0, store.profile(70)['enabled'])

    def test_customer_list_never_exposes_account_credentials(self):
        self.credit()
        self.assertEqual({'name', 'created_at', 'purchases'}, set(store.customers(10)[0]))
        self.assertEqual([], store.customers(999))

    def test_cash_history_is_paginated_and_scoped(self):
        for _ in range(12):
            self.credit()
        self.assertEqual(10, len(store.history(10)))
        self.assertEqual(2, len(store.history(10, 1)))
        self.assertEqual([], store.history(20))

    def test_assign_old_customer_owner_only_and_no_overwrite(self):
        shopdb.customer(80)
        with self.assertRaises(PermissionError):
            store.assign(10, 80, 10)
        store.assign(10, 80, 999)
        self.assertEqual(10, shopdb.customer(80).referrer)
        with self.assertRaises(ValueError):
            store.assign(10, 80, 999)
        with self.assertRaises(ValueError):
            store.assign(10, 10, 999)
        with self.assertRaises(ValueError):
            store.assign(10, 9999, 999)

    def test_old_customer_link_does_not_rewrite_existing_order(self):
        shopdb.customer(80)
        o = self.order(tg_id=80, status='approved')
        store.assign(10, 80, 999)
        self.assertIsNone(store.settle(o.id))
        new = self.order(tg_id=80, status='approved')
        self.assertEqual(20000, store.settle(new.id)['amount'])

    async def test_outsider_cannot_access_partner_callbacks(self):
        cb = SimpleNamespace(data='rp:home', from_user=SimpleNamespace(id=888), answer=mock.AsyncMock())
        await partners.callback(cb, mock.AsyncMock(), mock.AsyncMock())
        cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)

    async def test_successful_fulfillment_credits_and_notifies(self):
        o = self.order(status='approved')
        u = db.create_user('fixture', 10, 30)
        bot = mock.AsyncMock()
        with mock.patch('shop.db.create_user', return_value=u), \
                mock.patch('shop.membership.reconcile_user', new_callable=mock.AsyncMock), \
                mock.patch('shop.membership.prepare_account', new_callable=mock.AsyncMock, return_value=u), \
                mock.patch('shop.h.apply_user', new_callable=mock.AsyncMock), \
                mock.patch('shop.h.qr_png', return_value=b'qr'), \
                mock.patch('shop.links.sub_url', return_value='https://example.test/sub'), \
                mock.patch('shop.h.links_text', return_value='links'):
            await shop.fulfill(bot, o)
        self.assertEqual(20000, store.summary(10)['available'])
        self.assertTrue(any(call.args[0] == 10 for call in bot.send_message.await_args_list))

    async def test_failed_fulfillment_does_not_credit(self):
        o = self.order(status='approved')
        with mock.patch('shop.membership.reconcile_user', new_callable=mock.AsyncMock), \
                mock.patch('shop.db.create_user', side_effect=RuntimeError('delivery failed')), \
                self.assertRaises(RuntimeError):
            await shop.fulfill(mock.AsyncMock(), o)
        self.assertEqual(0, store.summary(10)['earned'])

    async def test_invalid_iban_not_accepted(self):
        msg = SimpleNamespace(text='IR00000000000000000000000000 | Name', answer=mock.AsyncMock())
        state = mock.AsyncMock()
        await partners.destination(msg, state)
        state.set_state.assert_not_awaited()
        msg.answer.assert_awaited_once()

    async def test_valid_iban_requires_final_confirmation(self):
        bban = '0' * 21 + '1'
        check = 98 - int(bban + '182700') % 97
        iban = f'IR{check:02d}{bban}'
        msg = SimpleNamespace(text=f'{iban} | Fixture', answer=mock.AsyncMock())
        state = mock.AsyncMock()
        state.get_data.return_value = {'amount': 1000}
        await partners.destination(msg, state)
        state.set_state.assert_awaited_once_with(partners.Edit.confirm)
        self.assertEqual(0, len(store.withdrawals(10)))

    async def test_nonowner_cannot_read_withdrawal_destination(self):
        cb = SimpleNamespace(data='pa:request:1', from_user=SimpleNamespace(id=888), answer=mock.AsyncMock())
        with mock.patch('partners.cfg', SimpleNamespace(admin_ids=[999])):
            await partners.admin_callback(cb, mock.AsyncMock())
        cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)

    async def test_expired_confirmation_never_creates_withdrawal(self):
        cb = SimpleNamespace(data='rp:submit', from_user=SimpleNamespace(id=10), answer=mock.AsyncMock(),
                             message=SimpleNamespace(answer=mock.AsyncMock()))
        state = mock.AsyncMock()
        state.get_state.return_value = None
        await partners.callback(cb, state, mock.AsyncMock())
        self.assertEqual([], store.withdrawals(10))

    def test_number_parser_rejects_overflow_and_nonintegers(self):
        for raw in ('inf', 'nan', '1.5', '-1', '9'*100, '9223372036854775808'):
            self.assertIsNone(partners.integer(raw))
        self.assertEqual(123000, partners.integer('۱۲۳٬۰۰۰'))

    def test_rate_validation_and_change_audit(self):
        for n in (-1, 101, 1.5, True):
            with self.assertRaises(ValueError):
                store.configure(10, n)
        with self.assertRaises(PermissionError):
            store.configure(10, 30, owner_id=10)
        store.configure(10, 30, owner_id=999)
        with db.connect() as c:
            r = c.execute('SELECT * FROM partner_rate_audit ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual((20, 30, 999), (r['old_percent'], r['new_percent'], r['owner_id']))

    def test_keyboard_only_shown_to_registered_partners(self):
        self.assertTrue(any(b.text == partners.BUTTON for row in partners.customer_kb(10).keyboard for b in row))
        self.assertFalse(any(b.text == partners.BUTTON for row in partners.customer_kb(20).keyboard for b in row))


if __name__ == '__main__':
    unittest.main()
