"""ローカル Web UI の待ち受け。HTTP の受け答えと、よそからの要求の締め出し。

画面の処理そのもの (ライブラリ・ダウンロード・Steam への登録など) は
:mod:`dlsite_deck.webui` の :class:`~dlsite_deck.webui.Backend` が持つ。
ここは、要求を受けて Backend に渡し、結果を返すところだけを受け持つ。

既定では 127.0.0.1 だけで待ち受ける。ただしそれでも、同じ端末のブラウザで
開いた**他のサイトのページ**はここへ要求を送れる。それを断るため、要求ごとに
次を確かめる (:meth:`Handler._refusal`)。

* ``Host`` が IP アドレスか localhost であること (DNS リバインディング対策)
* ``Origin`` が付いていれば自分と同じであること
* API には、起動ごとに作る合言葉が付いていること
* POST の本文が ``application/json`` であること

合言葉は画面には埋め込まず、ツールが開く URL の ``#`` の後ろで渡す
(:func:`app_url`)。``#`` の後ろはサーバーに送られないので、画面を取得しても
合言葉は得られない。ブラウザではない同じ端末のプロセス (ネットワークには出られる
が、ホームディレクトリは読めない Flatpak のアプリなど) を締め出すため。

さらに、場所やプログラムを指す設定と起動オプションは API から変えさせない
(:data:`dlsite_deck.webui.LOCKED_CONFIG_KEYS`)。合言葉が漏れても、ツールに任意の
プログラムを実行させたり、ゲームを別の場所へ落とさせたりはできない。

接続には時間切れと上限を設け (:class:`DeckServer`)、使われなくなったら自分で
終了する (:class:`IdleWatch`)。
"""

from __future__ import annotations

import gzip
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import config as config_module
from .webui import Backend, UiError

#: これを超える応答は gzip で送る
GZIP_THRESHOLD = 4096


#: 1 回の write で流す最大バイト数
WRITE_CHUNK = 16384


#: 相手が接続を切ったときに出る例外。
#: Windows ではセキュリティ製品が loopback を切ることがあり (WinError 10053)、
#: 応答の途中でこれらが飛んでくる。
_DISCONNECTED = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


def _q(query: dict[str, list[str]], name: str) -> str:
    """クエリ文字列から 1 つ取り出す。無ければ空文字。"""
    return (query.get(name) or [""])[0]


def _s(payload: dict[str, Any], name: str) -> str:
    """本文から文字列として取り出す。無ければ空文字。"""
    return str(payload.get(name, ""))


def _save_config(backend: "Backend", payload: dict[str, Any]) -> dict[str, Any]:
    values = payload.get("values")
    if not isinstance(values, dict):
        raise UiError("設定の内容が不正です。")
    return backend.save_config(values)


# ----------------------------------------------------------------------
# 経路
# ----------------------------------------------------------------------
#
# 以前は if/elif を積み上げていたが、経路が 30 本ほどに増えて見通しが悪くなった。
# 「どの URL がどれを呼ぶか」だけの表にしてある。

#: GET。``(Backend, クエリ)`` を受けて、そのまま JSON にする値を返す。
GET_ROUTES: dict[str, Callable[["Backend", dict[str, list[str]]], Any]] = {
    "/api/library": lambda b, q: b.library_json(refresh=_q(q, "refresh") == "1"),
    "/api/executables": lambda b, q: b.executables(_q(q, "work_id")),
    "/api/jobs": lambda b, q: b.jobs_json(),
    "/api/ping": lambda b, q: b.ping(),
    "/api/paths": lambda b, q: b.path_warnings(),
    "/api/steam/covers": lambda b, q: b.cover_task(),
    "/api/steam/state": lambda b, q: b.steam_state(),
    "/api/proton": lambda b, q: b.proton_json(),
    "/api/links": lambda b, q: b.links_json(),
    "/api/link/candidates": lambda b, q: b.link_candidates(_q(q, "work_id")),
    "/api/browse": lambda b, q: b.browse(_q(q, "work_id"), _q(q, "path")),
    "/api/delete/preview": lambda b, q: b.delete_preview(_q(q, "work_id")),
    "/api/patches": lambda b, q: b.patches(_q(q, "work_id")),
    "/api/status": lambda b, q: b.status_json(),
    "/api/config": lambda b, q: b.config_json(),
    "/api/local": lambda b, q: b.local_json(),
    "/api/local/browse": lambda b, q: b.local_browse(
        _q(q, "root"), _q(q, "path"), _q(q, "mode")
    ),
    "/api/local/names": lambda b, q: b.local_names(_q(q, "title")),
}


