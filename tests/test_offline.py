"""ネットワークに触れずに検証できる部分のテスト。

``py -m unittest discover -s tests`` (Windows) / ``python3 -m unittest discover -s tests``
で実行する。
"""

from __future__ import annotations

import email.message
import io
import json
import threading
import time
import socket
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dlsite_deck import (  # noqa: E402
    api,
    archive,
    category,
    config,
    cookies,
    download,
    romaji,
    state,
    steam,
    webui,
)


#: テスト用の最小の画像 (1x1)
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
JPEG_1PX = b"\xff\xd8\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xd9"


class RomajiTest(unittest.TestCase):
    def test_hepburn_consonants(self):
        cases = {
            "しんぶん": "shimbun",       # ん + b は m
            "せんぱい": "sempai",
            "しんや": "shin'ya",         # ん + y は n'
            "きんえん": "kin'en",        # ん + 母音 も n'
            "あんない": "annai",         # ん + 子音 は素の n
            "まっちゃ": "matcha",        # っ + ch は t
            "がっこう": "gakkou",
            "つづく": "tsuzuku",
            "ふじさん": "fujisan",
            "きょうと": "kyouto",
        }
        for kana, expected in cases.items():
            with self.subTest(kana=kana):
                self.assertEqual(romaji.kana_to_romaji(kana), expected)

    def test_katakana_and_long_vowels(self):
        self.assertEqual(romaji.kana_to_romaji("スーパー"), "suupaa")
        self.assertEqual(romaji.kana_to_romaji("スーパー", long_vowel="drop"), "supa")
        self.assertEqual(romaji.kana_to_romaji("ヴァンパイア"), "vampaia")
        self.assertEqual(romaji.kana_to_romaji("ジェットコースター"), "jettokoosutaa")

    # pykakasi は任意依存なので、入っている環境と入っていない環境で結果が変わる。
    # どちらの経路も明示的に作って、環境に依存しないようにする。

    def _with_pykakasi(self, result):
        """``_pykakasi_romanize`` の戻り値を差し替える。"""
        original = romaji._pykakasi_romanize
        romaji._pykakasi_romanize = lambda text: result
        self.addCleanup(setattr, romaji, "_pykakasi_romanize", original)

    def test_pykakasi_is_preferred_when_available(self):
        # 要件はヘボン式ローマ字なので、英語タイトルより読みの変換を優先する
        self._with_pykakasi("mahou shoujo ririka")
        name = romaji.directory_name("魔法少女リリカ", "Magical Girl Lyrica", "RJ111111")
        self.assertEqual(name, "mahou_shoujo_ririka")

    def test_english_title_used_when_pykakasi_absent(self):
        self._with_pykakasi(None)
        name = romaji.directory_name("魔法少女リリカ", "Magical Girl Lyrica", "RJ111111")
        self.assertEqual(name, "Magical_Girl_Lyrica")

    def test_kanji_without_english_falls_back_to_id(self):
        self._with_pykakasi(None)
        result = romaji.romanize_work("魔法少女", None, "RJ222222")
        self.assertEqual(result.source, "fallback")
        self.assertIn("RJ222222", result.text)

    def test_sanitize_strips_unsafe_characters(self):
        self.assertEqual(romaji.sanitize('a/b:c*d?e"f'), "a_b_c_d_e_f")
        self.assertEqual(romaji.sanitize("  ...trim...  "), "trim")


