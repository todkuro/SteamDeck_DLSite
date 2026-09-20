"""まだ CompatToolMapping が無い config.vdf への書き込み。

Steam を入れたばかりだと、この節そのものが存在しない。実機は既に 143 件
あったため気付けず、素の Ubuntu に入れた新品の Steam で初めて出た。
ここで諦めると、登録しても Proton が割り当てられないまま黙って進む。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import steam  # noqa: E402


#: 入れたばかりの Steam の config.vdf。CompatToolMapping がまだ無い。
FRESH = '''"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"AutoUpdateWindowEnabled"\t\t"0"
\t\t\t\t"ShaderCacheManager"
\t\t\t\t{
\t\t\t\t\t"HasCurrentBucket"\t\t"1"
\t\t\t\t}
\t\t\t\t"Accounts"
\t\t\t\t{
\t\t\t\t\t"someone"
\t\t\t\t\t{
\t\t\t\t\t\t"SteamID"\t\t"76561190000000000"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
'''


class FreshInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        root = Path(self.tmp.name)
        self.userdata = root / "userdata"
        (root / "config").mkdir(parents=True)
        self.userdata.mkdir()
        self.path = root / "config" / "config.vdf"
        self.path.write_text(FRESH, encoding="utf-8")

    def _read(self):
        return self.path.read_text(encoding="utf-8")

    def test_the_sample_really_lacks_the_section(self):
        self.assertNotIn("CompatToolMapping", FRESH)

    def test_it_creates_the_section(self):
        changed = steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        self.assertTrue(changed)
        self.assertIn('"CompatToolMapping"', self._read())

    def test_the_entry_has_the_expected_shape(self):
        steam.set_compat_tool(self.userdata, 1234, "GE-Proton10-10")
        text = self._read()
        self.assertIn('"1234"', text)
        self.assertIn('"name"\t\t"GE-Proton10-10"', text)
        self.assertIn('"config"\t\t""', text)
        self.assertIn(f'"priority"\t\t"{steam.DEFAULT_COMPAT_PRIORITY}"', text)

    def test_it_can_be_read_back(self):
        steam.set_compat_tool(self.userdata, 1234, "Proton-GE Latest")
        self.assertEqual(
            steam.get_compat_tool(self.userdata, 1234), "Proton-GE Latest"
        )

    def test_the_app_id_is_unsigned(self):
        """compatdata と同じ符号なし表現で入れる。"""
        signed = -1656165185
        steam.set_compat_tool(self.userdata, signed, "proton_experimental")
        self.assertIn(f'"{signed & 0xFFFFFFFF}"', self._read())

    def test_existing_settings_survive(self):
        steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        text = self._read()
        self.assertIn('"AutoUpdateWindowEnabled"\t\t"0"', text)
        self.assertIn('"SteamID"\t\t"76561190000000000"', text)
        self.assertIn('"HasCurrentBucket"\t\t"1"', text)

    def test_the_braces_still_balance(self):
        steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        text = self._read()
        self.assertEqual(text.count("{"), text.count("}"))
        # 先頭のブロックが最後まで閉じていること
        self.assertEqual(steam._matching_brace(text, text.find("{")), len(text.rstrip()) )

    def test_it_lands_inside_the_steam_block(self):
        """Accounts などと同じ高さに入っていること。"""
        steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        text = self._read()
        self.assertIn('\t\t\t\t"CompatToolMapping"', text)

    def test_a_backup_is_left(self):
        steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        backups = list(self.path.parent.glob("config.vdf.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), FRESH)

    def test_a_second_work_is_added_to_the_same_section(self):
        steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        steam.set_compat_tool(self.userdata, 5678, "Proton-GE Latest")

        text = self._read()
        self.assertEqual(text.count('"CompatToolMapping"'), 1)
        self.assertEqual(steam.get_compat_tool(self.userdata, 1234), "proton_experimental")
        self.assertEqual(steam.get_compat_tool(self.userdata, 5678), "Proton-GE Latest")

    def test_overwrite_false_still_creates_the_first_one(self):
        """未割り当てなら書く。overwrite=False は既存を守るための指定。"""
        changed = steam.set_compat_tool(
            self.userdata, 1234, "proton_experimental", overwrite=False
        )
        self.assertTrue(changed)
        self.assertEqual(steam.get_compat_tool(self.userdata, 1234), "proton_experimental")

    def test_unreadable_structure_is_refused(self):
        self.path.write_text('"Something"\n{\n}\n', encoding="utf-8")
        with self.assertRaises(steam.SteamError):
            steam.set_compat_tool(self.userdata, 1234, "proton_experimental")

    def test_case_differences_are_tolerated(self):
        self.path.write_text(FRESH.replace('"Steam"', '"steam"'), encoding="utf-8")
        steam.set_compat_tool(self.userdata, 1234, "proton_experimental")
        self.assertEqual(steam.get_compat_tool(self.userdata, 1234), "proton_experimental")


class SectionFinderTest(unittest.TestCase):
    def test_it_follows_the_nesting(self):
        brace = steam._find_section(FRESH, steam._COMPAT_SECTION_PATH)
        self.assertIsNotNone(brace)
        # 見つけた { の中に Steam ブロックの中身が入っている
        closing = steam._matching_brace(FRESH, brace)
        inside = FRESH[brace:closing]
        self.assertIn("AutoUpdateWindowEnabled", inside)
        self.assertNotIn('"Valve"', inside)

    def test_missing_level_returns_none(self):
        self.assertIsNone(
            steam._find_section(FRESH, ("InstallConfigStore", "NoSuchThing"))
        )

    def test_it_does_not_match_a_deeper_namesake(self):
        """別の場所に同じ名前があっても、道順どおりに辿ること。"""
        text = FRESH.replace(
            '\t\t\t\t"AutoUpdateWindowEnabled"\t\t"0"',
            '\t\t\t\t"Decoy"\n\t\t\t\t{\n\t\t\t\t\t"Valve"\t\t"1"\n\t\t\t\t}',
        )
        brace = steam._find_section(text, steam._COMPAT_SECTION_PATH)
        closing = steam._matching_brace(text, brace)
        self.assertIn("Decoy", text[brace:closing])



class ShutdownMessageTest(unittest.TestCase):
    """終了の指示が効かない環境への案内。

    Ubuntu のコンテナで実測: 指示は届く ("command line was forwarded" が
    ログに出る) のに Steam が畳まなかった。実機では 23 秒で終了している。
    """

    def setUp(self):
        from dlsite_deck import config as config_module
        from dlsite_deck import steam as steam_module
        from dlsite_deck import webui

        self.webui = webui
        self.steam = steam_module
        self.tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp2.cleanup)

        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp2.name) / "state.json")
        cfg.steam_userdata_dir = str(Path(self.tmp2.name) / "userdata")
        Path(cfg.steam_userdata_dir).mkdir(parents=True)
        self.backend = webui.Backend(cfg)

        self.real_session = steam_module.session_kind
        self.real_running = steam_module.is_steam_running
        self.real_shutdown = steam_module.shutdown_steam
        self.addCleanup(self._restore)

        steam_module.session_kind = lambda: steam_module.SESSION_DESKTOP
        steam_module.is_steam_running = lambda: True

    def _restore(self):
        self.steam.session_kind = self.real_session
        self.steam.is_steam_running = self.real_running
        self.steam.shutdown_steam = self.real_shutdown

    def test_timeout_tells_you_to_use_steams_own_menu(self):
        self.steam.shutdown_steam = lambda *a, **k: False
        result = self.backend.stop_steam()

        self.assertTrue(result["running"])
        self.assertIn("メニュー", result["message"])
        self.assertIn("Steam を終了しました", result["message"])

    def test_success_says_so_plainly(self):
        self.steam.shutdown_steam = lambda *a, **k: True
        result = self.backend.stop_steam()

        self.assertFalse(result["running"])
        self.assertNotIn("メニュー", result["message"])

if __name__ == "__main__":
    unittest.main()
