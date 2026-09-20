"""画面から結び付けを書き戻す・外す。

指し方が肝。以前は配列の添字で指していたので、1 件外すと以降の添字が
ずれ、画面を読み直す前に 2 つ目を押すと**別の結び付き**に当たった。
UI が無いうちは踏めなかったが、行に操作を付けるなら塞いでおく必要がある。
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


class LinkApiTest(unittest.TestCase):
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

        current = state.State(Path(cfg.state_file))
        for work_id, title, files in (
            ("BASE", "本編", {"Game.exe": "原本"}),
            ("DLC", "DLC", {"Game.exe": "DLC 版"}),
            ("OTHER", "別の DLC", {"extra.dat": "追加"}),
        ):
            directory = self.games / work_id
            for name, body in files.items():
                (directory / name).parent.mkdir(parents=True, exist_ok=True)
                (directory / name).write_text(body, encoding="utf-8")
            current.works[work_id] = state.InstalledWork(
                id=work_id, title=title, directory=str(directory)
            )
        current.save()

    def _apply(self, source_id="DLC"):
        return self.backend.apply_link({
            "source_id": source_id, "target_id": "BASE", "mode": "copy",
        })

    def _links(self):
        return self.backend.links_json()["links"]

    # -- 一覧 -----------------------------------------------------------

    def test_the_list_hands_out_keys(self):
        self._apply()
        item = self._links()[0]
        self.assertIn("key", item)
        self.assertNotIn("index", item, "添字で指す作りに戻っている")
        self.assertTrue(item["has_backup"])
        self.assertTrue(item["applied_at"])

    def test_an_unapplied_link_has_no_backup(self):
        """上書きが起きなければ退避もない。書き戻す口を出さないため。"""
        self.backend.apply_link({
            "source_id": "OTHER", "target_id": "BASE", "mode": "copy",
        })
        item = self._links()[0]
        self.assertFalse(item["has_backup"])

    # -- 指し方 ---------------------------------------------------------

    def test_removing_one_does_not_shift_the_other(self):
        self._apply("DLC")
        self.backend.apply_link({
            "source_id": "OTHER", "target_id": "BASE", "mode": "copy",
        })
        first, second = (item["key"] for item in self._links())

        self.backend.remove_link(first)
        remaining = self._links()
        self.assertEqual([item["key"] for item in remaining], [second])

    def test_a_stale_key_is_refused(self):
        self._apply()
        with self.assertRaises(webui.UiError):
            self.backend.remove_link("いない:->BASE:")
        with self.assertRaises(webui.UiError):
            self.backend.revert_link("いない:->BASE:")

    # -- 書き戻す -------------------------------------------------------

    def test_revert_puts_the_original_back(self):
        self._apply()
        base = self.games / "BASE" / "Game.exe"
        self.assertEqual(base.read_text(encoding="utf-8"), "DLC 版")

        key = self._links()[0]["key"]
        result = self.backend.revert_link(key)

        self.assertIn("書き戻しました", result["message"])
        self.assertEqual(base.read_text(encoding="utf-8"), "原本")
        # 記録は残り、未適用に戻る
        self.assertIsNone(self._links()[0]["applied_at"])

    def test_revert_survives_a_reapply(self):
        """重ね直したあとでも、戻る先は手つかずの原本であること。"""
        self._apply()
        (self.games / "DLC" / "Game.exe").write_text("DLC 版 2", encoding="utf-8")
        self._apply()

        self.backend.revert_link(self._links()[0]["key"])
        self.assertEqual(
            (self.games / "BASE" / "Game.exe").read_text(encoding="utf-8"), "原本")

    def test_reapplying_does_not_pile_up_backups(self):
        self._apply()
        self._apply()
        entry = self.backend.state().get("BASE")
        self.assertEqual(len(link.backup_dirs_for(entry)), 1)

    # -- 記録を外す -----------------------------------------------------

    def test_remove_keeps_the_files(self):
        self._apply()
        key = self._links()[0]["key"]
        result = self.backend.remove_link(key)

        self.assertIn("そのまま", result["message"])
        self.assertEqual(self._links(), [])
        self.assertEqual(
            (self.games / "BASE" / "Game.exe").read_text(encoding="utf-8"), "DLC 版")


if __name__ == "__main__":
    unittest.main()
