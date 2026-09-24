"""自由登録の取り込み元と共通パッチ置き場を、設定画面から変えられること。

利用者が決める場所なので画面から変えられる。ただし合言葉を知ったプログラムに
悪用されないよう、他のアプリの持ち物がある隠しディレクトリなどは断る。

* 取り込み元に ``~/.mozilla`` → Cookie のファイルを「ゲーム」として取り込まれる
* 共通パッチ置き場に ``~/.var/app/...`` → Flatpak のアプリが置いた exe を実行させられる
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module, webui  # noqa: E402
from tests import support  # noqa: E402


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as name:
        try:
            os.symlink(name, str(Path(name) / "link"), target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


needs_symlink = unittest.skipUnless(_can_symlink(), "この環境ではシンボリックリンクを作れません")


class UserDirsTest(unittest.TestCase):
    def setUp(self):
        self.root = support.use_temp_config(self)
        self.config_path = self.root / "config.json"
        # ホームディレクトリを一時ディレクトリに見立てる (本物のホームは触らない)。
        # Windows の一時ディレクトリは AppData の中にあり、それ自体が断られるので、
        # そこではプロジェクトの中に作る (終われば消す)。
        base = tempfile.TemporaryDirectory(
            prefix="tmp-user-dirs-",
            dir=str(ROOT) if sys.platform == "win32" else None)
        self.addCleanup(base.cleanup)
        self.home = Path(base.name) / "home"
        for name in ("Downloads/games", ".mozilla/firefox", ".var/app/evil/data", "AppData/Roaming"):
            (self.home / name).mkdir(parents=True)
        patch = mock.patch.object(Path, "home", return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)

        cfg = config_module.Config()
        cfg.state_file = str(self.root / "state.json")
        cfg.download_dir = str(self.root / "downloads")
        cfg.install_dirs = [str(self.root / "games")]
        cfg.install_dir = str(self.root / "games")
        self.backend = webui.Backend(cfg)

    def saved(self, key):
        return json.loads(self.config_path.read_text(encoding="utf-8"))[key]

    def refused(self, key, value):
        before = getattr(self.backend.config, key)
        with self.assertRaises(webui.UiError) as caught:
            self.backend.save_config({key: [value]})
        self.assertEqual(getattr(self.backend.config, key), before)
        self.assertFalse(self.config_path.exists(), "断ったのに保存された")
        return str(caught.exception)

    # -- 通るもの -------------------------------------------------------

    def test_an_ordinary_place_can_be_set(self):
        place = str(self.home / "Downloads" / "games")
        for key in ("import_dirs", "runtime_dirs"):
            with self.subTest(key=key):
                self.backend.save_config({key: [place]})
                self.assertEqual(self.saved(key), [place])

    def test_empty_lines_and_duplicates_are_dropped(self):
        place = str(self.home / "Downloads")
        self.backend.save_config({"import_dirs": ["", place, " ", place]})
        self.assertEqual(self.saved("import_dirs"), [place])

    def test_an_empty_list_is_allowed(self):
        self.backend.save_config({"runtime_dirs": []})
        self.assertEqual(self.saved("runtime_dirs"), [])

    def test_unchanged_default_saves_even_if_missing(self):
        """既定の imports/ がまだ無くても、他の設定の保存は止めない。"""
        current = list(self.backend.config.import_dirs)
        self.backend.save_config({"import_dirs": current, "delete_archives": False})
        self.assertFalse(json.loads(self.config_path.read_text(encoding="utf-8"))["delete_archives"])

    # -- 断るもの -------------------------------------------------------

    def test_hidden_directories_are_refused(self):
        for key, place in (
            ("import_dirs", self.home / ".mozilla" / "firefox"),
            ("runtime_dirs", self.home / ".var" / "app" / "evil" / "data"),
            ("import_dirs", self.home / ".mozilla"),
        ):
            with self.subTest(key=key, place=place):
                self.assertIn("隠しディレクトリ", self.refused(key, str(place)))

    def test_appdata_is_refused(self):
        self.refused("import_dirs", str(self.home / "AppData" / "Roaming"))

    def test_home_itself_is_refused(self):
        """ホームそのものは、中にある隠しディレクトリをたどれてしまう。"""
        self.refused("import_dirs", str(self.home))

    def test_root_is_refused(self):
        self.refused("runtime_dirs", str(Path(self.root.anchor)))

    def test_the_tool_itself_is_refused(self):
        for place in (config_module.PROJECT_ROOT, config_module.PROJECT_ROOT / "dlsite_deck"):
            with self.subTest(place=place):
                self.refused("runtime_dirs", str(place))

    def test_missing_directory_is_refused(self):
        self.assertIn("見つかりません", self.refused("import_dirs", str(self.home / "none")))

    def test_relative_path_is_refused(self):
        self.refused("import_dirs", "Downloads")

    def test_one_bad_entry_refuses_all(self):
        with self.assertRaises(webui.UiError):
            self.backend.save_config({"import_dirs": [
                str(self.home / "Downloads"), str(self.home / ".mozilla")]})
        self.assertFalse(self.config_path.exists())

    def test_not_a_list_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.backend.save_config({"import_dirs": str(self.home / "Downloads")})

    @needs_symlink
    def test_link_into_a_hidden_directory_is_refused(self):
        """見た目は普通の場所でも、リンクを解いて隠しディレクトリなら断る。"""
        link = self.home / "Downloads" / "firefox"
        os.symlink(str(self.home / ".mozilla" / "firefox"), str(link), target_is_directory=True)
        self.refused("import_dirs", str(link))

    def test_the_fields_are_editable_on_the_screen(self):
        fields = {f["key"]: f for f in webui.CONFIG_FIELDS}
        for key in ("import_dirs", "runtime_dirs"):
            self.assertFalse(fields[key].get("locked"), key)


if __name__ == "__main__":
    unittest.main()
