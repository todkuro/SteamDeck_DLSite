"""DLC の重ね合わせ。

実ライブラリで確認した 3 通りの形をそのまま再現して試す。作品によって
方向も対象も違うので、どれか 1 つだけ通っても意味がない。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import link, state  # noqa: E402


def make_state(root: Path, works: dict[str, dict[str, str]]) -> state.State:
    """作品ディレクトリを作り、それを記録した State を返す。"""
    current = state.State(root / "state.json")
    for work_id, files in works.items():
        directory = root / work_id
        for relative, body in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        current.works[work_id] = state.InstalledWork(
            id=work_id, title=work_id, directory=str(directory)
        )
    return current


class OverlayOntoBaseTest(unittest.TestCase):
    """DLC 全体を本編の直下へ。実物で最も多い形。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.state = make_state(self.root, {
            "BASE": {
                "GamePro.exe": "古い本体",
                "Data/BasicData.wolf": "古いデータ",
                "Data/Keep.wolf": "本編にしかない",
                "更新履歴.txt": "v1",
            },
            "DLC": {
                "GamePro.exe": "新しい本体",
                "Data/BasicData.wolf": "新しいデータ",
                "お読みくださいDLC.txt": "DLC の説明",
            },
        })

    def test_plan_counts_overwrites(self):
        item = link.Link(source_id="DLC", target_id="BASE")
        result = link.plan(self.state, item)

        self.assertEqual(sorted(result.overwrites),
                         ["Data/BasicData.wolf", "GamePro.exe"])
        self.assertEqual(result.additions, ["お読みくださいDLC.txt"])

    def test_apply_overwrites_and_keeps_base_only_files(self):
        item = link.Link(source_id="DLC", target_id="BASE")
        link.apply(self.state, item)
        base = self.root / "BASE"

        self.assertEqual((base / "GamePro.exe").read_text(encoding="utf-8"), "新しい本体")
        self.assertEqual((base / "Data/BasicData.wolf").read_text(encoding="utf-8"),
                         "新しいデータ")
        self.assertTrue((base / "お読みくださいDLC.txt").is_file())
        # 本編にしかないファイルは残ること
        self.assertEqual((base / "Data/Keep.wolf").read_text(encoding="utf-8"),
                         "本編にしかない")

    def test_backup_lets_us_go_back(self):
        item = link.Link(source_id="DLC", target_id="BASE")
        link.apply(self.state, item)
        self.assertTrue(item.backup_dir, "退避先が記録されていない")

        message = link.revert(self.state, item)
        base = self.root / "BASE"
        self.assertIn("書き戻しました", message)
        self.assertEqual((base / "GamePro.exe").read_text(encoding="utf-8"), "古い本体")
        # 追加されたものは残す方針
        self.assertTrue((base / "お読みくださいDLC.txt").is_file())
        self.assertIsNone(item.applied_at)


