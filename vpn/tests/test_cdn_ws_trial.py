import importlib.util
from pathlib import Path
import unittest
import json
from unittest import mock

spec = importlib.util.spec_from_file_location('cdn_ws_trial', Path(__file__).resolve().parents[1] / 'cdn-ws-trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class CDNWebSocketTrialTests(unittest.TestCase):
    def test_no_customer_accounts_or_reality_keys_are_copied(self):
        conf = trial.configuration()
        self.assertEqual(1, len(conf['inbounds']))
        inbound = conf['inbounds'][0]
        self.assertEqual('127.0.0.1', inbound['listen'])
        self.assertEqual(2054, inbound['port'])
        self.assertEqual('ws', inbound['streamSettings']['network'])
        self.assertEqual('tls', inbound['streamSettings']['security'])
        self.assertNotIn('realitySettings', inbound['streamSettings'])
        self.assertEqual(1, len(inbound['settings']['clients']))
        self.assertNotEqual(inbound['settings']['clients'][0]['id'],
                            trial.configuration()['inbounds'][0]['settings']['clients'][0]['id'])

    def test_fixed_http1_and_certificate_verification_are_not_disabled(self):
        tls = trial.configuration()['inbounds'][0]['streamSettings']['tlsSettings']
        self.assertEqual(['http/1.1'], tls['alpn'])
        self.assertNotIn('allowInsecure', tls)

    def test_firewall_is_scoped_to_one_ip_and_trial_port(self):
        networks = ['192.0.%d.0/24' % i for i in range(10)]
        rules = trial.firewall('91.107.160.49', networks)
        self.assertEqual(2, rules.count('tcp dport 8444'))
        self.assertNotIn('dport 22', rules)
        self.assertNotIn('dport 443 ', rules)
        self.assertIn('ip daddr 91.107.160.49', rules)
        self.assertIn('policy accept', rules)

    def test_rejects_bad_ranges_and_ipv6(self):
        for value in ('not-a-network', '::/0', '192.0.2.0/24', '0.0.0.0/0'):
            with self.assertRaises(ValueError):
                trial.ranges(value)

    def test_units_never_restart_or_stop_production_xray(self):
        content = '\n'.join(trial.units('91.107.160.49', '/lib/systemd/systemd-socket-proxyd').values())
        self.assertNotIn('restart xray', content)
        self.assertNotIn('stop xray', content)
        self.assertNotIn('WantedBy=', content)
        self.assertIn('127.0.0.1:2054', content)
        self.assertIn('ListenStream=91.107.160.49:8444', content)

    def test_rejects_ip_and_proxy_command_injection(self):
        with self.assertRaises(ValueError):
            trial.units('192.0.2.1; reboot', '/lib/systemd/systemd-socket-proxyd')
        with self.assertRaises(ValueError):
            trial.units('192.0.2.1', '/tmp/arbitrary-command')

    def test_wildcard_private_and_loopback_bind_are_rejected(self):
        for address in ('0.0.0.0', '127.0.0.1', '10.0.0.1', '::1'):
            with self.assertRaises(ValueError):
                trial.units(address, '/lib/systemd/systemd-socket-proxyd')

    def test_uses_official_public_api_and_validates_success(self):
        values = ['192.0.%d.0/24' % i for i in range(10)]
        with mock.patch.object(trial.urllib.request, 'urlopen') as open_url:
            response = open_url.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'success': True, 'result': {'ipv4_cidrs': values}}).encode()
            self.assertEqual(values, trial.fetch_ranges())
            self.assertEqual('https://api.cloudflare.com/client/v4/ips', open_url.call_args.args[0].full_url)
            response.read.return_value = b'{"success": false}'
            with self.assertRaises(ValueError):
                trial.fetch_ranges()
