"""「カバーを作り直す」。

設定を切り替えたあと、登録し直さずに画像を今の設定へ合わせ直せること。
作るだけでなく、**使わなくなった画像を引き上げる**ところまでを見る。
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dlsite_deck import config as config_module  # noqa: E402
from dlsite_deck import cover, state, steam, webui  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32

APP_ID = 2147483648


class RebuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        # 作品フォルダ。ストア画像はここに残してある想定。
        self.work_dir = root / "games" / "sample"
        self.work_dir.mkdir(parents=True)
        cover.store_image(self.work_dir, JPEG)

        # Steam の userdata/<id>/config
        self.config_dir = root / "userdata" / "1" / "config"
        self.config_dir.mkdir(parents=True)
        self.grid = steam.grid_dir(self.config_dir)
        self.userdata = root / "userdata"

        cfg = config_module.Config()
        cfg.state_file = str(root / "state.json")
        cfg.steam_userdata_dir = str(self.userdata)
        # 既定は環境で決まる。ここでは作り直しの判断だけを見たいので固定する。
        cfg.steam_cover_title = True
        cfg.steam_logo_source = "title"
        self.cfg = cfg
        self.backend = webui.Backend(cfg)

        current = state.State(Path(cfg.state_file))
        current.works["RJ1"] = state.InstalledWork(
            id="RJ1",
            title="テスト作品",
            directory=str(self.work_dir),
            steam_app_id=APP_ID,
            steam_title="テスト作品",
        )
        current.save()

        # 描画は差し替える。何を書くかの判断だけを見たいため。
        self._cover, self._logo = cover.build_cover, cover.build_logo
        cover.build_cover = lambda image, title: PNG
        cover.build_logo = lambda title: PNG
        self.addCleanup(self._restore)

        # 別環境の判定は shortcuts.vdf を読みに行くので、ここでは黙らせる
        self.backend.registered_shortcuts = lambda: (set(), set(), set())

    def _restore(self):
        cover.build_cover, cover.build_logo = self._cover, self._logo

    def _run(self):
        self.backend.rebuild_covers()
        for _ in range(200):
            if not self.backend.cover_task()["running"]:
                break
            time.sleep(0.02)
        task = self.backend.cover_task()
        self.assertFalse(task["running"], "作り直しが終わらない")
        return task

    def _grid_names(self):
        return sorted(p.name for p in self.grid.iterdir()) if self.grid.is_dir() else []

    # -- 作る ---------------------------------------------------------

    def test_writes_all_slots_and_logo(self):
        task = self._run()
        self.assertEqual(task["done"], 1)
        self.assertEqual(task["errors"], [])
        self.assertEqual(
            self._grid_names(),
            [f"{APP_ID}.jpg", f"{APP_ID}_hero.jpg", f"{APP_ID}_logo.png", f"{APP_ID}p.png"],
        )

    def test_cover_uses_the_generated_png_not_the_store_jpeg(self):
        self._run()
        # 縦長カバーだけ作り直したもの (PNG)、横長と背景は元画像 (JPEG)
        self.assertEqual((self.grid / f"{APP_ID}p.png").read_bytes(), PNG)
        self.assertEqual((self.grid / f"{APP_ID}.jpg").read_bytes(), JPEG)
        self.assertEqual((self.grid / f"{APP_ID}_hero.jpg").read_bytes(), JPEG)

    def test_cover_title_off_falls_back_to_the_store_image(self):
        self.cfg.steam_cover_title = False
        self._run()
        self.assertIn(f"{APP_ID}p.jpg", self._grid_names())
        self.assertNotIn(f"{APP_ID}p.png", self._grid_names())

    # -- 引き上げる ---------------------------------------------------

    def test_turning_the_logo_off_removes_it(self):
        self._run()
        self.assertIn(f"{APP_ID}_logo.png", self._grid_names())

        self.cfg.steam_logo_source = "none"
        task = self._run()

        self.assertNotIn(f"{APP_ID}_logo.png", self._grid_names())
        self.assertGreater(task["removed"], 0)

    def test_unrenderable_logo_is_left_alone(self):
        """作れなかっただけのときは消さない。

        rsvg-convert が無い環境で作り直すと、別の環境で付けたロゴを
        黙って剥がしてしまうため。
        """
        self._run()
        self.assertIn(f"{APP_ID}_logo.png", self._grid_names())

        cover.build_logo = lambda title: None       # 描けない環境を装う
        task = self._run()

        self.assertIn(f"{APP_ID}_logo.png", self._grid_names())
        self.assertEqual(task["removed"], 0)

    def test_dropping_a_slot_removes_it(self):
        self._run()
        self.assertIn(f"{APP_ID}_hero.jpg", self._grid_names())

        self.cfg.steam_grid_slots = ["cover"]
        self._run()

        left = self._grid_names()
        self.assertNotIn(f"{APP_ID}_hero.jpg", left)
        self.assertNotIn(f"{APP_ID}.jpg", left)
        self.assertIn(f"{APP_ID}p.png", left)

    def test_turning_images_off_removes_everything(self):
        self._run()
        self.cfg.steam_grid_images = False
        self._run()
        self.assertEqual(self._grid_names(), [])

    def test_switching_back_restores_the_cover(self):
        """設定を戻せば元に戻る。片道にならないこと。"""
        self._run()
        self.cfg.steam_grid_images = False
        self._run()
        self.cfg.steam_grid_images = True
        self._run()
        self.assertIn(f"{APP_ID}p.png", self._grid_names())

    # -- 触らないもの -------------------------------------------------

    def test_other_apps_are_untouched(self):
        self.grid.mkdir(parents=True, exist_ok=True)
        keep = self.grid / "999p.jpg"
        keep.write_bytes(JPEG)

        self._run()
        self.assertTrue(keep.is_file())

    def test_unregistered_work_is_skipped(self):
        current = self.backend.state()
        current.works["RJ1"].steam_app_id = None
        current.save()

        task = self._run()
        self.assertEqual(task["total"], 0)
        self.assertEqual(self._grid_names(), [])

    def test_foreign_entry_is_skipped(self):
        """別環境の登録には触らない。名前で照合するため書き換えてしまう。"""
        self.backend.registered_shortcuts = lambda: (set(), set(), {"テスト作品"})

        task = self._run()
        self.assertEqual(task["total"], 0)
        self.assertEqual(self._grid_names(), [])

    # -- 再取得しない -------------------------------------------------

    def test_does_not_fetch_when_the_image_is_cached(self):
        def explode():
            raise AssertionError("作り直しで再ダウンロードしてはいけない")

        self.backend.client = explode
        task = self._run()
        self.assertEqual(task["errors"], [])
        self.assertIn(f"{APP_ID}p.png", self._grid_names())

    def test_missing_image_is_reported_not_fatal(self):
        for extension in cover.CACHE_EXTENSIONS:
            (self.work_dir / (cover.CACHE_STEM + extension)).unlink(missing_ok=True)
        self.backend.grid_image = lambda entry: None

        task = self._run()
        self.assertEqual(task["done"], 1)
        self.assertEqual(task["errors"], [])

    # -- 二重起動 -----------------------------------------------------

    def test_refuses_to_start_twice(self):
        with self.backend._lock:
            self.backend._cover_task["running"] = True
        with self.assertRaises(webui.UiError):
            self.backend.rebuild_covers()
        with self.backend._lock:
            self.backend._cover_task["running"] = False

    def test_message_mentions_restarting_steam(self):
        task = self._run()
        self.assertIn("Steam", task["message"])


if __name__ == "__main__":
    unittest.main()
