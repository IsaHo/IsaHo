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
        db.update(self.u.id, tg_id=101, up=123, down=456, note="test", channel_trial=1)
        shopdb.customer(101)

    def tearDown(self):
        self.patcher.stop()
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def user(self):
        return db.get(self.u.id)

    async def test_slow_telegram_is_bounded_without_blocking_account(self):
        async def slow(*args):
            await asyncio.sleep(60)
        self.bot.get_chat_member.side_effect = slow
        with mock.patch.object(membership, 'STATUS_TIMEOUT', 0.02):
            self.assertIsNone(await asyncio.wait_for(membership.status(self.bot, 101), 0.2))
        self.assertFalse(self.user().channel_blocked)

    async def test_gate_cannot_queue_indefinitely_or_bypass_membership(self):
        message = SimpleNamespace(answer=mock.AsyncMock())
        async with membership._lock:
            with mock.patch.object(membership, 'GATE_TIMEOUT', 0.02):
                self.assertFalse(await asyncio.wait_for(membership.ensure(self.bot, 101, message), 0.2))
        self.assertFalse(self.user().channel_blocked)
        self.sync.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_telegram_session_keeps_tls_and_uses_ipv4(self):
        import socket

        from telegram_session import TelegramSession
        session = TelegramSession()
        self.assertEqual(session._connector_init['family'], socket.AF_INET)
        self.assertTrue(session._connector_init['ssl'].check_hostname)
        await session.close()

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

    async def test_exemption_restores_only_selected_account_and_survives_leave(self):
        other = db.create_user("other", 1, 3)
        db.update(other.id, tg_id=101, channel_blocked=1, channel_trial=1)
        db.update(self.u.id, channel_blocked=1)
        self.joined = False
        before = self.user()
        await membership.set_exemption(self.bot, self.u.id, True)
        await membership.reconcile_user(self.bot, 101)
        after = self.user()
        self.assertTrue(after.accessible)
        self.assertTrue(after.channel_exempt)
        self.assertTrue(db.get(other.id).channel_blocked)
        self.assertEqual((before.used, before.expire_at, before.uuid, before.sub_token),
                         (after.used, after.expire_at, after.uuid, after.sub_token))
        self.assertTrue((await membership.prepare_account(self.bot, before)).accessible)

    async def test_exemption_can_be_granted_with_telegram_unavailable(self):
        self.bot.get_chat_member.side_effect = TimeoutError()
        db.update(self.u.id, channel_blocked=1)
        self.sync.side_effect = RuntimeError("unavailable")
        await membership.set_exemption(self.bot, self.u.id, True)
        self.assertTrue(self.user().channel_pending)
        self.sync.side_effect = None
        await membership.reconcile(self.bot)
        self.assertTrue(self.user().accessible)
        self.assertFalse(self.user().channel_pending)
        self.sync.assert_awaited_with(mock.ANY, True, allow_restart=False)

    async def test_exemption_preserves_all_nonmembership_restrictions(self):
        for fields in ({"enabled": 0, "disabled_reason": "manual"},
                       {"enabled": 0, "disabled_reason": "iplimit:9999999999"},
                       {"expire_at": int(time.time()) - 1}, {"traffic_limit": 1}):
            db.update(self.u.id, **fields, channel_blocked=1)
            await membership.set_exemption(self.bot, self.u.id, True)
            self.assertFalse(self.user().accessible)
            self.assertFalse(self.sync.call_args.args[1])
            db.update(self.u.id, enabled=1, disabled_reason="", expire_at=0,
                      traffic_limit=db.GB, channel_exempt=0)

    async def test_revoke_exemption_rechecks_membership(self):
        await membership.set_exemption(self.bot, self.u.id, True)
        self.joined = False
        await membership.set_exemption(self.bot, self.u.id, False)
        self.assertFalse(self.user().channel_exempt)
        self.assertTrue(self.user().channel_blocked)
        self.sync.assert_awaited_with(mock.ANY, False, allow_restart=False)
        self.joined = True
        await membership.reconcile_user(self.bot, 101)
        self.assertTrue(self.user().accessible)

    async def test_unknown_membership_cannot_revoke_exemption(self):
        await membership.set_exemption(self.bot, self.u.id, True)
        self.bot.get_chat_member.side_effect = TimeoutError()
        with self.assertRaises(ValueError):
            await membership.set_exemption(self.bot, self.u.id, False)
        self.assertTrue(self.user().channel_exempt)
        self.assertTrue(self.user().accessible)

    async def test_duplicate_explicit_action_is_idempotent(self):
        await membership.set_exemption(self.bot, self.u.id, True)
        await membership.set_exemption(self.bot, self.u.id, True)
        self.sync.assert_awaited_once()
        self.assertTrue(self.user().channel_exempt)

    async def test_unlinked_exemption_pending_is_retried(self):
        db.update(self.u.id, tg_id=None)
        self.sync.side_effect = RuntimeError("unavailable")
        await membership.set_exemption(self.bot, self.u.id, True)
        self.sync.side_effect = None
        await membership.reconcile(self.bot)
        self.assertFalse(self.user().channel_pending)

    async def test_exemption_does_not_bypass_trial_gate(self):
        await membership.set_exemption(self.bot, self.u.id, True)
        self.joined = False
        message = SimpleNamespace(answer=mock.AsyncMock())
        self.assertFalse(await membership.ensure(self.bot, 101, message))
        self.assertTrue(self.user().accessible)

    async def test_only_owner_can_grant_or_revoke(self):
        for action in ("exempt", "setexempt"):
            cb = SimpleNamespace(data=f"membership:{action}:{self.u.id}:1",
                                 from_user=SimpleNamespace(id=101), answer=mock.AsyncMock())
            with mock.patch("membership.cfg", SimpleNamespace(admin_ids=[202])):
                await membership.exemption_callback(cb, self.bot)
            self.assertFalse(self.user().channel_exempt)
            cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)

    async def test_confirmation_button_and_reversible_card(self):
        import handlers
        cb = SimpleNamespace(data=f"membership:exempt:{self.u.id}",
                             from_user=SimpleNamespace(id=202), answer=mock.AsyncMock(),
                             message=SimpleNamespace(edit_text=mock.AsyncMock()))
        with mock.patch("membership.cfg", SimpleNamespace(admin_ids=[202])):
            await membership.exemption_callback(cb, self.bot)
        kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
        self.assertEqual(f"membership:setexempt:{self.u.id}:1", kb.inline_keyboard[0][0].callback_data)
        self.assertFalse(self.user().channel_exempt)
        await membership.set_exemption(self.bot, self.u.id, True)
        buttons = [b.text for row in handlers.user_kb(self.user()).inline_keyboard for b in row]
        self.assertIn("🔐 بازگرداندن شرط تست", buttons)

    async def test_owner_confirmation_applies_and_refreshes_card(self):
        cb = SimpleNamespace(data=f"membership:setexempt:{self.u.id}:1",
                             from_user=SimpleNamespace(id=202), answer=mock.AsyncMock())
        with mock.patch("membership.cfg", SimpleNamespace(admin_ids=[202])), \
                mock.patch("handlers.show_user", mock.AsyncMock()) as show:
            await membership.exemption_callback(cb, self.bot)
            show.assert_awaited_once_with(cb, mock.ANY)
        self.assertTrue(self.user().channel_exempt)
        self.assertTrue(self.user().accessible)

    async def test_waiting_list_shows_only_blocked_accounts(self):
        db.update(self.u.id, channel_blocked=1)
        db.create_user("active", 1, 3)
        cb = SimpleNamespace(data="membership:waiting:999", from_user=SimpleNamespace(id=202),
                             answer=mock.AsyncMock(), message=SimpleNamespace(edit_text=mock.AsyncMock()))
        with mock.patch("membership.db.admin_ids", return_value=[202]):
            await membership.waiting_callback(cb)
        kb = cb.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(2, len(kb))
        self.assertEqual(f"u:{self.u.id}", kb[0][0].callback_data)
        self.assertEqual("membership:menu", kb[-1][0].callback_data)

    async def test_invalid_owner_callback_never_changes_account(self):
        for data in ("membership:setexempt:bad:1", f"membership:setexempt:{self.u.id}:2",
                     f"membership:exempt:{self.u.id}:1"):
            cb = SimpleNamespace(data=data, from_user=SimpleNamespace(id=202), answer=mock.AsyncMock())
            with mock.patch("membership.cfg", SimpleNamespace(admin_ids=[202])):
                await membership.exemption_callback(cb, self.bot)
            self.assertFalse(self.user().channel_exempt)
            cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)

    def test_migration_keeps_existing_membership_and_defaults_no_exemptions(self):
        with db.connect() as c:
            c.execute("DROP TABLE channel_membership")
            c.execute("CREATE TABLE channel_membership(user_id INTEGER PRIMARY KEY, blocked INTEGER, pending INTEGER)")
            c.execute("INSERT INTO channel_membership VALUES (?,1,1)", (self.u.id,))
        db.init()
        db.init()
        self.assertTrue(self.user().channel_blocked)
        self.assertTrue(self.user().channel_pending)
        self.assertFalse(self.user().channel_exempt)

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

    async def test_join_confirms_registered_customer_without_account(self):
        shopdb.customer(303)
        event = SimpleNamespace(chat=SimpleNamespace(id=-100123),
                                old_chat_member=SimpleNamespace(status="left"),
                                new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=303, is_bot=False)))
        await membership.member_changed(event, self.bot)
        self.bot.send_message.assert_awaited_once()
        self.assertIn("عضویت شما تأیید شد", self.bot.send_message.call_args.args[1])

    async def test_join_has_one_confirmation_for_existing_account(self):
        db.update(self.u.id, channel_blocked=1)
        event = SimpleNamespace(chat=SimpleNamespace(id=-100123),
                                old_chat_member=SimpleNamespace(status="left"),
                                new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=101, is_bot=False)))
        await membership.member_changed(event, self.bot)
        self.assertTrue(self.user().accessible)
        self.bot.send_message.assert_awaited_once()

    async def test_unknown_channel_visitor_gets_no_unsolicited_confirmation(self):
        event = SimpleNamespace(chat=SimpleNamespace(id=-100123),
                                old_chat_member=SimpleNamespace(status="left"),
                                new_chat_member=SimpleNamespace(status="member", user=SimpleNamespace(id=404, is_bot=False)))
        await membership.member_changed(event, self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_removing_absent_xray_user_is_successful(self):
        self.patcher.stop()
        response = "rpc error: code = Unknown desc = proxy/vless: User trial not found."
        with mock.patch("xray._run", mock.AsyncMock(return_value=(0, response, ""))):
            self.assertTrue(await xray._api_remove("trial"))
        self.patcher.start()

    async def test_xray_removal_transport_or_unknown_tag_error_is_not_ignored(self):
        self.patcher.stop()
        for response in ("rpc error: connection refused", "rpc error: inbound handler not found"):
            with mock.patch("xray._run", mock.AsyncMock(return_value=(0, response, ""))):
                self.assertFalse(await xray._api_remove("trial"))
        with mock.patch("xray._run", mock.AsyncMock(return_value=(1, "proxy/vless: User trial not found.", ""))):
            self.assertFalse(await xray._api_remove("trial"))
        self.patcher.start()

    async def test_trial_gate_does_not_consume_trial(self):
        self.joined = False
        message = SimpleNamespace(from_user=SimpleNamespace(id=101, full_name="Test"),
                                  answer=mock.AsyncMock())
        db.set_setting("shop_test", "100:1")
        await shop.test_account(message, self.bot)
        self.assertEqual(0, shopdb.customer(101).test_used)
        self.assertEqual(1, len(db.all_users()))

    async def test_payment_does_not_check_membership(self):
        self.joined = False
        shopdb.add_balance(101, 12345)
        cb = SimpleNamespace(from_user=SimpleNamespace(id=101),
                             message=SimpleNamespace(answer=mock.AsyncMock()), answer=mock.AsyncMock())
        state = mock.AsyncMock()
        state.get_data.return_value = {}
        await shop.pay(cb, state, self.bot)
        state.get_data.assert_awaited_once()
        self.bot.get_chat_member.assert_not_awaited()
        self.assertEqual(12345, shopdb.customer(101).balance)

    async def test_unknown_new_trial_account_is_not_activated(self):
        self.bot.get_chat_member.side_effect = TimeoutError()
        user = await membership.prepare_account(self.bot, self.user(), new=True)
        self.assertTrue(user.channel_blocked)
        self.assertFalse(user.accessible)

    async def test_unknown_existing_trial_account_keeps_access(self):
        self.bot.get_chat_member.side_effect = TimeoutError()
        user = await membership.prepare_account(self.bot, self.user())
        self.assertTrue(user.accessible)

    async def test_paid_order_is_activated_after_leave_without_membership_api(self):
        self.joined = False
        shopdb.add_plan("Sample", 2, 30, 100)
        self.bot.send_photo = mock.AsyncMock()
        order = SimpleNamespace(id=1, tg_id=101, plan_id=shopdb.plans()[0].id,
                                kind="new", account_name="paid", discount_code="", final_price=100)
        with mock.patch("shop.h.apply_user", mock.AsyncMock()) as apply:
            await shop.fulfill(self.bot, order)
            apply.assert_awaited_once()
        account = db.get_by_name("paid")
        self.assertFalse(account.channel_blocked)
        self.assertFalse(account.channel_trial)
        self.assertTrue(account.accessible)
        self.bot.get_chat_member.assert_not_awaited()
        self.assertEqual(2 * db.GB, account.traffic_limit)
        self.bot.send_photo.assert_awaited_once()

    async def test_paid_preparation_clears_legacy_block_during_api_failure(self):
        db.update(self.u.id, channel_trial=0, channel_blocked=1)
        self.bot.get_chat_member.side_effect = TimeoutError()
        user = await membership.prepare_account(self.bot, self.user(), new=True)
        self.assertTrue(user.accessible)
        self.assertFalse(user.channel_blocked)
        self.bot.get_chat_member.assert_not_awaited()

    async def test_mixed_paid_and_trial_leave_blocks_only_trial(self):
        paid = db.create_user("paid", 2, 30)
        db.update(paid.id, tg_id=101, channel_blocked=1)
        self.joined = False
        await membership.reconcile_user(self.bot, 101)
        self.assertTrue(self.user().channel_blocked)
        self.assertFalse(db.get(paid.id).channel_blocked)
        self.assertTrue(db.get(paid.id).accessible)

    async def test_paid_only_reconciliation_never_calls_telegram(self):
        db.update(self.u.id, channel_trial=0, channel_blocked=1)
        self.bot.get_chat_member.side_effect = TimeoutError()
        await membership.reconcile(self.bot)
        self.bot.get_chat_member.assert_not_awaited()
        self.assertFalse(self.user().channel_pending)
        self.assertTrue(self.user().accessible)

    async def test_paid_unblocking_preserves_billing_and_manual_restrictions(self):
        for fields in ({"enabled": 0, "disabled_reason": "manual"},
                       {"enabled": 0, "disabled_reason": "iplimit:9999999999"},
                       {"expire_at": int(time.time()) - 1}, {"traffic_limit": 1}):
            db.update(self.u.id, channel_trial=0, channel_blocked=1, **fields)
            await membership.reconcile_user(self.bot, 101)
            self.assertFalse(self.user().channel_blocked)
            self.assertFalse(self.user().accessible)
            self.assertFalse(self.sync.call_args.args[1])
            db.update(self.u.id, enabled=1, disabled_reason="", expire_at=0, traffic_limit=db.GB)
        self.bot.get_chat_member.assert_not_awaited()

    def test_trial_identity_survives_editing_note(self):
        db.update(self.u.id, note="updated by admin")
        db.init()
        self.assertTrue(self.user().channel_trial)

    async def test_paid_renewal_converts_trial_permanently(self):
        self.joined = False
        db.update(self.u.id, channel_blocked=1)
        shopdb.add_plan("Renew", 2, 30, 100)
        self.bot.send_photo = mock.AsyncMock()
        order = SimpleNamespace(id=1, tg_id=101, plan_id=shopdb.plans()[0].id,
                                kind="renew", user_id=self.u.id, discount_code="", final_price=100)
        with mock.patch("shop.h.maybe_reactivate", mock.AsyncMock(side_effect=lambda u: u)):
            await shop.fulfill(self.bot, order)
        self.assertFalse(self.user().channel_trial)
        self.assertTrue(self.user().accessible)
        self.bot.get_chat_member.assert_not_awaited()
        await membership.reconcile_user(self.bot, 101)
        self.assertTrue(self.user().accessible)

    def test_legacy_trial_migration_respects_approved_purchase_and_is_idempotent(self):
        paid = db.create_user("converted", 2, 30)
        db.update(paid.id, note="test", tg_id=101, channel_blocked=1,
                  enabled=0, disabled_reason="manual")
        with db.connect() as c:
            c.execute("ALTER TABLE channel_membership DROP COLUMN trial")
            c.execute("INSERT INTO orders (tg_id,plan_id,kind,user_id,price,final_price,status,created_at) "
                      "VALUES (101,1,'renew',?,100,100,'approved',1)", (paid.id,))
        db.init()
        db.init()
        self.assertTrue(self.user().channel_trial)
        converted = db.get(paid.id)
        self.assertFalse(converted.channel_trial)
        self.assertFalse(converted.channel_blocked)
        self.assertTrue(converted.channel_pending)
        self.assertFalse(converted.accessible)
        self.assertEqual("manual", converted.disabled_reason)

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
