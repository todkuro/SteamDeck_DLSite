"""共通パッチ置き場と、「実行済み」を当てた先ごとに記録すること。

ランタイムやコーデックのインストーラは、1 つを多くのゲームに入れる。どのゲームに
入れたかを覚えていないと、2 本目のゲームでも「実行済み」と出てしまう。
"""

from __future__ import annotations

import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module, proton, state, webui  # noqa: E402


class _PatchCase(unittest.TestCase):
    """作品 3 つ (2 つは Steam 登録済み) と共通パッチ置き場を用意する。試験は持たない。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.games = self.root / "games"
        self.runtimes = self.root / "runtimes"
        self.outside = self.root / "outside"
        for path in (self.games, self.runtimes / "vc", self.outside):
            path.mkdir(parents=True)
        (self.runtimes / "vc" / "vcredist_x64.exe").write_bytes(b"MZ")
        (self.runtimes / "codec.msi").write_bytes(b"x")
        (self.runtimes / "readme.txt").write_text("x", encoding="utf-8")
        (self.outside / "evil.exe").write_bytes(b"MZ")

        self.config = config_module.Config()
        self.config.install_dir = str(self.games)
        self.config.install_dirs = [str(self.games)]
        self.config.runtime_dirs = [str(self.runtimes)]
        self.config.state_file = str(self.root / "state.json")
        self.config.download_dir = str(self.root / "downloads")
        self.config.steam_userdata_dir = str(self.root / "no-steam")
        self.backend = webui.Backend(self.config)

        current = self.backend.state()
        for work_id, app_id in (("RJ1", 101), ("RJ2", 102), ("RJ3", None)):
            directory = self.games / work_id
            (directory / "patch").mkdir(parents=True)
            (directory / "patch" / "a.exe").write_bytes(b"MZ")
            current.works[work_id] = state.InstalledWork(
                id=work_id, title=f"作品{work_id}", directory=str(directory),
                steam_app_id=app_id)
        current.save()

        # 実際には Proton を動かさない。渡されたものを覚えておく。
        self.runs = []

        def fake_run(userdata, app_id, exe, working_dir=None, args=None, launch_options="",
                     **kwargs):
            self.runs.append({"app_id": app_id, "exe": exe, "args": args,
                              "launch_options": launch_options, "working_dir": working_dir})
            return proton.RunResult(exe=exe.name, returncode=0, proton="p", prefix="x",
                                    launch_options=launch_options)

        # 当てる先のゲームの起動オプション (Steam の shortcuts.vdf から読む代わり)
        self.launch = {"作品RJ1": "LANG=ja_JP.UTF-8 ~/locales/run.sh %command%",
                       "作品RJ2": "-windowed"}

        for target, value in (
            (mock.patch.object(webui.proton, "run_exe", side_effect=fake_run), None),
            (mock.patch.object(webui.Backend, "_userdata", return_value=self.root), None),
            (mock.patch.object(webui.Backend, "_launch_options_by_name",
                               side_effect=lambda: self.launch), None),
        ):
            target.start()
            self.addCleanup(target.stop)

    def run_runtime(self, target_id, relative="vc/vcredist_x64.exe", args=""):
        return self.backend.run_patch(
            "RJ1", relative, target_id, source="runtime", root="0", args=args)


class RuntimePatchTest(_PatchCase):
    def test_runtime_is_recorded_on_the_target_only(self):
        self.run_runtime("RJ1")
        current = self.backend.state()
        key = f"runtime:{(self.runtimes / 'vc' / 'vcredist_x64.exe').resolve()}"
        self.assertEqual(current.get("RJ1").installed_patches, [key])
        # 別のゲームには入れていない
        self.assertEqual(current.get("RJ2").installed_patches, [])

        targets = {t["id"]: t["installed"] for t in self.backend.patches("RJ2")["targets"]}
        self.assertEqual(targets["RJ1"], [key])
        self.assertEqual(targets["RJ2"], [])

    def test_work_patch_is_recorded_on_the_target(self):
        """作品の中のパッチも、当てた先に記録する (パッチ集を別のゲームに当てる形)。"""
        self.backend.run_patch("RJ1", "patch/a.exe", "RJ2")
        current = self.backend.state()
        self.assertEqual(current.get("RJ2").installed_patches, ["work:RJ1:patch/a.exe"])
        self.assertEqual(current.get("RJ1").installed_patches, [])

    def test_twice_is_recorded_once(self):
        self.run_runtime("RJ1")
        self.run_runtime("RJ1")
        self.assertEqual(len(self.backend.state().get("RJ1").installed_patches), 1)

    def test_args_are_passed_without_a_shell(self):
        self.run_runtime("RJ1", args='/quiet "/D=C:\\Program Files\\x"')
        self.assertEqual(self.runs[0]["args"], ["/quiet", "/D=C:\\Program Files\\x"])

    def test_empty_args_pass_nothing(self):
        self.run_runtime("RJ1")
        self.assertEqual(self.runs[0]["args"], [])

    def test_bad_args_are_refused(self):
        for bad in ('"unclosed', "a\nb", "x" * 2000):
            with self.subTest(bad=bad[:10]), self.assertRaises(webui.UiError):
                self.run_runtime("RJ1", args=bad)
        self.assertEqual(self.runs, [])

    def test_outside_the_store_is_refused(self):
        for relative in ("../outside/evil.exe", str(self.outside / "evil.exe")):
            with self.subTest(relative=relative), self.assertRaises(webui.UiError):
                self.run_runtime("RJ1", relative=relative)
        self.assertEqual(self.runs, [])

    def test_only_exe_and_msi_run(self):
        with self.assertRaises(webui.UiError):
            self.run_runtime("RJ1", relative="readme.txt")
        self.run_runtime("RJ1", relative="codec.msi")
        self.assertEqual(self.runs[0]["exe"].name, "codec.msi")

    def test_unknown_source_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.backend.run_patch("RJ1", "patch/a.exe", "RJ1", source="anywhere")

    def test_unregistered_target_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.run_runtime("RJ3")

    def test_patches_lists_the_store(self):
        info = self.backend.patches("RJ1")
        store = info["runtimes"][0]
        self.assertTrue(store["exists"])
        self.assertEqual(
            sorted(item["relative"] for item in store["patches"]),
            ["codec.msi", "vc/vcredist_x64.exe"])
        self.assertEqual([item["relative"] for item in info["patches"]], ["patch/a.exe"])

    def test_missing_store_is_reported(self):
        self.config.runtime_dirs = [str(self.root / "none")]
        store = self.backend.patches("RJ1")["runtimes"][0]
        self.assertFalse(store["exists"])
        self.assertEqual(store["patches"], [])


class LaunchOptionsTest(_PatchCase):
    """パッチを、当てる先のゲームの起動オプションを付けて実行すること (既定)。

    日本語のインストーラは、ゲームと同じ LANG で動かさないと文字化けする。
    """

    def test_used_by_default(self):
        result = self.run_runtime("RJ1")
        self.assertEqual(self.runs[0]["launch_options"], self.launch["作品RJ1"])
        self.assertEqual(result["launch_options"], self.launch["作品RJ1"])

    def test_can_be_turned_off(self):
        self.backend.run_patch("RJ1", "patch/a.exe", "RJ1", use_launch_options=False)
        self.assertEqual(self.runs[0]["launch_options"], "")

    def test_without_command_mark_is_not_used(self):
        """%command% が無いものは、Steam ではゲームへの引数になる形。パッチには付けない。"""
        result = self.run_runtime("RJ2")
        self.assertEqual(self.runs[0]["launch_options"], "")
        self.assertIn("%command%", result["message"])

    def test_no_launch_options(self):
        self.launch = {}
        self.run_runtime("RJ1")
        self.assertEqual(self.runs[0]["launch_options"], "")

    def test_listed_for_each_target(self):
        targets = {t["id"]: t["launch_options"] for t in self.backend.patches("RJ1")["targets"]}
        self.assertEqual(targets["RJ1"], self.launch["作品RJ1"])
        self.assertEqual(targets["RJ2"], "-windowed")
        self.assertIsNone(targets["RJ3"])


class RunGameExeTest(_PatchCase):
    """「実行」: ゲームの中の exe (設定用のアプリなど) を、同じ Proton 環境で動かす。"""

    def setUp(self):
        super().setUp()
        tool = self.games / "RJ1" / "Tools"
        tool.mkdir()
        (tool / "Config.exe").write_bytes(b"MZ")

    def test_runs_in_the_games_own_prefix(self):
        self.backend.run_game_exe("RJ1", "Tools/Config.exe")
        self.assertEqual(self.runs[0]["app_id"], 101)
        self.assertEqual(self.runs[0]["exe"].name, "Config.exe")

    def test_starts_in_the_exe_directory(self):
        """設定用のアプリは、隣の設定ファイルを相対パスで読むことが多い。"""
        self.backend.run_game_exe("RJ1", "Tools/Config.exe")
        self.assertEqual(self.runs[0]["working_dir"], (self.games / "RJ1" / "Tools").resolve())

    def test_patches_still_start_in_the_target(self):
        self.backend.run_patch("RJ1", "patch/a.exe", "RJ2")
        self.assertEqual(self.runs[0]["working_dir"], self.games / "RJ2")

    def test_nothing_is_recorded(self):
        self.backend.run_game_exe("RJ1", "Tools/Config.exe")
        self.assertEqual(self.backend.state().get("RJ1").installed_patches, [])

    def test_launch_options_follow_the_checkbox(self):
        self.backend.run_game_exe("RJ1", "Tools/Config.exe")
        self.backend.run_game_exe("RJ1", "Tools/Config.exe", use_launch_options=False)
        self.assertEqual(self.runs[0]["launch_options"], self.launch["作品RJ1"])
        self.assertEqual(self.runs[1]["launch_options"], "")

    def test_args_are_passed(self):
        self.backend.run_game_exe("RJ1", "Tools/Config.exe", args="-lang ja")
        self.assertEqual(self.runs[0]["args"], ["-lang", "ja"])

    def test_unregistered_game_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.backend.run_game_exe("RJ3", "patch/a.exe")
        self.assertEqual(self.runs, [])

    def test_outside_the_game_is_refused(self):
        for relative in ("../RJ2/patch/a.exe", str(self.outside / "evil.exe")):
            with self.subTest(relative=relative), self.assertRaises(webui.UiError):
                self.backend.run_game_exe("RJ1", relative)
        self.assertEqual(self.runs, [])

    def test_only_exe_and_msi_run(self):
        (self.games / "RJ1" / "readme.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(webui.UiError):
            self.backend.run_game_exe("RJ1", "readme.txt")


class LaunchCommandTest(unittest.TestCase):
    """起動オプションの %command% を、Proton で実行するコマンドに置き換えること。"""

    def run_with(self, launch_options, suffix=".exe"):
        with tempfile.TemporaryDirectory() as name:
            exe = Path(name) / f"setup file{suffix}"
            exe.write_bytes(b"MZ")
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(proton, "resolve_proton", return_value=Path("/p/proton")),                     mock.patch.object(proton, "prefix_path", return_value=Path(name) / "pfx"),                     mock.patch.object(proton, "steam_root", return_value=Path(name)),                     mock.patch.object(proton.subprocess, "run", return_value=completed) as run:
                result = proton.run_exe(Path(name), 1, exe, args=["/S"],
                                        launch_options=launch_options)
            return run.call_args.args[0], exe, result

    def test_command_mark_is_replaced(self):
        command, exe, result = self.run_with("LANG=ja_JP.UTF-8 ~/locales/run.sh %command%")
        self.assertEqual(command[:2], ["/bin/sh", "-c"])
        # 空白を含むパスも 1 つの引数のまま渡る
        inner = shlex.join([str(Path("/p/proton")), "run", str(exe), "/S"])
        self.assertEqual(command[2], f"LANG=ja_JP.UTF-8 ~/locales/run.sh {inner}")
        self.assertEqual(result.launch_options, "LANG=ja_JP.UTF-8 ~/locales/run.sh %command%")

    def test_without_launch_options_no_shell(self):
        command, exe, _ = self.run_with("")
        self.assertEqual(command[:3], [str(Path("/p/proton")), "run", str(exe)])

    def test_without_command_mark_is_refused(self):
        with self.assertRaises(proton.ProtonError):
            self.run_with("-windowed")


class OldRecordTest(unittest.TestCase):
    def test_old_applied_patches_are_dropped(self):
        """以前の「実行済み」はどのゲームに当てたか分からないので捨てる。"""
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            path.write_text(
                '{"version": 1, "works": [{"id": "RJ1", "title": "t", "directory": "d",'
                ' "applied_patches": ["patch/a.exe"]}]}', encoding="utf-8")
            current = state.State.load(path)
            self.assertEqual(current.get("RJ1").installed_patches, [])
            current.save()
            self.assertNotIn("applied_patches", path.read_text(encoding="utf-8"))


class MsiCommandTest(unittest.TestCase):
    def test_msi_goes_through_msiexec(self):
        with tempfile.TemporaryDirectory() as name:
            msi = Path(name) / "codec.msi"
            msi.write_bytes(b"x")
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(proton, "resolve_proton", return_value=Path("/p/proton")), \
                    mock.patch.object(proton, "prefix_path", return_value=Path(name) / "pfx"), \
                    mock.patch.object(proton, "steam_root", return_value=Path(name)), \
                    mock.patch.object(proton.subprocess, "run", return_value=completed) as run:
                proton.run_exe(Path(name), 1, msi, args=["/quiet"])
            command = run.call_args.args[0]
        # 名前だけの "msiexec" では、失敗しても終了コードが 0 になる
        self.assertEqual(command[1:4], ["run", r"C:\windows\system32\msiexec.exe", "/i"])
        self.assertEqual(command[4], proton.windows_path(msi))
        self.assertEqual(command[-1], "/quiet")

    def test_windows_path(self):
        self.assertEqual(proton.windows_path(Path("/home/deck/a b.msi")), "Z:\\home\\deck\\a b.msi")


if __name__ == "__main__":
    unittest.main()
