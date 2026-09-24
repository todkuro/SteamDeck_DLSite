"""自由登録 (DLsite 以外のゲームの取り込み)。

取り込み元の外を読ませないこと、消してよいものだけを消すこと、DLsite に
問い合わせないことを中心に確かめる。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module, cover, local, state, webui  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as name:
        try:
            os.symlink(name, str(Path(name) / "link"))
        except (OSError, NotImplementedError):
            return False
    return True


needs_symlink = unittest.skipUnless(_can_symlink(), "この環境ではシンボリックリンクを作れません")


class LocalTestCase(unittest.TestCase):
    """取り込み元・インストール先・状態ファイルを一時ディレクトリに作る。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.imports = self.root / "imports"
        self.games = self.root / "games"
        self.outside = self.root / "outside"
        for path in (self.imports, self.games, self.outside):
            path.mkdir()
        (self.outside / "cookies.sqlite").write_text("secret", encoding="utf-8")

        self.config = config_module.Config()
        self.config.install_dir = str(self.games)
        self.config.install_dirs = [str(self.games)]
        self.config.import_dirs = [str(self.imports)]
        self.config.state_file = str(self.root / "state.json")
        self.config.download_dir = str(self.root / "downloads")
        # 開発機の本物の Steam を見に行かない
        self.config.steam_userdata_dir = str(self.root / "no-steam")
        self.config.steam_compat_tool = "proton_experimental"

        self.backend = webui.Backend(self.config)
        # DLsite に問い合わせたら失敗させる。自由登録はログインなしで動く必要がある。
        patch = mock.patch.object(
            webui.Backend, "client", side_effect=AssertionError("DLsite に問い合わせた"))
        patch.start()
        self.addCleanup(patch.stop)
        # Proton の一覧は Steam が無いと空になる。選べるものを 1 つ用意する。
        tools = mock.patch.object(
            webui.Backend, "_compat_tools",
            return_value=[{"value": "proton_experimental", "label": "Proton Experimental"},
                          {"value": "GE-Proton10-10", "label": "GE-Proton10-10"}])
        tools.start()
        self.addCleanup(tools.stop)

    def make_zip(self, name: str, files: dict[str, str]) -> Path:
        path = self.imports / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for member, body in files.items():
                archive.writestr(member, body)
        return path

    def run_import(self, **values) -> state.InstalledWork:
        """取り込みを積み、終わるまで待って記録を返す。"""
        payload = {
            "title": "テストゲーム", "name_mode": "custom", "name": "test_game",
            "root": 0, "path": "", "install_dir": str(self.games),
            "compat_tool": "proton_experimental",
        }
        payload.update(values)
        started = self.backend.start_local_import(payload)
        self.backend._worker.join(timeout=30)
        job = self.backend.jobs_json()["jobs"][0]
        self.assertEqual(job["status"], "done", job["message"])
        entry = self.backend.state().get(started["work_id"])
        self.assertIsNotNone(entry)
        return entry


class ResolveTest(LocalTestCase):
    """取り込み元の外を指させないこと。"""

    def test_inside_is_resolved(self):
        (self.imports / "a").mkdir()
        base, target = local.resolve(self.config, 0, "a")
        self.assertEqual(target, (self.imports / "a").resolve())
        self.assertEqual(base, self.imports.resolve())

    def test_parent_escape_is_refused(self):
        with self.assertRaises(local.LocalError):
            local.resolve(self.config, 0, "../outside/cookies.sqlite")

    def test_absolute_path_is_refused(self):
        with self.assertRaises(local.LocalError):
            local.resolve(self.config, 0, str(self.outside / "cookies.sqlite"))

    def test_unknown_root_is_refused(self):
        for root in (1, -1, "x", None):
            with self.subTest(root=root), self.assertRaises(local.LocalError):
                local.resolve(self.config, root, "")

    @needs_symlink
    def test_link_to_outside_is_refused(self):
        os.symlink(str(self.outside), str(self.imports / "escape"), target_is_directory=True)
        with self.assertRaises(local.LocalError):
            local.resolve(self.config, 0, "escape/cookies.sqlite")
        with self.assertRaises(webui.UiError):
            self.backend.local_browse("0", "escape", "source")

    def test_browse_marks_archives_and_images(self):
        (self.imports / "game.zip").write_bytes(b"")
        (self.imports / "cover.png").write_bytes(PNG)
        (self.imports / "readme.txt").write_text("x", encoding="utf-8")
        (self.imports / "dir").mkdir()

        source = {f["name"]: f["selectable"] for f in self.backend.local_browse("0", "", "source")["files"]}
        self.assertEqual(source, {"cover.png": False, "game.zip": True, "readme.txt": False})
        image = {f["name"]: f["selectable"] for f in self.backend.local_browse("0", "", "image")["files"]}
        self.assertEqual(image, {"cover.png": True, "game.zip": False, "readme.txt": False})
        self.assertEqual(
            [d["name"] for d in self.backend.local_browse("0", "", "source")["directories"]],
            ["dir"])


