import os
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot"))
sys.path.insert(0, BOT_DIR)

import db  # noqa: E402
import shopdb  # noqa: E402
import support  # noqa: E402
import supportdb  # noqa: E402


class SupportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        shopdb.init()
        supportdb.init()

    def tearDown(self):
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def test_ticket_lifecycle_and_customer_isolation(self):
        ticket = supportdb.create_ticket(
            1001, "connect", diagnostic="all paths checked", priority="urgent"
        )
        supportdb.add_message(ticket.id, 1001, "customer", "وصل نمی‌شود")

        self.assertEqual([ticket.id], [t.id for t in supportdb.list_for_customer(1001)])
        self.assertEqual([], supportdb.list_for_customer(2002))
        self.assertEqual("وصل نمی‌شود", supportdb.messages(ticket.id)[0].text)

        ticket = supportdb.update(ticket.id, status="in_progress", assigned_to=99)
        self.assertEqual("in_progress", ticket.status)
        self.assertEqual(99, ticket.assigned_to)
        self.assertEqual(1, supportdb.counts()["in_progress"])

        supportdb.update(ticket.id, status="resolved")
        self.assertEqual([], supportdb.list_open())

    def test_diagnostic_detects_healthy_account_and_path(self):
        user = db.create_user("customer_one", 20, 30)
        db.update(user.id, tg_id=1001)
        with (
            mock.patch(
                "support.tunnels.status", return_value=[("IR1", "relay", 443, 3)]
            ),
            mock.patch("support.links.all_links", return_value=["config"]),
            mock.patch("support.devices.count", return_value=1),
        ):
            text, priority = support.diagnostic_report("connect", 1001, user.id)

        self.assertIn("حساب فعال است", text)
        self.assertIn("1 از 1 مسیر آماده", text)
        self.assertEqual("normal", priority)

    def test_diagnostic_flags_missing_tunnels_as_urgent(self):
        user = db.create_user("customer_two", 20, 30)
        db.update(user.id, tg_id=1002)
        with (
            mock.patch(
                "support.tunnels.status", return_value=[("IR1", "relay", 443, 0)]
            ),
            mock.patch("support.links.all_links", return_value=["config"]),
        ):
            text, priority = support.diagnostic_report("connect", 1002, user.id)

        self.assertIn("0 از 1 مسیر آماده", text)
        self.assertEqual("urgent", priority)


if __name__ == "__main__":
    unittest.main()
