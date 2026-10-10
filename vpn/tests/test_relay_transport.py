import importlib.util
import json
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_relay_agent import embedded_function

path = Path(__file__).resolve().parents[1] / 'relay-wireguard.py'
spec = importlib.util.spec_from_file_location('relay_transport', path)
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)


class ManagedTransportTests(unittest.TestCase):
    def config(self, mode='backhaul'):
        return {'TRANSPORT': mode, 'PUBLIC_IP': '87.107.150.158',
                'BH_SERVICES': 'isaho-backhaul-de2,isaho-backhaul-control',
                'BH_DATA_ENDPOINTS': '127.0.0.1:13001', 'BH_CONTROL_ENDPOINT': '127.0.0.1:13002',
                'WG_INTERFACE': 'wg-isaho', 'WG_DATA_ENDPOINTS': '10.77.10.1:10443',
                'WG_CONTROL_ENDPOINT': '10.77.20.1:2097'}

    def test_updates_preserve_selected_transport(self):
        fn = embedded_function('update_command')
        fn.__globals__.update(re=re, shlex=shlex)
        for mode in ('wireguard', 'backhaul'):
            command = fn(self.config(mode), 'a' * 40)
            self.assertIn('bash -s ' + mode, command)
            self.assertNotIn('bash -s ssh', command)
            self.assertIn('pipefail', command)
        with self.assertRaises(ValueError):
            fn(self.config(), 'main; touch /tmp/injected')

    def test_backhaul_cannot_forward_to_public_or_private_network(self):
        for address in ('91.107.160.49:443', '10.77.10.1:443'):
            config = self.config();config['BH_DATA_ENDPOINTS'] = address
            with self.assertRaises(ValueError):transport.settings(config)

    def test_wireguard_cannot_forward_to_loopback_or_public_network(self):
        for address in ('127.0.0.1:443', '91.107.160.49:443'):
            config = self.config('wireguard');config['WG_DATA_ENDPOINTS'] = address
            with self.assertRaises(ValueError):transport.settings(config)

    def test_transport_names_cannot_inject_systemctl_arguments(self):
        config = self.config();config['BH_SERVICES'] = '--all'
        with self.assertRaises(ValueError):transport.settings(config)

    def test_node_data_never_receives_primary_proxy_protocol(self):
        for mode in ('wireguard', 'backhaul'):
            output = transport.render(self.config(mode))
            self.assertNotIn('send-proxy', output)
            # The subscription application intentionally has no public root handler.
            self.assertIn('http-check expect status 404', output)
            self.assertNotIn('shutdown-sessions', output)

    def test_private_routes_checked_before_cutover(self):
        result = subprocess.CompletedProcess([], 0, json.dumps([{'dev': 'eth0'}]), '')
        with mock.patch.object(transport.subprocess, 'run', return_value=result), mock.patch.object(transport.socket, 'create_connection') as connect:
            with self.assertRaises(ValueError):transport.check_routes(self.config('wireguard'))
            connect.assert_not_called()

    def test_missing_handshake_never_reports_healthy(self):
        result = subprocess.CompletedProcess([], 0, 'public-key\t0\n', '')
        with mock.patch.object(transport.subprocess, 'run', return_value=result), mock.patch.object(transport.socket, 'create_connection'), mock.patch.object(transport.http.client, 'HTTPConnection'):
            self.assertFalse(transport.status(self.config('wireguard'))['transport_healthy'])

    def test_accepting_socket_is_not_a_healthy_control_path(self):
        result = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(transport.subprocess, 'run', return_value=result), mock.patch.object(transport.socket, 'create_connection'), mock.patch.object(transport.http.client, 'HTTPConnection', side_effect=OSError):
            state = transport.status(self.config())
            self.assertFalse(state['control_up'])
            self.assertFalse(state['transport_healthy'])

    def test_version_update_preserves_peer_identity_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'relay.conf'
            config.write_text('TRANSPORT=backhaul\nBH_DATA_ENDPOINTS=127.0.0.1:13001\nVERSION=old\n')
            transport.set_version(config, 'a' * 40)
            self.assertIn('BH_DATA_ENDPOINTS=127.0.0.1:13001', config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':unittest.main()
