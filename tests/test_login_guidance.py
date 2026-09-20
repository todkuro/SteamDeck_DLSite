"""ログインがまだのときの出方。

一覧を取れないと画面が data=null のまま描画に進み、本当の理由が
「can't access property "items"」に覆い隠されていた。その再発を防ぐ。
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dlsite_deck import config as config_module  # noqa: E402
from dlsite_deck import cookies, webui  # noqa: E402

PAGE = (Path(__file__).resolve().parent.parent / "dlsite_deck" / "page.html").read_text("utf-8")


class ErrorShapeTest(unittest.TestCase):
    """サーバーが返す形。"""

    def test_login_required_is_a_ui_error(self):
        # UiError として扱われないと「内部エラー」に落ちてしまう
        self.assertTrue(issubclass(webui.LoginRequired, webui.UiError))

    def test_it_carries_a_kind(self):
        self.assertEqual(webui.LoginRequired("x").kind, "login")

    def test_plain_ui_errors_have_no_kind(self):
        self.assertEqual(webui.UiError("x").kind, "")

    def test_steps_tell_you_what_to_do(self):
        steps = webui.LoginRequired("x").steps
        self.assertTrue(steps)
        joined = " ".join(steps)
        self.assertIn("dlsite.com", joined)
        # Play 側のセッションも要る。ここを抜かすと通らない。
        self.assertIn("play.dlsite.com", joined)
        self.assertIn("再読み込み", joined)


class ClientTest(unittest.TestCase):
    """Cookie が読めないときに何が飛ぶか。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp.name) / "state.json")
        self.backend = webui.Backend(cfg)

        self.real = cookies.load_firefox
        self.addCleanup(setattr, cookies, "load_firefox", self.real)

    def test_missing_profile_becomes_guidance(self):
        def explode():
            raise cookies.CookieError("Firefox のプロファイルが見つかりませんでした。")

        cookies.load_firefox = explode
        with self.assertRaises(webui.LoginRequired) as caught:
            self.backend.client()
        self.assertIn("プロファイル", str(caught.exception))

    def test_no_cookies_becomes_guidance(self):
        cookies.load_firefox = lambda: []
        with self.assertRaises(webui.LoginRequired):
            self.backend.client()

    def test_failure_is_not_cached(self):
        """失敗を覚え込まないこと。

        覚えてしまうと、ブラウザでログインしてもツールを再起動するまで
        直らなくなる。画面では「再起動は要らない」と案内している。
        """
        cookies.load_firefox = lambda: []
        with self.assertRaises(webui.LoginRequired):
            self.backend.client()

        self.assertIsNone(self.backend._client)


class PageTest(unittest.TestCase):
    """画面側。ここが今回の直接の原因だった。"""

    def test_render_bails_out_without_data(self):
        body = re.search(r"function render\(\) \{(.{0,400})", PAGE, re.S).group(1)
        self.assertIn("if (!data) return;", body)

    def test_the_poll_loop_uses_the_same_handler(self):
        # notice(error.message) のままだと、案内を出しても上書きされる
        self.assertIn("} catch (error) { showFailure(error); }", PAGE)

    def test_failures_go_through_show_failure(self):
        self.assertIn("showFailure(error)", PAGE)
        self.assertIn("function showFailure(error)", PAGE)

    def test_it_says_no_restart_is_needed(self):
        self.assertIn("再起動する必要はありません", PAGE)

    def test_call_carries_the_kind_and_steps(self):
        self.assertIn("payload.error_kind", PAGE)
        self.assertIn("payload.error_steps", PAGE)

    def test_the_guide_offers_a_way_forward(self):
        self.assertIn("guide-reload", PAGE)
        self.assertIn("guide-status", PAGE)

    def test_the_status_button_presses_the_real_tab(self):
        # 独自に切り替えると、未保存の確認などを飛ばしてしまう
        self.assertIn("""document.querySelector('.tab[data-tab="status"]').click()""", PAGE)

    def test_steps_are_escaped(self):
        self.assertIn("esc(s)", PAGE)


if __name__ == "__main__":
    unittest.main()