class BackupStaysOneGenerationTest(unittest.TestCase):
    """重ね直しても退避は増えず、中身は手つかずの原本のままであること。

    以前は適用のたびに日時で新しい世代を作っていたので、作品を消すまで
    増え続けた。しかも 2 世代目以降は「前回の DLC が当たったあとの姿」で、
    本当に戻したいものは最古の 1 つにしかなかった。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.state = make_state(self.root, {
            "BASE": {"Game.exe": "原本"},
            "DLC": {"Game.exe": "DLC 版 1"},
            "DLC2": {"Game.exe": "別の DLC"},
        })
        self.base = self.state.works["BASE"]

    def _apply(self, source_id="DLC"):
        item = link.Link(source_id=source_id, target_id="BASE")
        link.apply(self.state, item)
        return item

    def test_applying_twice_makes_one_backup(self):
        self._apply()
        (self.root / "DLC" / "Game.exe").write_text("DLC 版 2", encoding="utf-8")
        self._apply()

        self.assertEqual(len(link.backup_dirs_for(self.base)), 1)

    def test_the_original_survives_a_second_apply(self):
        """2 回目の適用で控えが上書きされないこと。ここが肝。"""
        first = self._apply()
        (self.root / "DLC" / "Game.exe").write_text("DLC 版 2", encoding="utf-8")
        second = self._apply()

        self.assertEqual(first.backup_dir, second.backup_dir)
        link.revert(self.state, second)
        self.assertEqual(
            (self.root / "BASE" / "Game.exe").read_text(encoding="utf-8"), "原本")

    def test_a_different_link_gets_its_own_backup(self):
        """別の結び付きの控えと混ざらないこと。"""
        first = self._apply("DLC")
        second = self._apply("DLC2")

        self.assertNotEqual(first.backup_dir, second.backup_dir)
        self.assertEqual(len(link.backup_dirs_for(self.base)), 2)

    def test_the_place_does_not_move_between_runs(self):
        item = link.Link(source_id="DLC", target_id="BASE")
        again = link.Link(source_id="DLC", target_id="BASE")
        link.apply(self.state, item)
        link.apply(self.state, again)
        self.assertEqual(item.backup_dir, again.backup_dir)


class CopyBaseIntoDlcTest(unittest.TestCase):
    """本編の一部を DLC 側へ。続編が本編のデータを要求する形。

    こちらは方向が逆で、本編の ``Chapter1/Content`` だけが対象になる。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.state = make_state(self.root, {
            "BASE": {
                "Chapter1/Chapter1.exe": "本編 exe",
                "Chapter1/Content/movie/a.swf": "動画A",
                "Chapter1/Content/movie/b.swf": "動画B",
                "Chapter2/Chapter2.exe": "別の同梱作",
                "Chapter2/Content/x.swf": "混ぜてはいけない",
            },
            "FME": {
                "Sequel.exe": "DLC exe",
                "ContentFME/new.swf": "追加分",
            },
        })

    def test_only_named_subfolder_is_copied(self):
        item = link.Link(
            source_id="BASE", target_id="FME",
            source_path="Chapter1/Content", target_path="Content",
        )
        result = link.apply(self.state, item)
        fme = self.root / "FME"

        self.assertEqual(result.overwrites, [], "上書きは起きないはず")
        self.assertEqual(sorted(result.additions), ["movie/a.swf", "movie/b.swf"])
        self.assertEqual((fme / "Content/movie/a.swf").read_text(encoding="utf-8"), "動画A")
        # DLC 側の元の中身は無事
        self.assertTrue((fme / "ContentFME/new.swf").is_file())
        self.assertTrue((fme / "Sequel.exe").is_file())
        # 同梱の別作品を巻き込まないこと
        self.assertFalse((fme / "Content/x.swf").exists())


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.state = make_state(self.root, {
            "A": {"file.txt": "a"}, "B": {"file.txt": "b"},
        })

    def test_escaping_the_work_directory_is_refused(self):
        item = link.Link(source_id="A", target_id="B", source_path="../B")
        with self.assertRaises(link.LinkError):
            link.plan(self.state, item)

    def test_missing_work_is_reported(self):
        item = link.Link(source_id="NOPE", target_id="B")
        with self.assertRaises(link.LinkError):
            link.plan(self.state, item)

    def test_missing_source_path_is_reported(self):
        item = link.Link(source_id="A", target_id="B", source_path="ない")
        with self.assertRaises(link.LinkError):
            link.plan(self.state, item)


class PersistenceTest(unittest.TestCase):
    """結び付きが state.json に残ること。"""

    def test_links_survive_a_round_trip(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            current = state.State(path)
            current.works["RJ1"] = state.InstalledWork(
                id="RJ1", title="本編", directory=str(Path(name) / "g"),
                installed_patches=["work:RJ2:patch/a.exe"],
            )
            current.links.append(state.LinkRecord(
                source_id="RJ2", target_id="RJ1", source_path="", target_path="",
                note="DLC",
            ))
            current.save()

            again = state.State.load(path)
            self.assertEqual(len(again.links), 1)
            self.assertEqual(again.links[0].source_id, "RJ2")
            self.assertEqual(again.links[0].note, "DLC")
            self.assertEqual(again.works["RJ1"].installed_patches, ["work:RJ2:patch/a.exe"])

    def test_old_state_without_links_still_loads(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            path.write_text(
                '{"version": 1, "works": [{"id": "RJ1", "title": "t",'
                ' "directory": "/tmp/x"}]}', encoding="utf-8")
            again = state.State.load(path)
            self.assertEqual(again.links, [])
            self.assertEqual(again.works["RJ1"].installed_patches, [])


if __name__ == "__main__":
    unittest.main()
