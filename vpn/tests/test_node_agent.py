import ast
import ipaddress
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

NODE = Path(__file__).resolve().parents[1] / "node.sh"
BODY = NODE.read_text().split("cat >/usr/local/bin/isaho-node <<'AGENT'\n", 1)[1].split("\nAGENT\n", 1)[0]


class NodeAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.config = {"inbounds": [{"tag": "reality", "listen": "127.0.0.1", "port": 443}]}
        self.text = json.dumps(self.config, indent=2, sort_keys=True)
        self.path.write_text(self.text)
        self.calls = []
        self.state = "active"
        self.validate_code = 0
        self.restart_code = 0
        self.start_codes = [0]
        self.reset_code = 0
        self.ss_code = 0
        self.owners = ""
        self.now = 1000
        self.ns = {"os": os, "json": json, "re": re, "ipaddress": ipaddress, "sys": sys,
                   "time": SimpleNamespace(time=lambda: self.now), "CFG": str(self.path),
                   "RECOVERY_STAMP": str(Path(self.tmp.name) / "recovery-at"), "XRAY": "xray",
                   "run": self.run_command, "stats": mock.Mock(return_value={}), "pending": {}}
        funcs = [n for n in ast.parse(BODY).body if isinstance(n, ast.FunctionDef)
                 and n.name in {"merge", "port_preflight", "apply_config", "recover_failed"}]
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(NODE), "exec"), self.ns)

    def tearDown(self):
        self.tmp.cleanup()

    def run_command(self, *args):
        self.calls.append(args)
        code, output = 0, ""
        if args[0] == "xray":
            code = self.validate_code
        elif args[:2] == ("systemctl", "show"):
            output = "123"
        elif args[:2] == ("systemctl", "is-active"):
            output = self.state
        elif args[:2] == ("systemctl", "start"):
            code = self.start_codes.pop(0)
            if code == 0:
                self.state = "active"
        elif args[:2] == ("systemctl", "restart"):
            code = self.restart_code
        elif args[:2] == ("systemctl", "reset-failed"):
            code = self.reset_code
        elif args[0] == "ss":
            code, output = self.ss_code, self.owners
        return SimpleNamespace(returncode=code, stdout=output, stderr="withheld")

    def count(self, command):
        return sum(call[:len(command)] == command for call in self.calls)

    def apply_public(self):
        config = {"inbounds": [{"listen": "0.0.0.0", "port": 443}]}
        return self.ns["apply_config"](config, json.dumps(config), self.text)

    def test_unchanged_failed_validates_then_starts(self):
        self.state = "failed"
        self.assertEqual("in_sync", self.ns["apply_config"](self.config, self.text, self.text)["state"])
        result = self.ns["recover_failed"]()
        self.assertEqual("started", result["state"])
        self.assertEqual(1, self.count(("xray", "run", "-test")))
        self.assertEqual(1, self.count(("systemctl", "start")))
        self.assertEqual(0o600, os.stat(self.ns["RECOVERY_STAMP"]).st_mode & 0o777)

    def test_active_inactive_activating_never_recovered(self):
        for state in ("active", "inactive", "activating", "deactivating", "unknown"):
            with self.subTest(state=state):
                self.state = state
                self.assertEqual("not_needed", self.ns["recover_failed"]()["state"])
        self.assertEqual(0, self.count(("systemctl", "start")))
        self.assertEqual(0, self.count(("xray",)))

    def test_backoff_blocks_second_attempt(self):
        self.state = "failed"
        self.ns["recover_failed"]()
        self.state = "failed"
        self.now += 299
        self.assertEqual("backoff", self.ns["recover_failed"]()["state"])
        self.assertEqual(1, self.count(("systemctl", "start")))

    def test_after_backoff_can_retry(self):
        self.state = "failed"
        self.ns["recover_failed"]()
        self.state = "failed"
        self.now += 300
        self.start_codes = [0]
        self.assertEqual("started", self.ns["recover_failed"]()["state"])
        self.assertEqual(2, self.count(("systemctl", "start")))

    def test_start_limit_one_reset_one_retry(self):
        self.state = "failed"
        self.start_codes = [1, 0]
        result = self.ns["recover_failed"]()
        self.assertEqual({"state": "started", "start_code": 1, "reset_code": 0, "retry_code": 0}, result)
        self.assertEqual(2, self.count(("systemctl", "start")))
        self.assertEqual(1, self.count(("systemctl", "reset-failed")))

    def test_failed_retry_reports_failure_and_is_bounded(self):
        self.state = "failed"
        self.start_codes = [1, 2]
        self.assertEqual(2, self.ns["recover_failed"]()["retry_code"])
        self.assertEqual("backoff", self.ns["recover_failed"]()["state"])
        self.assertEqual(2, self.count(("systemctl", "start")))

    def test_reset_failure_does_not_retry(self):
        self.state = "failed"
        self.start_codes = [1]
        self.reset_code = 1
        self.assertEqual(1, self.ns["recover_failed"]()["reset_code"])
        self.assertEqual(1, self.count(("systemctl", "start")))

    def test_invalid_current_config_blocks_recovery(self):
        self.state, self.validate_code = "failed", 1
        self.assertEqual("blocked", self.ns["recover_failed"]()["state"])
        self.assertEqual(0, self.count(("systemctl", "start")))

    def test_public_config_foreign_owner_keeps_old_without_restart(self):
        self.owners = 'LISTEN 0 4096 192.0.2.2:443 *:* users:(("systemd",pid=1,fd=7))'
        self.assertEqual("rejected", self.apply_public()["state"])
        self.assertEqual(self.text, self.path.read_text())
        self.assertEqual(0, self.count(("systemctl", "restart")))
        self.ns["stats"].assert_not_called()
        self.assertFalse(self.path.with_name("config.new.json").exists())

    def test_foreign_python_is_not_allowed(self):
        self.owners = 'LISTEN 0 128 *:443 *:* users:(("python",pid=999,fd=7))'
        self.assertEqual("rejected", self.apply_public()["state"])

    def test_private_reality_can_coexist_with_public_cdn_socket(self):
        self.owners = 'LISTEN 0 128 192.0.2.2:443 *:* users:(("systemd",pid=1,fd=7))'
        self.assertEqual("", self.ns["port_preflight"](self.config))

    def test_private_bind_also_rejects_overlapping_foreign_owner(self):
        self.owners = 'LISTEN 0 128 127.0.0.1:443 *:* users:(("python",pid=999,fd=7))'
        self.assertIn("foreign", self.ns["port_preflight"](self.config))

    def test_ipv6_wildcard_is_conservatively_checked(self):
        self.owners = 'LISTEN 0 128 [::]:443 *:* users:(("systemd",pid=1,fd=7))'
        self.assertEqual("rejected", self.apply_public()["state"])

    def test_multiple_owners_must_all_match_service_pid(self):
        self.owners = 'LISTEN 0 128 *:443 *:* users:(("xray",pid=123,fd=7),("python",pid=999,fd=8))'
        self.assertEqual("rejected", self.apply_public()["state"])

    def test_own_xray_pid_can_be_replaced(self):
        self.owners = 'LISTEN 0 128 *:443 *:* users:(("xray",pid=123,fd=7))'
        self.assertEqual("applied", self.apply_public()["state"])
        self.assertEqual(1, self.count(("systemctl", "restart")))

    def test_missing_owner_or_ss_failure_rejected(self):
        self.owners = 'LISTEN 0 128 *:443 *:*'
        self.assertEqual("rejected", self.apply_public()["state"])
        self.ss_code, self.owners = 1, ""
        self.assertEqual("rejected", self.apply_public()["state"])

    def test_failed_config_validation_never_replaces(self):
        self.validate_code = 1
        self.assertEqual("rejected", self.apply_public()["state"])
        self.assertEqual(self.text, self.path.read_text())
        self.assertEqual(0, self.count(("systemctl", "restart")))

    def test_failed_restart_is_reported(self):
        self.restart_code = 1
        result = self.apply_public()
        self.assertEqual("restart_failed", result["state"])
        self.assertEqual(1, result["restart_code"])

    def test_recovery_preflight_blocks_existing_public_collision(self):
        self.path.write_text(json.dumps({"inbounds": [{"listen": "0.0.0.0", "port": 443}]}))
        self.state = "failed"
        self.owners = 'LISTEN 0 128 *:443 *:* users:(("systemd",pid=1,fd=7))'
        self.assertEqual("blocked", self.ns["recover_failed"]()["state"])
        self.assertEqual(0, self.count(("systemctl", "start")))

    def test_report_contains_apply_and_recovery_outcomes(self):
        assign = next(n for n in ast.parse(BODY).body if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "info")
        self.ns.update(socket=SimpleNamespace(gethostname=lambda: "node"),
                       stamp=str(Path(self.tmp.name) / "no-standby"),
                       config_apply={"state": "rejected"}, recovery={"state": "failed", "retry_code": 1},
                       reality_public=False)
        exec(compile(ast.Module(body=[assign], type_ignores=[]), str(NODE), "exec"), self.ns)
        self.assertEqual("rejected", self.ns["info"]["config_apply"]["state"])
        self.assertEqual(1, self.ns["info"]["recovery"]["retry_code"])

    def test_local_recovery_precedes_main_request(self):
        self.assertLess(BODY.index("recovery = recover_failed()"), BODY.index('new = request("GET", "/node/config")'))


if __name__ == "__main__":
    unittest.main()
