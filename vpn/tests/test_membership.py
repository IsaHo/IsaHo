import asyncio
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

# ruff: noqa: E402
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import db
import membership
import shop
import shopdb
import xray


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        shopdb.init()
        db.set_setting("membership_enabled", "1")
        db.set_setting("membership_channel", "-100123")
        db.set_setting("membership_url", "https://t.me/+example")
        membership._lock = asyncio.Lock()
        self.joined = True
        self.own_status = "administrator"

        async def get_member(chat, user):
            return SimpleNamespace(status=self.own_status if user == 999 else
                                   "member" if self.joined else "left")

        self.bot = SimpleNamespace(id=999, get_chat_member=mock.AsyncMock(side_effect=get_member),
                                   send_message=mock.AsyncMock())
        self.sync = mock.AsyncMock()
        self.patcher = mock.patch("membership.xray.sync_user", self.sync)
        self.patcher.start()
        self.u = db.create_user("trial", 1, 3)
        db.update(self.u.id, tg_id=101, up=123, down=456, note="test")
        shopdb.customer(101)

    def tearDown(self):
        self.patcher.stop()
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def user(self):
        return db.get(self.u.id)

    async def test_leave_suspends_without_changing_billing(self):
        before = self.user()
        self.joined = False
        self.assertFalse(await membership.reconcile_user(self.bot, 101))
        after = self.user()
        self.assertTrue(after.enabled)
        self.assertTrue(after.channel_blocked)
        self.assertFalse(after.accessible)
        self.assertEqual((before.used, before.expire_at, before.uuid, before.sub_token),
                         (after.used, after.expire_at, after.uuid, after.sub_token))
        self.assertEqual([], db.active_users())
        self.sync.assert_awaited_once_with(mock.ANY, False, allow_restart=False)

    async def test_join_restores_valid_account(self):
        db.update(self.u.id, channel_blocked=1)
        await membership.reconcile_user(self.bot, 101)
        self.assertTrue(self.user().accessible)
        self.sync.assert_awaited_once_with(mock.ANY, True, allow_restart=False)

    async def test_join_preserves_manual_disable(self):
        db.update(self.u.id, channel_blocked=1, enabled=0, disabled_reason="manual")
        await membership.reconcile_user(self.bot, 101)
        self.assertEqual("manual", self.user().disabled_reason)
        self.assertFalse(self.user().accessible)
        self.sync.assert_awaited_once_with(mock.ANY, False, allow_restart=False)

    async def test_join_never_revives_expired_or_exhausted_account(self):
        for fields in ({"expire_at": int(time.time()) - 1}, {"traffic_limit": 1}):
            db.update(self.u.id, channel_blocked=1, **fields)
            await membership.reconcile_user(self.bot, 101)
            self.assertFalse(self.user().accessible)
            self.assertFalse(self.sync.call_args.args[1])

    async def test_join_preserves_device_ban(self):
        db.update(self.u.id, channel_blocked=1, enabled=0, disabled_reason="iplimit:9999999999")
        await membership.reconcile_user(self.bot, 101)
        self.assertEqual("iplimit:9999999999", self.user().disabled_reason)
        self.assertFalse(self.user().accessible)

    async def test_api_failure_is_unknown_and_preserves_state(self):
        self.bot.get_chat_member.side_effect = TimeoutError()
        self.assertIsNone(await membership.reconcile_user(self.bot, 101))
        self.assertTrue(self.user().accessible)
        self.sync.assert_not_awaited()

    async def test_loss_of_admin_does_not_suspend_customers(self):
        self.own_status = "member"
        self.joined = False
        self.assertIsNone(await membership.reconcile_user(self.bot, 101))
        self.assertTrue(self.user().accessible)

    async def test_failed_hot_update_is_persisted_and_retried(self):
        self.joined = False
        self.sync.side_effect = RuntimeError("unavailable")
        await membership.reconcile_user(self.bot, 101)
        self.assertTrue(self.user().channel_pending)
        self.sync.side_effect = None
        await membership.reconcile_user(self.bot, 101)
        self.assertFalse(self.user().channel_pending)
        self.assertEqual(2, self.sync.await_count)

    async def test_disable_feature_unblocks_only_membership(self):
        db.update(self.u.id, channel_blocked=1, enabled=0, disabled_reason="manual")
        db.set_setting("membership_enabled", "0")
        self.joined = False
        await membership.reconcile(self.bot)
        self.assertFalse(self.user().channel_blocked)
        self.assertFalse(self.user().enabled)
        self.bot.get_chat_member.assert_not_awaited()

    async def test_linked_end_user_precedes_reseller(self):
        db.update(self.u.id, owner_tg=202)
        self.joined = False
        await membership.reconcile_user(self.bot, 202)
        self.assertFalse(self.user().channel_blocked)
        await membership.reconcile_user(self.bot, 101)
        self.assertTrue(self.user().channel_blocked)

    async def test_reseller_unlinked_account_is_checked(self):
        db.update(self.u.id, tg_id=None, owner_tg=202)
        self.joined = False
        await membership.reconcile_user(self.bot, 202)
        self.assertTrue(self.user().channel_blocked)

    async def test_stale_leave_event_uses_current_membership(self):
        event = SimpleNamespace(chat=SimpleNamespace(id=-100123),
                                new_chat_member=SimpleNamespace(user=SimpleNamespace(id=101, is_bot=False)))
        self.joined = True
        await membership.member_changed(event, self.bot)
        self.assertTrue(self.user().accessible)

    async def test_other_channel_event_is_ignored(self):
        event = SimpleNamespace(chat=SimpleNamespace(id=-100999))
        await membership.member_changed(event, self.bot)
        self.bot.get_chat_member.assert_not_awaited()

    async def test_trial_gate_does_not_consume_trial(self):
        self.joined = False
        message = SimpleNamespace(from_user=SimpleNamespace(id=101, full_name="Test"),
                                  answer=mock.AsyncMock())
        db.set_setting("shop_test", "100:1")
        await shop.test_account(message, self.bot)
        self.assertEqual(0, shopdb.customer(101).test_used)
        self.assertEqual(1, len(db.all_users()))

    async def test_payment_gate_does_not_debit_wallet_or_create_order(self):
        self.joined = False
        shopdb.add_balance(101, 12345)
        cb = SimpleNamespace(from_user=SimpleNamespace(id=101),
                             message=SimpleNamespace(answer=mock.AsyncMock()), answer=mock.AsyncMock())
        state = mock.AsyncMock()
        await shop.pay(cb, state, self.bot)
        state.get_data.assert_not_awaited()
        self.assertEqual(12345, shopdb.customer(101).balance)

    async def test_unknown_new_paid_account_is_not_activated(self):
        self.bot.get_chat_member.side_effect = TimeoutError()
        user = await membership.prepare_account(self.bot, self.user(), new=True)
        self.assertTrue(user.channel_blocked)
        self.assertFalse(user.accessible)

    async def test_unknown_existing_paid_account_keeps_access(self):
        self.bot.get_chat_member.side_effect = TimeoutError()
        user = await membership.prepare_account(self.bot, self.user())
        self.assertTrue(user.accessible)

    async def test_paid_order_is_delivered_but_not_activated_after_leave(self):
        self.joined = False
        shopdb.add_plan("Sample", 2, 30, 100)
        self.bot.send_photo = mock.AsyncMock()
        order = SimpleNamespace(id=1, tg_id=101, plan_id=shopdb.plans()[0].id,
                                kind="new", account_name="paid", discount_code="", final_price=100)
        with mock.patch("shop.h.apply_user", mock.AsyncMock()) as apply:
            await shop.fulfill(self.bot, order)
            apply.assert_not_awaited()
        account = db.get_by_name("paid")
        self.assertTrue(account.channel_blocked)
        self.assertFalse(account.accessible)
        self.assertEqual(2 * db.GB, account.traffic_limit)
        self.bot.send_photo.assert_awaited_once()

    async def test_return_to_channel_does_not_consume_another_trial(self):
        shopdb.update_customer(101, test_used=1)
        db.update(self.u.id, channel_blocked=1)
        await membership.reconcile_user(self.bot, 101)
        self.assertEqual(1, shopdb.customer(101).test_used)
        self.assertTrue(self.user().accessible)

    async def test_expiry_monitor_still_expires_suspended_accounts(self):
        import main
        db.update(self.u.id, channel_blocked=1, expire_at=int(time.time()) - 1)
        with mock.patch("main.notify", mock.AsyncMock()):
            await main.check_users(self.bot)
        self.assertFalse(self.user().enabled)
        self.assertEqual("expired", self.user().disabled_reason)

    def test_pending_join_request_is_not_membership(self):
        self.assertFalse(membership.is_member(SimpleNamespace(status="left")))
        self.assertFalse(membership.is_member(SimpleNamespace(status="restricted", is_member=False)))
        self.assertTrue(membership.is_member(SimpleNamespace(status="restricted", is_member=True)))

    def test_reset_trials_preserves_customer_and_account(self):
        shopdb.update_customer(101, test_used=1, balance=4321)
        self.assertEqual(1, shopdb.reset_trial_history())
        self.assertEqual(0, shopdb.reset_trial_history())
        self.assertEqual(0, shopdb.customer(101).test_used)
        self.assertEqual(4321, shopdb.customer(101).balance)
        self.assertEqual(579, self.user().used)

    def test_keyboard_has_direct_join_and_check_actions(self):
        kb = membership.keyboard().inline_keyboard
        self.assertEqual("https://t.me/+example", kb[0][0].url)
        self.assertEqual("membership:check", kb[1][0].callback_data)
        _, settings = membership.settings_view()
        self.assertEqual("nav:set:data", settings.inline_keyboard[-1][0].callback_data)

    def test_users_schema_remains_compatible_with_code_rollback(self):
        with db.connect() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        self.assertNotIn("channel_blocked", cols)
        self.assertNotIn("channel_pending", cols)

    async def test_running_xray_is_not_restarted_when_bot_starts(self):
        with mock.patch("xray.write_config", mock.AsyncMock()), \
                mock.patch("xray.is_active", mock.AsyncMock(return_value=True)), \
                mock.patch("xray.restart", mock.AsyncMock()) as restart:
            await xray.ensure_started()
            restart.assert_not_awaited()

    async def test_xray_hot_update_cannot_bypass_channel_block(self):
        self.patcher.stop()
        db.update(self.u.id, channel_blocked=1)
        with mock.patch("xray.write_config", mock.AsyncMock()), \
                mock.patch("xray.flush_stats", mock.AsyncMock()), \
                mock.patch("xray._api_remove", mock.AsyncMock(return_value=True)) as remove, \
                mock.patch("xray._api_add", mock.AsyncMock()) as add:
            await xray.sync_user(self.user(), True)
            remove.assert_awaited_once()
            add.assert_not_awaited()
        self.patcher.start()

    async def test_hot_update_failure_never_restarts_for_membership(self):
        self.patcher.stop()
        with mock.patch("xray.write_config", mock.AsyncMock()), \
                mock.patch("xray.flush_stats", mock.AsyncMock()), \
                mock.patch("xray._api_remove", mock.AsyncMock(return_value=False)), \
                mock.patch("xray.restart", mock.AsyncMock()) as restart:
            with self.assertRaises(RuntimeError):
                await xray.sync_user(self.user(), False, allow_restart=False)
            restart.assert_not_awaited()
        self.patcher.start()


if __name__ == "__main__":
    unittest.main()
