import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import db
import handlers
import health
import healthdb
import links
import nodes


class NodeHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg, self.old_reports = db.cfg, nodes.reports.copy()
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        healthdb.init()
        nodes.reports.clear()
        self.node = {"name": "DE2", "ip": "192.0.2.2", "private": True,
                     "domain": "cdn.example.test", "cdn_enabled": True, "cdn_port": 443}
        nodes.save([self.node])

    def tearDown(self):
        db.cfg = self.old_cfg
        nodes.reports.clear()
        nodes.reports.update(self.old_reports)
        self.tmp.cleanup()

    def report(self, **updates):
        nodes.record_stats(self.node, {}, {"xray": "active", "config_apply": {"state": "in_sync"},
                                          "reality_public": False, **updates})

    def test_active_fresh_is_healthy_failed_is_not(self):
        self.report()
        self.assertTrue(nodes.healthy(self.node))
        self.report(xray="failed")
        self.assertTrue(nodes.online(self.node))
        self.assertFalse(nodes.healthy(self.node))

    def test_stale_active_is_not_healthy(self):
        self.report()
        nodes.reports["DE2"]["seen"] -= 181
        self.assertFalse(nodes.healthy(self.node))

    def test_rejected_config_is_unhealthy_even_when_active(self):
        self.report(config_apply={"state": "rejected", "reason": "port 443 occupied"})
        self.assertFalse(nodes.healthy(nodes.all_nodes()[0]))
        text, _ = handlers.nodes_view()
        self.assertIn("کانفیگ اعمال نشد", text)
        self.assertIn("🔴 نیازمند بررسی", text)

    def test_failed_runtime_never_shows_green_node(self):
        self.report(xray="failed")
        text, _ = handlers.nodes_view()
        section = text.split("<b>DE2</b>", 1)[1]
        self.assertNotIn("🟢", section)
        self.assertIn("failed", section)

    async def test_runtime_incident_is_idempotent_and_closes(self):
        self.report(xray="failed")
        with mock.patch("health._notify", new_callable=mock.AsyncMock) as notify, mock.patch("resilience.auto_recover", new_callable=mock.AsyncMock) as recover:
            await health.evaluate_node_reports(mock.Mock())
            await health.evaluate_node_reports(mock.Mock())
            self.assertEqual(1, notify.await_count)
            self.assertEqual(1, len(healthdb.incidents(active_only=True)))
            recover.assert_not_awaited()
            self.report()
            await health.evaluate_node_reports(mock.Mock())
            self.assertEqual(2, notify.await_count)
            self.assertEqual([], healthdb.incidents(active_only=True))

    async def test_stale_failure_does_not_alert_or_close(self):
        self.report(xray="failed")
        nodes.reports["DE2"]["seen"] -= 181
        with mock.patch("health._notify", new_callable=mock.AsyncMock) as notify:
            await health.evaluate_node_reports(mock.Mock())
            notify.assert_not_awaited()
        self.assertEqual([], healthdb.incidents(active_only=True))

    async def test_rejection_triggers_node_incident(self):
        self.report(config_apply={"state": "rejected", "reason": "port occupied"})
        with mock.patch("health._notify", new_callable=mock.AsyncMock):
            await health.evaluate_node_reports(mock.Mock())
        self.assertEqual("node:DE2:runtime", healthdb.incidents(active_only=True)[0].path_key)

    def test_publication_waits_for_correct_role_ack(self):
        self.assertTrue(nodes.toggle_private("DE2", expected_private=True))
        node = nodes.all_nodes()[0]
        self.assertFalse(nodes.public_ready(node))
        self.assertEqual([], nodes.public_nodes())
        self.report(reality_public=False)
        self.assertTrue(nodes.all_nodes()[0]["mode_pending"])
        self.report(reality_public=True)
        node = nodes.all_nodes()[0]
        self.assertFalse(node["mode_pending"])
        self.assertTrue(nodes.public_ready(node))

    def test_rejected_mode_remains_pending_and_suppressed(self):
        nodes.toggle_private("DE2")
        self.report(reality_public=False, config_apply={"state": "rejected"})
        node = nodes.all_nodes()[0]
        self.assertTrue(node["config_rejected"])
        self.assertTrue(node["mode_pending"])
        self.assertFalse(nodes.public_ready(node))
        self.report(reality_public=True)
        self.assertTrue(nodes.public_ready(nodes.all_nodes()[0]))

    def test_unknown_apply_state_cannot_clear_rejection(self):
        self.report(config_apply={"state": "rejected"})
        self.report(config_apply={"state": "unknown"})
        self.assertTrue(nodes.all_nodes()[0]["config_rejected"])

    def test_stale_confirmation_does_not_toggle_back(self):
        nodes.toggle_private("DE2", expected_private=True)
        self.assertFalse(nodes.toggle_private("DE2", expected_private=True))
        self.assertFalse(nodes.all_nodes()[0]["private"])

    def test_pending_reality_does_not_hide_working_cdn(self):
        db.set_setting("link_types", "node")
        nodes.toggle_private("DE2")
        user = SimpleNamespace(id=1, name="probe", uuid="00000000-0000-0000-0000-000000000001")
        with mock.patch.object(links, "cfg", replace(links.cfg, reality_public_key="public")):
            self.assertEqual(["cdn:DE2"], [key for key, _ in links.route_entries(user)])

    async def test_explicit_cdn_node_public_button_is_blocked(self):
        cb = SimpleNamespace(data="nd:priv:DE2", answer=mock.AsyncMock(),
                             message=SimpleNamespace(answer=mock.AsyncMock()))
        await handlers.nodes_private(cb)
        self.assertTrue(nodes.all_nodes()[0]["private"])
        cb.message.answer.assert_not_awaited()

    async def test_preview_does_not_mutate_node(self):
        self.node.pop("cdn_enabled")
        nodes.save([self.node])
        cb = SimpleNamespace(data="nd:priv:DE2", answer=mock.AsyncMock(),
                             message=SimpleNamespace(answer=mock.AsyncMock()))
        await handlers.nodes_private(cb)
        cb.message.answer.assert_awaited_once()
        self.assertTrue(nodes.all_nodes()[0]["private"])

    async def test_confirm_rechecks_cdn_guard(self):
        cb = SimpleNamespace(data="nd:mode:DE2:1", answer=mock.AsyncMock())
        await handlers.nodes_mode_confirm(cb)
        self.assertTrue(nodes.all_nodes()[0]["private"])

    async def test_confirmation_sets_pending_without_claiming_success(self):
        self.node.pop("cdn_enabled")
        nodes.save([self.node])
        cb = SimpleNamespace(data="nd:mode:DE2:1", answer=mock.AsyncMock(),
                             message=SimpleNamespace(edit_text=mock.AsyncMock()))
        await handlers.nodes_mode_confirm(cb)
        self.assertTrue(nodes.all_nodes()[0]["mode_pending"])
        self.assertFalse(nodes.all_nodes()[0]["private"])
        self.assertIn("منتظر", cb.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
