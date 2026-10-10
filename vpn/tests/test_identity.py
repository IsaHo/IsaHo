import os
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace

# ruff: noqa: E402
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import db
import identity
import links
import xray


def user(uid, name="u"):
    return SimpleNamespace(id=uid, name=name, uuid=f"00000000-0000-0000-0000-{uid:012d}")


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_cfg, self.old_links_cfg, self.old_xray_cfg = db.cfg, links.cfg, xray.cfg
        self.old_identity_cfg = identity.cfg
        installed = replace(db.cfg, data_dir=self.tmp.name, reality_short_id="aabbccdd",
                            reality_public_key="pub", reality_sni="example.com",
                            server_ip="203.0.113.5", reality_port=443, brand="T")
        db.cfg = links.cfg = xray.cfg = identity.cfg = installed
        db.init()
        identity.init()

    def tearDown(self):
        db.cfg, links.cfg = self.old_db_cfg, self.old_links_cfg
        xray.cfg, identity.cfg = self.old_xray_cfg, self.old_identity_cfg
        self.tmp.cleanup()

    # --- the pool -------------------------------------------------------------------

    def test_falls_back_to_the_installed_short_id(self):
        """An upgrade must keep serving the id that existing links already carry."""
        self.assertEqual(["aabbccdd"], identity.short_ids())

    def test_ensure_pool_keeps_the_installed_id_first(self):
        values = identity.ensure_pool(4)
        self.assertEqual(4, len(values))
        self.assertEqual("aabbccdd", values[0])
        self.assertEqual(4, len(set(values)))

    def test_ensure_pool_is_idempotent(self):
        first = identity.ensure_pool(4)
        self.assertEqual(first, identity.ensure_pool(4))

    def test_ensure_pool_never_shrinks_an_existing_pool(self):
        identity.ensure_pool(4)
        self.assertEqual(4, len(identity.ensure_pool(2)))

    def test_invalid_short_ids_are_not_served(self):
        db.set_setting("reality_short_ids", "zzzz,abc,aabb,,0011223344556677889900")
        self.assertEqual(["aabb"], identity.short_ids())

    def test_set_short_ids_rejects_an_all_invalid_list(self):
        with self.assertRaises(ValueError):
            identity.set_short_ids(["zz", "abc"])

    def test_set_short_ids_drops_duplicates_and_normalises_case(self):
        self.assertEqual(["aabb", "ccdd"], identity.set_short_ids(["AABB", "aabb", "ccdd"]))

    # --- per-account assignment -----------------------------------------------------

    def test_cohort_and_spider_are_stable(self):
        identity.ensure_pool(4)
        u = user(1)
        self.assertEqual(identity.cohort(u), identity.cohort(u))
        self.assertEqual(identity.spider(u), identity.spider(u))
        self.assertEqual(identity.short_id(u), identity.short_id(u))

    def test_spider_differs_between_accounts(self):
        spiders = {identity.spider(user(i)) for i in range(1, 12)}
        self.assertGreater(len(spiders), 1)
        self.assertTrue(all(s.startswith("/") for s in spiders))

    def test_spider_can_be_switched_off(self):
        u = user(1)
        self.assertTrue(identity.spider(u))
        db.set_setting("reality_spider", "0")
        self.assertEqual("", identity.spider(u))

    def test_short_id_is_always_one_the_server_serves(self):
        identity.ensure_pool(4)
        served = set(identity.short_ids())
        for i in range(1, 30):
            self.assertIn(identity.short_id(user(i)), served)

    def test_cohort_survives_a_shrunk_pool(self):
        """A pool trimmed below a stored cohort must not hand out an unserved id."""
        identity.ensure_pool(4)
        users = [user(i) for i in range(1, 20)]
        for u in users:
            identity.short_id(u)
        identity.set_short_ids(identity.short_ids()[:2])
        served = set(identity.short_ids())
        for u in users:
            self.assertIn(identity.short_id(u), served)

    def test_members_counts_every_account(self):
        identity.ensure_pool(4)
        for i in range(1, 25):
            identity.short_id(user(i))
        self.assertEqual(24, sum(identity.members().values()))

    # --- rotation -------------------------------------------------------------------

    def test_rotation_changes_only_its_own_cohort(self):
        identity.ensure_pool(4)
        before = identity.short_ids()
        identity.rotate(2)
        after = identity.short_ids()
        self.assertNotEqual(before[2], after[2])
        self.assertEqual(before[:2], after[:2])
        self.assertEqual(before[3:], after[3:])

    def test_rotation_keeps_other_accounts_links_intact(self):
        identity.ensure_pool(4)
        holders = {}
        for i in range(1, 40):
            u = user(i)
            holders[i] = (identity.cohort(u), identity.short_id(u))
        target = holders[1][0]
        identity.rotate(target)
        for i, (cohort, old) in holders.items():
            new = identity.short_id(user(i))
            if cohort == target:
                self.assertNotEqual(old, new)
            else:
                self.assertEqual(old, new)

    def test_rotate_rejects_an_unknown_cohort(self):
        identity.ensure_pool(2)
        with self.assertRaises(ValueError):
            identity.rotate(9)

    def test_forgetting_an_account_lets_it_be_reassigned(self):
        u = user(1)
        spider = identity.spider(u)
        identity.forget(u.id)
        self.assertEqual(0, sum(identity.members().values()))
        self.assertTrue(identity.spider(u))
        self.assertNotEqual("", spider)

    def test_deleting_a_user_clears_its_identity(self):
        created = db.create_user("someone", 1, 1)
        identity.short_id(created)
        self.assertEqual(1, sum(identity.members().values()))
        db.delete(created.id)
        self.assertEqual(0, sum(identity.members().values()))

    # --- what reaches the client and the server -------------------------------------

    def test_link_carries_the_accounts_own_short_id_and_spider(self):
        identity.ensure_pool(4)
        u = user(7, "seven")
        link = links.reality_link(u)
        self.assertIn(f"sid={identity.short_id(u)}", link)
        self.assertIn("spx=%2F", link)

    def test_link_omits_spider_when_switched_off(self):
        db.set_setting("reality_spider", "0")
        self.assertNotIn("spx=", links.reality_link(user(7, "seven")))

    def test_server_serves_every_cohort(self):
        values = identity.ensure_pool(4)
        config = xray.build_config([user(1, "one")])
        reality = next(i for i in config["inbounds"] if i["tag"] == xray.REALITY_TAG)
        self.assertEqual(values, reality["streamSettings"]["realitySettings"]["shortIds"])

    def test_server_config_never_serves_an_empty_short_id_list(self):
        db.set_setting("reality_short_ids", "nonsense")
        config = xray.build_config([user(1, "one")])
        reality = next(i for i in config["inbounds"] if i["tag"] == xray.REALITY_TAG)
        self.assertEqual(["aabbccdd"],
                         reality["streamSettings"]["realitySettings"]["shortIds"])


if __name__ == "__main__":
    unittest.main()