class ArchivePartsTest(LocalTestCase):
    def touch(self, *names: str) -> None:
        for name in names:
            (self.imports / name).write_bytes(b"x")

    def names(self, first: str) -> list[str]:
        return [path.name for path in local.archive_parts(self.imports / first)]

    def test_numbered_parts(self):
        self.touch("g.7z.001", "g.7z.002", "g.7z.003", "other.7z.001")
        self.assertEqual(self.names("g.7z.001"), ["g.7z.001", "g.7z.002", "g.7z.003"])

    def test_rar_parts(self):
        self.touch("g.part1.rar", "g.part2.rar", "gg.part2.rar")
        self.assertEqual(self.names("g.part1.rar"), ["g.part1.rar", "g.part2.rar"])

    def test_split_zip(self):
        self.touch("g.zip", "g.z01", "g.z02", "h.z01")
        self.assertEqual(self.names("g.zip"), ["g.zip", "g.z01", "g.z02"])

    def test_single_archive(self):
        self.touch("g.7z", "g.7z.bak")
        self.assertEqual(self.names("g.7z"), ["g.7z"])

    def test_middle_part_is_refused(self):
        self.assertIsNotNone(local.first_part_problem(Path("g.7z.002")))
        self.assertIsNotNone(local.first_part_problem(Path("g.part3.rar")))
        self.assertIsNone(local.first_part_problem(Path("g.7z.001")))
        self.assertIsNone(local.first_part_problem(Path("g.part1.rar")))
        self.assertIsNone(local.first_part_problem(Path("g.zip")))

    def test_plan(self):
        self.touch("single.zip", "split.zip", "split.z01", "g.7z")
        self.assertEqual(local.archive_plan(self.imports / "single.zip").kind, "zip")
        self.assertEqual(local.archive_plan(self.imports / "split.zip").kind, "external")
        self.assertEqual(local.archive_plan(self.imports / "g.7z").kind, "external")


class DirectoryNameTest(LocalTestCase):
    def test_title_keeps_japanese_and_replaces_forbidden(self):
        self.assertEqual(local.title_directory_name("ゲーム: 第1章/前編"), "ゲーム_ 第1章_前編")

    def test_custom_name_is_checked(self):
        for bad in ("", "..", "a/b", "a\\b", "a:b", "name."):
            with self.subTest(bad=bad), self.assertRaises(local.LocalError):
                local.check_directory_name(bad)
        self.assertEqual(local.check_directory_name(" my_game "), "my_game")

    def test_modes(self):
        self.assertEqual(local.directory_name("title", "Game", "", self.config), "Game")
        self.assertEqual(local.directory_name("custom", "Game", "x", self.config), "x")
        self.assertTrue(local.directory_name("romaji", "げーむ", "", self.config))
        with self.assertRaises(local.LocalError):
            local.directory_name("", "Game", "", self.config)


