import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock

# ruff: noqa: E402
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import db
import pathdb
import xray


class PathUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        pathdb.init()

    def tearDown(self):
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def row(self, key):
        return next(r for r in pathdb.rows() if r["path_key"] == key)

    def age(self, key, seconds):
        with db.connect() as c:
            c.execute("UPDATE path_usage SET rotated_at=? WHERE path_key=?",
                      (int(time.time()) - seconds, key))

    # --- accounting -----------------------------------------------------------------

    def test_samples_accumulate(self):
        pathdb.add_usage({"reality": 100})
        pathdb.add_usage({"reality": 50})
        self.assertEqual(150, self.row("reality")["bytes"])
        self.assertEqual(150, self.row("reality")["total_bytes"])

    def test_zero_and_negative_samples_are_ignored(self):
        pathdb.add_usage({"reality": 0, "cdn": -5, "": 10})
        self.assertEqual([], pathdb.rows())

    def test_paths_are_tracked_separately(self):
        pathdb.add_usage({"reality": 10, "cdn": 20})
        self.assertEqual(10, self.row("reality")["bytes"])
        self.assertEqual(20, self.row("cdn")["bytes"])

    # --- thresholds -----------------------------------------------------------------

    def test_nothing_is_due_below_both_thresholds(self):
        pathdb.add_usage({"reality": 1024})
        self.assertEqual([], pathdb.due())

    def test_volume_threshold_marks_a_path_due(self):
        db.set_setting("rotate_gb", "1")
        pathdb.add_usage({"reality": pathdb.GB})
        row = self.row("reality")
        self.assertTrue(row["due"])
        self.assertTrue(row["by_bytes"])
        self.assertFalse(row["by_age"])

    def test_age_threshold_marks_a_path_due_on_its_own(self):
        db.set_setting("rotate_days", "4")
        pathdb.add_usage({"reality": 10})
        self.age("reality", 5 * db.DAY)
        row = self.row("reality")
        self.assertTrue(row["due"])
        self.assertTrue(row["by_age"])
        self.assertFalse(row["by_bytes"])

    def test_thresholds_come_from_settings(self):
        db.set_setting("rotate_gb", "2")
        db.set_setting("rotate_days", "9")
        self.assertEqual(2 * pathdb.GB, pathdb.rotate_bytes())
        self.assertEqual(9 * db.DAY, pathdb.rotate_age())

    def test_unparsable_settings_fall_back_to_defaults(self):
        db.set_setting("rotate_gb", "soon")
        db.set_setting("rotate_days", "")
        self.assertEqual(pathdb.DEFAULT_ROTATE_GB * pathdb.GB, pathdb.rotate_bytes())
        self.assertEqual(pathdb.DEFAULT_ROTATE_DAYS * db.DAY, pathdb.rotate_age())

    # --- rotation -------------------------------------------------------------------

    def test_rotation_resets_the_baseline_but_keeps_the_lifetime_total(self):
        db.set_setting("rotate_gb", "1")
        pathdb.add_usage({"reality": 2 * pathdb.GB})
        pathdb.rotate("reality")
        row = self.row("reality")
        self.assertEqual(0, row["bytes"])
        self.assertEqual(2 * pathdb.GB, row["total_bytes"])
        self.assertFalse(row["due"])

    def test_rotation_clears_the_age_trigger(self):
        pathdb.add_usage({"reality": 10})
        self.age("reality", 30 * db.DAY)
        self.assertTrue(self.row("reality")["due"])
        pathdb.rotate("reality")
        self.assertFalse(self.row("reality")["due"])

    def test_forget_drops_a_path_that_no_longer_exists(self):
        pathdb.add_usage({"node:old": 10})
        pathdb.forget("node:old")
        self.assertEqual([], pathdb.rows())

    # --- alert rate limiting --------------------------------------------------------

    def test_a_path_warns_once_per_window(self):
        pathdb.add_usage({"reality": 10})
        self.assertTrue(pathdb.should_warn("reality"))
        self.assertFalse(pathdb.should_warn("reality"))

    def test_rotation_re_arms_the_warning(self):
        pathdb.add_usage({"reality": 10})
        pathdb.should_warn("reality")
        pathdb.rotate("reality")
        self.assertTrue(pathdb.should_warn("reality"))

    def test_warning_one_path_does_not_silence_another(self):
        pathdb.add_usage({"reality": 10, "cdn": 10})
        self.assertTrue(pathdb.should_warn("reality"))
        self.assertTrue(pathdb.should_warn("cdn"))


class BurnedAddressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cfg = db.cfg
        db.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        pathdb.init()

    def tearDown(self):
        db.cfg = self.old_cfg
        self.tmp.cleanup()

    def test_a_burned_address_is_parked_until_its_cooldown_passes(self):
        db.set_setting("burn_cooldown_days", "7")
        pathdb.burn("203.0.113.5", kind="reality", note="swap")
        parked = pathdb.parked()
        self.assertEqual(1, len(parked))
        self.assertFalse(parked[0]["reusable"])
        self.assertEqual([], pathdb.reusable())

    def test_cooldown_expiry_marks_it_reusable(self):
        pathdb.burn("203.0.113.5")
        with db.connect() as c:
            c.execute("UPDATE burned_addresses SET cooldown_until=?", (int(time.time()) - 1,))
        self.assertTrue(pathdb.parked()[0]["reusable"])
        self.assertEqual(1, len(pathdb.reusable()))

    def test_re_burning_restarts_the_cooldown(self):
        pathdb.burn("203.0.113.5")
        with db.connect() as c:
            c.execute("UPDATE burned_addresses SET cooldown_until=?", (int(time.time()) - 1,))
        pathdb.burn("203.0.113.5")
        self.assertFalse(pathdb.parked()[0]["reusable"])
        self.assertEqual(1, len(pathdb.parked()))

    def test_release_removes_an_address(self):
        pathdb.burn("203.0.113.5")
        pathdb.release("203.0.113.5")
        self.assertEqual([], pathdb.parked())

    def test_an_empty_address_is_refused(self):
        with self.assertRaises(ValueError):
            pathdb.burn("   ")

    def test_parked_addresses_are_ordered_by_when_they_free_up(self):
        pathdb.burn("198.51.100.1")
        pathdb.burn("203.0.113.5")
        with db.connect() as c:
            c.execute("UPDATE burned_addresses SET cooldown_until=? WHERE address=?",
                      (int(time.time()) + 10, "203.0.113.5"))
        self.assertEqual("203.0.113.5", pathdb.parked()[0]["address"])

    def test_zero_cooldown_frees_an_address_immediately(self):
        db.set_setting("burn_cooldown_days", "0")
        pathdb.burn("203.0.113.5")
        self.assertTrue(pathdb.parked()[0]["reusable"])


class InboundStatsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_cfg, self.old_xray_cfg = db.cfg, xray.cfg
        db.cfg = xray.cfg = replace(db.cfg, data_dir=self.tmp.name)
        db.init()
        pathdb.init()

    def tearDown(self):
        db.cfg, xray.cfg = self.old_db_cfg, self.old_xray_cfg
        self.tmp.cleanup()

    @staticmethod
    def payload(entries):
        import json
        return json.dumps({"stat": [{"name": name, "value": value} for name, value in entries]})

    async def collect(self, entries, code=0):
        out = self.payload(entries)
        with mock.patch("xray._run", new_callable=mock.AsyncMock, return_value=(code, out, "")):
            return await xray.collect_inbound_stats()

    async def test_uplink_and_downlink_are_summed_per_inbound(self):
        result = await self.collect([
            ("inbound>>>reality>>>traffic>>>uplink", 10),
            ("inbound>>>reality>>>traffic>>>downlink", 90),
            ("inbound>>>cdn>>>traffic>>>uplink", 5),
        ])
        self.assertEqual({"reality": 100, "cdn": 5}, result)

    async def test_non_customer_inbounds_are_not_counted(self):
        """The API inbound is bot traffic and must not drive endpoint rotation."""
        result = await self.collect([
            ("inbound>>>api>>>traffic>>>uplink", 1000),
            ("inbound>>>reality>>>traffic>>>uplink", 7),
        ])
        self.assertEqual({"reality": 7}, result)

    async def test_malformed_names_are_skipped(self):
        result = await self.collect([
            ("garbage", 1),
            ("inbound>>>reality>>>traffic>>>uplink", 3),
        ])
        self.assertEqual({"reality": 3}, result)

    async def test_a_failed_query_yields_nothing(self):
        self.assertEqual({}, await self.collect([("inbound>>>reality>>>traffic>>>uplink", 3)], code=1))

    async def test_invalid_json_yields_nothing(self):
        with mock.patch("xray._run", new_callable=mock.AsyncMock, return_value=(0, "{", "")):
            self.assertEqual({}, await xray.collect_inbound_stats())

    async def test_flush_records_path_usage(self):
        with mock.patch("xray.collect_stats", new_callable=mock.AsyncMock, return_value={}), \
                mock.patch("xray.collect_inbound_stats", new_callable=mock.AsyncMock,
                           return_value={"reality": 42}):
            await xray.flush_stats()
        self.assertEqual(42, pathdb.rows()[0]["bytes"])

    async def test_accounting_failure_never_breaks_the_stats_flush(self):
        """Per-path accounting is bookkeeping; it must not take the data plane down."""
        with mock.patch("xray.collect_stats", new_callable=mock.AsyncMock, return_value={}), \
                mock.patch("xray.collect_inbound_stats", new_callable=mock.AsyncMock,
                           side_effect=RuntimeError("boom")):
            await xray.flush_stats()  # must not raise
        self.assertEqual([], pathdb.rows())


if __name__ == "__main__":
    unittest.main()
