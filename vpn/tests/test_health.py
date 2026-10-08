import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

# ruff: noqa: E402

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot"))
sys.path.insert(0, BOT_DIR)

import db
import health
import healthdb
import shopdb


class HealthProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        shopdb.init()
        healthdb.init()
        health._relay_jobs.clear()
        health._force_relays.clear()

    def tearDown(self):
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def test_latest_history_failures_and_score(self):
        now = int(time.time())
        healthdb.add("vpn:main", "Main", "vpn", "main", True, 120, "HTTP 204", now - 10)
        healthdb.add("vpn:main", "Main", "vpn", "main", False, 0, "timeout", now)
        healthdb.add("vpn:cdn", "CDN", "cdn", "main", True, 400, "HTTP 204", now)

        latest = {row.path_key: row for row in healthdb.latest()}
        self.assertFalse(latest["vpn:main"].ok)
        self.assertEqual(2, len(healthdb.history("vpn:main")))
        self.assertEqual(1, healthdb.failures("vpn:main", 2))
        self.assertEqual(45, healthdb.score(list(latest.values()), now))

    def test_stale_check_scores_zero(self):
        old = healthdb.add(
            "vpn:main",
            "Main",
            "vpn",
            "main",
            True,
            100,
            "HTTP 204",
            int(time.time()) - 16 * 60,
        )
        self.assertEqual(0, healthdb.path_score(old))

    def test_relay_result_is_ingested_without_credentials(self):
        report = {
            "ip": "94.184.47.122",
            "probe_result": {
                "job_id": "123-IR1",
                "checked_at": int(time.time()),
                "vpn": {"ok": True, "latency_ms": 210, "detail": "HTTP 204"},
                "sub": {"ok": True, "latency_ms": 80, "detail": "HTTP 200"},
            },
        }
        with mock.patch("health.links.relays", return_value=[("94.184.47.122", 443)]):
            health.ingest_relay_result(report)

        rows = healthdb.latest()
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row.origin == "iran" for row in rows))
        self.assertNotIn("uuid", " ".join(row.detail for row in rows).lower())

    def test_build_specs_includes_main_cdn_and_subscription(self):
        fake_cfg = replace(
            health.cfg,
            server_ip="82.115.18.62",
            domain="vpn.example.com",
            reality_public_key="public",
        )
        user = SimpleNamespace(
            uuid="00000000-0000-0000-0000-000000000001",
            sub_token="test-value",  # noqa: S106
        )
        with (
            mock.patch.object(health, "cfg", fake_cfg),
            mock.patch(
                "health.links.cdn_sub_url",
                return_value="https://vpn.example.com/sub/token",
            ),
        ):
            specs = health.build_specs(user)

        self.assertEqual({"vpn:main", "vpn:cdn", "sub:cdn"}, {s.key for s in specs})

    def test_relay_result_includes_each_private_node(self):
        report = {
            "ip": "94.184.47.122",
            "probe_result": {
                "checked_at": int(time.time()),
                "nodes": {
                    "FR": {"ok": True, "latency_ms": 220, "detail": "HTTP 204"},
                    "unknown": {"ok": True, "latency_ms": 1, "detail": "ignored"},
                },
            },
        }
        with (
            mock.patch("health.links.relays", return_value=[("94.184.47.122", 443)]),
            mock.patch(
                "health.nodes.all_nodes",
                return_value=[{"name": "FR", "ip": "202.133.88.44", "private": True}],
            ),
        ):
            health.ingest_relay_result(report)

        rows = healthdb.latest()
        self.assertEqual(
            ["relay:94.184.47.122:node:FR"], [row.path_key for row in rows]
        )

    def test_current_checks_ignores_removed_paths(self):
        now = int(time.time())
        healthdb.add("vpn:main", "Main", "vpn", "main", True, 100, "HTTP 204", now)
        healthdb.add("node:old", "Old node", "node", "main", False, 0, "timeout", now)

        with mock.patch(
            "health._expected_paths", return_value={"vpn:main": ("Main", "vpn")}
        ):
            rows = health.current_checks()

        self.assertEqual(["vpn:main"], [row.path_key for row in rows])


if __name__ == "__main__":
    unittest.main()
