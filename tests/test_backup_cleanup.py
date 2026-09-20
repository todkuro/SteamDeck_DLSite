"""作品を消したら、DLC の退避も一緒に片付けること。

退避は作品フォルダの**隣**（インストール先の直下）に置かれるので、作品を
消しただけでは残り続けていた。戻す先が無くなった控えなので連れていく。
実環境で「976.6 KiB を解放」と出しながら同量が残るのを確認して直した。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module  # noqa: E402
from dlsite_deck import link, state, webui  # noqa: E402


class BackupLookupTest(unittest.TestCase):
    """どれがその作品の退避かを見分けられること。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.games = Path(self.tmp.name) / "games"
        self.work = self.games / "a"
        self.work.mkdir(parents=True)
        self.entry = state.InstalledWork(
            id="RJ1", title="作品", directory=str(self.work)
        )
        self.backups = self.games / link.BACKUP_DIR_NAME

    def _make(self, name):
        path = self.backups / name
        path.mkdir(parents=True)
        (path / "kept.dat").write_bytes(b"x" * 100)
        return path

    def test_nothing_yet(self):
        self.assertEqual(link.backup_dirs_for(self.entry), [])

    def test_finds_its_own(self):
        wanted = self._make("RJ1-20260101-000000")
        self.assertEqual(link.backup_dirs_for(self.entry), [wanted])

    def test_ignores_other_works(self):
        self._make("RJ2-20260101-000000")
        self.assertEqual(link.backup_dirs_for(self.entry), [])

    def test_a_longer_id_is_not_mistaken_for_it(self):
        """RJ1 が RJ12 の退避を巻き込まないこと。区切りまで見る。"""
        self._make("RJ12-20260101-000000")
        self.assertEqual(link.backup_dirs_for(self.entry), [])

    def test_several_generations_come_back_in_order(self):
        second = self._make("RJ1-20260102-000000")
        first = self._make("RJ1-20260101-000000")
        self.assertEqual(link.backup_dirs_for(self.entry), [first, second])

    def test_stray_files_are_skipped(self):
        self.backups.mkdir(parents=True, exist_ok=True)
        (self.backups / "RJ1-notadir").write_bytes(b"")
        self.assertEqual(link.backup_dirs_for(self.entry), [])


class DeleteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.games = root / "games"
        self.games.mkdir(parents=True)

        cfg = config_module.Config()
        cfg.state_file = str(root / "state.json")
        cfg.download_dir = str(root / "downloads")
        cfg.install_dirs = [str(self.games)]
        cfg.install_dir = str(self.games)
        self.cfg = cfg

        self.backend = webui.Backend(cfg)
        self.backend.registered_shortcuts = lambda: (set(), set(), set())

        self.work = self.games / "a"
        (self.work / "Content").mkdir(parents=True)
        (self.work / "Content" / "story.dat").write_bytes(b"A" * 1000)

        current = state.State(Path(cfg.state_file))
        current.works["RJ1"] = state.InstalledWork(
            id="RJ1", title="作品", directory=str(self.work)
        )
        current.works["RJ2"] = state.InstalledWork(
            id="RJ2", title="別の作品", directory=str(self.games / "b")
        )
        (self.games / "b").mkdir()
        current.save()

        self.backups = self.games / link.BACKUP_DIR_NAME

    def _backup(self, work_id, size=2000):
        path = self.backups / f"{work_id}-20260101-000000"
        (path / "Content").mkdir(parents=True)
        (path / "Content" / "story.dat").write_bytes(b"B" * size)
        return path

    # -- 下見 ---------------------------------------------------------

    def test_preview_counts_the_backups(self):
        self._backup("RJ1")
        preview = self.backend.delete_preview("RJ1")
        self.assertEqual(preview["backup_count"], 1)
        self.assertNotEqual(preview["backup_label"], "0 B")

    def test_preview_reports_none_when_there_are_none(self):
        preview = self.backend.delete_preview("RJ1")
        self.assertEqual(preview["backup_count"], 0)

    def test_preview_does_not_count_other_works(self):
        self._backup("RJ2")
        self.assertEqual(self.backend.delete_preview("RJ1")["backup_count"], 0)

    # -- 削除 ---------------------------------------------------------

    def test_the_backup_goes_too(self):
        path = self._backup("RJ1")
        result = self.backend.delete_work("RJ1")

        self.assertFalse(path.exists())
        self.assertIn("退避", result["message"])
        self.assertEqual(result["backup_freed"], 2000)

    def test_freed_includes_the_backup(self):
        self._backup("RJ1", size=2000)
        result = self.backend.delete_work("RJ1")
        # 展開先 1000 + 退避 2000
        self.assertEqual(result["freed"], 3000)

    def test_other_works_backups_survive(self):
        mine = self._backup("RJ1")
        theirs = self._backup("RJ2")

        self.backend.delete_work("RJ1")
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists(), "他の作品の退避まで消してはいけない")

    def test_the_empty_container_is_removed(self):
        self._backup("RJ1")
        self.backend.delete_work("RJ1")
        self.assertFalse(self.backups.exists())

    def test_the_container_stays_when_others_remain(self):
        self._backup("RJ1")
        self._backup("RJ2")
        self.backend.delete_work("RJ1")
        self.assertTrue(self.backups.is_dir())

    def test_no_backup_is_not_an_error(self):
        result = self.backend.delete_work("RJ1")
        self.assertEqual(result["backup_freed"], 0)
        self.assertNotIn("退避", result["message"])

    def test_record_only_work_still_cleans_its_backup(self):
        """フォルダが無い (記録のみ) でも、退避は片付ける。"""
        path = self._backup("RJ1")
        import shutil

        shutil.rmtree(self.work)

        result = self.backend.delete_work("RJ1")
        self.assertFalse(path.exists())
        self.assertEqual(result["backup_freed"], 2000)

    def test_backups_outside_the_install_dirs_are_left_alone(self):
        """設定を書き換えて別の場所を指させても、そこは掃除しない。"""
        outside = Path(self.tmp.name) / "elsewhere"
        work = outside / "a"
        work.mkdir(parents=True)

        current = self.backend.state()
        current.works["RJ3"] = state.InstalledWork(
            id="RJ3", title="外の作品", directory=str(work)
        )
        current.save()

        stray = outside / link.BACKUP_DIR_NAME / "RJ3-20260101-000000"
        stray.mkdir(parents=True)
        (stray / "keep.dat").write_bytes(b"C" * 500)

        # 展開先が外にあるので削除そのものは断られる
        with self.assertRaises(webui.UiError):
            self.backend.delete_work("RJ3")
        self.assertTrue(stray.exists(), "インストール先の外を消してはいけない")


if __name__ == "__main__":
    unittest.main()
