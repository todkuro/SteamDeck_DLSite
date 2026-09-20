"""Proton の割り当て変更。

書き込む先は Steam 自身の ``config.vdf`` の ``CompatToolMapping``。実機の 143 件は
Steam の設定画面から設定したものもツールが書いたものも、すべて
``name`` / ``config`` / ``priority`` の同じ形をしていた。ここではその形を崩さずに
書けているか、既存の項目を壊していないかを確かめる。
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import steam  # noqa: E402


#: 実機の config.vdf と同じ入れ方をした最小の見本
SAMPLE = '''"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"0"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"proton_experimental"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"75"
\t\t\t\t\t}
\t\t\t\t\t"3394802138"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"proton_experimental"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t\t"2381323176"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"GE-Proton10-10"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\t"Accounts"
\t\t\t\t{
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
'''


def entries(text: str) -> dict[str, str]:
    """CompatToolMapping の中を {appid: 割り当て名} で取り出す。"""
    start = text.index('"CompatToolMapping"')
    end = text.index('"Accounts"', start)
    segment = text[start:end]
    found = re.findall(
        r'"(\d+)"\s*\{[^}]*?"name"\s*"([^"]*)"', segment, re.S
    )
    return dict(found)


class SetCompatToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "steam"
        self.userdata = root / "userdata"
        self.userdata.mkdir(parents=True)
        self.config = root / "config" / "config.vdf"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(SAMPLE, encoding="utf-8")

    def _read(self) -> str:
        return self.config.read_text(encoding="utf-8")

    def test_existing_entry_is_updated_in_place(self):
        changed = steam.set_compat_tool(self.userdata, 3394802138, "GE-Proton10-13")
        self.assertTrue(changed)

        found = entries(self._read())
        self.assertEqual(found["3394802138"], "GE-Proton10-13")
        # 他の項目に触っていないこと
        self.assertEqual(found["2381323176"], "GE-Proton10-10")
        self.assertEqual(found["0"], "proton_experimental")

    def test_signed_app_id_finds_the_unsigned_entry(self):
        """shortcuts.vdf 側は符号付きで持っている。

        3394802138 と -900165158 は同じゲーム。取り違えると別のゲームの
        設定を書き換えてしまう。
        """
        steam.set_compat_tool(self.userdata, -900165158, "proton_8")
        self.assertEqual(entries(self._read())["3394802138"], "proton_8")

    def test_new_entry_keeps_the_same_shape(self):
        """新しく足すときも name / config / priority の形を崩さないこと。"""
        steam.set_compat_tool(self.userdata, 4290825716, "proton_7")
        text = self._read()
        self.assertEqual(entries(text)["4290825716"], "proton_7")

        block = re.search(
            r'"4290825716"\s*\{(.*?)\}', text, re.S
        ).group(1)
        keys = re.findall(r'"(\w+)"\s*"', block)
        self.assertEqual(keys, ["name", "config", "priority"])

    def test_setting_the_same_tool_reports_no_change(self):
        self.assertFalse(
            steam.set_compat_tool(self.userdata, 3394802138, "proton_experimental")
        )

    def test_overwrite_false_leaves_an_existing_assignment(self):
        changed = steam.set_compat_tool(
            self.userdata, 2381323176, "proton_9", overwrite=False
        )
        self.assertFalse(changed)
        self.assertEqual(entries(self._read())["2381323176"], "GE-Proton10-10")

    def test_a_backup_is_left_behind(self):
        steam.set_compat_tool(self.userdata, 3394802138, "proton_8")
        backups = list(self.config.parent.glob("config.vdf.bak-*"))
        self.assertTrue(backups, "控えが残っていない")
        self.assertIn("proton_experimental",
                      backups[0].read_text(encoding="utf-8"))

    def test_the_file_stays_parseable(self):
        """書き換えたあとも読み直せること。壊すと Steam の設定全体を失う。"""
        for app_id, tool in ((3394802138, "proton_8"), (4290825716, "proton_7"),
                             (2381323176, "Proton-GE Latest")):
            steam.set_compat_tool(self.userdata, app_id, tool)

        text = self._read()
        self.assertEqual(text.count("{"), text.count("}"), "括弧が釣り合っていない")
        for app_id, tool in ((3394802138, "proton_8"), (4290825716, "proton_7"),
                             (2381323176, "Proton-GE Latest")):
            self.assertEqual(
                steam.get_compat_tool(self.userdata, app_id), tool
            )

    def test_missing_config_is_reported(self):
        self.config.unlink()
        with self.assertRaises(steam.SteamError):
            steam.set_compat_tool(self.userdata, 3394802138, "proton_8")


class GetCompatToolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "steam"
        self.userdata = root / "userdata"
        self.userdata.mkdir(parents=True)
        config = root / "config" / "config.vdf"
        config.parent.mkdir(parents=True)
        config.write_text(SAMPLE, encoding="utf-8")

    def test_reads_assignments(self):
        self.assertEqual(
            steam.get_compat_tool(self.userdata, 2381323176), "GE-Proton10-10"
        )

    def test_unassigned_game_is_none(self):
        self.assertIsNone(steam.get_compat_tool(self.userdata, 111222333))



class LaunchOptionsTest(unittest.TestCase):
    """起動オプションの書き込み。

    日本語のゲームはロケールを渡さないと表示もファイル名も文字化けする
    (SteamOS には ja_JP.UTF-8 が入っていない)。ツールから設定できる必要がある
    一方、Steam 上で利用者が設定した内容を黙って消してはいけない。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "shortcuts.vdf"

    def _shortcut(self, name="ゲーム", options=""):
        return steam.Shortcut(
            app_name=name, exe='"/games/a.exe"', start_dir='"/games"',
            launch_options=options,
        )

    def test_new_shortcut_gets_the_options(self):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(
            document, self._shortcut(options="LANG=ja_JP.UTF-8 %command%")
        )
        entry = document["shortcuts"]["0"]
        self.assertEqual(
            steam.get_field(entry, "LaunchOptions"), "LANG=ja_JP.UTF-8 %command%"
        )

    def test_existing_options_are_kept_when_not_asked(self):
        """指示が無ければ触らない。Steam 上での設定を守るため。"""
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, self._shortcut(options="手で設定した %command%"))

        steam.upsert_shortcut(document, self._shortcut(options="別のもの"))
        entry = document["shortcuts"]["0"]
        self.assertEqual(steam.get_field(entry, "LaunchOptions"), "手で設定した %command%")

    def test_existing_options_are_replaced_when_asked(self):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, self._shortcut(options="古い %command%"))

        steam.upsert_shortcut(
            document, self._shortcut(options="新しい %command%"), set_launch_options=True
        )
        entry = document["shortcuts"]["0"]
        self.assertEqual(steam.get_field(entry, "LaunchOptions"), "新しい %command%")

    def test_can_be_cleared_explicitly(self):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, self._shortcut(options="消したい %command%"))

        steam.upsert_shortcut(
            document, self._shortcut(options=""), set_launch_options=True
        )
        self.assertEqual(
            steam.get_field(document["shortcuts"]["0"], "LaunchOptions"), ""
        )

    def test_survives_a_write_and_read(self):
        """日本語やチルダを含む値が壊れずに往復すること。"""
        options = "LANG=ja_JP.UTF-8 ~/locales/run.sh  %command%"
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, self._shortcut(options=options))
        steam.save_shortcuts(self.path, document, backup=False)

        again = steam.load_shortcuts(self.path)
        entry = again["shortcuts"]["0"]
        self.assertEqual(steam.get_field(entry, "LaunchOptions"), options)

    def test_other_fields_are_untouched_on_update(self):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, self._shortcut())
        entry = document["shortcuts"]["0"]
        entry["icon"] = "/my/icon.png"
        entry["sortas"] = "並べ替え用"

        steam.upsert_shortcut(document, self._shortcut(options="x"), set_launch_options=True)
        self.assertEqual(steam.get_field(entry, "icon"), "/my/icon.png")
        self.assertEqual(steam.get_field(entry, "sortas"), "並べ替え用")