#: POST。``(Backend, 本文)`` を受ける。
POST_ROUTES: dict[str, Callable[["Backend", dict[str, Any]], Any]] = {
    "/api/download": lambda b, p: b.start_download(
        _s(p, "work_id"),
        str(p["install_dir"]) if p.get("install_dir") else None,
    ),
    "/api/download/cancel": lambda b, p: b.cancel_download(int(p.get("job_id", 0))),
    # 起動オプションは受け取らない。送られてきても使わない (register_steam を参照)。
    "/api/steam/register": lambda b, p: b.register_steam(
        _s(p, "work_id"),
        _s(p, "executable"),
        _s(p, "title"),
    ),
    "/api/steam/unregister": lambda b, p: b.unregister_steam(_s(p, "work_id")),
    "/api/steam/covers/rebuild": lambda b, p: b.rebuild_covers(),
    "/api/steam/stop": lambda b, p: b.stop_steam(),
    "/api/delete": lambda b, p: b.delete_work(_s(p, "work_id")),
    "/api/shutdown": lambda b, p: b.shutdown(),
    "/api/proton/set": lambda b, p: b.set_proton(_s(p, "work_id"), _s(p, "tool")),
    "/api/link/preview": lambda b, p: b.preview_link(p),
    "/api/link/apply": lambda b, p: b.apply_link(p),
    "/api/link/revert": lambda b, p: b.revert_link(_s(p, "key")),
    "/api/link/remove": lambda b, p: b.remove_link(_s(p, "key")),
    # 共通パッチ置き場のものは、場所ではなく置き場の番号とその中の相対パスで受け取る
    "/api/patch/run": lambda b, p: b.run_patch(
        _s(p, "work_id"), _s(p, "relative"), _s(p, "target_id"),
        source=_s(p, "source") or "work", root=_s(p, "root"), args=_s(p, "args"),
        # 送られてこなければ使う (既定)。false のときだけ外す。
        use_launch_options=p.get("use_launch_options") is not False,
    ),
    # ゲームの中の exe (設定用のアプリなど) を、そのゲームの Proton 環境で実行する
    "/api/run": lambda b, p: b.run_game_exe(
        _s(p, "work_id"), _s(p, "relative"), args=_s(p, "args"),
        use_launch_options=p.get("use_launch_options") is not False,
    ),
    "/api/config": _save_config,
    # 取り込み元は番号と相対パスで受け取る。場所そのものは受け取らない (local を参照)。
    "/api/local/import": lambda b, p: b.start_local_import(p),
    "/api/local/archive/delete": lambda b, p: b.delete_local_archive(_s(p, "work_id")),
    "/api/local/image": lambda b, p: b.set_local_image(
        _s(p, "work_id"), _s(p, "root"), _s(p, "path")
    ),
}


#: 受け付ける本文の上限。画面が送るのは設定の保存が最大で、数 KB に収まる。
MAX_BODY = 1_000_000


#: 1 本の接続で、相手が何も送ってこない・受け取らないまま待つ上限 (秒)。
#:
#: 数えるのは**通信が止まっている時間だけ**で、ツールが処理している時間は
#: 含まない。ダウンロードは別のスレッドで進み、画面は短い問い合わせを繰り返す
#: だけなので、何十分かかっても影響しない。「Steam を終了する」のように 90 秒
#: 待つ操作も、その間は通信していないので切れない。画面は接続するとすぐに
#: 要求を送るので、1 分あれば十分に余裕がある。
CONNECTION_TIMEOUT = 60


