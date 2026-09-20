"""起動できることだけを確かめる、最低限の網。

これまで単体テストは全部通るのに ``python3 -m dlsite_deck`` が
import エラーで起動しない、ということが起きた。個々の関数を試すだけでは
「モジュールとして読み込めるか」「コマンドが組み立てられるか」を見ていなかったため。
"""

from __future__ import annotations

import importlib
import io
import pkgutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dlsite_deck  # noqa: E402


class ImportTest(unittest.TestCase):
    def test_every_module_imports(self):
        """パッケージ内の全モジュールが読み込めること。"""
        failures = []
        for info in pkgutil.iter_modules(dlsite_deck.__path__):
            name = f"dlsite_deck.{info.name}"
            try:
                importlib.import_module(name)
            except Exception as error:  # noqa: BLE001
                failures.append(f"{name}: {type(error).__name__}: {error}")

        self.assertEqual(failures, [], "読み込めないモジュールがある")

    def test_tools_import(self):
        """移行支援スクリプトも構文と import が通ること。"""
        tools = ROOT / "tools"
        if not tools.is_dir():
            self.skipTest("tools/ がありません")

        failures = []
        for path in sorted(tools.glob("*.py")):
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(path)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failures.append(f"{path.name}: {result.stderr.strip()[:120]}")

        self.assertEqual(failures, [], "構文が通らないスクリプトがある")