class ShortcutPlatformTest(unittest.TestCase):
    """ショートカットがどの環境のものか。

    Steam はデバイス間で非 Steam ゲームの情報を見せることがある。実際、開発機の
    Windows 側には 12 件、SteamDeck 側には 137 件あり、いずれも自分の環境の
    パスだけだった。とはいえ混ざった場合に取り違えると、動かないものを
    「登録済み」と扱ったり、同名という理由で別環境の登録を壊しかねない。
    """

    def test_windows_paths(self):
        for exe in (r'"H:\Games\DLSite\妹！せいかつ\Game.exe"',
                    r'C:\Program Files\a.exe',
                    '"C:/Program Files/a.exe"',
                    r'"D:\programs\TV\TVTest\TVTest.exe"'):
            with self.subTest(exe=exe):
                self.assertEqual(steam.shortcut_platform(exe), "windows")

    def test_posix_paths(self):
        for exe in ('"/run/media/deck/SDCARD/dlsite_games/a/GamePro.exe"',
                    "/home/deck/Applications/dlsite_deck/dlsite-deck.sh",
                    '"/usr/bin/foo"'):
            with self.subTest(exe=exe):
                self.assertEqual(steam.shortcut_platform(exe), "posix")

    def test_undecidable_paths(self):
        """判別できないものは、よそのものと決めつけない。"""
        for exe in ("", "a.exe", "steam://run/440", r"\server\share\a.exe"):
            with self.subTest(exe=exe):
                self.assertEqual(steam.shortcut_platform(exe), "unknown")

    def test_foreign_depends_on_where_we_run(self):
        windows_entry = {"AppName": "w", "Exe": r'"H:\Games\a.exe"'}
        posix_entry = {"AppName": "p", "Exe": '"/home/deck/a.exe"'}
        unknown_entry = {"AppName": "u", "Exe": "a.exe"}

        if sys.platform == "win32":
            self.assertFalse(steam.is_foreign_shortcut(windows_entry))
            self.assertTrue(steam.is_foreign_shortcut(posix_entry))
        else:
            self.assertTrue(steam.is_foreign_shortcut(windows_entry))
            self.assertFalse(steam.is_foreign_shortcut(posix_entry))

        # 判別できないものは弾かない (自分の登録を取りこぼさないため)
        self.assertFalse(steam.is_foreign_shortcut(unknown_entry))