#: 同時に開いておける接続の数。ブラウザが 1 つの相手に張る接続は多くて 6 本
#: ほどなので、画面を複数開いても足りる。超えた接続はすぐに閉じる。
#: 上限が無いと、接続を開いたまま居座るだけでスレッドを増やし続けられる。
MAX_CONNECTIONS = 32


#: 使われていないかを確かめる間隔 (秒)
IDLE_CHECK_INTERVAL = 30


#: 画面が API を呼ぶときに合言葉を載せるヘッダー
TOKEN_HEADER = "X-DLsite-Deck-Token"


#: 合言葉を渡す URL の ``#`` の後ろの名前 (``#t=<合言葉>``)
TOKEN_FRAGMENT = "t"


def app_url(host: str, port: int, token: str) -> str:
    """ブラウザで開く URL。合言葉を ``#`` の後ろに付ける。

    ``#`` の後ろはブラウザの中だけで使われ、サーバーには送られない。
    合言葉を画面の HTML に入れると、``/`` を取得するだけで誰でも読めてしまう。
    """
    return f"http://{host or '127.0.0.1'}:{port}/#{TOKEN_FRAGMENT}={token}"


def _is_direct_host(value: str, port: int) -> bool:
    """``Host`` ヘッダーが、この待ち受けを直接指しているか。

    IP アドレスか localhost で、ポートが合っているものだけを通す。
    DNS リバインディングは、攻撃者のドメインを途中で 127.0.0.1 に向け直す手口で、
    ブラウザはそれを攻撃者のサイトと同じ出所として扱う。必ず**名前**で来るので、
    localhost 以外の名前を断れば防げる。
    """
    value = value.strip()
    if not value:
        return False

    if value.startswith("["):
        # IPv6 は [::1]:8765 の形で来る
        name, _, rest = value[1:].partition("]")
        if rest and not rest.startswith(":"):
            return False
        port_text = rest[1:]
    else:
        name, separator, port_text = value.rpartition(":")
        if not separator:
            name, port_text = value, ""

    try:
        given = int(port_text) if port_text else 80
    except ValueError:
        return False
    if given != port:
        return False

    if name.lower() == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


#: 合言葉が合わないときに返す文言。画面はこれを見て案内を出す。
TOKEN_MISMATCH = (
    "この画面からは操作できません。"
    " ツールの端末に表示された URL (末尾の #t=… まで含む) で開き直してください。"
    " ツールを起動し直した場合は、新しく開いたタブを使ってください。"
)


