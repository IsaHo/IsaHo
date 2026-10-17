import hashlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bot')))
import tcpanel


class PanelBridgeTests(unittest.TestCase):
    def setUp(self):
        self.config = {'url': 'https://panel.example:2053/private/', 'token': 'PRIVATE_TOKEN',
                       'sha256': hashlib.sha256(b'certificate').hexdigest()}
        self.connection = mock.Mock()
        self.connection.sock.getpeercert.return_value = b'certificate'
        response = self.connection.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({'success': True, 'obj': {
            'xray': {'state': 'running'}, 'uptime': 7200, 'private': 'must not escape'}}).encode()

    def test_status_exposes_only_safe_fields_and_uses_get(self):
        with mock.patch('tcpanel.http.client.HTTPSConnection', return_value=self.connection):
            status = tcpanel.read_status(self.config)
        self.assertEqual(status, {'xray_running': True, 'uptime': 7200, 'monitor_only': True})
        self.assertEqual(self.connection.request.call_args.args, ('GET', '/private/panel/api/server/status'))
        self.connection.close.assert_called_once()

    def test_pin_mismatch_never_sends_token(self):
        self.connection.sock.getpeercert.return_value = b'wrong certificate'
        with mock.patch('tcpanel.http.client.HTTPSConnection', return_value=self.connection):
            with self.assertRaises(tcpanel.PanelUnavailable):
                tcpanel.read_status(self.config)
        self.connection.request.assert_not_called()
        self.connection.close.assert_called_once()

    def test_redirect_and_error_are_not_followed(self):
        for status in (302, 401, 403, 500):
            self.connection.getresponse.return_value.status = status
            with mock.patch('tcpanel.http.client.HTTPSConnection', return_value=self.connection):
                with self.assertRaises(tcpanel.PanelUnavailable):
                    tcpanel.read_status(self.config)

    def test_remote_error_body_is_not_exposed(self):
        self.connection.getresponse.return_value.read.return_value = b'PRIVATE_TOKEN invalid JSON'
        with mock.patch('tcpanel.http.client.HTTPSConnection', return_value=self.connection):
            with self.assertRaises(tcpanel.PanelUnavailable) as error:
                tcpanel.read_status(self.config)
        self.assertNotIn('PRIVATE_TOKEN', str(error.exception))

    def test_rejects_oversized_response(self):
        self.connection.getresponse.return_value.read.return_value = b' ' * (1024 * 1024 + 1)
        with mock.patch('tcpanel.http.client.HTTPSConnection', return_value=self.connection):
            with self.assertRaises(tcpanel.PanelUnavailable):
                tcpanel.read_status(self.config)

    def test_rejects_world_readable_config(self):
        with mock.patch('tcpanel.Path') as path:
            path.return_value.stat.return_value.st_mode = 0o100644
            with self.assertRaises(tcpanel.PanelUnavailable):
                tcpanel.load_config()
            path.return_value.read_text.assert_not_called()

    def test_load_requires_https_and_exact_pin(self):
        for update in ({'url': 'http://panel.example/'}, {'sha256': 'bad'},
                       {'url': 'https://user:password@panel.example/'}, {'token': ''}):
            with mock.patch('tcpanel.Path') as path:
                path.return_value.stat.return_value.st_mode = 0o100600
                path.return_value.read_text.return_value = json.dumps({**self.config, **update})
                with self.assertRaises(tcpanel.PanelUnavailable):
                    tcpanel.load_config()


class PanelCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_cannot_query_panel(self):
        cb = mock.Mock()
        cb.from_user.id = 7
        cb.answer = mock.AsyncMock()
        with mock.patch('db.admin_ids', return_value=[1]), mock.patch('tcpanel.load_config') as load:
            await tcpanel.status_callback(cb)
        load.assert_not_called()
        cb.answer.assert_awaited_once()

    async def test_admin_acknowledged_before_network(self):
        cb = mock.Mock()
        cb.from_user.id = 1
        cb.answer = mock.AsyncMock()
        cb.message.answer = mock.AsyncMock()
        async def check(*args):
            cb.answer.assert_awaited_once()
            return {'xray_running': True, 'uptime': 42}
        with (mock.patch('db.admin_ids', return_value=[1]),
              mock.patch('tcpanel.load_config', return_value={}),
              mock.patch('tcpanel.asyncio.to_thread', side_effect=check)):
            await tcpanel.status_callback(cb)
        cb.message.answer.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
