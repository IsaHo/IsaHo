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
import health
import healthdb
import links
import routing


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        healthdb.init()
        self.catalog = {"relay:ir1": ("IR1", ["relay:ir1:vpn"]),
                        "main": ("DE", ["relay:ir1:main", "relay:ir2:main"])}
        self.patch = mock.patch("routing.catalog", return_value=self.catalog)
        self.patch.start()
        self.now = int(time.time())
        self.entries = [("main", "direct"), ("relay:ir1", "tunnel")]

    def tearDown(self):
        self.patch.stop()
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def sample(self, key, ok, stamp, origin="iran", latency=100):
        healthdb.add(key, key, "vpn", origin, ok, latency, "", stamp)
        routing.refresh()

    def series(self, key, ok, start=None):
        start = self.now - 600 if start is None else start
        for i in range(3):
            self.sample(key, ok, start + i * 60)

    def test_three_independent_failures_then_three_successes(self):
        db.set_setting("routing_mode", "hide")
        self.series("relay:ir1:vpn", False)
        self.assertEqual("down", routing.states()["relay:ir1"]["state"])
        self.assertEqual([("main", "direct")], routing.choose(self.entries))
        for i in range(2):
            self.sample("relay:ir1:vpn", True, self.now - 180 + i * 60)
            self.assertEqual("down", routing.states()["relay:ir1"]["state"])
        self.sample("relay:ir1:vpn", True, self.now)
        self.assertEqual("healthy", routing.states()["relay:ir1"]["state"])
        self.assertEqual("tunnel", routing.choose(self.entries)[0][1])

    def test_same_sample_never_counts_three_times(self):
        for _ in range(5):
            self.sample("relay:ir1:vpn", False, self.now)
        self.assertEqual("unknown", routing.states()["relay:ir1"]["state"])

    def test_late_report_cannot_replace_a_newer_success(self):
        with mock.patch("health.links.relays", return_value=[("ir1", 443)]):
            for stamp, ok in [(self.now, True), (self.now - 60, False)]:
                health.ingest_relay_result({"ip": "ir1", "probe_result": {"checked_at": stamp,
                                           "vpn": {"ok": ok, "latency_ms": 100}}})
        self.assertEqual(1, len(healthdb.history("relay:ir1:vpn")))
        self.assertTrue(healthdb.latest()[0].ok)

    def test_all_sources_must_confirm_failure(self):
        self.series("relay:ir1:main", False)
        self.assertEqual("unknown", routing.states()["main"]["state"])
        self.series("relay:ir2:main", False)
        self.assertEqual("down", routing.states()["main"]["state"])

    def test_one_working_iran_source_preserves_route(self):
        self.series("relay:ir1:main", False)
        self.series("relay:ir2:main", True)
        self.assertEqual("healthy", routing.states()["main"]["state"])

    def test_germany_success_cannot_override_iran_failure(self):
        self.series("relay:ir1:vpn", False)
        self.sample("relay:ir1:vpn", True, self.now, origin="main")
        self.assertEqual("down", routing.states()["relay:ir1"]["state"])

    def test_old_results_are_unknown_and_do_not_hide(self):
        db.set_setting("routing_mode", "hide")
        self.series("relay:ir1:vpn", False)
        with mock.patch("routing.time.time", return_value=self.now + routing.FRESH + 1):
            self.assertEqual("unknown", routing.states()["relay:ir1"]["state"])
            self.assertEqual(2, len(routing.choose(self.entries)))

    def test_monitoring_gap_resets_consecutive_chain(self):
        self.series("relay:ir1:vpn", False)
        with mock.patch("routing.time.time", return_value=self.now + 1800):
            self.sample("relay:ir1:vpn", True, self.now + 1800)
            self.assertEqual("unknown", routing.states()["relay:ir1"]["state"])

    def test_all_failed_routes_keep_one_emergency_candidate(self):
        db.set_setting("routing_mode", "hide")
        for key in ["relay:ir1:vpn", "relay:ir1:main", "relay:ir2:main"]:
            self.series(key, False)
        self.assertEqual(1, len(routing.choose(self.entries)))

    def test_off_restores_original_order_and_manual_pause_reversible(self):
        db.set_setting("routing-paused:main", "1")
        self.assertEqual("tunnel", routing.choose(self.entries)[0][1])
        db.set_setting("routing_mode", "off")
        self.assertEqual(self.entries, routing.choose(self.entries))
        db.set_setting("routing-paused:main", "0")
        self.assertEqual("unknown", routing.states()["main"]["state"])

    def test_health_ingestion_deduplicates_replay(self):
        report = {"ip": "ir1", "probe_result": {"checked_at": self.now,
                  "vpn": {"ok": False, "latency_ms": 5000}}}
        with mock.patch("health.links.relays", return_value=[("ir1", 443)]):
            for _ in range(3):
                health.ingest_relay_result(report)
        self.assertEqual(1, len(healthdb.history("relay:ir1:vpn")))
        self.assertEqual("unknown", routing.states()["relay:ir1"]["state"])

    def test_unsupported_public_paths_and_future_samples_are_ignored(self):
        with mock.patch("health.links.relays", return_value=[("ir1", 443)]), \
                mock.patch("health.nodes.public_nodes", return_value=[]):
            health.ingest_relay_result({"ip": "ir1", "probe_result": {"checked_at": self.now,
                "paths": {"public:fake": {"ok": True}}}})
            health.ingest_relay_result({"ip": "ir1", "probe_result": {"checked_at": self.now + 300,
                "vpn": {"ok": False}}})
        self.assertEqual([], healthdb.latest())

    async def test_nonowner_cannot_change_routing(self):
        cb = SimpleNamespace(data="rt:mode:hide", from_user=SimpleNamespace(id=101), answer=mock.AsyncMock())
        with mock.patch("routing.db.admin_ids", return_value=[101]), \
                mock.patch("config.cfg", SimpleNamespace(admin_ids=[202])):
            await routing.callback(cb)
        self.assertEqual("rank", routing.mode())
        cb.answer.assert_awaited_once_with(mock.ANY, show_alert=True)

    async def test_subscriber_notices_are_opt_in_and_target_previous_delivery(self):
        u = db.create_user("buyer", 1, 30)
        db.update(u.id, tg_id=303)
        routing.record_delivery(u.id, [("relay:ir1", "never stored credential")])
        db.set_setting("routing_last_states", '{"relay:ir1":"healthy"}')
        self.series("relay:ir1:vpn", False)
        bot = mock.AsyncMock()
        with mock.patch("routing.db.admin_ids", return_value=[]):
            await routing.notify_changes(bot)
            bot.send_message.assert_not_awaited()
            db.set_setting("routing_customer_notices", "1")
            db.set_setting("routing_last_states", '{"relay:ir1":"healthy"}')
            await routing.notify_changes(bot)
            bot.send_message.assert_awaited_once()
            self.assertEqual(303, bot.send_message.call_args.args[0])

    def test_subscription_content_is_shared_with_manual_links(self):
        u = db.create_user("customer", 1, 30)
        db.set_setting("routing_mode", "hide")
        self.series("relay:ir1:vpn", False)
        with mock.patch("links.route_entries", return_value=self.entries):
            self.assertEqual(["direct"], links.all_links(u))
            import base64
            self.assertEqual("direct", base64.b64decode(links.sub_body(u)).decode())
        with db.connect() as c:
            row = c.execute("SELECT route_keys FROM routing_subscribers").fetchone()
            self.assertEqual('["main"]', row[0])


if __name__ == "__main__":
    unittest.main()