class ImportTest(LocalTestCase):
    def test_zip_is_extracted_and_recorded(self):
        self.make_zip("game.zip", {"Game/Game.exe": "exe", "Game/data.bin": "d"})
        entry = self.run_import(path="game.zip")

        self.assertEqual(entry.id, "LOCAL-0001")
        self.assertTrue(entry.is_local)
        self.assertEqual(entry.source_kind, "archive")
        self.assertEqual(entry.compat_tool, "proton_experimental")
        # 単一のディレクトリは引き上げる (ライブラリの作品と同じ扱い)
        self.assertTrue((self.games / "test_game" / "Game.exe").is_file())
        self.assertEqual(Path(entry.executable).name, "Game.exe")
        # 取り込み元のアーカイブは残す (削除は利用者が選ぶ)
        self.assertTrue((self.imports / "game.zip").is_file())

    def test_directory_is_copied(self):
        source = self.imports / "MyGame"
        (source / "sub").mkdir(parents=True)
        (source / "Game.exe").write_text("exe", encoding="utf-8")
        (source / "sub" / "a.dat").write_text("a", encoding="utf-8")

        entry = self.run_import(path="MyGame", name_mode="title", title="My Game")
        target = self.games / "My Game"
        self.assertEqual(entry.directory, str(target))
        self.assertEqual(entry.source_kind, "directory")
        self.assertTrue((target / "sub" / "a.dat").is_file())
        # 元のディレクトリはそのまま
        self.assertTrue((source / "Game.exe").is_file())

    def test_ids_count_up(self):
        self.make_zip("a.zip", {"a.exe": "x"})
        self.make_zip("b.zip", {"b.exe": "x"})
        first = self.run_import(path="a.zip", name="a")
        second = self.run_import(path="b.zip", name="b")
        self.assertEqual((first.id, second.id), ("LOCAL-0001", "LOCAL-0002"))

    def test_existing_directory_is_refused(self):
        self.make_zip("game.zip", {"a.exe": "x"})
        (self.games / "test_game").mkdir()
        with self.assertRaises(webui.UiError):
            self.backend.start_local_import({
                "title": "t", "name_mode": "custom", "name": "test_game",
                "root": 0, "path": "game.zip",
            })

    def test_import_root_itself_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.backend.start_local_import({
                "title": "t", "name_mode": "custom", "name": "n", "root": 0, "path": "",
            })

    def test_non_archive_file_is_refused(self):
        (self.imports / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(webui.UiError):
            self.backend.start_local_import({
                "title": "t", "name_mode": "custom", "name": "n", "root": 0, "path": "notes.txt",
            })

    def test_outside_source_is_refused(self):
        with self.assertRaises(webui.UiError):
            self.backend.start_local_import({
                "title": "t", "name_mode": "custom", "name": "n",
                "root": 0, "path": "../outside",
            })
        self.assertFalse((self.games / "n").exists())

    def test_unknown_install_dir_is_refused(self):
        self.make_zip("game.zip", {"a.exe": "x"})
        with self.assertRaises(webui.UiError):
            self.backend.start_local_import({
                "title": "t", "name_mode": "custom", "name": "n", "root": 0,
                "path": "game.zip", "install_dir": str(self.outside),
            })

    def test_unknown_compat_tool_is_refused(self):
        self.make_zip("game.zip", {"a.exe": "x"})
        with self.assertRaises(webui.UiError):
            self.backend.start_local_import({
                "title": "t", "name_mode": "custom", "name": "n", "root": 0,
                "path": "game.zip", "compat_tool": "evil",
            })

    def test_broken_archive_leaves_nothing(self):
        (self.imports / "broken.zip").write_bytes(b"not a zip")
        started = self.backend.start_local_import({
            "title": "t", "name_mode": "custom", "name": "broken",
            "root": 0, "path": "broken.zip",
        })
        self.backend._worker.join(timeout=30)
        job = self.backend.jobs_json()["jobs"][0]
        self.assertEqual(job["status"], "error")
        self.assertFalse((self.games / "broken").exists(), "作りかけが残った")
        self.assertIsNone(self.backend.state().get(started["work_id"]))

    @needs_symlink
    def test_links_to_outside_are_dropped_when_copying(self):
        source = self.imports / "MyGame"
        source.mkdir()
        (source / "Game.exe").write_text("exe", encoding="utf-8")
        os.symlink(str(self.outside / "cookies.sqlite"), str(source / "steal"))
        os.symlink("Game.exe", str(source / "alias.exe"))

        self.run_import(path="MyGame")
        target = self.games / "test_game"
        self.assertFalse(os.path.lexists(target / "steal"), "外を指すリンクを持ち込んだ")
        self.assertTrue((target / "alias.exe").is_symlink(), "中を指すリンクは残す")


class LocalListTest(LocalTestCase):
    def test_lists_only_local_works_without_dlsite(self):
        self.make_zip("game.zip", {"a.exe": "x"})
        self.run_import(path="game.zip")
        current = self.backend.state()
        current.works["RJ1"] = state.InstalledWork(
            id="RJ1", title="DLsite の作品", directory=str(self.games / "rj1"))
        current.save()

        result = self.backend.local_json()
        self.assertEqual([item["id"] for item in result["items"]], ["LOCAL-0001"])
        item = result["items"][0]
        self.assertTrue(item["archive_exists"])
        self.assertTrue(item["archive_deletable"])
        self.assertEqual(result["roots"][0]["path"], str(self.imports))

    def test_grid_image_does_not_ask_dlsite(self):
        entry = state.InstalledWork(
            id="LOCAL-0001", title="t", directory=str(self.games / "x"),
            origin=state.ORIGIN_LOCAL)
        self.assertIsNone(self.backend.grid_image(entry))


class ArchiveDeleteTest(LocalTestCase):
    def test_parts_are_deleted(self):
        self.make_zip("game.zip", {"a.exe": "x"})
        entry = self.run_import(path="game.zip")
        # 分割 ZIP の続きがあるとみなす
        (self.imports / "game.z01").write_bytes(b"x" * 10)
        (self.imports / "keep.zip").write_bytes(b"x")

        result = self.backend.delete_local_archive(entry.id)
        self.assertEqual(len(result["removed"]), 2)
        self.assertFalse((self.imports / "game.zip").exists())
        self.assertFalse((self.imports / "game.z01").exists())
        self.assertTrue((self.imports / "keep.zip").exists(), "別のアーカイブを消した")
        # ゲームは残る
        self.assertTrue((self.games / "test_game" / "a.exe").is_file())

    def test_outside_import_dirs_is_not_deleted(self):
        """設定から外した取り込み元のアーカイブは消さない。"""
        self.make_zip("game.zip", {"a.exe": "x"})
        entry = self.run_import(path="game.zip")
        self.config.import_dirs = [str(self.outside)]

        with self.assertRaises(webui.UiError):
            self.backend.delete_local_archive(entry.id)
        self.assertTrue((self.imports / "game.zip").exists())

    def test_recorded_source_outside_is_not_deleted(self):
        """記録を書き換えられても、取り込み元の外は消さない。"""
        self.make_zip("game.zip", {"a.exe": "x"})
        entry = self.run_import(path="game.zip")
        current = self.backend.state()
        current.works[entry.id].source = str(self.outside / "cookies.sqlite")
        current.save()

        with self.assertRaises(webui.UiError):
            self.backend.delete_local_archive(entry.id)
        self.assertTrue((self.outside / "cookies.sqlite").exists())

    def test_dlsite_work_is_refused(self):
        current = self.backend.state()
        current.works["RJ1"] = state.InstalledWork(
            id="RJ1", title="t", directory=str(self.games / "rj1"),
            source=str(self.imports / "game.zip"), source_kind="archive")
        current.save()
        (self.imports / "game.zip").write_bytes(b"x")
        with self.assertRaises(webui.UiError):
            self.backend.delete_local_archive("RJ1")
        self.assertTrue((self.imports / "game.zip").exists())

    def test_directory_source_is_refused(self):
        (self.imports / "MyGame").mkdir()
        (self.imports / "MyGame" / "a.exe").write_text("x", encoding="utf-8")
        entry = self.run_import(path="MyGame")
        with self.assertRaises(webui.UiError):
            self.backend.delete_local_archive(entry.id)
        self.assertTrue((self.imports / "MyGame").is_dir())


class LocalImageTest(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.make_zip("game.zip", {"a.exe": "x"})
        self.entry = self.run_import(path="game.zip")

    def test_image_is_stored(self):
        (self.imports / "cover.png").write_bytes(PNG)
        self.backend.set_local_image(self.entry.id, "0", "cover.png")
        self.assertEqual(cover.cached_image(self.entry.path), PNG)
        self.assertTrue(self.backend.local_json()["items"][0]["has_image"])

    def test_not_an_image_is_refused(self):
        (self.imports / "fake.png").write_bytes(b"not an image")
        with self.assertRaises(webui.UiError):
            self.backend.set_local_image(self.entry.id, "0", "fake.png")

    def test_outside_image_is_refused(self):
        (self.outside / "cover.png").write_bytes(PNG)
        with self.assertRaises(webui.UiError):
            self.backend.set_local_image(self.entry.id, "0", "../outside/cover.png")
        self.assertIsNone(cover.cached_image(self.entry.path))


class CompatToolTest(LocalTestCase):
    def test_chosen_tool_is_used_when_registering(self):
        self.make_zip("game.zip", {"a.exe": "x"})
        entry = self.run_import(path="game.zip", compat_tool="GE-Proton10-10")

        with mock.patch.object(webui.install, "register_to_steam") as register, \
                mock.patch.object(webui.Backend, "_userdata", return_value=self.root), \
                mock.patch.object(webui.Backend, "_require_steam_stopped"), \
                mock.patch.object(webui.Backend, "_launch_options_for", return_value=("", "config")), \
                mock.patch.object(webui.steam, "unregister"):
            register.return_value = mock.Mock(app_id=1, created=True, updated=[], backups=[], images=[])
            self.backend.register_steam(entry.id, entry.executable, "テスト")
        self.assertEqual(register.call_args.kwargs["compat_tool"], "GE-Proton10-10")

    def test_dlsite_work_uses_default(self):
        """DLsite の作品は今までどおり設定の既定値 (None を渡して install 側で既定値)。"""
        target = self.games / "rj"
        target.mkdir()
        (target / "a.exe").write_text("x", encoding="utf-8")
        current = self.backend.state()
        current.works["RJ1"] = state.InstalledWork(id="RJ1", title="t", directory=str(target))
        current.save()

        with mock.patch.object(webui.install, "register_to_steam") as register, \
                mock.patch.object(webui.Backend, "_userdata", return_value=self.root), \
                mock.patch.object(webui.Backend, "_require_steam_stopped"), \
                mock.patch.object(webui.Backend, "grid_image", return_value=None), \
                mock.patch.object(webui.Backend, "_launch_options_for", return_value=("", "config")), \
                mock.patch.object(webui.steam, "unregister"):
            register.return_value = mock.Mock(app_id=1, created=True, updated=[], backups=[], images=[])
            self.backend.register_steam("RJ1", str(target / "a.exe"), "t")
        self.assertIsNone(register.call_args.kwargs["compat_tool"])


class StateTest(unittest.TestCase):
    def test_next_local_id(self):
        current = state.State(Path("unused.json"))
        self.assertEqual(current.next_local_id(), "LOCAL-0001")
        current.works["LOCAL-0007"] = state.InstalledWork(id="LOCAL-0007", title="", directory="")
        current.works["RJ1"] = state.InstalledWork(id="RJ1", title="", directory="")
        self.assertEqual(current.next_local_id(), "LOCAL-0008")
        self.assertEqual(current.next_local_id(["LOCAL-0009"]), "LOCAL-0010")

    def test_old_records_are_dlsite(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            path.write_text(
                '{"version": 1, "works": [{"id": "RJ1", "title": "t", "directory": "d"}]}',
                encoding="utf-8")
            entry = state.State.load(path).get("RJ1")
        self.assertEqual(entry.origin, state.ORIGIN_DLSITE)
        self.assertFalse(entry.is_local)


class ConfigTest(unittest.TestCase):
    def test_import_dirs_can_be_changed_from_the_screen(self):
        self.assertNotIn("import_dirs", webui.LOCKED_CONFIG_KEYS)
        self.assertNotIn("runtime_dirs", webui.LOCKED_CONFIG_KEYS)

    def test_import_dirs_do_not_need_terminal_approval(self):
        """画面で足した場所に承認を求めると、端末の無い起動でツールが起動しなくなる。"""
        cfg = config_module.Config()
        cfg.import_dirs = [str(Path(tempfile.gettempdir()) / "dlsite-import-test")]
        self.assertNotIn(Path(cfg.import_dirs[0]), config_module.external_paths(cfg))


if __name__ == "__main__":
    unittest.main()
