import concurrent.futures
import io
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bot')))
import db
import partnerdb
import partnerwork
import shop
import shopdb
import workdb


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        shopdb.init()
        shopdb.customer(10, 'Rep')
        shopdb.update_customer(10, reseller_percent=20)
        partnerdb.configure(10, 25)
        shopdb.customer(20, 'Other rep')
        partnerdb.configure(20, 30)
        shopdb.add_plan('Plan', 10, 30, 100000)
        self.rows = [('Ali', 120000), ('Sara', 150000)]
        self.bank = mock.patch('workdb.smspay.unique_amount', side_effect=lambda amount: amount+1)
        self.bank.start()

    def tearDown(self):
        self.bank.stop()
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def checkout(self, key='key', rows=None):
        return workdb.checkout(10, 1, rows or self.rows, key, 80000, [10, 30])[0]

    def approve(self):
        o = self.checkout()
        self.assertTrue(shopdb.auto_approve(o.id))
        return shopdb.order(o.id)

    def ready(self):
        o = self.approve()
        rows = workdb.provision(10, o.id)
        for r in rows:
            workdb.applied(10, r['id'])
        return o, workdb.items(10, o.id)

    def test_parse_names_prices_bounds_and_persian_digits(self):
        self.assertEqual([('علی', 123000)], workdb.parse_rows('علی | ۱۲۳٬۰۰۰'))
        for raw in ('', 'no delimiter', 'Ali | -10', 'Ali | nan', 'Ali | 1.5', 'Ali | 1\nAli | 2',
                    '\n'.join(f'{i} | 1' for i in range(21))):
            with self.assertRaises(ValueError):
                workdb.parse_rows(raw)

    def test_crm_private_and_idempotent(self):
        cid = workdb.add_contact(10, 'Ali', 'note')
        self.assertEqual(cid, workdb.add_contact(10, 'Ali', 'other'))
        self.assertEqual([], workdb.contacts(20))
        self.assertIsNone(workdb.contact(20, cid))
        self.assertEqual('note', workdb.contact(10, cid)['note'])

    def test_one_invoice_discount_wallet_and_cash(self):
        shopdb.add_balance(10, 50000)
        o = self.checkout()
        self.assertEqual('bulk', o.kind)
        self.assertEqual((160000, 50000, 110001), (o.price, o.wallet_used, o.final_price))
        self.assertEqual(0, shopdb.customer(10).balance)
        self.assertEqual(160001, sum(i['cost'] for i in workdb.items(10, o.id)))
        self.assertEqual([], db.all_users())

    def test_duplicate_checkout_never_spends_twice(self):
        shopdb.add_balance(10, 200000)
        o = self.checkout()
        second, created = workdb.checkout(10, 1, self.rows, 'key', 80000)
        self.assertFalse(created)
        self.assertEqual(o.id, second.id)
        self.assertEqual(40000, shopdb.customer(10).balance)

    def test_concurrent_duplicate_checkout_one_order(self):
        shopdb.add_balance(10, 200000)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            rs = list(executor.map(lambda _: workdb.checkout(10, 1, self.rows, 'key', 80000), range(2)))
        self.assertEqual(1, sum(created for _, created in rs))
        self.assertEqual(rs[0][0].id, rs[1][0].id)
        self.assertEqual(40000, shopdb.customer(10).balance)

    def test_two_batches_cannot_overdraw_wallet(self):
        shopdb.add_balance(10, 200000)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            rs = list(executor.map(lambda key: workdb.checkout(10, 1, self.rows, key, 80000), ['a', 'b']))
        self.assertEqual(200000, sum(o.wallet_used for o, _ in rs))
        self.assertEqual(0, shopdb.customer(10).balance)

    def test_failed_checkout_rolls_back_wallet_order_and_contacts(self):
        shopdb.add_balance(10, 200000)
        with mock.patch('workdb._contact', side_effect=RuntimeError('failure')), self.assertRaises(RuntimeError):
            self.checkout()
        self.assertEqual(200000, shopdb.customer(10).balance)
        self.assertEqual([], workdb.batches(10))
        self.assertEqual([], workdb.contacts(10))

    def test_price_and_terms_changes_require_new_preview(self):
        shopdb.set_price(1, 200000)
        with self.assertRaises(ValueError):
            self.checkout()
        shopdb.set_price(1, 100000)
        with self.assertRaises(ValueError):
            workdb.checkout(10, 1, self.rows, 'key', 80000, [20, 30])

    def test_paused_or_unknown_partner_cannot_buy(self):
        partnerdb.configure(10, 25, False)
        with self.assertRaises(ValueError):
            self.checkout()
        with self.assertRaises(ValueError):
            workdb.checkout(999, 1, self.rows, 'new', 100000)

    def test_form_key_cannot_be_reused_by_other_rep(self):
        self.checkout()
        with self.assertRaises(ValueError):
            workdb.checkout(20, 1, self.rows, 'key', 100000)

    def test_unapproved_orders_never_provision(self):
        o = self.checkout()
        with self.assertRaises(ValueError):
            workdb.provision(10, o.id)
        self.assertEqual([], db.all_users())

    def test_repeat_provision_preserves_credentials_and_ownership(self):
        o = self.approve()
        first = workdb.provision(10, o.id)
        users = [db.get(r['user_id']) for r in first]
        second = workdb.provision(10, o.id)
        self.assertEqual([r['user_id'] for r in first], [r['user_id'] for r in second])
        self.assertEqual(2, len(db.all_users()))
        for u in users:
            self.assertEqual(10, u.owner_tg)
            self.assertEqual(0, u.enabled)
            self.assertEqual(30, u.pending_days)
            self.assertEqual(0, u.expire_at)
        with self.assertRaises(ValueError):
            workdb.provision(20, o.id)

    def test_provision_is_atomic_on_conflicting_account_name(self):
        o = self.approve()
        db.create_user(f'rp{o.id}_2_fixed', 10, 30)
        with mock.patch('workdb.secrets.token_hex', return_value='fixed'), self.assertRaises(sqlite3.IntegrityError):
            workdb.provision(10, o.id)
        self.assertEqual(1, len(db.all_users()))
        self.assertTrue(all(r['user_id'] is None for r in workdb.items(10, o.id)))

    def test_batch_uses_purchased_terms_after_plan_changes(self):
        o = self.approve()
        with db.connect() as c:
            c.execute('UPDATE plans SET gb=100,days=365 WHERE id=1')
        rows = workdb.provision(10, o.id)
        self.assertEqual(10*db.GB, db.get(rows[0]['user_id']).traffic_limit)
        self.assertEqual(30, db.get(rows[0]['user_id']).pending_days)

    def test_batch_cannot_generate_referral_commission(self):
        shopdb.update_customer(10, referrer=20)
        o = self.approve()
        self.assertIsNone(partnerdb.settle(o.id))
        self.assertEqual(0, partnerdb.summary(20)['earned'])

    def test_QR_package_owner_only_complete_and_no_path_traversal(self):
        o, rows = self.ready()
        with mock.patch('partnerwork.h.qr_png', return_value=b'fixture PNG'), \
                mock.patch('partnerwork.links.sub_url', side_effect=lambda u: f'https://example.test/{u.id}'), \
                mock.patch('partnerwork.links.all_links', return_value=['fixture link']):
            data = partnerwork.package(10, o.id)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                self.assertEqual(6, len(z.namelist()))
                self.assertTrue(all('..' not in name and '/' not in name for name in z.namelist()))
                self.assertIn(b'Ali', z.read(f"account-{rows[1]['user_id']}.txt"))
            with self.assertRaises(ValueError):
                partnerwork.package(20, o.id)
            db.update(rows[0]['user_id'], owner_tg=20)
            with self.assertRaises(ValueError):
                partnerwork.package(10, o.id)

    def test_partial_batch_cannot_export(self):
        o = self.approve()
        workdb.provision(10, o.id)
        with self.assertRaises(ValueError):
            partnerwork.package(10, o.id)

    def test_gross_profit_only_after_real_received_amount(self):
        _, rows = self.ready()
        s = workdb.financials(10)
        self.assertEqual(0, s['gross_profit'])
        self.assertEqual(160001, s['inventory'])
        r = rows[0]
        workdb.record_sale(10, r['id'], 140000)
        s = workdb.financials(10)
        self.assertEqual(140000, s['revenue'])
        self.assertEqual(140000-r['cost'], s['gross_profit'])
        self.assertEqual(160001-r['cost'], s['inventory'])
        self.assertEqual(0, partnerdb.summary(10)['available'])

    def test_sale_record_idempotent_and_foreign_item_forbidden(self):
        _, rows = self.ready()
        r = rows[0]
        self.assertTrue(workdb.record_sale(10, r['id'], 150000))
        self.assertFalse(workdb.record_sale(10, r['id'], 150000))
        with self.assertRaises(ValueError):
            workdb.record_sale(10, r['id'], 160000)
        with self.assertRaises(ValueError):
            workdb.record_sale(20, r['id'], 150000)

    def test_unfulfilled_item_sale_forbidden(self):
        o = self.approve()
        rows = workdb.provision(10, o.id)
        with self.assertRaises(ValueError):
            workdb.record_sale(10, rows[0]['id'], 100000)

    def test_ledger_and_csv_scoped_without_credentials(self):
        _, rows = self.ready()
        workdb.record_sale(10, rows[0]['id'], 150000)
        self.assertEqual({'purchase', 'retail'}, {r['kind'] for r in workdb.ledger(10)})
        self.assertEqual([], workdb.ledger(20))
        content = partnerwork.financial_csv(10).decode('utf-8')
        self.assertNotIn(db.get(rows[0]['user_id']).uuid, content)
        self.assertNotIn(db.get(rows[0]['user_id']).sub_token, content)

    async def test_hot_update_failure_retry_no_new_accounts_or_restart(self):
        o = self.approve()
        bot = mock.AsyncMock()
        with mock.patch('partnerwork.membership.reconcile_user', new_callable=mock.AsyncMock), \
                mock.patch('partnerwork.membership.prepare_account', side_effect=lambda bot,u,new: u), \
                mock.patch('partnerwork.xray.sync_user', new_callable=mock.AsyncMock, side_effect=RuntimeError('API unavailable')) as sync:
            with self.assertRaises(RuntimeError):
                await partnerwork.fulfill_bulk(bot, o)
            self.assertFalse(sync.await_args.kwargs['allow_restart'])
        first = [u.uuid for u in db.all_users()]
        with mock.patch('partnerwork.membership.reconcile_user', new_callable=mock.AsyncMock), \
                mock.patch('partnerwork.membership.prepare_account', side_effect=lambda bot,u,new: u), \
                mock.patch('partnerwork.xray.sync_user', new_callable=mock.AsyncMock) as sync, \
                mock.patch('partnerwork.package', return_value=b'zip'):
            await shop.fulfill(bot, o)
        self.assertEqual(first, [u.uuid for u in db.all_users()])
        self.assertTrue(all(r['applied'] for r in workdb.items(10, o.id)))
        self.assertTrue(all(call.kwargs['allow_restart'] is False for call in sync.await_args_list))

    async def test_manual_disabled_account_not_reenabled_during_retry(self):
        o = self.approve()
        rows = workdb.provision(10, o.id)
        db.update(rows[0]['user_id'], enabled=0, disabled_reason='manual')
        with mock.patch('partnerwork.membership.reconcile_user', new_callable=mock.AsyncMock), \
                mock.patch('partnerwork.membership.prepare_account', side_effect=lambda bot,u,new: u), \
                mock.patch('partnerwork.xray.sync_user', new_callable=mock.AsyncMock), \
                mock.patch('partnerwork.package', return_value=b'zip'):
            await partnerwork.fulfill_bulk(mock.AsyncMock(), o)
        self.assertEqual(0, db.get(rows[0]['user_id']).enabled)

    async def test_private_chat_required_and_outsider_denied(self):
        for tg, chat in [(10, 'group'), (999, 'private')]:
            cb = SimpleNamespace(data='pw:finance', from_user=SimpleNamespace(id=tg), answer=mock.AsyncMock(),
                                 message=SimpleNamespace(chat=SimpleNamespace(type=chat)))
            await partnerwork.callback(cb, mock.AsyncMock(), mock.AsyncMock())
            cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)

    async def test_membership_gate_prevents_checkout(self):
        cb = SimpleNamespace(data='pw:pay', from_user=SimpleNamespace(id=10), answer=mock.AsyncMock(),
            message=SimpleNamespace(chat=SimpleNamespace(type='private'), answer=mock.AsyncMock()))
        state = mock.AsyncMock()
        state.get_state.return_value = partnerwork.Edit.preview.state
        with mock.patch('partnerwork.membership.ensure', new_callable=mock.AsyncMock, return_value=False):
            await partnerwork.callback(cb, state, mock.AsyncMock())
        self.assertEqual([], workdb.batches(10))

    async def test_recovery_only_approved_and_backoff(self):
        self.checkout()
        with mock.patch('partnerwork.fulfill_bulk', new_callable=mock.AsyncMock) as fulfill:
            await partnerwork.recover(mock.AsyncMock())
            fulfill.assert_not_awaited()
            self.assertTrue(shopdb.auto_approve(1))
            await partnerwork.recover(mock.AsyncMock())
            fulfill.assert_awaited_once()
            workdb.attempt(10, 1)
            await partnerwork.recover(mock.AsyncMock())
            fulfill.assert_awaited_once()

    def test_lookup_old_item_beyond_latest_100(self):
        _, rows = self.ready()
        self.assertEqual(rows[0]['id'], workdb.items(10, item_id=rows[0]['id'])[0]['id'])
        self.assertEqual([], workdb.items(20, item_id=rows[0]['id']))

    def test_legacy_account_link_owner_only_no_financial_invention(self):
        cid = workdb.add_contact(10, 'Old customer')
        u = db.create_user('old', 10, 30)
        db.update(u.id, owner_tg=10)
        self.assertTrue(workdb.link_account(10, cid, u.id))
        self.assertFalse(workdb.link_account(10, cid, u.id))
        self.assertEqual(1, len(workdb.legacy_accounts(10, cid)))
        self.assertEqual(0, workdb.financials(10)['purchases'])
        with self.assertRaises(ValueError):
            workdb.link_account(20, cid, u.id)
        with self.assertRaises(ValueError):
            workdb.link_account(10, workdb.add_contact(10, 'Another'), u.id)

    def test_referred_direct_customer_account_cannot_be_claimed(self):
        cid = workdb.add_contact(10, 'Customer')
        shopdb.customer(30, referrer=10)
        u = db.create_user('direct', 10, 30)
        db.update(u.id, tg_id=30)
        with self.assertRaises(ValueError):
            workdb.link_account(10, cid, u.id)

    def test_csv_manifest_neutralizes_formula(self):
        self.assertEqual("'=danger", partnerwork.csv_safe('=danger'))
        self.assertEqual('customer', partnerwork.csv_safe('customer'))

    def test_late_receipt_does_not_reopen_approved_order(self):
        o = self.approve()
        self.assertFalse(shopdb.submit_receipt(o.id, 10, 'photo'))
        self.assertEqual('approved', shopdb.order(o.id).status)

    def test_receipt_customer_bound_and_submitted_once(self):
        o = self.checkout()
        self.assertFalse(shopdb.submit_receipt(o.id, 20, 'photo'))
        self.assertTrue(shopdb.submit_receipt(o.id, 10, 'photo'))
        self.assertFalse(shopdb.submit_receipt(o.id, 10, 'second photo'))
        self.assertEqual('photo', shopdb.order(o.id).receipt)

    def test_receipt_vs_auto_approval_never_reopens_order(self):
        o = self.checkout()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fs = [executor.submit(shopdb.submit_receipt, o.id, 10, 'photo'),
                  executor.submit(shopdb.auto_approve, o.id)]
            [f.result() for f in fs]
        self.assertEqual('approved', shopdb.order(o.id).status)

    def test_single_checkout_cannot_overspend_wallet_reserved_by_batch(self):
        shopdb.add_balance(10, 200000)
        self.checkout()
        with self.assertRaises(ValueError):
            shopdb.create_order(reserve_wallet=True, tg_id=10, plan_id=1, kind='new',
                price=80000, wallet_used=80000, final_price=0, status='pending')
        self.assertEqual(40000, shopdb.customer(10).balance)

    def test_wallet_reservation_rolls_back_when_snapshot_fails(self):
        shopdb.add_balance(10, 100000)
        with mock.patch('partnerdb.snapshot', side_effect=RuntimeError('failure')), self.assertRaises(RuntimeError):
            shopdb.create_order(reserve_wallet=True, tg_id=10, plan_id=1, kind='new',
                price=80000, wallet_used=80000, final_price=0, status='pending')
        self.assertEqual(100000, shopdb.customer(10).balance)

    def test_rejection_releases_purchase_wallet_once(self):
        shopdb.add_balance(10, 50000)
        o = self.checkout()
        self.assertTrue(shopdb.submit_receipt(o.id, 10, 'photo'))
        self.assertTrue(shopdb.decide_order(o.id, 'rejected', 999))
        self.assertEqual(50000, shopdb.customer(10).balance)
        self.assertFalse(shopdb.decide_order(o.id, 'rejected', 999))
        self.assertEqual(50000, shopdb.customer(10).balance)

    def test_expiry_refunds_atomically_once_and_skips_approved(self):
        shopdb.add_balance(10, 50000)
        o = self.checkout()
        shopdb.update_order(o.id, created_at=1)
        self.assertEqual(1, len(shopdb.expire_waiting(2)))
        self.assertEqual(50000, shopdb.customer(10).balance)
        self.assertEqual([], shopdb.expire_waiting(2))
        self.assertFalse(shopdb.auto_approve(o.id))
        newer = self.checkout(key='new')
        self.assertTrue(shopdb.auto_approve(newer.id))
        self.assertEqual([], shopdb.expire_waiting(2**31))
        self.assertEqual(0, shopdb.customer(10).balance)

    def test_real_QR_bundle_contains_PNG_and_customer_manifest(self):
        o, rows = self.ready()
        with mock.patch('partnerwork.links.sub_url', side_effect=lambda u: f'https://example.test/sub/{u.id}'), \
                mock.patch('partnerwork.links.all_links', return_value=[]):
            data = partnerwork.package(10, o.id)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            png = z.read(f"account-{rows[0]['user_id']}.png")
            self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn('Ali', z.read('customers.csv').decode('utf-8-sig'))

    async def test_stale_individual_payment_cannot_consume_group_form(self):
        cb = SimpleNamespace(from_user=SimpleNamespace(id=10), answer=mock.AsyncMock(),
            message=SimpleNamespace(answer=mock.AsyncMock(), edit_reply_markup=mock.AsyncMock()))
        state = mock.AsyncMock()
        state.get_data.return_value = {'workspace': True, 'plan_id': 1}
        with mock.patch('shop.membership.ensure', new_callable=mock.AsyncMock, return_value=True):
            await shop.pay(cb, state, mock.AsyncMock())
        cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)
        self.assertEqual([], shopdb.open_orders(0))

    async def test_left_channel_after_payment_blocks_bulk_accounts(self):
        o = self.approve()
        with mock.patch('partnerwork.membership.reconcile_user', new_callable=mock.AsyncMock), \
                mock.patch('partnerwork.membership.required', return_value=True), \
                mock.patch('partnerwork.membership.status', new_callable=mock.AsyncMock, return_value=False), \
                mock.patch('partnerwork.xray.sync_user', new_callable=mock.AsyncMock), \
                mock.patch('partnerwork.package', return_value=b'zip'):
            await partnerwork.fulfill_bulk(mock.AsyncMock(), o)
        self.assertTrue(all(u.channel_blocked and not u.accessible for u in db.all_users()))