class ShutdownSteamTest(unittest.TestCase):
    """Steam に行儀よく終了してもらう処理。

    ``pkill`` で落とすと ``shortcuts.vdf`` を書き戻す前に死ぬことがあるので、
    Steam 自身の ``-shutdown`` に任せる。実際に落ちるまで待つ必要がある
    (指示そのものはすぐ返ってくる)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.userdata = self.root / "Steam" / "userdata"
        self.userdata.mkdir(parents=True)

        self.real_which = steam.shutil.which
        self.real_run = steam.subprocess.run
        self.real_running = steam.is_steam_running
        self.addCleanup(setattr, steam.shutil, "which", self.real_which)
        self.addCleanup(setattr, steam.subprocess, "run", self.real_run)
        self.addCleanup(setattr, steam, "is_steam_running", self.real_running)

        steam.shutil.which = lambda name: None
        self.commands = []

    def _fake_run(self, alive_after: int):
        """``-shutdown`` を受けた後、何回目の確認で落ちたことにするか。"""
        state = {"checks": 0}

        def run(command, **kwargs):
            self.commands.append(command)
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def running():
            state["checks"] += 1
            return state["checks"] <= alive_after

        steam.subprocess.run = run
        steam.is_steam_running = running

    def test_executable_is_found_on_the_path(self):
        steam.shutil.which = lambda name: "/usr/bin/steam" if name == "steam" else None
        self.assertEqual(str(steam.steam_executable()), str(Path("/usr/bin/steam")))

    def test_executable_is_found_next_to_userdata(self):
        """Windows では PATH に無い。Steam のフォルダから辿る。"""
        name = "steam.exe" if sys.platform == "win32" else "steam"
        (self.userdata.parent / name).write_bytes(b"stub")
        found = steam.steam_executable(self.userdata)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, name)

    def test_missing_executable_is_reported(self):
        with self.assertRaises(steam.SteamError) as caught:
            steam.shutdown_steam(self.userdata)
        self.assertIn("見つかりません", str(caught.exception))

    def test_waits_until_steam_actually_exits(self):
        steam.shutil.which = lambda name: "/usr/bin/steam"
        self._fake_run(alive_after=3)

        self.assertTrue(steam.shutdown_steam(self.userdata, timeout=10))
        # Windows では Path が区切りを変えるので、文字列そのままでは比べない
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(Path(self.commands[0][0]), Path("/usr/bin/steam"))
        self.assertEqual(self.commands[0][1], "-shutdown")

    def test_gives_up_after_the_timeout(self):
        steam.shutil.which = lambda name: "/usr/bin/steam"
        self._fake_run(alive_after=10**6)   # いつまでも落ちない

        self.assertFalse(steam.shutdown_steam(self.userdata, timeout=1.2))

    def test_launch_failure_is_reported(self):
        steam.shutil.which = lambda name: "/usr/bin/steam"

        def boom(*args, **kwargs):
            raise OSError("起動できない")

        steam.subprocess.run = boom
        with self.assertRaises(steam.SteamError):
            steam.shutdown_steam(self.userdata)


class ConfiguredExecutableTest(unittest.TestCase):
    """設定で指定した Steam の場所。

    既定は SteamDeck の ``/usr/bin/steam``。そこに無い環境でも動くように、
    見つからなければ PATH と Steam のフォルダを順に試す。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.userdata = self.root / "Steam" / "userdata"
        self.userdata.mkdir(parents=True)

        self.real_which = steam.shutil.which
        self.addCleanup(setattr, steam.shutil, "which", self.real_which)
        steam.shutil.which = lambda name: None

    def test_default_points_at_the_deck(self):
        from dlsite_deck import config as config_module

        self.assertEqual(config_module.Config().steam_executable, "/usr/bin/steam")

    def test_configured_path_wins(self):
        chosen = self.root / "どこかの" / "steam"
        chosen.parent.mkdir(parents=True)
        chosen.write_bytes(b"stub")
        # PATH にも別のものがあるが、設定を優先する
        steam.shutil.which = lambda name: str(self.root / "path" / "steam")

        found = steam.steam_executable(self.userdata, str(chosen))
        self.assertEqual(found, chosen)

    def test_falls_back_when_the_configured_path_is_absent(self):
        """設定が的外れでも、Steam を見つけられること。

        既定は SteamDeck の場所なので、そこに Steam が無い環境ではこの道を通る。
        (実機では /usr/bin/steam が実在するため、確実に無い名前で試す)
        """
        name = "steam.exe" if sys.platform == "win32" else "steam"
        beside = self.userdata.parent / name
        beside.write_bytes(b"stub")

        found = steam.steam_executable(self.userdata, "/usr/bin/__no_such_steam__")
        self.assertEqual(found, beside)

    def test_nothing_anywhere_is_none(self):
        self.assertIsNone(steam.steam_executable(self.userdata, "/どこにも/無い"))

    def test_empty_setting_still_searches(self):
        name = "steam.exe" if sys.platform == "win32" else "steam"
        beside = self.userdata.parent / name
        beside.write_bytes(b"stub")

        self.assertEqual(steam.steam_executable(self.userdata, ""), beside)

if __name__ == "__main__":
    unittest.main()
