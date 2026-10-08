import os
import sys
import unittest
from unittest import mock

# ruff: noqa: E402

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot"))
sys.path.insert(0, BOT_DIR)

import admin_ui
import handlers
import shop


def callbacks(markup) -> set[str]:
    return {
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


class AdminNavigationTests(unittest.TestCase):
    def test_persistent_admin_keyboard_has_six_top_level_actions(self):
        labels = [button.text for row in handlers.ADMIN_KB.keyboard for button in row]
        self.assertEqual(6, len(labels))
        self.assertEqual(4, len(handlers.ADMIN_KB.keyboard))
        self.assertIn(handlers.BTN_ADMIN_HOME, labels)
        self.assertIn(handlers.BTN_OPERATIONS, labels)

    def test_every_admin_hub_has_home_route(self):
        with (
            mock.patch("admin_ui.db.all_users", return_value=[]),
            mock.patch("admin_ui.db.get_setting", return_value=""),
            mock.patch("admin_ui.nodes.all_nodes", return_value=[]),
            mock.patch("admin_ui.links.relays", return_value=[]),
            mock.patch("admin_ui._iran_health", return_value=(0, 0)),
        ):
            for view in (
                admin_ui.customers_view,
                admin_ui.operations_view,
                admin_ui.management_view,
                admin_ui.network_settings_view,
                admin_ui.access_settings_view,
                admin_ui.data_settings_view,
                admin_ui.system_settings_view,
            ):
                _, markup = view()
                self.assertIn("nav:home", callbacks(markup), view.__name__)

    def test_shop_uses_progressive_sections(self):
        routes = callbacks(shop.shop_admin_kb())
        self.assertEqual(
            4, len([route for route in routes if route.startswith("sa:section:")])
        )
        self.assertIn("nav:home", routes)

    def test_sensitive_system_actions_require_confirmation(self):
        _, markup = admin_ui.system_settings_view()
        routes = callbacks(markup)
        self.assertIn("nav:restart:ask", routes)
        self.assertIn("nav:rebuild:ask", routes)
        self.assertNotIn("set:restart", routes)
        self.assertNotIn("set:rebuild", routes)


if __name__ == "__main__":
    unittest.main()