class CommandLineTest(unittest.TestCase):
    """引数の組み立てだけを確かめる。ネットワークにも設定にも触れない。"""

    def _parser(self):
        from dlsite_deck import cli

        return cli._build_parser()

    def test_help_builds(self):
        parser = self._parser()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            parser.print_help()
        text = buffer.getvalue()

        for command in ("check", "login", "init", "serve", "list", "download",
                        "adopt", "steam"):
            self.assertIn(command, text, f"{command} が help に出ない")

    def test_every_subcommand_has_a_handler(self):
        parser = self._parser()
        # 副コマンドを引き当てて、handler が結び付いているか見る
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        self.assertTrue(actions, "副コマンドが見つからない")

        missing = []
        for name, sub in actions[0].choices.items():
            defaults = sub.get_default("handler")
            if not callable(defaults):
                missing.append(name)
        self.assertEqual(missing, [], "handler が無い副コマンドがある")

    def test_arguments_parse(self):
        parser = self._parser()
        cases = [
            ["check"],
            ["login"],
            ["init"],
            ["serve", "--host", "127.0.0.1", "--port", "9999", "--no-browser"],
            ["list", "--category", "game", "--updates"],
            ["list", "--all", "--installed"],
            ["download", "RJ123456", "--dry-run"],
            ["download", "--all", "--install-dir", "/games", "--keep-archives"],
            ["adopt", "mapping.json", "--dry-run"],
            ["steam", "RJ123456", "--choose", "--no-images"],
            ["steam", "--remove", "--dry-run"],
            ["steam", "--images-only"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertTrue(callable(args.handler))

    def test_module_entry_point_runs(self):
        """``python -m dlsite_deck --help`` が実際に起動すること。"""
        result = subprocess.run(
            [sys.executable, "-m", "dlsite_deck", "--help"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr[:400])
        self.assertIn("dlsite_deck", result.stdout)



class RoutingTest(unittest.TestCase):
    """経路の振り分けと、失敗したときの応答。

    do_GET / do_POST は同じ例外処理を別々に持っていたため、ここで
    「どの経路でも同じように失敗を返すこと」を押さえておく。
    """

    def setUp(self):
        import json
        import threading
        from http.server import ThreadingHTTPServer
        from dlsite_deck import webui

        class FakeBackend:
            """本物に触れずに、経路が正しい先を呼ぶかだけを見る。"""

            def __init__(self):
                self.calls = []

            def _record(self, name, *args):
                self.calls.append((name, args))
                return {"ok": name}

            def library_json(self, refresh=False):
                return self._record("library", refresh)

            def executables(self, work_id):
                return self._record("executables", work_id)

            def jobs_json(self):
                return self._record("jobs")

            def status_json(self):
                return self._record("status")

            def config_json(self):
                return self._record("config")

            def save_config(self, values):
                return self._record("save_config", tuple(sorted(values)))

            def start_download(self, work_id, install_dir=None):
                return self._record("download", work_id, install_dir)

            def cancel_download(self, job_id):
                return self._record("cancel", job_id)

            def register_steam(self, work_id, executable, title, launch_options=None):
                return self._record("register", work_id, executable, title, launch_options)

            def unregister_steam(self, work_id):
                return self._record("unregister", work_id)

            def links_json(self):
                return self._record("links")

            def proton_json(self):
                return self._record("proton")

            def steam_state(self):
                return self._record("steam_state")

            def stop_steam(self):
                return self._record("stop_steam")

            def delete_work(self, work_id):
                return self._record("delete_work", work_id)

            def delete_preview(self, work_id):
                return self._record("delete_preview", work_id)

            def set_proton(self, work_id, tool):
                return self._record("set_proton", work_id, tool)

            def link_candidates(self, work_id):
                return self._record("link_candidates", work_id)

            def browse(self, work_id, path=""):
                return self._record("browse", work_id, path)

            def patches(self, work_id):
                return self._record("patches", work_id)

            def preview_link(self, values):
                return self._record("preview_link", values.get("source_id"))

            def apply_link(self, values):
                return self._record("apply_link", values.get("source_id"))

            def revert_link(self, key):
                return self._record("revert_link", key)

            def remove_link(self, key):
                return self._record("remove_link", key)

            def run_patch(self, work_id, relative, target_id):
                return self._record("run_patch", work_id, relative, target_id)

            def boom(self):
                raise RuntimeError("想定外")

        self.json = json
        self.webui = webui
        self.backend = FakeBackend()
        handler = type("H", (webui.Handler,), {"backend": self.backend})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def _request(self, path, payload=None, raw=None):
        """1 回だけ要求を出す。接続が切られたら数回まで出し直す。

        Windows の一部の環境では loopback への接続が一定の割合で
        RST される。標準の BaseHTTPRequestHandler でも同じ割合で起きるため
        ツール側の問題ではない (SteamDeck 実機では 200 回中 0 回)。
        ここで再試行しないと、この環境でだけテストが落ちる。
        """
        import time
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{self.port}{path}"
        if raw is not None:
            data = raw
        else:
            data = self.json.dumps(payload).encode() if payload is not None else None

        last = None
        for attempt in range(5):
            request = urllib.request.Request(url, data=data)
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                # これは応答が返っている。中身を見たいので再試行しない。
                return error.code, error.read()
            except (ConnectionError, TimeoutError, OSError) as error:
                last = error
                # 混んでいると読み取りごと待たされる。少しずつ間隔を空ける。
                time.sleep(0.2 * (attempt + 1))

        raise AssertionError(f"{path} に接続できませんでした: {last}")

    def test_page_is_served(self):
        status, body = self._request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())

    def test_get_routes_reach_the_backend(self):
        cases = {
            "/api/library": ("library", (False,)),
            "/api/library?refresh=1": ("library", (True,)),
            "/api/executables?work_id=RJ1": ("executables", ("RJ1",)),
            "/api/jobs": ("jobs", ()),
            "/api/status": ("status", ()),
            "/api/config": ("config", ()),
            "/api/links": ("links", ()),
            "/api/proton": ("proton", ()),
            "/api/steam/state": ("steam_state", ()),
            "/api/delete/preview?work_id=RJ1": ("delete_preview", ("RJ1",)),
            "/api/link/candidates?work_id=RJ1": ("link_candidates", ("RJ1",)),
            "/api/browse?work_id=RJ1&path=Data": ("browse", ("RJ1", "Data")),
            "/api/patches?work_id=RJ1": ("patches", ("RJ1",)),
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.backend.calls.clear()
                status, _ = self._request(path)
                self.assertEqual(status, 200)
                # 再送があると同じ呼び出しが複数回並ぶので、回数ではなく中身を見る
                self.assertTrue(self.backend.calls, f"{path} が backend に届いていない")
                self.assertEqual(set(self.backend.calls), {expected})

    def test_post_routes_reach_the_backend(self):
        cases = [
            ("/api/download", {"work_id": "RJ1"}, ("download", ("RJ1", None))),
            ("/api/download", {"work_id": "RJ1", "install_dir": "/g"},
             ("download", ("RJ1", "/g"))),
            ("/api/download/cancel", {"job_id": 3}, ("cancel", (3,))),
            ("/api/steam/register", {"work_id": "RJ1", "executable": "a.exe",
                                     "title": "T"},
             ("register", ("RJ1", "a.exe", "T", None))),
            # 起動オプションを明示した場合はそのまま届くこと
            ("/api/steam/register", {"work_id": "RJ1", "executable": "a.exe",
                                     "title": "T",
                                     "launch_options": "LANG=ja_JP.UTF-8 %command%"},
             ("register", ("RJ1", "a.exe", "T", "LANG=ja_JP.UTF-8 %command%"))),
            ("/api/steam/unregister", {"work_id": "RJ1"}, ("unregister", ("RJ1",))),
            ("/api/config", {"values": {"b": 1, "a": 2}},
             ("save_config", (("a", "b"),))),
            ("/api/link/preview", {"source_id": "RJ2", "target_id": "RJ1"},
             ("preview_link", ("RJ2",))),
            ("/api/link/apply", {"source_id": "RJ2", "target_id": "RJ1"},
             ("apply_link", ("RJ2",))),
            ("/api/delete", {"work_id": "RJ1"}, ("delete_work", ("RJ1",))),
            ("/api/steam/stop", {}, ("stop_steam", ())),
            ("/api/proton/set", {"work_id": "RJ1", "tool": "GE-Proton10-10"},
             ("set_proton", ("RJ1", "GE-Proton10-10"))),
            ("/api/link/revert", {"key": "A:->B:"}, ("revert_link", ("A:->B:",))),
            ("/api/link/remove", {"key": "A:x->B:y"}, ("remove_link", ("A:x->B:y",))),
            ("/api/patch/run",
             {"work_id": "VJ100002", "relative": "特典パッチ/bonuspatch.exe",
              "target_id": "VJ100001"},
             ("run_patch", ("VJ100002", "特典パッチ/bonuspatch.exe", "VJ100001"))),
        ]
        for path, payload, expected in cases:
            with self.subTest(path=path):
                self.backend.calls.clear()
                status, _ = self._request(path, payload)
                self.assertEqual(status, 200)
                self.assertTrue(self.backend.calls, f"{path} が backend に届いていない")
                self.assertEqual(set(self.backend.calls), {expected})

    def test_unknown_paths_are_404(self):
        for path, payload in (("/api/nope", None), ("/api/nope", {})):
            with self.subTest(path=path, post=payload is not None):
                status, body = self._request(path, payload)
                self.assertEqual(status, 404)
                self.assertIn("error", self.json.loads(body))

    def test_bad_config_payload_is_reported(self):
        status, body = self._request("/api/config", {"values": "文字列"})
        self.assertEqual(status, 400)
        self.assertIn("error", self.json.loads(body))

    def test_unexpected_failure_becomes_500(self):
        """予期しない例外でも、接続を落とさず JSON で返すこと。"""
        def explode(*args, **kwargs):
            raise RuntimeError("想定外")

        self.backend.status_json = explode
        status, body = self._request("/api/status")
        self.assertEqual(status, 500)
        self.assertIn("error", self.json.loads(body))

    def test_malformed_json_is_reported(self):
        status, body = self._request("/api/download", raw=b"{ not json")
        self.assertEqual(status, 400)
        self.assertIn("error", self.json.loads(body))


class PageScriptTest(unittest.TestCase):
    """画面の JS が、配信される形で壊れていないこと。

    構文が壊れると、そこから下の定義がまるごと失われて画面全体が動かなくなる。
    ブラウザを開くまで気付けないので、ここで確かめる。

    以前は画面を Python の文字列として埋め込んでいたため、``\n`` が Python 側で
    改行に解釈されて JS を壊す事故が繰り返し起きた。今は ``page.html`` に
    字面のまま置いてあるので、その罠は無い。
    """

    def _script(self) -> str:
        import re

        from dlsite_deck import webui

        found = re.search(r"<script>(.*?)</script>", webui.PAGE, re.S)
        self.assertIsNotNone(found, "script が見つからない")
        return found.group(1)

    def test_node_accepts_it(self):
        """node があれば実際に構文を確かめる。無ければ飛ばす。"""
        import shutil
        import tempfile

        node = shutil.which("node")
        if not node:
            # ここが唯一の砦なので、飛ばしたことが分かるようにしておく
            self.skipTest("node が無いため JS の構文を確認できません")

        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "page.js"
            path.write_text(self._script(), encoding="utf-8")
            # node のエラーには日本語がそのまま載る。text=True だと Windows の
            # 既定コーデック (cp932) で復号に失敗し、肝心の中身が消える。
            result = subprocess.run([node, "--check", str(path)], capture_output=True)

        message = result.stderr.decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, message[:600])

    def test_dialog_helpers_are_defined(self):
        """画面から呼ぶ関数が定義されていること。

        途中で構文エラーになると、そこから下の定義がまるごと失われる。
        """
        script = self._script()
        for name in ("function render", "function setSteamRunning",
                     "function ensureSteamStopped", "function openSteamDialog",
                     "function openLinkDialog", "function openPatchDialog",
                     "function loadProton", "function loadStatus"):
            self.assertIn(name, script, f"{name} が無い")


class ShutdownTest(unittest.TestCase):
    """画面の「終了」から待ち受けを畳めること。

    ``server.shutdown()`` を要求を捌いているスレッドから直接呼ぶと、自分の
    完了を待って固まる。応答を返しきってから別のスレッドで叩く必要がある。
    """

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from dlsite_deck import config as config_module, webui

        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp.name) / "state.json")

        self.webui = webui
        self.backend = webui.Backend(cfg)
        handler = type("H", (webui.Handler,), {"backend": self.backend})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.backend._server = self.server
        self.port = self.server.server_address[1]

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)

    def test_request_stops_the_server(self):
        import json
        import time
        import urllib.error
        import urllib.request

        url = f"http://127.0.0.1:{self.port}"

        # 応答が返ること (ここで固まらないこと自体が要点)。
        # この環境では loopback が一定の割合で切られるので数回試す。
        answered = False
        for _ in range(5):
            request = urllib.request.Request(
                url + "/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read())
                self.assertIn("終了", payload["message"])
                answered = True
                break
            except (ConnectionError, OSError):
                # 既に畳まれた後なら、それは成功しているということ
                if not self.thread.is_alive():
                    answered = True
                    break
                time.sleep(0.1)
        self.assertTrue(answered, "終了要求に応答が返らない")

        # 実際に待ち受けが止まること
        self.thread.join(timeout=10)
        self.assertFalse(self.thread.is_alive(), "serve_forever が抜けていない")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(url + "/api/steam/state", timeout=2)
            except (urllib.error.URLError, OSError):
                return
            time.sleep(0.1)
        self.fail("まだ応答を返している")

    def test_reports_how_many_jobs_are_abandoned(self):
        from dlsite_deck import webui as module

        self.backend._jobs[1] = module.Job(id=1, work_id="RJ1", title="a", status="running")
        self.backend._jobs[2] = module.Job(id=2, work_id="RJ2", title="b", status="queued")
        self.backend._jobs[3] = module.Job(id=3, work_id="RJ3", title="c", status="done")

        self.assertEqual(self.backend.shutdown()["abandoned"], 2)

    def test_without_a_server_it_says_so(self):
        self.backend._server = None
        with self.assertRaises(self.webui.UiError):
            self.backend.shutdown()

if __name__ == "__main__":
    unittest.main()
