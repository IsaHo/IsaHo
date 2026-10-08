import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

# ruff: noqa: E402

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot"))
sys.path.insert(0, BOT_DIR)

import db
import healthdb
import resilience


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        healthdb.init()

    def tearDown(self):
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    async def test_auto_recovery_only_queues_failed_relay_path(self):
        check = healthdb.add(
            "relay:94.184.47.122:vpn",
            "IR1 real path",
            "relay",
            "iran",
            False,
            0,
            "timeout",
        )
        incident = healthdb.open_incident(check)
        bot = mock.AsyncMock()
        with (
            mock.patch("resilience.mode", return_value="auto"),
            mock.patch("resilience.healthdb.failures", return_value=3),
            mock.patch("resilience.relays.queue") as queue,
            mock.patch("resilience.db.admin_ids", return_value=[]),
        ):
            await resilience.auto_recover(bot, check, incident)

        queue.assert_called_once_with("94.184.47.122", "restart")
        self.assertIn("ریستارت", healthdb.incidents(active_only=True)[0].action)

    async def test_auto_recovery_does_not_restart_for_single_node(self):
        check = healthdb.add(
            "relay:94.184.47.122:node:FR",
            "IR1 to FR",
            "node",
            "iran",
            False,
            0,
            "timeout",
        )
        incident = healthdb.open_incident(check)
        with (
            mock.patch("resilience.mode", return_value="auto"),
            mock.patch("resilience.relays.queue") as queue,
        ):
            await resilience.auto_recover(mock.AsyncMock(), check, incident)
        queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
