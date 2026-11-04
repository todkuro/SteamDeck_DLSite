"""作品のディレクトリをファイルマネージャーで開くこと。

開くのは記録にある作品のディレクトリだけ。画面からパスは受け取らない。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module, server, state, webui  # noqa: E402


class OpenDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.games = self.root / "games"
        (self.games / "RJ1").mkdir(parents=True)

        self.config = config_module.Config()
        self.config.install_dir = str(self.games)
        self.config.install_dirs = [str(self.games)]
        self.config.state_file = str(self.root / "state.json")
        self.config.download_dir = str(self.root / "downloads")
        self.config.steam_userdata_dir = str(self.root / "no-steam")
        self.backend = webui.Backend(self.config)

        current = self.backend.state()
        current.works["RJ1"] = state.InstalledWork(
            id="RJ1", title="作品1", directory=str(self.games / "RJ1"))
        # 記録だけあって実体が無いもの (SD カードが外れているなど)
        current.works["RJ2"] = state.InstalledWork(
            id="RJ2", title="作品2", directory=str(self.games / "RJ2"))
        current.save()

        self.popen = mock.patch.object(webui.subprocess, "Popen")
        self.started = self.popen.start()
        self.addCleanup(self.popen.stop)

    def test_opens_the_recorded_directory(self):
        with mock.patch.object(webui, "_file_manager_command",
                               side_effect=lambda path: ["opener", str(path)]):
            result = self.backend.open_directory("RJ1")

        command = self.started.call_args.args[0]
        self.assertEqual(command, ["opener", str(self.games / "RJ1")])
        self.assertEqual(result["path"], str(self.games / "RJ1"))

    def test_refuses_unknown_or_missing_directories(self):
        for work_id in ("RJ9", "RJ2"):
            with self.subTest(work_id=work_id), self.assertRaises(webui.UiError):
                self.backend.open_directory(work_id)
        self.started.assert_not_called()

    def test_without_an_opener_it_points_to_copying_the_path(self):
        with mock.patch.object(webui, "_file_manager_command", return_value=None):
            with self.assertRaises(webui.UiError) as caught:
                self.backend.open_directory("RJ1")
        self.assertIn("パスをコピー", str(caught.exception))
        self.started.assert_not_called()

    def test_linux_uses_xdg_open(self):
        with mock.patch.object(webui.sys, "platform", "linux"), \
                mock.patch.object(webui.shutil, "which", return_value="/usr/bin/xdg-open"):
            self.assertEqual(webui._file_manager_command(Path("/g")),
                             ["/usr/bin/xdg-open", str(Path("/g"))])
        with mock.patch.object(webui.sys, "platform", "linux"), \
                mock.patch.object(webui.shutil, "which", return_value=None):
            self.assertIsNone(webui._file_manager_command(Path("/g")))

    def test_the_route_takes_only_the_work_id(self):
        # パスを添えて送っても使わない。記録から引いたディレクトリを開く。
        with mock.patch.object(webui, "_file_manager_command",
                               side_effect=lambda path: ["opener", str(path)]):
            server.POST_ROUTES["/api/open"](self.backend, {"work_id": "RJ1", "path": "/etc"})
        self.assertEqual(self.started.call_args.args[0][1], str(self.games / "RJ1"))


if __name__ == "__main__":
    unittest.main()