class DeckServer(ThreadingHTTPServer):
    """同時接続数に上限のある待ち受け。

    標準の ThreadingHTTPServer は接続ごとにスレッドを作り、上限も時間切れも
    無い。何も送らない接続を 200 本開いたところ、スレッドが 200 本増えたまま
    減らなかった。しかもそれらが終わるのを待つので、ツールを終了できなくなった。
    """

    #: 残った接続のスレッドを待たずに終了できるようにする
    daemon_threads = True

    def __init__(self, *args: Any, max_connections: int = MAX_CONNECTIONS, **kwargs: Any):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            # 上限を超えた接続は、スレッドを作らずにすぐ閉じる
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class IdleWatch:
    """しばらく使われていなければ、ツールを終わらせる合図を出す。

    デスクトップモードでは、ブラウザのタブを閉じてもツールは裏で動き続ける。
    待ち受けを開けたままにしておく理由は無いので、使われなくなったら閉じる。

    「使われている」とみなすもの:

    * 合言葉付きの API 呼び出し (画面は開いている間、1 分ごとに合図を送る)
    * 処理中の要求 (「Steam を終了する」の待ちなど)
    * ダウンロード・展開・順番待ち・表紙の作り直し (``busy``)

    合言葉の無い要求は数えない。よそのプログラムが接続し続けるだけで、
    ツールを居座らせられないようにするため。

    端末がスリープしていた間は数えない。確かめる間隔が大きく空いたら
    スリープ明けとみなし、そこから数え直す。起きた直後、画面が合図を送る
    より先に終了してしまわないため。
    """

    def __init__(
        self,
        minutes: int,
        busy: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        interval: float = IDLE_CHECK_INTERVAL,
    ) -> None:
        self.limit = max(0, minutes) * 60
        self.busy = busy
        self.clock = clock
        self.interval = interval
        self._lock = threading.Lock()
        now = clock()
        self._last = now
        self._previous_check = now
        self._in_flight = 0

    def begin(self) -> None:
        with self._lock:
            self._in_flight += 1
            self._last = self.clock()

    def end(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._last = self.clock()

    def should_stop(self) -> bool:
        """今終わらせるべきか。確かめる担当が一定間隔で呼ぶ。"""
        now = self.clock()
        with self._lock:
            gap = now - self._previous_check
            self._previous_check = now
            if gap > self.interval * 3:
                # スリープ明け。寝ていた間は数えない。
                self._last = now
            if self.limit <= 0:
                return False
            if self._in_flight or self.busy():
                self._last = now
                return False
            return now - self._last >= self.limit


class Handler(BaseHTTPRequestHandler):
    backend: Backend  # make_handler() が差し込む
    #: 通信が止まったまま待つ上限 (:data:`CONNECTION_TIMEOUT`)
    timeout = CONNECTION_TIMEOUT
    #: 起動ごとに作る合言葉。:func:`make_handler` が差し込む。
    #: 空のままなら API は一切受け付けない。
    token: str = ""

    server_version = "dlsite_deck"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 既定の実装は要求のたびに stderr へ書き出すので、出さないようにする
        pass

    # -- 送信 ---------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        encoding = None

        # ライブラリの一覧は 80 KB 前後になる。圧縮すると 1 割程度に縮み、
        # 大きな応答で詰まる環境 (Windows のセキュリティ製品が loopback を
        # 覗く場合など) を避けられる。
        if len(body) > GZIP_THRESHOLD and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=6)
            encoding = "gzip"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if encoding:
            self.send_header("Content-Encoding", encoding)
        # 内容の種類を推測させず、参照元を送らせず、保存させない
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        if self.command == "HEAD":
            return

        # 一度に大量に流すと詰まる環境があるので小分けにする
        view = memoryview(body)
        for start in range(0, len(view), WRITE_CHUNK):
            self.wfile.write(view[start : start + WRITE_CHUNK])

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error_json(
        self,
        message: str,
        status: int = 400,
        kind: str = "",
        steps: tuple[str, ...] = (),
    ) -> None:
        payload: dict[str, Any] = {"error": message}
        if kind:
            payload["error_kind"] = kind
        if steps:
            payload["error_steps"] = list(steps)
        self._send_json(payload, status=status)

    def _send_safe_error(self, message: str) -> None:
        """エラー応答自体が失敗しても握りつぶす。

        接続が既に切れている場合、エラーを返そうとするとそこでも例外が出る。
        本来の失敗はログに出ているので、ここでは静かに諦める。
        """
        try:
            self._send_error_json(message, status=500)
        except _DISCONNECTED:
            pass

    def _discard_body(self) -> None:
        """断る要求の本文を読み捨てる。

        読まずに応答すると、受信側に残ったデータのせいで OS が接続を強制切断
        (RST) し、相手は断った理由を受け取れない。起動し直したあとに古い画面から
        送った要求がこれに当たり、「読み込み直してください」ではなく通信失敗に
        見えてしまう。大きすぎる本文は読まずに接続を閉じる。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if 0 < length <= MAX_BODY:
            self.rfile.read(length)
        elif length != 0:
            self.close_connection = True

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise UiError("要求が大きすぎます。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UiError(f"要求を解釈できませんでした: {error}") from error

    # -- 出どころの確認 -------------------------------------------------

    def _refusal(self) -> str | None:
        """この要求を断る理由。受け付けてよければ ``None``。

        127.0.0.1 で待ち受けていても、同じ端末のブラウザで開いた他のサイトの
        ページはここへ要求を送れる。放っておくと、作品の削除や Steam の終了、
        起動オプションに任意のコマンドを入れた登録まで外から実行できてしまう。
        """
        host = self.headers.get("Host", "")
        if not _is_direct_host(host, self.server.server_address[1]):
            return "この宛先からの要求は受け付けません。"

        # 別のサイトから送られた要求には、ブラウザが送り元を Origin に書く。
        # 自分の画面からの要求なら自分と同じになる。
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() != f"http://{host}".lower():
            return "別のサイトからの要求は受け付けません。"

        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            # 画面そのもの。合言葉は入っていない (URL の # の後ろで渡す) ので、
            # 誰が取得してもかまわない。
            return None

        sent = self.headers.get(TOKEN_HEADER, "").encode("utf-8", "replace")
        if not self.token or not hmac.compare_digest(sent, self.token.encode("utf-8")):
            return TOKEN_MISMATCH

        # よそのページが事前確認なしで送れる POST は text/plain などに限られる。
        # JSON だけを受け付ければ、ブラウザは送る前にこちらへ確認を取りに来て、
        # こちらが許可しないので送られない。
        if self.command == "POST":
            kind = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if kind != "application/json":
                return "要求の形式が正しくありません。"

        return None

    # -- 経路 ---------------------------------------------------------

    def _dispatch(self, route: Callable[[], None]) -> None:
        """経路の処理を包み、失敗を画面に返せる形にそろえる。

        GET と POST で同じ後始末が要る。別々に書くと片方だけ直して
        食い違うので、例外の扱いはここ 1 か所にまとめてある。
        """
        try:
            reason = self._refusal()
            if reason:
                self._discard_body()
                self._send_error_json(reason, status=403, kind="forbidden")
                return

            # 合言葉を確かめた API 呼び出しだけを「使われている」に数える
            watch = getattr(self.backend, "idle", None)
            counted = watch is not None and self.path.startswith("/api/")
            if counted:
                watch.begin()
            try:
                route()
            finally:
                if counted:
                    watch.end()
        except _DISCONNECTED:
            # 相手が切った後にエラー応答を書こうとすると二重に失敗する
            return
        except UiError as error:
            self._send_error_json(
                str(error), kind=error.kind, steps=error.steps
            )
        except Exception as error:  # 予期しない失敗も画面に返す
            traceback.print_exc()
            self._send_safe_error(f"内部エラー: {error}")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch(self._post)

    def _get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        route = GET_ROUTES.get(parsed.path)
        if route is None:
            self._send_error_json("見つかりません", status=404)
            return

        self._send_json(route(self.backend, urllib.parse.parse_qs(parsed.query)))

    def _post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)

        # 経路が無くても本文は読み切っておくこと。読まずに応答を返すと、
        # 残った本文が次の要求の先頭として解釈されて接続が壊れる。
        payload = self._read_json()

        route = POST_ROUTES.get(parsed.path)
        if route is None:
            self._send_error_json("見つかりません", status=404)
            return

        self._send_json(route(self.backend, payload))


def _is_loopback(host: str) -> bool:
    """自分自身にだけ開いているか。"""
    return host in ("127.0.0.1", "localhost", "::1", "") or host.startswith("127.")


#: 応答の Server ヘッダーに入れている名前。自分の実体かどうかの判別に使う。
SERVER_NAME = "dlsite_deck"


def probe_port(host: str, port: int, timeout: float = 1.0) -> str:
    """待ち受け状況を調べる。

    戻り値は ``"free"`` / ``"ours"`` / ``"other"``。既に自分が動いている場合と、
    無関係のプログラムが使っている場合とで、利用者に伝えることが違うため区別する。
    """
    target = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host

    try:
        connection = socket.create_connection((target, port), timeout=timeout)
    except OSError:
        # つながらない = 誰も使っていない
        return "free"

    # つながった時点で「誰かが使っている」ことは確定。あとは相手が自分かどうか。
    # HTTP で応答しない相手 (無関係のサービス) を空きと誤判定しないよう、
    # 読み取りの失敗は "other" として扱う。
    with connection:
        try:
            connection.sendall(
                b"HEAD / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            head = connection.recv(512).decode("latin-1", errors="replace")
        except OSError:
            return "other"

    return "ours" if SERVER_NAME.lower() in head.lower() else "other"


def _ancestors(pid: int) -> set[int]:
    """自分から辿れる親プロセスの一覧。

    ``timeout`` や起動用のシェルは自分の祖先なので、別インスタンスとして
    数えてはいけない。
    """
    chain = {pid}
    current = pid

    for _ in range(24):  # 壊れた親子関係で回り続けないための上限
        try:
            stat = Path(f"/proc/{current}/stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        # "pid (comm) state ppid ..." の形。comm に空白や括弧が入りうるので
        # 最後の ')' より後ろを見る。
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 2 or not tail[1].isdigit():
            break
        current = int(tail[1])
        if current <= 1 or current in chain:
            break
        chain.add(current)

    return chain


def _is_serve_process(args: list[str]) -> bool:
    """引数の並びが ``python -m dlsite_deck ... serve`` かどうか。

    コマンドラインに文字列が含まれるだけでは判定しない。それだと、この判定を
    起動したシェル自身まで拾ってしまう。
    """
    if "serve" not in args:
        return False

    for index, value in enumerate(args):
        if value == "-m" and index + 1 < len(args) and args[index + 1] == "dlsite_deck":
            return True
        # スクリプトを直接指した場合
        if value.endswith("__main__.py") and "dlsite_deck" in value:
            return True

    return False


def find_other_instances() -> list[tuple[int, str]]:
    """同じツールの別プロセスを探す。

    2 つ動くと state.json を同時に書き換えうるので、見つけたら知らせる。
    ``/proc`` が無い環境では調べられないので空を返す。
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return []

    skip = _ancestors(os.getpid())
    found = []

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in skip:
            continue

        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue

        args = [part for part in raw.decode("utf-8", errors="replace").split("\x00") if part]
        if _is_serve_process(args):
            found.append((pid, " ".join(args)[:100]))

    return found


