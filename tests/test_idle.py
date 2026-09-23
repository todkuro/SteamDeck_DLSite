"""使われなくなったら自動で終了すること (A) と、接続に上限があること (B)。

デスクトップモードでは、ブラウザのタブを閉じてもツールは裏で動き続けていた。
また待ち受けに時間切れも同時接続数の上限も無く、何も送らない接続を 200 本
開くとスレッドが 200 本増えたまま減らず、ツールを終了できなくなった。
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import config as config_module  # noqa: E402
from dlsite_deck import webui  # noqa: E402
from dlsite_deck import server as deck_server  # noqa: E402
from tests import support  # noqa: E402

TOKEN = "idle-test-token"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class IdleWatchTest(unittest.TestCase):
    """時計を差し替えて、終了の判断だけを確かめる。"""

    def setUp(self):
        self.clock = FakeClock()
        self.busy = False
        self.watch = deck_server.IdleWatch(
            30, busy=lambda: self.busy, clock=self.clock, interval=30)

    def _wait(self, minutes):
        """確かめる担当が 30 秒ごとに見に来る様子をまねる。最後の判断を返す。"""
        result = False
        for _ in range(int(minutes * 2)):
            self.clock.advance(30)
            result = self.watch.should_stop()
        return result

    def test_stops_after_the_limit(self):
        self.assertFalse(self._wait(29.5))
        self.assertTrue(self._wait(0.5))

    def test_a_request_starts_the_count_again(self):
        self._wait(20)
        self.watch.begin()
        self.watch.end()
        self.assertFalse(self._wait(20), "合図があったのに終了した")
        self.assertTrue(self._wait(10))

    def test_never_while_a_request_is_in_flight(self):
        """「Steam を終了する」の待ちのように、処理に時間がかかる要求。"""
        self.watch.begin()
        self.assertFalse(self._wait(120))
        self.watch.end()
        self.assertFalse(self._wait(29.5))
        self.assertTrue(self._wait(0.5))

    def test_never_while_busy(self):
        """ダウンロードや展開の途中。終わってから数え始める。"""
        self.busy = True
        self.assertFalse(self._wait(240), "ダウンロード中に終了した")
        self.busy = False
        self.assertFalse(self._wait(29.5))
        self.assertTrue(self._wait(0.5))

    def test_zero_means_never(self):
        watch = deck_server.IdleWatch(0, clock=self.clock, interval=30)
        for _ in range(1000):
            self.clock.advance(30)
            self.assertFalse(watch.should_stop())

    def test_sleep_is_not_counted(self):
        """スリープ明けに、画面が合図を送る前に終了しないこと。"""
        self._wait(20)
        self.clock.advance(8 * 3600)  # 一晩スリープしていた
        self.assertFalse(self.watch.should_stop(), "スリープ明けにすぐ終了した")
        self.assertFalse(self._wait(29.5))
        self.assertTrue(self._wait(0.5))


class _Backend:
    def __init__(self, watch):
        self.idle = watch

    def status_json(self):
        return {"ok": True}

    def ping(self):
        return {"ok": True}


def _serve(testcase, handler, **kwargs):
    return support.start_server(testcase, handler, deck_server.DeckServer, **kwargs)


def _get(port, path, token=True):
    headers = {deck_server.TOKEN_HEADER: TOKEN} if token else {}
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    status, _body = support.open_with_retry(request)
    return status


class CountingTest(unittest.TestCase):
    """合言葉付きの API 呼び出しだけを「使われている」に数えること。"""

    def setUp(self):
        self.clock = FakeClock()
        self.watch = deck_server.IdleWatch(30, clock=self.clock)
        handler = deck_server.make_handler(_Backend(self.watch), token=TOKEN)
        self.server = _serve(self, handler)
        self.port = self.server.server_address[1]

    def test_a_proper_call_counts(self):
        self.clock.advance(600)
        self.assertEqual(_get(self.port, "/api/ping"), 200)
        self.assertEqual(self.watch._last, self.clock.now)

    def test_calls_without_the_token_do_not_count(self):
        """よそのプログラムが接続し続けるだけでは、居座らせられないこと。"""
        before = self.watch._last
        self.clock.advance(600)
        self.assertEqual(_get(self.port, "/api/ping", token=False), 403)
        self.assertEqual(_get(self.port, "/"), 200)
        self.assertEqual(self.watch._last, before)


class BusyTest(unittest.TestCase):
    def setUp(self):
        cfg = config_module.Config()
        cfg.state_file = str(Path(tempfile.mkdtemp()) / "state.json")
        self.backend = webui.Backend(cfg)

    def test_idle_backend(self):
        self.assertFalse(self.backend.is_busy())

    def test_queued_and_running_jobs(self):
        for status in ("queued", "running"):
            with self.subTest(status=status):
                self.backend._jobs = {1: webui.Job(id=1, work_id="RJ1", title="a", status=status)}
                self.assertTrue(self.backend.is_busy())

    def test_finished_jobs_are_not_busy(self):
        self.backend._jobs = {
            1: webui.Job(id=1, work_id="RJ1", title="a", status=status)
            for status in ("done", "error", "cancelled")
        }
        self.assertFalse(self.backend.is_busy())

    def test_cover_rebuild(self):
        self.backend._cover_task["running"] = True
        self.assertTrue(self.backend.is_busy())


class ConnectionLimitTest(unittest.TestCase):
    """接続に時間切れと上限があること。"""

    def _server(self, timeout=60, max_connections=32):
        handler = deck_server.make_handler(_Backend(None), token=TOKEN)
        handler.timeout = timeout
        server = _serve(self, handler, max_connections=max_connections)
        return server, server.server_address[1]

    def _closed_by_server(self, sock, within):
        """相手 (待ち受け) が接続を閉じたか。閉じられると recv が空か RST になる。"""
        sock.settimeout(within)
        try:
            return sock.recv(1) == b""
        except (ConnectionResetError, ConnectionAbortedError):
            return True
        except socket.timeout:
            return False

    def test_the_timeout_is_a_minute(self):
        """短すぎると画面の操作に響くので、1 分にしてある。処理時間は数えない。"""
        self.assertEqual(deck_server.Handler.timeout, 60)
        self.assertEqual(deck_server.CONNECTION_TIMEOUT, 60)

    def test_a_silent_connection_is_closed(self):
        _server, port = self._server(timeout=1)
        sock = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(sock.close)
        self.assertTrue(self._closed_by_server(sock, within=5), "居座りを切っていない")

    def test_a_slow_request_is_not_cut_short(self):
        """処理に時間がかかっても、通信が止まっていなければ切らない。

        「Steam を終了する」は最大 90 秒待つ。その間は通信が無いが、
        待っているのはツール側なので時間切れにはならない。
        """
        class SlowBackend(_Backend):
            def status_json(self):
                time.sleep(2.5)
                return {"ok": True}

        handler = deck_server.make_handler(SlowBackend(None), token=TOKEN)
        handler.timeout = 1
        server = _serve(self, handler)
        self.assertEqual(_get(server.server_address[1], "/api/status"), 200)

    def test_connections_beyond_the_cap_are_closed(self):
        server, port = self._server(max_connections=3)
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(3)]
        for sock in held:
            self.addCleanup(sock.close)
        time.sleep(0.3)

        extra = socket.create_connection(("127.0.0.1", port))
        self.addCleanup(extra.close)
        self.assertTrue(self._closed_by_server(extra, within=5), "上限を超えても受け付けた")

        # 居座りが去れば、また使える
        for sock in held:
            sock.close()
        time.sleep(0.5)
        self.assertEqual(_get(port, "/api/status"), 200)

    def test_threads_do_not_pile_up(self):
        _server, port = self._server(max_connections=3)
        before = threading.active_count()
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(30)]
        for sock in held:
            self.addCleanup(sock.close)
        time.sleep(0.5)
        self.assertLessEqual(threading.active_count(), before + 3)

    def test_leftover_connections_do_not_block_exit(self):
        """残った接続のスレッドを待たずに終了できること。"""
        self.assertTrue(deck_server.DeckServer.daemon_threads)


class SettingTest(unittest.TestCase):
    def setUp(self):
        root = support.use_temp_config(self)
        cfg = config_module.Config()
        cfg.state_file = str(root / "state.json")
        self.backend = webui.Backend(cfg)

    def test_default_is_thirty_minutes(self):
        self.assertEqual(config_module.Config().idle_shutdown_minutes, 30)

    def test_choices_can_be_saved(self):
        for minutes in (0, 10, 30, 60, 120):
            with self.subTest(minutes=minutes):
                self.backend.save_config({"idle_shutdown_minutes": minutes})
                self.assertEqual(self.backend.config.idle_shutdown_minutes, minutes)

    def test_other_values_are_refused(self):
        for value in (5, -1, "30", True, None):
            with self.subTest(value=value):
                with self.assertRaises(webui.UiError):
                    self.backend.save_config({"idle_shutdown_minutes": value})


class PageTest(unittest.TestCase):
    def test_the_page_keeps_sending_the_signal(self):
        self.assertIn('call("/api/ping")', deck_server.PAGE)
        self.assertIn("setInterval(ping, 60 * 1000)", deck_server.PAGE)


if __name__ == "__main__":
    unittest.main()
