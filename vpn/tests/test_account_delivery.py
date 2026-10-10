"""Account delivery must not put a growing config bundle in a photo caption."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bot")))
import handlers
import shop


class AccountDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def bundle(self):
        return "🔗 <b>Configs</b>\n\n" + "\n\n".join(
            f"<b>Path {i}</b>\n<code>vless://{'a' * 450}?route={i}</code>" for i in range(20))

    def test_many_links_are_preserved_in_bounded_html_messages(self):
        text = self.bundle()
        with mock.patch("handlers.links_text", return_value=text):
            chunks = handlers.link_messages(None)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(text, "\n\n".join(chunks))
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 3500)
            self.assertEqual(chunk.count("<code>"), chunk.count("</code>"))

    def test_oversized_block_is_escaped_and_unicode_safe(self):
        with mock.patch("handlers.links_text", return_value="<code>" + "😀&lt;&amp;" * 3000 + "</code>"):
            chunks = handlers.link_messages(None)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-16-le")) // 2, 3500)

    async def test_admin_creation_uses_short_qr_caption_and_separate_links(self):
        user = SimpleNamespace(id=1)
        msg = SimpleNamespace(answer=mock.AsyncMock(), answer_photo=mock.AsyncMock())
        state = mock.AsyncMock()
        state.get_data.return_value = {"name": "sample", "traffic": 1}
        with (mock.patch("handlers.db.create_user", return_value=user),
              mock.patch("handlers.apply_user", mock.AsyncMock()),
              mock.patch("handlers.fmt.user_card", return_value="card"),
              mock.patch("handlers.user_kb", return_value=None),
              mock.patch("handlers.links.sub_url", return_value="https://example.test/sub"),
              mock.patch("handlers.links_text", return_value=self.bundle())):
            await handlers._finish_add(msg, state, 30)
        self.assertLess(len(msg.answer_photo.call_args.kwargs["caption"]), 1024)
        self.assertTrue(any("vless://" in call.args[0] for call in msg.answer.call_args_list))

    async def test_purchase_menu_does_not_query_channel(self):
        msg = SimpleNamespace(answer=mock.AsyncMock(),
                              from_user=SimpleNamespace(id=101, full_name="customer"))
        state = mock.AsyncMock()
        with (mock.patch("shop.membership.ensure", mock.AsyncMock(side_effect=AssertionError("must not gate"))),
              mock.patch("shop.shop_open", return_value=True),
              mock.patch("shop.shopdb.customer", return_value=SimpleNamespace(reseller_percent=0)),
              mock.patch("shop.shopdb.plans", return_value=[])):
            await shop.buy(msg, state, mock.AsyncMock())
        msg.answer.assert_awaited_once()
