import ast
import concurrent.futures
import os
import sys
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

RELAY = Path(__file__).resolve().parents[1] / "relay.sh"


def embedded_function(name: str):
    text = RELAY.read_text()
    marker = "cat >/usr/local/bin/isaho-agent <<'AGENT'"
    body = text.split(marker, 1)[1].split("\nAGENT\n", 1)[0].lstrip("\n")
    tree = ast.parse(body)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    namespace = {"os": os, "sys": sys, "urllib": urllib, "time": time, "concurrent": concurrent}
    exec(  # noqa: S102 -- isolate a trusted function from the repository's embedded agent
        compile(ast.Module(body=[function], type_ignores=[]), str(RELAY), "exec"),
        namespace,
    )
    return namespace[name]


class RelayProbeConfigTests(unittest.TestCase):
    def test_bounded_probe_batch_preserves_public_and_cdn_results(self):
        run_probe = embedded_function("run_probe")
        response = mock.MagicMock()
        response.__enter__.return_value = SimpleNamespace(status=200)
        opener = SimpleNamespace(open=mock.Mock(return_value=response))
        from urllib import request
        with mock.patch.object(request, "build_opener", return_value=opener):
            run_probe.__globals__["ensure_probe_xray"] = mock.Mock()
            run_probe.__globals__["probe_vless"] = lambda link: {"ok": link.endswith("good")}
            result = run_probe({"id": "batch", "sub_url": "http://localhost/sub/test",
                                "link": "vless://relay-good", "cdn_link": "vless://cdn-bad",
                                "nodes": [{"name": "FR", "link": "vless://private-good"}],
                                "paths": {"main": "vless://main-bad", "public:UK": "vless://uk-good",
                                          "cdn:UK": "vless://ukcdn-bad", "ignored": "https://not-vless"}})
        self.assertTrue(result["vpn"]["ok"])
        self.assertFalse(result["cdn"]["ok"])
        self.assertTrue(result["nodes"]["FR"]["ok"])
        self.assertTrue(result["paths"]["public:UK"]["ok"])
        self.assertFalse(result["paths"]["cdn:UK"]["ok"])
        self.assertNotIn("ignored", result["paths"])

    def test_cdn_probe_includes_complete_xhttp_tls_settings(self):
        probe_outbound = embedded_function("probe_outbound")
        outbound = probe_outbound(
            "vless://00000000-0000-0000-0000-000000000001@cdn.example.com:443?"
            "encryption=none&security=tls&sni=vpn.example.com&fp=chrome&"
            "alpn=h2%2Chttp%2F1.1&type=xhttp&host=vpn.example.com&"
            "path=%2Fhidden-path&mode=packet-up"
        )

        stream = outbound["streamSettings"]
        self.assertEqual("xhttp", stream["network"])
        self.assertEqual("vpn.example.com", stream["tlsSettings"]["serverName"])
        self.assertEqual(["h2", "http/1.1"], stream["tlsSettings"]["alpn"])
        self.assertEqual("vpn.example.com", stream["xhttpSettings"]["host"])
        self.assertEqual("/hidden-path", stream["xhttpSettings"]["path"])
        self.assertEqual("packet-up", stream["xhttpSettings"]["mode"])

    def test_reality_probe_keeps_reality_settings(self):
        probe_outbound = embedded_function("probe_outbound")
        outbound = probe_outbound(
            "vless://00000000-0000-0000-0000-000000000001@127.0.0.1:443?"
            "encryption=none&flow=xtls-rprx-vision&security=reality&"
            "sni=example.com&fp=chrome&pbk=public&sid=abcd&type=tcp"
        )

        stream = outbound["streamSettings"]
        self.assertEqual("tcp", stream["network"])
        self.assertEqual("example.com", stream["realitySettings"]["serverName"])
        self.assertEqual("public", stream["realitySettings"]["publicKey"])


if __name__ == "__main__":
    unittest.main()
