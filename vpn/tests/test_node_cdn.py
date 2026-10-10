import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

# ruff: noqa: E402
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import db
import health
import healthdb
import links
import nodes
import routing


class PrivateNodeCDNTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        healthdb.init()
        db.set_setting("relays", "192.0.2.1:443")
        db.set_setting("link_types", "node")
        db.set_setting("cdn_public_port", "2053")
        self.node = {"name": "DE2", "ip": "192.0.2.2", "private": True,
                     "domain": "vpn.example.test", "cdn_enabled": True, "cdn_port": 443}
        nodes.save([self.node])
        self.user = SimpleNamespace(uuid="00000000-0000-0000-0000-000000000001", name="probe")
        self.cfg_patch = mock.patch.object(links, "cfg", replace(links.cfg, reality_public_key="public"))
        self.cfg_patch.start()

    def tearDown(self):
        self.cfg_patch.stop()
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def test_explicit_private_cdn_publishes_tls_link_without_public_reality(self):
        entries = links.route_entries(self.user)
        self.assertEqual(["cdn:DE2"], [key for key, _ in entries])
        url = urlsplit(entries[0][1])
        self.assertEqual("vpn.example.test", url.hostname)
        self.assertEqual(443, url.port)
        query = parse_qs(url.query)
        self.assertEqual(["tls"], query["security"])
        self.assertEqual(["xhttp"], query["type"])
        self.assertEqual(["vpn.example.test"], query["sni"])
        self.assertNotIn("pbk", query)
        self.assertEqual([], nodes.public_nodes())

    def test_private_nodes_require_explicit_boolean_and_domain(self):
        for update in ({"cdn_enabled": False}, {"cdn_enabled": "true"}, {"domain": ""}):
            nodes.save([{**self.node, **update}])
            self.assertEqual([], links.route_entries(self.user))
            self.assertNotIn("cdn:DE2", health.public_probe_links(self.user))
        node = {key: value for key, value in self.node.items() if key != "cdn_enabled"}
        nodes.save([node])
        self.assertEqual([], nodes.cdn_nodes())

    def test_public_node_defaults_remain_compatible(self):
        nodes.save([{"name": "UK", "ip": "192.0.2.3", "private": False,
                     "domain": "uk.example.test"}])
        entries = links.route_entries(self.user)
        self.assertEqual(["node:UK", "cdn:UK"], [key for key, _ in entries])
        self.assertEqual(2053, urlsplit(entries[1][1]).port)
        self.assertIn("public:UK", health.public_probe_links(self.user))
        self.assertIn("cdn:UK", routing.catalog())

    def test_cdn_setting_does_not_expose_private_xray_inbounds(self):
        config = {"inbounds": [{"tag": "reality", "listen": "0.0.0.0"},
                               {"tag": "cdn", "listen": "0.0.0.0"}]}
        with mock.patch("nodes.xray.build_config", return_value=config):
            result = nodes.config_for(self.node)
        self.assertTrue(all(inbound["listen"] == "127.0.0.1" for inbound in result["inbounds"]))

    def test_private_cdn_is_probed_and_routed_using_iran_results(self):
        probes = health.public_probe_links(self.user)
        self.assertEqual(443, urlsplit(probes["cdn:DE2"]).port)
        self.assertNotIn("public:DE2", probes)
        expected = health._expected_paths()
        self.assertIn("relay:192.0.2.1:cdn:DE2", expected)
        self.assertNotIn("relay:192.0.2.1:public:DE2", expected)
        self.assertIn("cdn:DE2", routing.catalog())
        self.assertNotIn("node:DE2", routing.catalog())

        now = int(time.time())
        for offset in (-120, -60, 0):
            health.ingest_relay_result({"ip": "192.0.2.1", "probe_result": {
                "checked_at": now + offset, "paths": {
                    "cdn:DE2": {"ok": False, "latency_ms": 5000},
                    "public:DE2": {"ok": True, "latency_ms": 1},
                }}})
        self.assertEqual("down", routing.states()["cdn:DE2"]["state"])
        self.assertEqual([], healthdb.history("relay:192.0.2.1:public:DE2"))
        db.set_setting("routing_mode", "hide")
        entries = links.route_entries(self.user) + [("relay:192.0.2.1", "fallback")]
        self.assertEqual([("relay:192.0.2.1", "fallback")], routing.choose(entries))

    def test_invalid_node_port_falls_back_to_current_global_port(self):
        for value in ("invalid", -1, 65536, None):
            self.assertEqual(2053, links.node_cdn_port({"cdn_port": value}))


if __name__ == "__main__":
    unittest.main()
