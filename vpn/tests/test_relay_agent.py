import ast
import os
import sys
import unittest
import urllib.parse
from pathlib import Path

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
    namespace = {"os": os, "sys": sys, "urllib": urllib}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(RELAY), "exec"),
        namespace,
    )
    return namespace[name]


class RelayProbeConfigTests(unittest.TestCase):
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
