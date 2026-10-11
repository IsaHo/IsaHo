import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


class TunnelNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setup_check(self, available=False, online=True):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'bot/main.py').read_text())
        function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'check_tunnels')
        report = {'transport': 'backhaul', 'seen': 1, 'transport_available': available}
        namespace = {'Bot': object, 'tunnels': SimpleNamespace(status=lambda: [('IR1', 'host', 443, int(bool(available)))]),
                     'relays': SimpleNamespace(reports={'host': report}, online=lambda host: online),
                     '_tunnel_seen': {'host': True}, '_tunnel_down': {}, '_tunnel_report_seen': {},
                     '_tunnel_unconfirmed': set(), 'DOWN_AFTER': 2,
                     'notify_admins': AsyncMock(), 'notify_status': AsyncMock()}
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<check>', 'exec'), namespace)
        return namespace, report

    async def test_one_bad_report_cannot_be_counted_twice(self):
        ns, report = self.setup_check()
        await ns['check_tunnels'](None);await ns['check_tunnels'](None)
        ns['notify_admins'].assert_not_awaited()
        report['seen'] = 2;await ns['check_tunnels'](None)
        ns['notify_admins'].assert_awaited_once()

    async def test_unknown_monitoring_never_announces_customer_outage(self):
        for online, available in [(False, False), (True, None)]:
            ns, _ = self.setup_check(available, online)
            await ns['check_tunnels'](None);await ns['check_tunnels'](None)
            ns['notify_admins'].assert_awaited_once();ns['notify_status'].assert_not_awaited()

    async def test_available_partial_path_does_not_trigger_outage(self):
        ns, _ = self.setup_check(True)
        await ns['check_tunnels'](None)
        self.assertEqual(ns['_tunnel_down']['host'], 0)
        ns['notify_admins'].assert_not_awaited()
