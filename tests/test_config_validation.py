"""設定の検証が、画面の選択肢と食い違わないこと。

以前は「画面に出す選択肢」と「保存時に通す値」を別々に書いていた。
項目を増やすと片方だけ直して、**画面には出るのに保存できない**状態になる。
実際 steam_logo_source を足したときに両方へ書く必要があった。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module  # noqa: E402
from dlsite_deck import webui  # noqa: E402


class SingleSourceTest(unittest.TestCase):
    def test_choices_come_from_the_screen_definition(self):
        for key in webui._CHOICE_MESSAGES:
            self.assertTrue(
                webui._choice_values(key), f"{key} の選択肢が引けていない"
            )

    def test_every_choice_field_is_validated(self):
        """選択式の項目に検証の漏れが無いこと。"""
        on_screen = {
            f["key"] for f in webui.CONFIG_FIELDS if f.get("type") == "choice"
        }
        self.assertEqual(on_screen, set(webui._CHOICE_MESSAGES))

    def test_defaults_pass_their_own_validation(self):
        """既定値が自分の選択肢に入っていること。"""
        defaults = config_module.Config()
        for key in webui._CHOICE_MESSAGES:
            self.assertIn(getattr(defaults, key), webui._choice_values(key))

    def test_slot_labels_match_the_slots_steam_knows(self):
        from dlsite_deck import steam

        ours = {key for key, _label in webui.GRID_SLOT_LABELS}
        known = set(steam.GRID_SLOTS) - {steam.LOGO_SLOT}
        self.assertEqual(ours, known)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp.name) / "state.json")
        self.backend = webui.Backend(cfg)

    def _merged(self, **changes):
        from dataclasses import asdict

        merged = asdict(self.backend.config)
        merged.update(changes)
        return merged

    def test_a_good_setting_passes(self):
        self.backend._validate_config(self._merged())

    def test_unknown_choice_is_refused(self):
        for key in webui._CHOICE_MESSAGES:
            with self.subTest(key=key):
                with self.assertRaises(webui.UiError):
                    self.backend._validate_config(self._merged(**{key: "__nope__"}))

    def test_every_allowed_choice_is_accepted(self):
        for key in webui._CHOICE_MESSAGES:
            for value in webui._choice_values(key):
                with self.subTest(key=key, value=value):
                    self.backend._validate_config(self._merged(**{key: value}))

    def test_unknown_slot_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.backend._validate_config(self._merged(steam_grid_slots=["nope"]))

    def test_logo_slot_is_not_selectable(self):
        """ロゴは作り方が別なので、画像スロットとしては選べない。"""
        with self.assertRaises(webui.UiError):
            self.backend._validate_config(self._merged(steam_grid_slots=["logo"]))

    def test_wrong_types_are_refused(self):
        cases = [
            {"strip_versions": "yes"},
            {"directory_template": 1},
            {"exclude": "RJ1"},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(webui.UiError):
                    self.backend._validate_config(self._merged(**changes))

    def test_blank_paths_are_refused(self):
        for key in ("download_dir", "install_dir", "state_file"):
            with self.subTest(key=key):
                with self.assertRaises(webui.UiError):
                    self.backend._validate_config(self._merged(**{key: "   "}))

    def test_default_install_dir_must_be_listed(self):
        with self.assertRaises(webui.UiError):
            self.backend._validate_config(
                self._merged(install_dirs=["/a"], install_dir="/b")
            )

    def test_blank_lines_in_install_dirs_are_dropped(self):
        merged = self._merged(
            install_dirs=["/a", "  ", ""], install_dir="/a"
        )
        self.backend._validate_config(merged)
        self.assertEqual(merged["install_dirs"], ["/a"])

    def test_at_least_one_install_dir(self):
        with self.assertRaises(webui.UiError):
            self.backend._validate_config(self._merged(install_dirs=[]))


class CompatToolDefaultTest(unittest.TestCase):
    """既定の Proton は OS で決まること。

    Windows では exe がそのまま動くので割り当てない。当ててしまうと、実体の
    無いツールを指す CompatToolMapping を config.vdf に書く (節が無ければ
    作るので、Linux 専用の節を新設することになる)。
    """

    def _platform(self, name):
        import unittest.mock

        patch = unittest.mock.patch.object(config_module.sys, "platform", name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_windows_assigns_nothing(self):
        self._platform("win32")
        self.assertEqual(config_module.default_compat_tool(), "")
        self.assertEqual(config_module.Config().steam_compat_tool, "")

    def test_linux_gets_experimental(self):
        self._platform("linux")
        self.assertEqual(config_module.default_compat_tool(), "proton_experimental")
        self.assertEqual(
            config_module.Config().steam_compat_tool, "proton_experimental")

    def test_a_written_setting_wins_on_either(self):
        """設定ファイルに書いてあれば OS に関わらずそちらを使う。"""
        import json

        for name in ("win32", "linux"):
            with self.subTest(platform=name):
                self._platform(name)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(
                        json.dumps({"steam_compat_tool": "GE-Proton10-10"}),
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        config_module.load(path).steam_compat_tool, "GE-Proton10-10")


if __name__ == "__main__":
    unittest.main()
