"""パッチ exe の列挙と、Proton の引き当て。

実際に Proton を動かす部分は実機でしか試せないので、ここでは「どの Proton を
選ぶか」「どの exe を候補に出すか」という、間違えると壊れる部分を押さえる。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import proton  # noqa: E402


class PrefixTest(unittest.TestCase):
    def test_signed_app_id_becomes_unsigned(self):
        """shortcuts.vdf は符号付き、compatdata は符号なしで持っている。

        実機で appid -900165158 / プレフィックス 3394802138 という組になっていた。
        """
        userdata = Path("/home/deck/.steam/steam/userdata")
        self.assertTrue(
            str(proton.prefix_path(userdata, -900165158)).endswith("3394802138")
        )
        self.assertTrue(
            str(proton.prefix_path(userdata, 3394802138)).endswith("3394802138")
        )


class ResolveProtonTest(unittest.TestCase):
    """割り当てられている Proton を正しく引き当てること。

    違う版で実行するとプレフィックスが変換され、元に戻せない。実機では
    proton_experimental / GE-Proton10-10 / proton_7 などが混在していた。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "steam"
        self.userdata = root / "userdata"
        self.userdata.mkdir(parents=True)

        for name in ("Proton - Experimental", "Proton 7.0", "Proton 8.0",
                     "Proton 11.0", "Proton Hotfix"):
            directory = root / "steamapps" / "common" / name
            directory.mkdir(parents=True)
            (directory / "proton").write_text("#!/usr/bin/env python", encoding="utf-8")
        for name in ("GE-Proton10-10", "Proton-GE Latest"):
            directory = root / "compatibilitytools.d" / name
            directory.mkdir(parents=True)
            (directory / "proton").write_text("#!/usr/bin/env python", encoding="utf-8")

        self.config = root / "config"
        self.config.mkdir(parents=True)

    def _write_mapping(self, app_id: int, name: str) -> None:
        self.config.joinpath("config.vdf").write_text(
            '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
            '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n'
            f'\t\t\t\t\t"{app_id}"\n\t\t\t\t\t{{\n\t\t\t\t\t\t"name"\t\t"{name}"\n'
            '\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"priority"\t\t"250"\n'
            '\t\t\t\t\t}\n\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n',
            encoding="utf-8",
        )

    def test_experimental(self):
        self._write_mapping(123, "proton_experimental")
        self.assertEqual(
            proton.resolve_proton(self.userdata, 123).parent.name,
            "Proton - Experimental",
        )

    def test_custom_ge_proton(self):
        self._write_mapping(123, "GE-Proton10-10")
        self.assertEqual(
            proton.resolve_proton(self.userdata, 123).parent.name, "GE-Proton10-10"
        )

    def test_name_with_spaces(self):
        self._write_mapping(123, "Proton-GE Latest")
        self.assertEqual(
            proton.resolve_proton(self.userdata, 123).parent.name, "Proton-GE Latest"
        )

    def test_numbered_official_proton(self):
        self._write_mapping(123, "proton_7")
        self.assertEqual(
            proton.resolve_proton(self.userdata, 123).parent.name, "Proton 7.0"
        )

    def test_hotfix_is_not_mistaken_for_experimental(self):
        """実機に proton_hotfix 割り当てのゲームがあった。

        Proton Hotfix は別物なので、Experimental に落としてはいけない。
        違う版で動かすとプレフィックスが変換され、元に戻せない。
        """
        self._write_mapping(123, "proton_hotfix")
        self.assertEqual(
            proton.resolve_proton(self.userdata, 123).parent.name, "Proton Hotfix"
        )

    def test_two_digit_version(self):
        self._write_mapping(123, "proton_11")
        self.assertEqual(
            proton.resolve_proton(self.userdata, 123).parent.name, "Proton 11.0"
        )

    def test_unmapped_game_falls_back(self):
        self._write_mapping(999, "proton_8")
        chosen = proton.resolve_proton(self.userdata, 123, fallback="proton_experimental")
        self.assertEqual(chosen.parent.name, "Proton - Experimental")

    def test_unknown_tool_does_not_crash(self):
        self._write_mapping(123, "まだ入れていないProton")
        chosen = proton.resolve_proton(self.userdata, 123)
        self.assertEqual(chosen.parent.name, "Proton - Experimental")


class FindPatchesTest(unittest.TestCase):
    """パッチ特典セット (VJ100002) の形。12 個の exe を個別に選べること。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for relative in [
            "アフターシナリオパッチ/れいなアフター/reinna_after_patch.exe",
            "アフターシナリオパッチ/真闇アフター/makura_after_patch.exe",
            "アフターシナリオパッチ/稀咲アフター/kisaki_after_patch.exe",
            "男抜きCGパッチ/男抜きCGパッチ.exe",
            "特典パッチ/bonuspatch.exe",
            "追加Hシーンパッチ/れいな/reina_tuika_h.exe",
            "追加Hシーンパッチ/ニイロク/26_tuika_h.exe",
            # 混ざりがちな、パッチではないもの
            "unins000.exe",
            "redist/vcredist_x86.exe",
            "DXSETUP.exe",
            "readme.txt",
        ]:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"MZ" + b"\0" * 100)

    def test_patch_exes_are_listed_with_their_folders(self):
        found = proton.find_patches(self.root)
        names = [item.relative for item in found]

        self.assertEqual(len(names), 7, f"想定と違う: {names}")
        # フォルダ名まで見せないと、どのキャラのパッチか区別できない
        self.assertIn("追加Hシーンパッチ/れいな/reina_tuika_h.exe", names)
        self.assertIn("追加Hシーンパッチ/ニイロク/26_tuika_h.exe", names)

    def test_installers_and_runtimes_are_excluded(self):
        names = [item.relative for item in proton.find_patches(self.root)]
        for unwanted in ("unins000.exe", "redist/vcredist_x86.exe", "DXSETUP.exe"):
            self.assertNotIn(unwanted, names)

    def test_runtimes_are_kept_in_the_shared_store(self):
        """共通パッチ置き場は、ランタイムを入れるための場所なので外さない。"""
        names = [item.relative for item in proton.find_patches(self.root, keep_runtimes=True)]
        self.assertIn("redist/vcredist_x86.exe", names)
        self.assertIn("DXSETUP.exe", names)
        # アンインストーラは、どこにあっても当てるものではない
        self.assertNotIn("unins000.exe", names)

    def test_msi_is_listed(self):
        (self.root / "codec.msi").write_bytes(b"x")
        names = [item.relative for item in proton.find_patches(self.root)]
        self.assertIn("codec.msi", names)

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(proton.find_patches(self.root / "ない"), [])


if __name__ == "__main__":
    unittest.main()
