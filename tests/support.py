"""テストで共通に使う補助。

テストのファイルではない (名前が ``test_`` で始まらない) ので、これ自体は走らない。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


def open_with_retry(request: urllib.request.Request, attempts: int = 5,
                    timeout: float = 10) -> tuple[int, bytes]:
    """要求を 1 回出し、``(状態コード, 本文)`` を返す。

    Windows の一部の環境 (開発機) では、loopback への接続が一定の割合で
    横から切られる。標準の BaseHTTPRequestHandler でも同じ割合で起きるので
    ツール側の問題ではない (SteamDeck 実機では 200 回中 0 回)。ここで数回
    やり直さないと、その環境でだけテストが落ちる。

    エラーの状態コード (4xx / 5xx) は応答が返っているので、やり直さずに返す。
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()
        except OSError as error:
            last = error
            # 混んでいると読み取りごと待たされる。少しずつ間隔を空ける。
            time.sleep(0.2 * (attempt + 1))
    raise AssertionError(f"{request.full_url} に接続できませんでした: {last}")


def start_server(testcase: unittest.TestCase, handler,
                 server_class=ThreadingHTTPServer, **kwargs):
    """待ち受けを別スレッドで動かす。テストが終われば止めて閉じる。"""
    server = server_class(("127.0.0.1", 0), handler, **kwargs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    testcase.addCleanup(server.server_close)
    testcase.addCleanup(server.shutdown)
    return server


def use_temp_config(testcase: unittest.TestCase) -> Path:
    """設定の保存先を一時的な場所へ向け、その場所 (フォルダ) を返す。

    向けないと、保存を伴うテストがプロジェクトの ``config.json`` を書き換える。
    """
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    patch = mock.patch.dict(os.environ, {"DLSITE_DECK_CONFIG": str(root / "config.json")})
    patch.start()
    testcase.addCleanup(patch.stop)
    return root
