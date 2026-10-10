import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'bot')))
import resilience
import xray


class ResilienceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_all_reports_restart_failure(self):
        with mock.patch.object(xray, 'write_config', new_callable=mock.AsyncMock), mock.patch.object(xray, 'restart', new_callable=mock.AsyncMock, return_value=False):
            self.assertFalse(await xray.apply_all())

    async def test_failed_rotation_rolls_back_and_acknowledges_immediately(self):
        await self.rotation_case([False, True], 'مقدار قبلی برگشت')

    async def test_failed_rollback_does_not_claim_success(self):
        await self.rotation_case([False, False], 'نیازمند بررسی')

    async def rotation_case(self, results, expected):
        cb = SimpleNamespace(data='rs:sid:go:0', answer=mock.AsyncMock(),
                             message=SimpleNamespace(edit_text=mock.AsyncMock()))
        async def apply():
            cb.answer.assert_awaited_once()
            return results.pop(0)
        with mock.patch.object(resilience.identity, 'short_ids', return_value=['aabb', 'ccdd']), mock.patch.object(resilience.identity, 'rotate'), mock.patch.object(resilience.identity, 'set_short_ids') as restore, mock.patch.object(resilience.xray, 'apply_all', side_effect=apply), mock.patch.object(resilience, 'cohort_panel', return_value=('panel', None)), self.assertLogs('resilience', level='ERROR'):
            await resilience.cohort_rotate(cb)
        restore.assert_called_once_with(['aabb', 'ccdd'])
        self.assertIn(expected, cb.message.edit_text.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