def make_handler(backend: Backend, token: str | None = None) -> type[Handler]:
    """待ち受けに渡す Handler を作る。

    合言葉は起動ごとに作り直す。前回の画面を開いたままのタブや、よそのページが
    どこかで知った古い合言葉では操作できないようにするため。
    """
    return type("BoundHandler", (Handler,), {
        "backend": backend,
        "token": token or secrets.token_urlsafe(32),
    })


def serve(
    cfg: config_module.Config,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Web UI を起動する。Ctrl-C で終了する。

    起動できなかった場合は、理由を出して 1 を返す (例外は投げない)。
    """
    url = f"http://{host if host else '127.0.0.1'}:{port}/"

    # 起動前の確認。塞がっていれば、何が使っているかを見てから諦める。
    occupied = probe_port(host, port)
    if occupied == "ours":
        print(f"既に起動しています: {url}", file=sys.stderr)
        print(
            "  起動中のツールの端末に表示された URL (# から後ろも含む) で開いてください。",
            file=sys.stderr,
        )
        print("  止めるには、動いている方の端末で Ctrl-C を押してください。", file=sys.stderr)
        return 1
    if occupied == "other":
        print(f"ポート {port} は別のプログラムが使っています。", file=sys.stderr)
        print(f"  別のポートを指定してください: --port {port + 1}", file=sys.stderr)
        return 1

    # ポートは空いていても、同じツールが別のポートで動いていると危ない。
    # state.json を同時に書き換えると記録が壊れる。
    others = find_other_instances()
    if others:
        print("警告: このツールが既に別プロセスで動いています。", file=sys.stderr)
        for pid, command in others[:3]:
            print(f"  PID {pid}: {command}", file=sys.stderr)
        print(
            "  2 つ同時に動かすと state.json の記録が壊れることがあります。",
            file=sys.stderr,
        )
        print("  不要な方を終了してください。", file=sys.stderr)
        print(file=sys.stderr)

    backend = Backend(cfg)
    handler = make_handler(backend)

    try:
        server = DeckServer((host, port), handler)
    except OSError as error:
        # 確認から実際の bind までの間に取られることもある
        print(f"待ち受けを開始できませんでした: {host}:{port}", file=sys.stderr)
        print(f"  {error}", file=sys.stderr)
        print(f"  別のポートを指定してください: --port {port + 1}", file=sys.stderr)
        return 1

    # 画面の「終了」から止められるようにする
    backend._server = server

    # 合言葉付きの URL。これを開いた画面だけが操作できる。
    url = app_url(host, server.server_port, handler.token)

    if not _is_loopback(host):
        # 合言葉があっても、通信は暗号化されない (HTTP)。他の端末に開くと、
        # 同じネットワークで盗み見た相手に合言葉ごと奪われる。止めはしないが、
        # 知らせずに開くことはしない。
        print()
        print("=" * 70, file=sys.stderr)
        print(
            f"警告: {host} で待ち受けます。他の端末からも接続できます。",
            file=sys.stderr,
        )
        print(
            "      操作には起動ごとの合言葉が要りますが、通信は暗号化されません。",
            file=sys.stderr,
        )
        print(
            "      同じネットワークで盗み見られると、合言葉と購入済み作品の一覧を",
            file=sys.stderr,
        )
        print(
            "      読まれ、ダウンロードや削除などの操作をされてしまいます。",
            file=sys.stderr,
        )
        print(
            "      信頼できないネットワークでは 127.0.0.1 のまま使ってください。",
            file=sys.stderr,
        )
        print("=" * 70, file=sys.stderr)
        print()

    # すぐに書き出す。出力を端末以外 (ログのファイルなど) に向けると Python は
    # 書き出しを溜めるので、画面を開くのに要る合言葉付きの URL が出てこない。
    print(f"Web UI を起動しました: {url}", flush=True)
    print("  別のウィンドウで開くときは、# から後ろも含めてこの URL を使ってください。")
    print("終了するには画面右上の「終了」を押すか、Ctrl-C を押してください。", flush=True)

    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    # 使われなくなったら自分で閉じる (デスクトップモードで裏に残り続けないため)
    stopping = threading.Event()
    auto_stopped = threading.Event()
    minutes = cfg.idle_shutdown_minutes
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        # config.json を手で書き違えても起動は止めない
        print(
            f"警告: idle_shutdown_minutes の値 ({minutes!r}) が正しくないため、"
            f"{config_module.Config().idle_shutdown_minutes} 分とします。",
            file=sys.stderr,
        )
        minutes = config_module.Config().idle_shutdown_minutes
    if minutes > 0:
        watch = IdleWatch(minutes, busy=backend.is_busy)
        backend.idle = watch
        print(f"{minutes} 分間使われなければ、自動で終了します。", flush=True)

        def keep_watch() -> None:
            while not stopping.wait(IDLE_CHECK_INTERVAL):
                if watch.should_stop():
                    auto_stopped.set()
                    server.shutdown()
                    return

        threading.Thread(target=keep_watch, daemon=True).start()

    try:
        server.serve_forever()
        if auto_stopped.is_set():
            print(f"{minutes} 分間使われなかったため、終了しました。")
        else:
            # 画面の「終了」から止めた場合はここに来る
            print("終了しました。")
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        stopping.set()
        server.server_close()


#: 画面そのもの。HTML・CSS・JavaScript をまとめて持つ。
#:
#: Python の文字列として埋め込んでいた時期があったが、``\n`` のような
#: エスケープが Python 側で解釈されてしまい、JavaScript の文字列が壊れる事故が
#: 何度か起きた。字面のまま扱える別ファイルに置いている。
PAGE_PATH = Path(__file__).with_name("page.html")


def _load_page() -> str:
    try:
        return PAGE_PATH.read_text(encoding="utf-8")
    except OSError as error:  # 配置漏れは起動時に分かるようにする
        raise RuntimeError(
            f"画面ファイルを読めませんでした: {PAGE_PATH}\n"
            "  dlsite_deck ディレクトリごと配置されているか確認してください。"
        ) from error


PAGE = _load_page()
