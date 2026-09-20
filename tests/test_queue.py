"""ダウンロードの順番待ち。

以前は要求のたびにスレッドを起こしていたため、一括取得を押すと表示中の
未取得すべて (実ライブラリで 18 本・60 GiB) がいっせいに走った。DLsite にも
回線にも負担がかかるうえ、進行状況は 1 件しか出ないので何本走っているかも
分からなかった。押した順に 1 本ずつ処理することを確かめる。
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import api, config as config_module, webui  # noqa: E402


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        cfg = config_module.Config()
        cfg.install_dir = str(root / "games")
        cfg.install_dirs = [str(root / "games")]
        cfg.state_file = str(root / "state.json")
        cfg.download_dir = str(root / "downloads")
        Path(cfg.install_dir).mkdir(parents=True)

        self.backend = webui.Backend(cfg)

        # ライブラリ取得と実処理を、試験用の作り物に差し替える
        # title は names から導かれる
        self.works = [
            api.Work(id=f"RJ{index:04d}", names={"ja_JP": f"作品{index}"})
            for index in range(6)
        ]
        self.backend.library = lambda refresh=False: (self.works, [])

        self.order: list[str] = []
        self.concurrent = 0
        self.peak = 0
        self.gate = threading.Event()
        self.guard = threading.Lock()

        def fake_run(job, work, target_root):
            with self.guard:
                self.concurrent += 1
                self.peak = max(self.peak, self.concurrent)
                self.order.append(work.id)
            self.gate.wait(timeout=5)
            with self.guard:
                self.concurrent -= 1
            job.status = "done"
            job.finished_at = time.time()

        self.backend._run_download = fake_run

    def _wait(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_only_one_runs_at_a_time(self):
        for work in self.works:
            self.backend.start_download(work.id)

        self.assertTrue(self._wait(lambda: len(self.order) >= 1), "1 本目が始まらない")
        # 少し待っても 2 本目が始まらないこと
        time.sleep(0.3)
        self.assertEqual(self.peak, 1, f"同時に {self.peak} 本走っている")

        payload = self.backend.jobs_json()
        self.assertEqual(payload["running"], 1)
        self.assertEqual(payload["queued"], len(self.works) - 1)

        self.gate.set()
        self.assertTrue(self._wait(lambda: len(self.order) == len(self.works)),
                        f"全部走らない: {self.order}")
        self.assertEqual(self.peak, 1, "どこかで同時に走った")

    def test_processed_in_the_order_pressed(self):
        wanted = ["RJ0003", "RJ0000", "RJ0005", "RJ0001"]
        for work_id in wanted:
            self.backend.start_download(work_id)

        self.gate.set()
        self.assertTrue(self._wait(lambda: len(self.order) == len(wanted)),
                        f"全部走らない: {self.order}")
        self.assertEqual(self.order, wanted, "押した順に処理されていない")

    def test_queue_positions_are_numbered(self):
        for work in self.works[:4]:
            self.backend.start_download(work.id)
        self.assertTrue(self._wait(lambda: len(self.order) >= 1))

        jobs = {j["work_id"]: j for j in self.backend.jobs_json()["jobs"]}
        self.assertIsNone(jobs["RJ0000"]["queue_position"], "走っている分に順番が付いている")
        self.assertEqual(jobs["RJ0001"]["queue_position"], 1)
        self.assertEqual(jobs["RJ0002"]["queue_position"], 2)
        self.assertEqual(jobs["RJ0003"]["queue_position"], 3)
        self.gate.set()

    def test_same_work_is_not_queued_twice(self):
        first = self.backend.start_download("RJ0000")
        second = self.backend.start_download("RJ0000")

        self.assertFalse(first["already_running"])
        self.assertTrue(second["already_running"])
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.gate.set()

    def test_queued_job_can_be_cancelled_without_running(self):
        for work in self.works[:3]:
            self.backend.start_download(work.id)
        self.assertTrue(self._wait(lambda: len(self.order) >= 1))

        jobs = {j["work_id"]: j for j in self.backend.jobs_json()["jobs"]}
        target = jobs["RJ0002"]
        self.assertTrue(target["cancellable"], "順番待ちは取り消せるべき")

        result = self.backend.cancel_download(target["id"])
        self.assertEqual(result["job"]["status"], "cancelled")

        self.gate.set()
        self.assertTrue(self._wait(lambda: len(self.order) == 2))
        self.assertNotIn("RJ0002", self.order, "取り消したのに走っている")

    def test_a_job_added_as_the_worker_finishes_still_runs(self):
        """担当が終わりかけている隙に積んでも拾われること。

        起動の判断と「仕事が無い」の判断を別々の錠で行うと、この隙間に
        積まれた分が誰にも拾われず永久に待つ。
        """
        self.gate.set()  # すぐ終わるようにしておく

        for index in range(20):
            self.backend.start_download(f"RJ{index % 6:04d}")
            # 担当が終わる頃合いを狙って積む
            time.sleep(0.01)
            if len(self.order) >= 6:
                break

        self.assertTrue(
            self._wait(lambda: self.backend.jobs_json()["queued"] == 0, timeout=5),
            f"順番待ちが残ったまま: {self.backend.jobs_json()['queued']}",
        )

    def test_worker_restarts_after_going_idle(self):
        self.gate.set()
        self.backend.start_download("RJ0000")
        self.assertTrue(self._wait(lambda: len(self.order) == 1))
        # 担当が仕事を終えて降りるのを待つ
        self.assertTrue(self._wait(lambda: not self.backend._worker_active))

        self.backend.start_download("RJ0001")
        self.assertTrue(self._wait(lambda: len(self.order) == 2), "再開されない")


if __name__ == "__main__":
    unittest.main()