class VersionStripTest(unittest.TestCase):
    def test_marked_versions(self):
        cases = {
            "MyGame_ver1.05.exe": "MyGame.exe",
            "MyGame v2.exe": "MyGame.exe",
            "MyGame Ver.1.2.3.exe": "MyGame.exe",
            "MyGame(ver1.0).exe": "MyGame.exe",
            "MyGame_v1.0a.exe": "MyGame.exe",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(archive.strip_version(name), expected)

    def test_bare_trailing_version(self):
        self.assertEqual(archive.strip_version("MyGame_1.05.exe"), "MyGame.exe")
        # ドットの無い数字は版番号と断定できないので残す
        self.assertEqual(archive.strip_version("MyGame_2.exe"), "MyGame_2.exe")

    def test_directory_names(self):
        self.assertEqual(archive.strip_version("MyGame_ver1.05", is_dir=True), "MyGame")
        self.assertEqual(archive.strip_version("MyGame", is_dir=True), "MyGame")

    def test_never_empties_the_name(self):
        self.assertEqual(archive.strip_version("ver1.0.exe"), "ver1.0.exe")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _make_zip(self, name: str, entries: dict[str, bytes]) -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as zip_file:
            for entry_name, payload in entries.items():
                zip_file.writestr(entry_name, payload)
        return path

    def test_plan_single_zip(self):
        archive_path = self._make_zip("RJ1.zip", {"a.txt": b"x"})
        plan = archive.plan_archive([archive_path])
        self.assertEqual(plan.kind, "zip")

    def test_plan_split_rar_orders_parts(self):
        names = ["RJ1.part3.bin", "RJ1.exe", "RJ1.part2.bin"]
        paths = []
        for name in names:
            path = self.root / name
            path.write_bytes(b"x")
            paths.append(path)

        plan = archive.plan_archive(paths)
        self.assertEqual(plan.kind, "split_rar")
        self.assertEqual(plan.files[0].name, "RJ1.exe")

    def test_split_parts_sort_numerically(self):
        # 実際に DLsite が返す名前。名前順だと part10 が part2 より前に来てしまう。
        names = [
            "RJ200001.part10.rar",
            "RJ200001.part2.rar",
            "RJ200001.part1.exe",
            "RJ200001.part3.rar",
        ]
        paths = []
        for name in names:
            path = self.root / name
            path.write_bytes(b"x")
            paths.append(path)

        plan = archive.plan_archive(paths)
        self.assertEqual(
            [path.name for path in plan.files],
            [
                "RJ200001.part1.exe",
                "RJ200001.part2.rar",
                "RJ200001.part3.rar",
                "RJ200001.part10.rar",
            ],
        )

    def test_split_parts_sort_classic_rar_volumes(self):
        names = ["RJ1.r01", "RJ1.exe", "RJ1.r00", "RJ1.r10"]
        paths = []
        for name in names:
            path = self.root / name
            path.write_bytes(b"x")
            paths.append(path)

        plan = archive.plan_archive(paths)
        self.assertEqual(
            [path.name for path in plan.files], ["RJ1.exe", "RJ1.r00", "RJ1.r01", "RJ1.r10"]
        )

    def test_extract_flattens_single_root(self):
        archive_path = self._make_zip(
            "RJ1.zip",
            {"GameFolder/game_ver1.05.exe": b"MZ", "GameFolder/data/x.dat": b"d"},
        )
        output = self.root / "out"
        result = archive.extract(archive.plan_archive([archive_path]), output)

        self.assertTrue(result.flattened)
        # 二重の入れ子になっていないこと
        self.assertFalse((output / "GameFolder").exists())
        self.assertTrue((output / "data" / "x.dat").is_file())
        # バージョン番号が除去されていること
        self.assertTrue((output / "game.exe").is_file())

    def test_extract_keeps_multiple_roots(self):
        archive_path = self._make_zip("RJ2.zip", {"game.exe": b"MZ", "readme.txt": b"r"})
        output = self.root / "out2"
        result = archive.extract(archive.plan_archive([archive_path]), output)

        self.assertFalse(result.flattened)
        self.assertTrue((output / "game.exe").is_file())
        self.assertTrue((output / "readme.txt").is_file())

    def test_rejects_paths_pointing_outside(self):
        # 親参照・絶対パス・ドライブ指定はいずれも受け付けない
        for index, entry in enumerate(
            ("../escaped.txt", "a/../../escaped.txt", "/etc/passwd", "C:\\Windows\\evil.txt")
        ):
            with self.subTest(entry=entry):
                path = self.root / f"evil{index}.zip"
                with zipfile.ZipFile(path, "w") as zip_file:
                    zip_file.writestr(entry, b"x")

                with self.assertRaises(archive.ArchiveError):
                    archive.extract(archive.plan_archive([path]), self.root / f"outbad{index}")

    def test_accepts_windows_backslash_entries(self):
        # Windows で作られた ZIP は区切りがバックスラッシュのことがある
        path = self.root / "win.zip"
        with zipfile.ZipFile(path, "w") as zip_file:
            zip_file.writestr("GameDir\\sub\\data.dat", b"x")

        output = self.root / "outwin"
        archive.extract(archive.plan_archive([path]), output)
        self.assertTrue((output / "data.dat").is_file())

    def test_decodes_cp932_entry_names(self):
        # UTF-8 フラグの無い ZIP を zipfile は cp437 として読む。国内製アーカイブは
        # 実際には cp932 なので、読み直せていることを確認する。
        info = zipfile.ZipInfo()
        info.flag_bits &= ~0x800
        info.filename = "ゲーム/実行.txt".encode("cp932").decode("cp437")
        self.assertEqual(archive._decode_zip_name(info), "ゲーム/実行.txt")

        # UTF-8 フラグがある場合はそのまま使う
        utf8_info = zipfile.ZipInfo("ゲーム/実行.txt")
        utf8_info.flag_bits |= 0x800
        self.assertEqual(archive._decode_zip_name(utf8_info), "ゲーム/実行.txt")

    def test_unflagged_utf8_entry_names(self):
        # フラグを立てずに UTF-8 で書く作成ツールもある。cp932 を先に試すと
        # ほとんどのバイト列が「読めてしまう」ため化ける。
        info = zipfile.ZipInfo()
        info.flag_bits &= ~0x800
        info.filename = "ゲーム/実行.txt".encode("utf-8").decode("cp437")
        self.assertEqual(archive._decode_zip_name(info), "ゲーム/実行.txt")

    def test_real_dlsite_zip_names_are_cp932(self):
        # 実際の DLsite の ZIP はフラグ無しの cp932 だった（実物で確認済み）。
        # UTF-8 としては不正なので、cp932 へのフォールバックが効く。
        original = "作品名_v2/Game/"
        raw = original.encode("cp932")
        with self.assertRaises(UnicodeDecodeError):
            raw.decode("utf-8")

        info = zipfile.ZipInfo()
        info.flag_bits &= ~0x800
        info.filename = raw.decode("cp437")
        self.assertEqual(archive._decode_zip_name(info), original)

    def test_find_executables_prefers_shallow_non_installer(self):
        base = self.root / "game"
        (base / "tools").mkdir(parents=True)
        (base / "Game.exe").write_bytes(b"M" * 5000)
        (base / "uninstall.exe").write_bytes(b"M" * 9000)
        (base / "tools" / "editor.exe").write_bytes(b"M" * 9000)

        found = archive.find_executables(base)
        self.assertEqual(found[0].name, "Game.exe")


class CategoryTest(unittest.TestCase):
    def test_game_types(self):
        for work_type in ("RPG", "SLN", "ADV", "ACN", "PZL", "TBL", "STG"):
            with self.subTest(work_type=work_type):
                self.assertEqual(category.classify(work_type), category.GAME)

    def test_non_game_types(self):
        self.assertEqual(category.classify("MNG"), category.MANGA)
        self.assertEqual(category.classify("ICG"), category.CG)
        self.assertEqual(category.classify("MOV"), category.VIDEO)
        self.assertEqual(category.classify("MUS"), category.MUSIC)
        self.assertEqual(category.classify("SOU"), category.VOICE)

    def test_etc_is_not_a_game(self):
        # 実ライブラリでは ETC がイラスト集に使われていた。
        # ゲーム扱いにすると既定の一覧に紛れ込む。
        self.assertEqual(category.classify("ETC", "IME"), category.CG)
        self.assertEqual(category.classify("ETC", "ET1"), category.CG)
        self.assertEqual(category.classify("ETC", None), category.OTHER)
        self.assertNotEqual(category.classify("ETC", "IME"), category.GAME)

    def test_work_exposes_category(self):
        work = api._parse_work(
            {
                "workno": "VJ1",
                "name": {"ja_JP": "イラスト集"},
                "maker": {"name": {}},
                "work_type": "ETC",
                "file_type": "IME",
            }
        )
        self.assertEqual(work.category, category.CG)
        self.assertEqual(work.category_label, "CG・イラスト")
        self.assertFalse(work.is_game)

    def test_unknown_type_falls_back(self):
        self.assertEqual(category.classify("ZZZ"), category.OTHER)
        self.assertEqual(category.classify(None), category.OTHER)


class ExecutableCandidateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_candidates_are_ranked_and_annotated(self):
        (self.root / "Game").mkdir()
        (self.root / "Game" / "痴　漢　電　車.exe").write_bytes(b"M" * 600)
        (self.root / "Game" / "UnityCrashHandler32.exe").write_bytes(b"M" * 9000)
        (self.root / "Config.exe").write_bytes(b"M" * 300)

        found = archive.executable_candidates(self.root)
        names = [item.relative for item in found]

        self.assertIn("Game/痴　漢　電　車.exe", names)
        # 除外語を含むものは後ろに回り、注意が付く
        self.assertFalse(found[0].suspicious)
        suspicious = {item.relative for item in found if item.suspicious}
        self.assertIn("Config.exe", suspicious)
        self.assertIn("Game/UnityCrashHandler32.exe", suspicious)

    def test_empty_directory(self):
        self.assertEqual(archive.executable_candidates(self.root), [])


class InstallDirsTest(unittest.TestCase):
    def test_default_is_added_to_the_list(self):
        cfg = config.Config(install_dir="/games/a", install_dirs=["/games/b"])
        self.assertEqual(cfg.install_dirs, ["/games/a", "/games/b"])

    def test_empty_list_falls_back_to_the_default(self):
        cfg = config.Config(install_dir="/games/a", install_dirs=[])
        self.assertEqual(cfg.install_dirs, ["/games/a"])

    def test_duplicates_and_blanks_are_dropped(self):
        cfg = config.Config(
            install_dir="/games/a", install_dirs=["/games/a", "  ", "/games/a", "/games/b"]
        )
        self.assertEqual(cfg.install_dirs, ["/games/a", "/games/b"])

    def test_resolve_accepts_only_configured_places(self):
        cfg = config.Config(install_dir="/games/a", install_dirs=["/games/a", "/games/b"])

        self.assertEqual(cfg.resolve_install_dir(None), Path("/games/a"))
        self.assertEqual(cfg.resolve_install_dir("/games/b"), Path("/games/b"))

        with self.assertRaises(ValueError):
            cfg.resolve_install_dir("/somewhere/else")

    def test_external_paths_covers_every_install_dir(self):
        inside = str(config.PROJECT_ROOT / "games")
        cfg = config.Config(
            download_dir=str(config.PROJECT_ROOT / "downloads"),
            state_file=str(config.PROJECT_ROOT / "state.json"),
            install_dir=inside,
            install_dirs=[inside, "/mnt/sdcard/games"],
        )
        external = [str(path) for path in config.external_paths(cfg)]
        self.assertEqual(external, [str(Path("/mnt/sdcard/games"))])

    def test_archives_are_deleted_by_default(self):
        self.assertTrue(config.Config().delete_archives)

    def test_acknowledged_paths_are_not_asked_again(self):
        # 端末の無い起動 (デスクトップや Steam から) では確認に答えられないので、
        # 一度承認したパスは訊き直さない
        outside = "/mnt/sdcard/games"
        cfg = config.Config(install_dir=outside, install_dirs=[outside])

        pending = config.unacknowledged_paths(cfg)
        self.assertIn(Path(outside), pending)

        config.acknowledge(cfg, pending)
        self.assertEqual(config.unacknowledged_paths(cfg), [])
        # 承認は蓄積され、重複しない
        # (記録される文字列は OS ごとの表記になるので、件数で確かめる)
        config.acknowledge(cfg, pending)
        self.assertEqual(len(cfg.acknowledged_paths), 1)

    def test_new_external_path_is_asked_even_if_others_acknowledged(self):
        first, second = "/mnt/a", "/mnt/b"
        cfg = config.Config(install_dir=first, install_dirs=[first])
        config.acknowledge(cfg, config.unacknowledged_paths(cfg))

        cfg.install_dirs = [first, second]
        self.assertEqual(config.unacknowledged_paths(cfg), [Path(second)])


class CacheCleanupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_discard_removes_files_and_empty_directory(self):
        cache = self.root / "RJ1"
        cache.mkdir()
        parts = []
        for name, payload in (("a.zip", b"x" * 100), ("b.bin", b"y" * 50)):
            path = cache / name
            path.write_bytes(payload)
            parts.append(path)

        freed = download.discard_cache(parts, cache)
        self.assertEqual(freed, 150)
        self.assertFalse(cache.exists())

    def test_keeps_directory_when_something_remains(self):
        cache = self.root / "RJ2"
        cache.mkdir()
        archive_path = cache / "a.zip"
        archive_path.write_bytes(b"x")
        (cache / "leftover.part").write_bytes(b"z")

        download.discard_cache([archive_path], cache)
        self.assertTrue(cache.is_dir(), "取り残しがある場合は消さない")
        self.assertFalse(archive_path.exists())

    def test_missing_files_are_tolerated(self):
        self.assertEqual(download.discard_cache([self.root / "nope.zip"], None), 0)


class WebUiStartupTest(unittest.TestCase):
    """起動前の確認。塞がっているときに例外を投げず、理由を伝えて止まること。"""

    def _free_port(self) -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_free_port_is_detected(self):
        self.assertEqual(webui.probe_port("127.0.0.1", self._free_port()), "free")

    def test_recognises_our_own_server(self):
        from http.server import ThreadingHTTPServer

        backend = object()
        handler = type("H", (webui.Handler,), {"backend": backend})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        # 束縛は生成時に済んでいるが、応答を返せるようになるのは
        # serve_forever が回り始めてから。待たずに判定すると "other" になる。
        result = "free"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            result = webui.probe_port("127.0.0.1", port)
            if result == "ours":
                break
            time.sleep(0.05)

        self.assertEqual(result, "ours")

    def test_recognises_a_stranger(self):
        # HTTP ですらない待ち受けは "other" として扱う
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        self.assertEqual(webui.probe_port("127.0.0.1", port, timeout=0.5), "other")

    def test_serve_refuses_when_port_is_taken(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        self.addCleanup(listener.close)

        # 例外ではなく終了コードで知らせる
        code = webui.serve(config.Config(), host="127.0.0.1", port=port, open_browser=False)
        self.assertEqual(code, 1)

    def test_finds_no_instance_of_itself(self):
        # 自分自身も、自分を起動した親プロセスも数えない
        found = webui.find_other_instances()
        skip = webui._ancestors(os.getpid())
        self.assertFalse([pid for pid, _ in found if pid in skip])

    def test_identifies_serve_processes_precisely(self):
        # 本物
        self.assertTrue(
            webui._is_serve_process(["python3", "-m", "dlsite_deck", "serve", "--port", "8765"])
        )
        self.assertTrue(
            webui._is_serve_process(["python3", "/opt/dlsite_deck/__main__.py", "serve"])
        )

        # timeout などの起動ラッパーは、実際に serve を動かしているので本物扱い。
        # 自分の親である場合は祖先の除外で弾く。
        self.assertTrue(
            webui._is_serve_process(["timeout", "6", "python3", "-m", "dlsite_deck", "serve"])
        )

        # 文字列が混ざっているだけのものは拾わない。
        # (これを拾うと、判定を起動したシェル自身まで別インスタンス扱いになる)
        for args in (
            ["bash", "-c", "cd dlsite_deck && python3 -m dlsite_deck serve"],
            ["python3", "-m", "dlsite_deck", "check"],
            ["grep", "-r", "serve", "dlsite_deck"],
            ["python3", "-m", "other_tool", "serve"],
            ["ssh", "deck", "cd dlsite_deck && cp serve.py ."],
        ):
            with self.subTest(args=args):
                self.assertFalse(webui._is_serve_process(args))


class WebUiHostTest(unittest.TestCase):
    def test_recognises_loopback(self):
        for host in ("127.0.0.1", "localhost", "::1", "127.0.1.1", ""):
            with self.subTest(host=host):
                self.assertTrue(webui._is_loopback(host))

    def test_recognises_external(self):
        # 外部に開く指定は警告の対象。認証機構が無いため。
        for host in ("0.0.0.0", "192.168.1.10", "::", "example.local"):
            with self.subTest(host=host):
                self.assertFalse(webui._is_loopback(host))


class CookieTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_filters_to_dlsite_and_drops_expired(self):
        path = self.root / "cookies.json"
        path.write_text(
            json.dumps(
                [
                    {"domain": ".dlsite.com", "name": "keep", "value": "1"},
                    {"domain": "play.dlsite.com", "name": "play", "value": "2"},
                    {"domain": "example.com", "name": "drop", "value": "3"},
                    {
                        "domain": ".dlsite.com",
                        "name": "expired",
                        "value": "4",
                        "expirationDate": 1,
                    },
                    # ドメイン末尾が紛らわしいものは弾く
                    {"domain": "notdlsite.com", "name": "drop2", "value": "5"},
                ]
            ),
            encoding="utf-8",
        )

        found = cookies.load_manual(path)
        self.assertEqual({cookie.name for cookie in found}, {"keep", "play"})

    def test_play_session_is_not_borrowed(self):
        # ブラウザが持つ play_session を持ち込むとダウンロード API が 500 になる。
        # こちらは OAuth チェーンで自前のセッションを張るので取り込まない。
        path = self.root / "cookies.json"
        path.write_text(
            json.dumps(
                [
                    {"domain": "play.dlsite.com", "name": "play_session", "value": "stale"},
                    {"domain": "play.dl.dlsite.com", "name": "play_session", "value": "stale"},
                    {"domain": ".dlsite.com", "name": "__DLsite_SID", "value": "keep"},
                ]
            ),
            encoding="utf-8",
        )

        found = cookies.load_manual(path)
        self.assertEqual([cookie.name for cookie in found], ["__DLsite_SID"])

    def test_netscape_format_with_httponly_prefix(self):
        path = self.root / "cookies.txt"
        path.write_text(
            "# Netscape HTTP Cookie File\n"
            "#HttpOnly_.dlsite.com\tTRUE\t/\tTRUE\t0\tsession\tabc\n"
            ".example.com\tTRUE\t/\tTRUE\t0\tother\txyz\n",
            encoding="utf-8",
        )

        found = cookies.load_manual(path)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "session")
        self.assertTrue(found[0].http_only)

    def test_cookiejar_conversion(self):
        raw = [
            cookies.RawCookie(".dlsite.com", "a", "1", "/", None, True, True),
            cookies.RawCookie("play.dlsite.com", "b", "2", "/", None, True, False),
        ]
        jar = cookies.to_cookiejar(raw)
        self.assertEqual({cookie.name for cookie in jar}, {"a", "b"})


class ApiParsingTest(unittest.TestCase):
    def test_classify_download_location(self):
        cases = {
            "https://www.dlsite.com/home/download/=/product_id/RJ1.html": "direct",
            "https://www.dlsite.com/home/download/split/=/product_id/RJ1.html": "split",
            "https://www.dlsite.com/home/split/=/product_id/RJ1.html": "split",
            "https://www.dlsite.com/home/serial/=/product_id/RJ1.html": "serial",
            "https://www.dlsite.com/home/download/serial/=/product_id/RJ1.html": "serial",
            "https://example.com/whatever": "unknown",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(api._classify_download_location(url), expected)

    def test_extract_download_links_and_part_numbers(self):
        page = "https://www.dlsite.com/home/download/split/=/product_id/RJ1.html"
        body = """
            <a href="https://www.dlsite.com/home/download/=/number/2/product_id/RJ1.html">2</a>
            <a href="https://www.dlsite.com/home/download/=/number/1/product_id/RJ1.html">1</a>
            <a href="https://example.com/other">x</a>
        """
        links = api._extract_download_links(page, body)
        self.assertEqual(len(links), 2)
        self.assertEqual(sorted(api._download_part_number(url) for url in links), [1, 2])

    def test_extract_download_links_survives_unbalanced_quotes(self):
        # 実ページは JavaScript を大量に含み、引用符が釣り合っていない。
        # 単純な引用符の総当たりだと href をまたいでリンクを取り逃がす。
        page = "https://www.dlsite.com/home/download/split/=/product_id/RJ200001.html"
        body = """
            <script>var s = 'it\\'s a trap "; if (a !== b.charAt(0)) { x = "unclosed;</script>
            <link rel="canonical" href="https://www.dlsite.com/home/download/split">
            <a href="https://www.dlsite.com/home/download/=/number/1/product_id/RJ200001.html">1</a>
            <script>var t = "another ' unbalanced;</script>
            <a href="https://www.dlsite.com/home/download/=/number/2/product_id/RJ200001.html">2</a>
            <a href="https://www.dlsite.com/home/download/split/=/product_id/RJ200001.html/?locale=en_US">en</a>
        """
        links = api._extract_download_links(page, body)

        self.assertEqual(
            sorted(api._download_part_number(url) for url in links if "number" in url),
            [1, 2],
        )
        # locale 付きの言語切り替えリンクは拾わない
        self.assertFalse([url for url in links if "locale" in url])

    def test_download_url_validation_rejects_script_fragments(self):
        page = "https://www.dlsite.com/home/download/split/=/product_id/RJ1.html"
        for fragment in (
            "!==e.charAt(0)&&(e=",
            "cc.hE:hide_element},getVersion:function(){return version}",
            "&& !(dlsite.isFemale())",
        ):
            with self.subTest(fragment=fragment):
                url = "https://www.dlsite.com/home/download/split/=/product_id/" + fragment
                self.assertIsNone(api._download_url_params(url))

        # 素のダウンロードリンクは通す
        self.assertIsNotNone(
            api._download_url_params(
                "https://www.dlsite.com/home/download/=/number/3/product_id/RJ1.html"
            )
        )
        self.assertIsNotNone(
            api._download_url_params(
                "https://www.dlsite.com/home/download/=/product_id/VJ12345.html"
            )
        )
        # クエリや別ホストは通さない
        self.assertIsNone(
            api._download_url_params(
                "https://www.dlsite.com/home/download/=/product_id/RJ1.html?locale=en_US"
            )
        )
        self.assertIsNone(
            api._download_url_params("https://evil.example/home/download/=/product_id/RJ1.html")
        )
        # "#" だけの href は urljoin の結果が Python の版で変わる
        # (3.8 は "#" を落としてページ自身の URL になり、3.10 以降は "#" が残る)。
        # どちらにせよ新しいダウンロード対象は生まれない、というのが本来の不変条件。
        links = api._extract_download_links(page, "<a href='#'>x</a>")
        self.assertTrue(all(url == page for url in links), links)

    def test_parse_serial_numbers(self):
        body = """
            <h2>シリアル番号</h2>
            <table>
              <tr><th>シリアルコード</th><td>ABCD-1234</td></tr>
              <tr><th>備考</th><td>無関係</td></tr>
            </table>
        """
        self.assertEqual(api._parse_serial_numbers(body), [("シリアルコード", "ABCD-1234")])

    def test_parses_dlsite_timestamps(self):
        # DLsite は "2023-04-27T15:00:00.000000Z" を返す。末尾の Z と秒未満を
        # 落とさないと Python 3.11 より前で読めず、更新検出が黙って働かなくなる。
        parsed = api._parse_datetime("2023-04-27T15:00:00.000000Z")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2023)
        self.assertEqual(parsed.hour, 15)
        self.assertIsNotNone(parsed.tzinfo)

        self.assertIsNotNone(api._parse_datetime("2026-06-23 10:57:46"))
        self.assertIsNotNone(api._parse_datetime("2020-08-14"))
        self.assertIsNone(api._parse_datetime(""))
        self.assertIsNone(api._parse_datetime(None))
        self.assertIsNone(api._parse_datetime("よくわからない値"))

    def test_dates_are_shown_in_japan_time(self):
        # DLsite は JST の 0 時を UTC 15:00 として返すので、
        # UTC のまま日付を出すと 1 日ずれる
        self.assertEqual(api.format_date("2023-04-27T15:00:00.000000Z"), "2023-04-28")
        self.assertEqual(api.format_date("2022-03-23T15:00:00.000000Z"), "2022-03-24")
        self.assertEqual(api.format_date("2026-06-23 10:57:46"), "2026-06-23")
        self.assertEqual(api.format_date(None), "")
        self.assertEqual(api.format_date("壊れた値"), "")

    def test_extracts_version_from_title(self):
        # 実際のライブラリにあった書き方
        cases = {
            "ダンジョンズレギオン-魔王に捧ぐ乙女の肢体- Ver.1.3.2": "1.3.2",
            "【Live2D】コン狐との日常+(ぷらす) withコンプデータ ver1.4逆バニー": "1.4",
            "なにか Version 2.0": "2.0",
            "タイトル ver 1.05a": "1.05a",
            "アンダーバー区切り ver1_2": "1.2",
        }
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(api.extract_version(title), expected)

    def test_does_not_invent_versions(self):
        # 目印のない数字を版と決めつけない
        for title in (
            "版の書かれていない題名",
            "続編のタイトル2",
            "題名 ～副題～",
            "1.2.3 だけの題名",
            "",
        ):
            with self.subTest(title=title):
                self.assertIsNone(api.extract_version(title))

    def test_takes_the_last_version_when_several(self):
        self.assertEqual(api.extract_version("Ver1.0 の続編 Ver2.0"), "2.0")

    def test_parse_work(self):
        work = api._parse_work(
            {
                "workno": "RJ123456",
                "name": {"ja_JP": "テスト作品", "en_US": "Test Work"},
                "maker": {"id": "RG1", "name": {"ja_JP": "サークル"}},
                "work_type": "RPG",
                "upgrade_date": "2026-05-01 12:00:00",
            }
        )
        self.assertEqual(work.id, "RJ123456")
        self.assertEqual(work.title, "テスト作品")
        self.assertEqual(work.english_title, "Test Work")
        self.assertTrue(work.is_game)
        self.assertIsNotNone(work.updated_at)


class DownloadRedirectTest(unittest.TestCase):
    """``open_stream`` のリダイレクト追従。

    自動追従を切ってあるため 3xx は ``HTTPError`` として上がる。これを
    リダイレクトとして扱えていないと、ダウンロードが 302 で必ず失敗する。
    """

    class _FakeOpener:
        def __init__(self, script):
            self.script = list(script)
            self.requests = []

        def open(self, request, timeout=None):
            self.requests.append(request)
            outcome = self.script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    class _FakeResponse:
        def __init__(self, status=200):
            self.status = status
            self.headers = {}

        def close(self):
            pass

    def _client(self, script):
        client = api.DlsiteClient(cookies.to_cookiejar([]))
        client._opener = self._FakeOpener(script)
        return client

    def _http_error(self, code, location=None):
        headers = email.message.Message()
        if location:
            headers["Location"] = location
        return urllib.error.HTTPError(
            "https://example.invalid/a", code, "moved", headers, io.BytesIO(b"")
        )

    def test_follows_redirects(self):
        final = self._FakeResponse(200)
        client = self._client(
            [
                self._http_error(302, "https://cdn.example.invalid/b"),
                self._http_error(302, "/c"),
                final,
            ]
        )

        result = client.open_stream("https://example.invalid/a")
        self.assertIs(result, final)
        self.assertEqual(
            [request.full_url for request in client._opener.requests],
            [
                "https://example.invalid/a",
                "https://cdn.example.invalid/b",
                "https://cdn.example.invalid/c",
            ],
        )

    def test_range_header_set_when_resuming(self):
        client = self._client([self._FakeResponse(206)])
        client.open_stream("https://example.invalid/a", start=1024)
        self.assertEqual(client._opener.requests[0].get_header("Range"), "bytes=1024-")

    def test_416_means_already_complete(self):
        client = self._client([self._http_error(416)])
        with self.assertRaises(api.AlreadyCompleteError):
            client.open_stream("https://example.invalid/a", start=99)

    def test_redirect_without_location_fails(self):
        client = self._client([self._http_error(302)])
        with self.assertRaises(api.DlsiteApiError):
            client.open_stream("https://example.invalid/a")

    def test_redirect_loop_gives_up(self):
        # 使い回すと閉じたレスポンスを再度開くことになるので毎回作り直す。
        # 使い切る数ちょうどにしないと未使用分が ResourceWarning になる。
        script = [
            self._http_error(302, "https://example.invalid/a")
            for _ in range(api.MAX_DOWNLOAD_REDIRECTS)
        ]
        client = self._client(script)
        with self.assertRaises(api.DlsiteApiError):
            client.open_stream("https://example.invalid/a")


class StateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _work(self, upgrade: str | None) -> api.Work:
        return api._parse_work(
            {
                "workno": "RJ1",
                "name": {"ja_JP": "作品"},
                "maker": {"name": {"ja_JP": "M"}},
                "work_type": "RPG",
                "upgrade_date": upgrade,
            }
        )

    def test_roundtrip(self):
        path = self.root / "state.json"
        store = state.State.load(path)
        directory = self.root / "games" / "sakuhin"
        directory.mkdir(parents=True)
        store.record(self._work("2026-01-01 00:00:00"), directory)
        store.save()

        reloaded = state.State.load(path)
        self.assertIn("RJ1", reloaded.works)
        self.assertTrue(reloaded.is_installed("RJ1"))

    def test_needs_update(self):
        store = state.State(self.root / "state.json")
        entry = store.record(self._work("2026-01-01 00:00:00"), self.root)

        self.assertFalse(state.needs_update(entry, self._work("2026-01-01 00:00:00")))
        self.assertTrue(state.needs_update(entry, self._work("2026-06-01 00:00:00")))
        self.assertFalse(state.needs_update(entry, self._work(None)))
        self.assertFalse(state.needs_update(None, self._work("2026-06-01 00:00:00")))

    def _versioned(self, title: str) -> api.Work:
        return api._parse_work(
            {
                "workno": "RJ2",
                "name": {"ja_JP": title},
                "maker": {"name": {}},
                "work_type": "RPG",
                # DLsite が返さない作品を想定
                "upgrade_date": None,
            }
        )

    def test_falls_back_to_title_version(self):
        # upgrade_date が無い作品は、タイトルの版で更新を見る
        store = state.State(self.root / "state.json")
        entry = store.record(self._versioned("ゲーム Ver.1.0"), self.root)
        self.assertEqual(entry.version, "1.0")

        self.assertFalse(state.needs_update(entry, self._versioned("ゲーム Ver.1.0")))
        self.assertTrue(state.needs_update(entry, self._versioned("ゲーム Ver.1.1")))
        # 版が読めない側があれば判定しない
        self.assertFalse(state.needs_update(entry, self._versioned("ゲーム")))

    def test_upgrade_date_wins_over_version(self):
        # 日付が使えるなら、そちらを優先する
        store = state.State(self.root / "state.json")
        entry = store.record(self._work("2026-01-01 00:00:00"), self.root)
        entry.version = "1.0"

        newer = self._work("2026-01-01 00:00:00")
        newer.names = {"ja_JP": "作品 Ver.9.9"}
        self.assertFalse(
            state.needs_update(entry, newer), "日付が同じなら版が違っても更新扱いにしない"
        )


class SteamTest(unittest.TestCase):
    def test_binary_vdf_roundtrip(self):
        document = {
            "shortcuts": {
                "0": {
                    "appid": -1234567,
                    "AppName": "テストゲーム",
                    "Exe": '"/games/Example/Game.exe"',
                    "StartDir": '"/games/Example"',
                    "IsHidden": 0,
                    "tags": {"0": "DLsite"},
                }
            }
        }
        encoded = steam.serialize_binary_vdf(document)
        self.assertEqual(steam.parse_binary_vdf(encoded), document)

    def test_app_id_is_stable_and_high_bit_set(self):
        app_id = steam.generate_app_id('"/g/Game.exe"', "Game")
        self.assertEqual(app_id, steam.generate_app_id('"/g/Game.exe"', "Game"))
        self.assertTrue(app_id & 0x80000000)

    def test_upsert_replaces_by_name_and_keeps_launch_options(self):
        document = {"shortcuts": {}}
        first = steam.Shortcut("Game", '"/g/Game.exe"', '"/g"')
        app_id, created = steam.upsert_shortcut(document, first)
        self.assertTrue(created)

        document["shortcuts"]["0"]["LaunchOptions"] = "PROTON_LOG=1 %command%"

        second = steam.Shortcut("Game", '"/g/Game.exe"', '"/g"')
        app_id2, created2 = steam.upsert_shortcut(document, second)
        self.assertFalse(created2)
        self.assertEqual(app_id, app_id2)
        self.assertEqual(len(document["shortcuts"]), 1)
        self.assertEqual(
            document["shortcuts"]["0"]["LaunchOptions"], "PROTON_LOG=1 %command%"
        )

    def test_matches_steam_written_lowercase_keys(self):
        # 実際の Steam は appname / exe / openvr を小文字で書き、sortas を足すことがある。
        # 大小を無視して照合できないと、実行のたびに重複が増えていく。
        document = {
            "shortcuts": {
                "0": {
                    "appid": -536874110,
                    "appname": "既存ゲーム",
                    "exe": '"G:\\old\\Game.exe"',
                    "StartDir": '"G:\\old"',
                    "LaunchOptions": "-windowed",
                    "sortas": "既存ゲーム",
                    "openvr": 0,
                    "tags": {"0": "手動"},
                }
            }
        }

        shortcut = steam.build_shortcut("既存ゲーム", Path("G:/new/Game.exe"))
        _app_id, created = steam.upsert_shortcut(document, shortcut)

        self.assertFalse(created, "既存エントリを見つけられず重複を作っている")
        self.assertEqual(len(document["shortcuts"]), 1)

        entry = document["shortcuts"]["0"]
        # 既存キーの綴りを保ったまま更新すること
        self.assertIn("appname", entry)
        self.assertNotIn("AppName", entry)
        self.assertIn("Game.exe", entry["exe"])
        # ユーザーが Steam 上で設定した内容は温存すること
        self.assertEqual(entry["LaunchOptions"], "-windowed")
        self.assertEqual(entry["sortas"], "既存ゲーム")
        self.assertEqual(entry["tags"], {"0": "手動"})

        self.assertTrue(steam.remove_shortcut(document, "既存ゲーム"))
        self.assertEqual(document["shortcuts"], {})

    def test_new_entry_uses_steam_key_spelling(self):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, steam.Shortcut("A", '"/a/A.exe"', '"/a"'))
        entry = document["shortcuts"]["0"]
        for key in ("appid", "appname", "exe", "StartDir", "openvr", "tags"):
            self.assertIn(key, entry)

    def test_register_creates_backup_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / "userdata" / "1234567" / "config"
            config.mkdir(parents=True)
            game = root / "game"
            game.mkdir()
            executable = game / "Game.exe"
            executable.write_bytes(b"MZ")

            userdata = root / "userdata"

            first = steam.register(userdata, "テストゲーム", executable)
            self.assertTrue(first.created)
            self.assertEqual(len(first.updated), 1)
            # 新規作成時は元ファイルが無いので控えも無い
            self.assertEqual(first.backups, [])

            shortcuts = steam.load_shortcuts(config / "shortcuts.vdf")["shortcuts"]
            self.assertEqual(len(shortcuts), 1)

            # 同じ内容で登録し直しても増えず、控えが残る
            second = steam.register(userdata, "テストゲーム", executable)
            self.assertFalse(second.created)
            self.assertEqual(second.app_id, first.app_id)
            self.assertEqual(len(second.backups), 1)

            shortcuts = steam.load_shortcuts(config / "shortcuts.vdf")["shortcuts"]
            self.assertEqual(len(shortcuts), 1)

    def test_rename_does_not_leave_the_old_entry(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "userdata" / "1" / "config").mkdir(parents=True)
            executable = root / "Game.exe"
            executable.write_bytes(b"MZ")
            userdata = root / "userdata"

            steam.register(userdata, "長いタイトル ～副題～", executable)
            # 改名するときは呼び出し側が古い名前を外してから登録する
            steam.unregister(userdata, "長いタイトル ～副題～")
            steam.register(userdata, "短いタイトル", executable)

            shortcuts = steam.load_shortcuts(
                root / "userdata" / "1" / "config" / "shortcuts.vdf"
            )["shortcuts"]
            names = [steam.get_field(v, "AppName") for v in shortcuts.values()]
            self.assertEqual(names, ["短いタイトル"])

    def test_unregister_reports_nothing_when_absent(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "userdata" / "1" / "config").mkdir(parents=True)
            result = steam.unregister(root / "userdata", "存在しない")
            self.assertEqual(result.updated, [])

    def test_grid_images_use_unsigned_appid(self):
        # Steam は grid の画像を appid の符号なし表現で名前付けする。
        # 実機の "S.T.A.L.K.E.R." は shortcuts.vdf 上 -536874110 で、
        # 画像は 3758093186p.png だった。
        with tempfile.TemporaryDirectory() as name:
            config = Path(name)
            written = steam.install_grid_images(config, 3758093186, PNG_1PX)
            names = sorted(path.name for path in written)
            self.assertEqual(
                names,
                ["3758093186.png", "3758093186_hero.png", "3758093186p.png"],
            )

    def test_grid_extension_follows_content(self):
        with tempfile.TemporaryDirectory() as name:
            config = Path(name)
            written = steam.install_grid_images(config, 1, JPEG_1PX, slots=["cover"])
            self.assertEqual(written[0].name, "1p.jpg")

            with self.assertRaises(steam.SteamError):
                steam.install_grid_images(config, 1, b"GIF89a", slots=["cover"])

    def test_grid_replaces_other_extension(self):
        # 拡張子違いが残ると Steam がどちらを使うか読めない
        with tempfile.TemporaryDirectory() as name:
            config = Path(name)
            steam.install_grid_images(config, 7, JPEG_1PX, slots=["cover"])
            steam.install_grid_images(config, 7, PNG_1PX, slots=["cover"])

            left = sorted(p.name for p in steam.grid_dir(config).iterdir())
            self.assertEqual(left, ["7p.png"])

    def test_remove_grid_images(self):
        with tempfile.TemporaryDirectory() as name:
            config = Path(name)
            steam.install_grid_images(config, 9, PNG_1PX)
            (steam.grid_dir(config) / "9_logo.png").write_bytes(PNG_1PX)
            # 別作品の画像は消さない
            steam.install_grid_images(config, 10, PNG_1PX, slots=["cover"])

            removed = steam.remove_grid_images(config, 9)
            self.assertEqual(len(removed), 4)
            left = sorted(p.name for p in steam.grid_dir(config).iterdir())
            self.assertEqual(left, ["10p.png"])

    def test_register_installs_images_and_unregister_removes_them(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = root / "userdata" / "1" / "config"
            config.mkdir(parents=True)
            executable = root / "Game.exe"
            executable.write_bytes(b"MZ")
            userdata = root / "userdata"

            result = steam.register(userdata, "ゲーム", executable, image=PNG_1PX)
            self.assertEqual(len(result.images), 3)
            self.assertTrue(
                (config / "grid" / f"{result.app_id}p.png").is_file()
            )

            removal = steam.unregister(userdata, "ゲーム", app_id=result.app_id)
            self.assertEqual(len(removal.images), 3)
            self.assertEqual(list(steam.grid_dir(config).iterdir()), [])

    def _config_vdf(self, root: Path, body: str) -> Path:
        """CompatToolMapping を含む config.vdf を模した最小構成を作る。"""
        (root / "config").mkdir(parents=True, exist_ok=True)
        path = root / "config" / "config.vdf"
        path.write_text(
            '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
            '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n'
            + body
            + "\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n",
            encoding="utf-8",
        )
        (root / "userdata").mkdir(exist_ok=True)
        return path

    def test_assigns_proton_when_absent(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self._config_vdf(root, "")
            userdata = root / "userdata"

            self.assertIsNone(steam.get_compat_tool(userdata, 123))
            self.assertTrue(
                steam.set_compat_tool(userdata, 123, "proton_experimental")
            )
            self.assertEqual(
                steam.get_compat_tool(userdata, 123), "proton_experimental"
            )
            # 他の設定を壊していないこと
            self.assertIn('"InstallConfigStore"', path.read_text(encoding="utf-8"))

    def test_keeps_existing_assignment_unless_forced(self):
        body = (
            '\t\t\t\t\t"456"\n\t\t\t\t\t{\n'
            '\t\t\t\t\t\t"name"\t\t"GE-Proton10-10"\n'
            '\t\t\t\t\t\t"config"\t\t""\n'
            '\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}\n'
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self._config_vdf(root, body)
            userdata = root / "userdata"

            # 利用者が選んだものを勝手に変えない
            self.assertFalse(
                steam.set_compat_tool(userdata, 456, "proton_experimental", overwrite=False)
            )
            self.assertEqual(steam.get_compat_tool(userdata, 456), "GE-Proton10-10")

            # 明示的に指示されたら変える
            self.assertTrue(
                steam.set_compat_tool(userdata, 456, "proton_experimental", overwrite=True)
            )
            self.assertEqual(
                steam.get_compat_tool(userdata, 456), "proton_experimental"
            )

    def test_does_not_touch_other_entries(self):
        body = (
            '\t\t\t\t\t"111"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton_7"\n'
            '\t\t\t\t\t\t"config"\t\t""\n\t\t\t\t\t\t"priority"\t\t"250"\n\t\t\t\t\t}\n'
        )
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = self._config_vdf(root, body)
            userdata = root / "userdata"

            steam.set_compat_tool(userdata, 222, "proton_experimental")
            self.assertEqual(steam.get_compat_tool(userdata, 111), "proton_7")
            self.assertEqual(steam.get_compat_tool(userdata, 222), "proton_experimental")
            # 控えが残っていること
            self.assertTrue(list(path.parent.glob("config.vdf.bak-*")))

    def test_official_proton_naming(self):
        # 設定に書く名前はフォルダ名と綴りが違う
        cases = {
            "Proton - Experimental": "proton_experimental",
            "Proton Hotfix": "proton_hotfix",
            "Proton 11.0": "proton_11",
            "Proton 9.0 (Beta)": "proton_9",
            "Proton 5.13": "proton_513",
            "Proton 6.3": "proton_63",
            "Proton 3.16": "proton_316",
            "GE-Proton10-10": None,  # 追加ツールはフォルダ名がそのまま名前
            "Steam Linux Runtime": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(steam._official_proton_key(name), expected)

    def test_lists_official_and_custom_tools(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "userdata").mkdir()
            common = root / "steamapps" / "common"
            common.mkdir(parents=True)
            for folder in ("Proton - Experimental", "Proton 8.0", "Steam Linux Runtime"):
                (common / folder).mkdir()

            custom = root / "compatibilitytools.d" / "GE-Proton10-10"
            custom.mkdir(parents=True)
            (custom / "compatibilitytool.vdf").write_text("{}", encoding="utf-8")
            # 定義ファイルが無いものは互換ツールとみなさない
            (root / "compatibilitytools.d" / "junk").mkdir()

            tools = steam.list_compat_tools(root / "userdata")
            values = [tool["value"] for tool in tools]

            self.assertEqual(values[0], "proton_experimental", "Experimental を先頭に")
            self.assertIn("proton_8", values)
            self.assertIn("GE-Proton10-10", values)
            self.assertNotIn("junk", values)
            # ランタイムは Proton ではない
            self.assertFalse([v for v in values if "runtime" in v.lower()])

    def test_remove_reindexes(self):
        document = {"shortcuts": {}}
        steam.upsert_shortcut(document, steam.Shortcut("A", '"/a"', '"/"'))
        steam.upsert_shortcut(document, steam.Shortcut("B", '"/b"', '"/"'))
        self.assertTrue(steam.remove_shortcut(document, "A"))
        self.assertEqual(list(document["shortcuts"]), ["0"])
        self.assertEqual(steam.get_field(document["shortcuts"]["0"], "AppName"), "B")



class BuriedExecutableTest(unittest.TestCase):
    """除外語の名前がついたフォルダの中にある本体も候補に出すこと。

    以前はそういう階層を丸ごと掘らずに捨てていた。実物に
    ``InstallSet/InstallData/<本体>.exe`` という形があり、``InstallSet`` が
    除外語 "install" に当たるため、**候補が StartMenu.exe の 1 つしか
    出なかった**。捨てずに順位を下げるだけにしてある。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _build_dohna(self) -> None:
        """実物と同じ形を作る。"""
        data = self.root / "InstallSet" / "InstallData"
        data.mkdir(parents=True)
        (self.root / "StartMenu.exe").write_bytes(b"M" * 86016)
        (data / "maingame.exe").write_bytes(b"M" * 4156944)
        (data / "startup.exe").write_bytes(b"M" * 31925776)
        (data / "Uninstaller.exe").write_bytes(b"M" * 107008)
        (data / "ResetConfig.exe").write_bytes(b"M" * 144384)
        (self.root / "InstallSet" / "Installer.exe").write_bytes(b"M" * 133120)

    def test_the_real_body_is_offered(self):
        self._build_dohna()
        names = [item.relative for item in archive.executable_candidates(self.root)]

        self.assertIn("InstallSet/InstallData/maingame.exe", names,
                      f"本体が候補に出ていない: {names}")
        self.assertIn("StartMenu.exe", names)

    def test_nothing_is_thrown_away(self):
        self._build_dohna()
        found = archive.executable_candidates(self.root)
        self.assertEqual(len(found), 6, [item.relative for item in found])

    def test_buried_ones_are_flagged_and_ranked_lower(self):
        self._build_dohna()
        found = archive.executable_candidates(self.root)

        # 直下の素直なものが先頭
        self.assertEqual(found[0].relative, "StartMenu.exe")
        self.assertFalse(found[0].suspicious)

        buried = {item.relative: item for item in found}
        # 本体でも、除外語のフォルダに入っていることは伝える
        self.assertTrue(buried["InstallSet/InstallData/maingame.exe"].suspicious)

    def test_uninstaller_never_outranks_the_body(self):
        """Inno Setup の unins000.exe は "uninst" に当たらず素通りしていた。"""
        installed = self.root / "installed"
        installed.mkdir()
        (installed / "unins000.exe").write_bytes(b"M" * 718685)
        (installed / "anone.exe").write_bytes(b"M" * 613888)
        (self.root / "Setup.exe").write_bytes(b"M" * 340469261)

        found = archive.executable_candidates(self.root)
        self.assertEqual(found[0].relative, "installed/anone.exe",
                         [item.relative for item in found])

    def test_deeply_buried_body_is_still_found(self):
        deep = self.root / "setup" / "install" / "data" / "config"
        deep.mkdir(parents=True)
        (deep / "Game.exe").write_bytes(b"M" * 5000)

        names = [item.relative for item in archive.executable_candidates(self.root)]
        self.assertEqual(names, ["setup/install/data/config/Game.exe"])

if __name__ == "__main__":
    unittest.main()
