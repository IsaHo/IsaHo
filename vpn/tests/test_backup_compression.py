import asyncio
import gzip
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bot')))
import nodes
import sub_server


class BackupCompressionTests(unittest.TestCase):
    def test_gzip_roundtrip_and_legacy_json(self):
        bundle = {'db': 'A' * 20000, 'env': 'test', 'at': 123}
        with mock.patch.object(nodes, 'by_key', return_value={'name': 'test'}), \
             mock.patch.object(nodes, 'standby_bundle', return_value=bundle):
            for header in ('', 'gzip'):
                response = asyncio.run(sub_server._node_backup(
                    SimpleNamespace(query={'key': 'fake'}, headers={'Accept-Encoding': header})))
                if header:
                    self.assertEqual('gzip', response.headers['Content-Encoding'])
                    self.assertLess(len(response.body), 200)
                    body = gzip.decompress(response.body)
                else:
                    self.assertNotIn('Content-Encoding', response.headers)
                    body = response.body
                self.assertEqual(bundle, json.loads(body))

    def test_unauthorized_cannot_get_backup(self):
        from aiohttp import web
        with mock.patch.object(nodes, 'by_key', return_value=None), \
             mock.patch.object(nodes, 'standby_bundle') as bundle:
            with self.assertRaises(web.HTTPNotFound):
                asyncio.run(sub_server._node_backup(SimpleNamespace(query={}, headers={})))
            bundle.assert_not_called()
