"""ローカル Web UI。

SteamDeck の Desktop Mode はタッチ操作が主なので、一覧の絞り込みや実行ファイルの
選択はブラウザ上で行えたほうが扱いやすい。標準ライブラリの ``http.server`` だけで
組んであり、追加の依存はない。

待ち受けは 127.0.0.1 のみ。ライブラリの内容も購入情報も外に出さない。
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import (
    __version__,
    api,
    archive,
    category,
    config as config_module,
    cookies,
    cover,
    download,
    state,
    install,
    link,
    proton,
    steam,
)

#: 設定画面の組み立て方。順番がそのまま画面の並びになる。
CONFIG_FIELDS = [
    {"key": "download_dir", "label": "キャッシュ（アーカイブ置き場）", "type": "path",
     "help": "ダウンロードしたアーカイブの一時置き場。展開後は既定で削除する"},
    {"key": "install_dirs", "label": "インストール先の一覧", "type": "list",
     "help": "1 行に 1 つ。ダウンロード時にこの中から選ぶ"},
    {"key": "install_dir", "label": "既定のインストール先", "type": "install_default",
     "help": "上の一覧から選ぶ。ダウンロード画面で最初に選択された状態になる"},
    {"key": "state_file", "label": "状態ファイル", "type": "path",
     "help": "導入済みの記録"},
    {"key": "cookie_source", "label": "Cookie の取得元", "type": "choice",
     "choices": [{"value": "firefox", "label": "Firefox から読む"},
                 {"value": "manual", "label": "書き出したファイルから読む"}]},
    {"key": "cookie_file", "label": "Cookie ファイル", "type": "path",
     "help": "取得元が「書き出したファイル」のときに読む", "depends": "cookie_source=manual"},
    {"key": "directory_template", "label": "展開先の名前", "type": "text",
     "help": "{romaji} と {id} が使える"},
    {"key": "long_vowel", "label": "長音符「ー」の扱い", "type": "choice",
     "choices": [{"value": "repeat", "label": "母音を重ねる（スーパー→suupaa）"},
                 {"value": "drop", "label": "捨てる（スーパー→supa）"}]},
    {"key": "strip_versions", "label": "名前からバージョン番号を除去", "type": "bool"},
    {"key": "flatten_single_root", "label": "単一フォルダを展開先直下に引き上げる",
     "type": "bool"},
    {"key": "delete_archives", "label": "展開後にアーカイブを削除", "type": "bool"},
    {"key": "include_non_games", "label": "一括取得でゲーム以外も対象にする", "type": "bool"},
    {"key": "register_to_steam", "label": "ダウンロード後に自動で Steam 登録",
     "type": "bool"},
    {"key": "steam_userdata_dir", "label": "Steam の userdata", "type": "path",
     "help": "空なら自動検出"},
    {"key": "steam_grid_images", "label": "作品画像を表紙・背景に使う", "type": "bool"},
    {"key": "steam_grid_slots", "label": "画像を設定するスロット", "type": "slots",
     "help": "DLsite の画像は 4:3 なので、縦長のカバーは左右が切れる",
     "depends": "steam_grid_images=true"},
    {"key": "steam_cover_title", "label": "縦長カバーにタイトルを載せる", "type": "bool",
     "help": "作品画像を中心から 2:3 に切り出し、下部にタイトルを描く。"
             " ライブラリ一覧でゲーム名が分かるようになる（rsvg-convert が要る）",
     "depends": "steam_grid_images=true"},
    {"key": "steam_logo_source", "label": "ロゴ画像に使うもの", "type": "choice",
     "choices": [{"value": "title", "label": "タイトルの透過 PNG"},
                 {"value": "exe", "label": "exe の一番大きなアイコン"},
                 {"value": "none", "label": "設定しない"}]},
    {"key": "rebuild_covers", "label": "登録済みの画像を作り直す", "type": "action",
     "button": "カバーを作り直す",
     "help": "上の設定を保存したあとに押す。登録済みのゲームの表紙・背景・ロゴを"
             " 今の設定どおりに作り直し、使わなくなった画像は引き上げる。"
             " 元の画像は作品フォルダに残してあるので、通常は再ダウンロードしない"},
    {"key": "steam_compat_tool", "label": "登録時に割り当てる Proton", "type": "compat_tool",
     "help": "新しく登録するゲームに適用する。既に割り当て済みのものは変更しない"},
    {"key": "steam_executable", "label": "Steam の実行ファイル", "type": "path",
     "help": "「Steam を終了する」で使う。既定は SteamDeck の場所。"
             " ここに無ければ PATH と Steam のフォルダからも探す"},
    {"key": "steam_launch_options", "label": "既定の起動オプション", "type": "text",
     "help": "登録画面の初期値になる。日本語の文字化けを避けるには"
             " ロケールを渡す。例: LANG=ja_JP.UTF-8 ~/locales/run.sh %command%"},
    {"key": "tool_dirs", "label": "展開ツールを探す場所", "type": "list",
     "help": "7z や unrar が PATH に無い場合のディレクトリ。1 行に 1 つ"},
    {"key": "exclude", "label": "対象外にする作品 ID", "type": "list",
     "help": "1 行に 1 つ"},
]

#: 画像スロットの並びと表示名。設定画面の選択肢と、保存時の検証で共有する。
GRID_SLOT_LABELS = (
    ("capsule", "横長カプセル"),
    ("cover", "カバー（縦長）"),
    ("hero", "背景"),
)

#: 選択式の項目で、値が外れていたときに出す文言。
#: 選べる値そのものは CONFIG_FIELDS から引く。
_CHOICE_MESSAGES = {
    "cookie_source": "Cookie の取得元は firefox か manual を指定してください。",
    "long_vowel": "長音符の扱いは repeat か drop を指定してください。",
    "steam_logo_source": "ロゴ画像は title か exe か none を指定してください。",
}


def _choice_values(key: str) -> list[str]:
    """設定画面が出している選択肢の値。"""
    for field_def in CONFIG_FIELDS:
        if field_def["key"] == key:
            return [choice["value"] for choice in field_def.get("choices", [])]
    return []


#: ライブラリ取得は時間がかかるので、この秒数だけ結果を使い回す
LIBRARY_CACHE_SECONDS = 300

#: これを超える応答は gzip で送る
GZIP_THRESHOLD = 4096

#: 1 回の write で流す最大バイト数
WRITE_CHUNK = 16384

#: 相手が接続を切ったときに出る例外。
#: Windows ではセキュリティ製品が loopback を切ることがあり (WinError 10053)、
#: 応答の途中でこれらが飛んでくる。
_DISCONNECTED = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)


class UiError(RuntimeError):
    """UI からの操作が失敗した。"""

    #: 画面で出し分けるための種別。空なら普通のエラーとして出す。
    kind = ""

    #: 次に何をすればよいか。画面ではこれを手順として並べる。
    steps: tuple[str, ...] = ()


class LoginRequired(UiError):
    """DLsite のログインが要る。

    利用者の手違いではなく**まだ済ませていないだけ**なので、失敗として赤く
    出すのではなく、何をすればよいかを画面に並べる。

    **ツールの再起動は要らない。** セッションは成功したときにしか覚えないので、
    ブラウザでログインしてから「再読み込み」を押せばその場で通る。
    """

    kind = "login"
    steps = (
        "Firefox で https://www.dlsite.com/ にログインする（2FA も済ませる）",
        "続けて https://play.dlsite.com/ を一度開く（こちらのセッションが要る）",
        "この画面の「再読み込み」を押す",
    )


def _new_cover_task() -> dict[str, Any]:
    """カバー作り直しの進み具合の初期値。"""
    return {
        "running": False,
        "done": 0,
        "total": 0,
        "written": 0,
        "removed": 0,
        "errors": [],
        "message": "",
    }


# ----------------------------------------------------------------------
# ジョブ
# ----------------------------------------------------------------------


@dataclass
class Job:
    """ダウンロードのような時間のかかる処理 1 件。"""

    id: int
    work_id: str
    title: str
    #: "queued" | "running" | "done" | "error" | "cancelled"
    status: str = "queued"
    #: "download" | "extract"。展開中は途中で止められない。
    phase: str = "download"
    message: str = ""
    #: 現在処理しているファイル名
    current: str = ""
    written: int = 0
    total: int | None = None
    #: 順番待ちに入った時刻。並び順もこれで決まる。
    queued_at: float = field(default_factory=time.time)
    #: 実際に走り始めた時刻
    started_at: float | None = None
    finished_at: float | None = None
    #: 中止を指示されたか
    cancel_requested: bool = False
    #: 片付けの対象。中止時にここだけを消す。
    cache_dir: Path | None = None
    #: このジョブが新しく作った展開先。既存の導入を消さないよう、
    #: 元からあったフォルダは片付けの対象にしない。
    created_output: Path | None = None
    #: 順番待ちの位置 (1 が次)。表示のために外から入れる。
    queue_position: int | None = None

    @property
    def cancellable(self) -> bool:
        """今この瞬間、中止を受け付けられるか。

        順番待ちのものは何も始めていないので、いつでも取り消せる。
        走っている分は、展開が外部コマンドに委ねられていて途中で安全に
        止められないため、ダウンロード中だけ受け付ける。
        """
        if self.status == "queued":
            return True
        return self.status == "running" and self.phase == "download"

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "work_id": self.work_id,
            "title": self.title,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "current": self.current,
            "written": self.written,
            "total": self.total,
            "written_label": download.format_size(self.written),
            "total_label": download.format_size(self.total),
            "ratio": (self.written / self.total) if self.total else None,
            "elapsed": round(
                (self.finished_at or time.time()) - (self.started_at or self.queued_at), 1
            ),
            "cancellable": self.cancellable,
            "cancel_requested": self.cancel_requested,
            #: 順番待ちの何番目か。走っているものと済んだものは None。
            "queue_position": self.queue_position,
        }


class Backend:
    """UI からの要求を実処理につなぐ。

    HTTP ハンドラは要求ごとに別スレッドで動くので、状態を触る箇所は錠で守る。
    """

    def __init__(self, cfg: config_module.Config) -> None:
        self.config = cfg
        # 展開ツールの探索先は archive 側が持つので起動時に渡しておく
        archive.add_tool_dirs(cfg.tool_dirs)
        self._lock = threading.Lock()
        self._client: api.DlsiteClient | None = None
        self._library: list[api.Work] = []
        self._unavailable: list[str] = []
        self._library_fetched_at = 0.0
        self._jobs: dict[int, Job] = {}
        #: 順番待ちの引数。走り出すまで持っておく。
        self._queued_args: dict[int, tuple[api.Work, Path]] = {}
        #: 順番待ちを捌く担当。同時に 1 本だけ。
        self._worker: threading.Thread | None = None
        #: 担当が動いているか。起動の判断は必ず _lock の中で行う。
        self._worker_active = False
        #: 終了要求を受けたときに畳む相手。serve() が差し込む。
        self._server: ThreadingHTTPServer | None = None
        self._next_job_id = 1
        #: 同時に走らせるダウンロードは 1 件に絞る
        self._download_lock = threading.Lock()
        #: カバーの作り直しの進み具合
        self._cover_task: dict[str, Any] = _new_cover_task()

    # -- セッション ---------------------------------------------------

    def client(self) -> api.DlsiteClient:
        with self._lock:
            if self._client is not None:
                return self._client

        try:
            found = (
                cookies.load_manual(self.config.cookie_path)
                if self.config.cookie_source == "manual"
                else cookies.load_firefox()
            )
        except cookies.CookieError as error:
            # 「まだログインしていない」も「読めなかった」もここに来る。
            # 利用者から見ればどちらも次にやることは同じ。
            raise LoginRequired(str(error)) from error

        if not found:
            raise LoginRequired("DLsite の Cookie が見つかりません。")

        client = api.DlsiteClient(cookies.to_cookiejar(found))
        if not client.ensure_session():
            raise LoginRequired("DLsite のセッションが切れています。")

        with self._lock:
            self._client = client
        return client

    # -- ライブラリ ---------------------------------------------------

    def library(self, refresh: bool = False) -> tuple[list[api.Work], list[str]]:
        with self._lock:
            fresh = time.time() - self._library_fetched_at < LIBRARY_CACHE_SECONDS
            if self._library and fresh and not refresh:
                return self._library, self._unavailable

        result = self.client().library()
        excluded = set(self.config.exclude)
        works = [work for work in result.works if work.id not in excluded]

        with self._lock:
            self._library = works
            self._unavailable = result.unavailable
            self._library_fetched_at = time.time()

        return works, result.unavailable

    def state(self) -> state.State:
        return state.State.load(self.config.state_path)

    def _userdata(self) -> Path:
        """Steam の userdata を返す。無ければ理由を添えて断る。

        同じ確認が各所に散っていて、案内の文言が 3 通りに割れていた。
        ここに寄せて揃える。
        """
        userdata = config_module.steam_userdata_path(self.config)
        if userdata is None:
            raise UiError(
                "Steam の userdata が見つかりません。"
                " 設定の steam_userdata_dir で場所を指定してください。"
            )
        return userdata

    def _require_installed(
        self, work_id: str, current: "state.State | None" = None
    ) -> state.InstalledWork:
        """展開済みの作品を引く。記録だけあって実体が無いものは断る。

        SD カードが外れているとこの状態になる。呼び出し側で個別に確かめると
        文言が割れるので、ここに寄せる。
        """
        entry = (current or self.state()).get(work_id)
        if entry is None or not entry.path.is_dir():
            raise UiError(f"{work_id} はまだ展開されていません。")
        return entry

    def _require_entry(
        self, work_id: str, current: "state.State | None" = None
    ) -> state.InstalledWork:
        """記録を引く。無ければ断る。"""
        entry = (current or self.state()).get(work_id)
        if entry is None:
            raise UiError(f"{work_id} の記録がありません。")
        return entry

    def registered_shortcuts(self) -> "tuple[set[int], set[str], set[str]] | None":
        """実際に登録されているショートカットの AppID と名前を集める。

        記録 (``state.json`` の ``steam_app_id``) だけを見ると、Steam 側で直接
        削除されたときに古いまま残る。実ファイルを見れば本当のところが分かる。

        Steam が見つからない環境では ``None`` を返す。その場合は判定できないので、
        呼び出し側は「分からない」として扱うこと (嘘の断定をしない)。

        戻り値は ``(AppID, 名前, 別環境の名前)``。3 つ目は、パスから**別の
        プラットフォームのものと確証が持てた**エントリの名前。これはこちらの
        登録として数えず、触りもしない。
        """
        userdata = config_module.steam_userdata_path(self.config)
        if userdata is None:
            return None

        app_ids: set[int] = set()
        names: set[str] = set()
        foreign: set[str] = set()
        try:
            config_dirs = steam.find_user_config_dirs(userdata)
        except steam.SteamError:
            return None

        for config_dir in config_dirs:
            document = steam.load_shortcuts(config_dir / "shortcuts.vdf")
            for entry in document.get("shortcuts", {}).values():
                if not isinstance(entry, dict):
                    continue
                name = str(steam.get_field(entry, "AppName") or "")

                # Steam はデバイス間で非 Steam ゲームの情報を見せることがある。
                # 別環境のパスを持つものをこちらの登録として数えると、動かない
                # ものを「登録済み」と扱ってしまう。
                if steam.is_foreign_shortcut(entry):
                    foreign.add(name)
                    continue

                names.add(name)
                app_id = steam.get_field(entry, "appid")
                if isinstance(app_id, int):
                    # shortcuts.vdf は符号付き、記録は符号なしで持っている
                    app_ids.add(app_id)
                    app_ids.add(app_id + 2**32 if app_id < 0 else app_id)
        return app_ids, names, foreign

    def library_json(self, refresh: bool = False) -> dict[str, Any]:
        works, unavailable = self.library(refresh=refresh)
        current = self.state()
        counts: dict[str, int] = {}
        items = []

        # 取得しただけの作品と Steam に登録済みの作品を見分けるために使う
        shortcuts = self.registered_shortcuts()

        for status in state.build_status(works, current):
            work = status.work
            counts[work.category] = counts.get(work.category, 0) + 1
            entry = status.entry
            items.append(
                {
                    "id": work.id,
                    "title": work.title,
                    "maker": work.maker,
                    "category": work.category,
                    "category_label": work.category_label,
                    "work_type": work.work_type,
                    "size": work.content_size,
                    "size_label": download.format_size(work.content_size),
                    "installed": status.installed,
                    "outdated": status.outdated,
                    "update_unknown": status.update_unknown,
                    # None は「Steam が見つからず判定できない」
                    "steam_registered": _is_registered(entry, shortcuts),
                    # 同名のショートカットが別環境のものとして存在する
                    "steam_foreign": _is_foreign(entry, shortcuts),
                    # 日付は日本時間で出す (DLsite は JST 0 時を UTC 15:00 で返す)
                    "upgrade_date": api.format_date(work.updated_at),
                    # 並べ替えに使う生の値。表示はしないが順序が要る。
                    "upgrade_sort": (
                        work.updated_at.isoformat() if work.updated_at else ""
                    ),
                    "purchased_at": api.format_date(work.purchased_at),
                    "purchased_sort": (
                        work.purchased_at.isoformat() if work.purchased_at else ""
                    ),
                    "installed_upgrade_date": (
                        api.format_date(entry.upgrade_date) if entry else ""
                    ),
                    # upgrade_date が無い作品の予備。タイトルに書かれた版と導入日時。
                    "version": work.version or "",
                    "installed_version": (entry.version if entry else "") or "",
                    "installed_at": (
                        api.format_datetime(entry.installed_at) if entry else ""
                    ),
                    "status_label": status.label,
                    "directory": entry.directory if entry else None,
                    "executable": entry.executable if entry else None,
                    "steam_title": (entry.steam_title if entry else None) or work.title,
                    "steam_app_id": entry.steam_app_id if entry else None,
                }
            )

        # 既定の並び。手を動かす必要がある順に置く。
        # 同じ段の中はタイトル順 (画面の他の並べ替えと揃える)。
        items.sort(key=lambda item: (_default_rank(item), item["title"].strip()))

        return {
            "categories": [
                {
                    "key": item.key,
                    "label": item.label,
                    "count": counts.get(item.key, 0),
                }
                for item in category.CATEGORIES
            ],
            "items": items,
            "unavailable": unavailable,
            "steam": self.steam_status(),
            "paths": self.path_warnings(),
            "install_dirs": self.config.install_dirs,
            "install_dir": self.config.install_dir,
            "delete_archives": self.config.delete_archives,
        }

    # -- 状態 ---------------------------------------------------------

    def _probe_session(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Cookie が読めるか、セッションが生きているかを確かめる。

        ``status_json`` の中で一番手数が多く、通信も伴う部分。応答の組み立てと
        混ざっていると読みにくいので分けてある。

        有効なセッションが作れた場合は覚えておく。状態画面を開いた直後に
        一覧を取りに行くとき、もう一度張り直さずに済む。
        """
        cfg = self.config

        cookie_state: dict[str, Any] = {"ok": False, "detail": "", "hosts": []}
        session_state: dict[str, Any] = {"ok": False, "detail": "未確認", "purchases": None}

        try:
            found = (
                cookies.load_manual(cfg.cookie_path)
                if cfg.cookie_source == "manual"
                else cookies.load_firefox()
            )
        except cookies.CookieError as error:
            found = []
            cookie_state["detail"] = str(error)

        if found:
            by_host: dict[str, list[str]] = {}
            for cookie in found:
                by_host.setdefault(cookie.host, []).append(cookie.name)
            cookie_state = {
                "ok": True,
                "detail": f"{len(found)} 件",
                "hosts": [
                    {"host": host, "names": sorted(names)}
                    for host, names in sorted(by_host.items())
                ],
            }

            try:
                client = api.DlsiteClient(cookies.to_cookiejar(found))
                if client.validate_session():
                    session_state = {"ok": True, "detail": "有効", "purchases": None}
                elif client.refresh_session():
                    session_state = {
                        "ok": True,
                        "detail": "張り直して有効",
                        "purchases": None,
                    }
                else:
                    session_state = {
                        "ok": False,
                        "detail": "無効または期限切れ",
                        "purchases": None,
                    }

                if session_state["ok"]:
                    count = client.content_count()
                    session_state["purchases"] = count.get("user")
                    with self._lock:
                        self._client = client
            except api.DlsiteApiError as error:
                session_state = {"ok": False, "detail": str(error), "purchases": None}
        elif not cookie_state["detail"]:
            cookie_state["detail"] = "DLsite の Cookie が見つかりません"

        return cookie_state, session_state

    def status_json(self) -> dict[str, Any]:
        """CLI の ``check`` と同じ内容を UI 向けに返す。

        Cookie が無い / セッションが切れている場合もここで分かるようにして、
        端末に戻らなくても対処できるようにする。
        """
        cfg = self.config

        cookie_state, session_state = self._probe_session()

        rar_tool = archive._find_rar_tool()
        try:
            import pykakasi  # noqa: F401

            pykakasi_state = {"ok": True, "detail": "あり"}
        except ImportError:
            pykakasi_state = {
                "ok": False,
                "detail": "なし（漢字タイトルは英語タイトルか作品 ID になる）",
            }

        free = download.free_space(cfg.install_path)

        return {
            "login_steps": list(cookies.LOGIN_STEPS),
            "login_note": cookies.LOGIN_NOTE,
            "cookies": cookie_state,
            "session": session_state,
            "paths": [
                {
                    "label": label,
                    "path": str(path),
                    "inside_project": config_module.is_inside_project(path),
                }
                for label, path in (
                    ("ダウンロード", cfg.download_path),
                    ("展開先", cfg.install_path),
                    ("状態ファイル", cfg.state_path),
                )
            ],
            "free_space": download.format_size(free) if free is not None else None,
            "archive_tool": (
                {"ok": True, "detail": f"{rar_tool[0]}（{rar_tool[1]}）"}
                if rar_tool
                else {
                    "ok": False,
                    "detail": "未検出。分割 RAR は展開できない"
                    "（7z / unar / unrar / bsdtar のいずれかが要る）",
                }
            ),
            "pykakasi": pykakasi_state,
            "steam": self.steam_status(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "version": __version__,
        }

    # -- 設定 ---------------------------------------------------------

    def config_json(self) -> dict[str, Any]:
        """設定の現在値と、UI が編集欄を組み立てるための定義を返す。"""
        values = asdict(self.config)
        return {
            "path": str(config_module.default_config_path()),
            "project_root": str(config_module.PROJECT_ROOT),
            "values": values,
            "fields": CONFIG_FIELDS,
            "categories": [
                {"key": item.key, "label": item.label} for item in category.CATEGORIES
            ],
            "grid_slots": [
                {"key": key, "label": label} for key, label in GRID_SLOT_LABELS
            ],
            "compat_tools": self._compat_tools(),
            # 「カバーにタイトルを載せる」を入れてよい環境か。
            # 入れられない場合は理由も返し、画面でその場に出す。
            "cover_title_supported": cover.title_supported(),
            "cover_title_problem": cover.title_support_problem(),
        }

    def _compat_tools(self) -> list[dict[str, str]]:
        """選べる Proton の一覧。検出できなければ現在値だけを返す。"""
        userdata = config_module.steam_userdata_path(self.config)
        tools: list[dict[str, str]] = []
        if userdata:
            try:
                tools = steam.list_compat_tools(userdata)
            except OSError:
                tools = []

        # 設定済みの値が一覧に無い場合でも選択肢から消えないようにする
        current = self.config.steam_compat_tool
        if current and not any(tool["value"] == current for tool in tools):
            tools.insert(0, {"value": current, "label": f"{current}（未検出）"})
        return tools

    def _validate_config(self, merged: dict[str, Any]) -> None:
        """設定の中身を確かめる。おかしければ :class:`UiError` を送出する。

        ``merged`` は必要なら整えて返す (インストール先の空行を落とすなど)。
        """
        # 型は現在値から見る
        for key, value in merged.items():
            expected = type(getattr(self.config, key))
            if expected is bool and not isinstance(value, bool):
                raise UiError(f"{key} は true/false で指定してください。")
            if expected is str and not isinstance(value, str):
                raise UiError(f"{key} は文字列で指定してください。")
            if expected is list and not isinstance(value, list):
                raise UiError(f"{key} は一覧で指定してください。")

        # 選べる値は CONFIG_FIELDS が正。ここに書き写すと、項目を増やしたときに
        # 画面には出るのに保存できない、という食い違いが起きる。
        for key, message in _CHOICE_MESSAGES.items():
            allowed = _choice_values(key)
            if allowed and merged.get(key) not in allowed:
                raise UiError(message)

        allowed_slots = {key for key, _label in GRID_SLOT_LABELS}
        for slot in merged.get("steam_grid_slots", []):
            if slot not in allowed_slots:
                raise UiError(f"知らない画像スロットです: {slot}")

        for key in ("download_dir", "install_dir", "state_file"):
            if not str(merged.get(key, "")).strip():
                raise UiError(f"{key} を指定してください。")

        install_dirs = [
            str(item).strip() for item in merged.get("install_dirs", []) if str(item).strip()
        ]
        if not install_dirs:
            raise UiError("インストール先を 1 つ以上指定してください。")
        if merged["install_dir"] not in install_dirs:
            raise UiError(
                "既定のインストール先は一覧に含まれている必要があります: "
                + merged["install_dir"]
            )
        merged["install_dirs"] = install_dirs

    def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        """UI から届いた設定を検証して書き出す。"""
        known = set(config_module.Config.__dataclass_fields__)
        unknown = sorted(set(values) - known)
        if unknown:
            raise UiError(f"知らない設定項目です: {', '.join(unknown)}")

        merged = asdict(self.config)
        merged.update(values)
        self._validate_config(merged)

        # 切り替えたときだけ知らせる。使えない環境で毎回言われても煩いだけ。
        warnings = []
        if merged.get("steam_cover_title") and not self.config.steam_cover_title:
            problem = cover.title_support_problem()
            if problem:
                warnings.append("カバーにタイトルを載せられません。" + problem)

        updated = config_module.Config(**merged)

        # 設定画面には外部パスの警告を出しているので、保存した時点で承認とみなす。
        # そうしないと、端末の無い起動 (デスクトップや Steam から) で毎回止まる。
        config_module.acknowledge(updated, config_module.external_paths(updated))
        path = config_module.save(updated)

        self.config = updated
        archive.add_tool_dirs(updated.tool_dirs)
        with self._lock:
            # 取得元が変わった可能性があるのでセッションを作り直させる
            self._client = None
            self._library = []
            self._library_fetched_at = 0.0

        external = [str(item) for item in config_module.external_paths(updated)]
        return {"saved": str(path), "external": external, "warnings": warnings}

    # -- 実行ファイル -------------------------------------------------

    def _refuse_if_foreign(self, entry: state.InstalledWork, action: str) -> None:
        """別環境の登録と名前がぶつかるなら、触らずに断る。

        ``shortcuts.vdf`` の照合は AppName で行うため、別環境のエントリと同名だと
        そちらを書き換えたり消したりしてしまう。名前を変えれば操作できる。
        """
        shortcuts = self.registered_shortcuts()
        if not _is_foreign(entry, shortcuts):
            return

        name = entry.steam_title or entry.title
        raise UiError(
            f"「{name}」と同じ名前のショートカットが、別の環境 (別 OS) のものとして"
            f" Steam に登録されています。このまま{action}すると、そちらを"
            " 書き換えてしまうため中止しました。"
            " 登録名を変えるか、Steam 側で重複を整理してください。"
        )

    def _current_launch_options(self, entry: state.InstalledWork) -> str:
        """既に登録されているショートカットの起動オプションを読む。

        登録し直すときに、Steam 側で設定した内容を画面に出すため。空を返せば
        呼び出し側が設定の既定値を使う。
        """
        userdata = config_module.steam_userdata_path(self.config)
        if userdata is None:
            return ""

        name = entry.steam_title or entry.title
        for config_dir in steam.find_user_config_dirs(userdata):
            document = steam.load_shortcuts(config_dir / "shortcuts.vdf")
            for existing in document.get("shortcuts", {}).values():
                if not isinstance(existing, dict):
                    continue
                if steam.get_field(existing, "AppName") == name:
                    return str(steam.get_field(existing, "LaunchOptions") or "")
        return ""

    def executables(self, work_id: str) -> dict[str, Any]:
        entry = self._require_installed(work_id)

        candidates = archive.executable_candidates(entry.path)
        return {
            "work_id": work_id,
            "directory": str(entry.path),
            "title": entry.title,
            "steam_title": entry.steam_title or entry.title,
            "selected": entry.executable,
            "launch_options": self._current_launch_options(entry)
            or self.config.steam_launch_options,
            "candidates": [
                {
                    "path": str(item.path),
                    "relative": item.relative,
                    "size": item.size,
                    "size_label": download.format_size(item.size),
                    "suspicious": item.suspicious,
                }
                for item in candidates
            ],
        }

    # -- 終了 -----------------------------------------------------------

    def shutdown(self) -> dict[str, Any]:
        """待ち受けを畳んで、プロセスを終わらせる。

        ``server.shutdown()`` は要求を捌いているスレッドから呼ぶと自分の完了を
        待って固まる。応答を返しきってから別のスレッドで叩く必要がある。

        走っている取得があるかは呼び出し側 (画面) が確認して尋ねる。ここまで
        来たら止める。
        """
        server = self._server
        if server is None:
            raise UiError("終了の手続きが用意されていません。")

        with self._lock:
            abandoned = sum(
                1 for job in self._jobs.values()
                if job.status in ("running", "queued")
            )

        def stop() -> None:
            # 応答が相手に届くだけの間を置いてから畳む
            time.sleep(0.4)
            server.shutdown()

        threading.Thread(target=stop, daemon=True).start()
        return {"message": "終了します。", "abandoned": abandoned}

    # -- 置き場の確認 ----------------------------------------------------

    def path_warnings(self) -> dict[str, Any]:
        """設定した置き場が実在するかを確かめる。

        SteamDeck では展開先を SD カードに置くのが普通で、カードが外れていたり
        マウントされていなければ丸ごと見えなくなる。その状態では導入済みの作品が
        一斉に「記録のみ」になり、取り直しを促す表示になってしまう。
        気付けるように、上部で知らせる。
        """
        current = self.state()
        checked: list[dict[str, Any]] = []

        targets = [(directory, "インストール先") for directory in self.config.install_dirs]
        targets.append((str(self.config.download_path), "キャッシュ"))

        missing = []
        for raw, label in targets:
            path = Path(raw)
            if path.is_dir():
                continue
            # この置き場に記録されているのに実体が無いもの
            affected = [
                work for work in current.works.values()
                if not work.path.is_dir() and _is_under(work.path, path)
            ]
            entry = {
                "path": str(path),
                "label": label,
                "record_only": len(affected),
                # 取り外せる場所に見えるか
                "removable": _looks_removable(path),
            }
            missing.append(entry)

        record_only_total = sum(
            1 for work in current.works.values() if not work.path.is_dir()
        )
        return {
            "missing": missing,
            "record_only_total": record_only_total,
            # 記録のみが出ていて、その置き場が取り外せる場所なら、まず外れを疑う
            "suggest_removable": any(
                item["removable"] and item["record_only"] for item in missing
            ),
        }

    # -- Steam の起動状態 ------------------------------------------------

    def _steam_blocked(self, action: str) -> str:
        """Steam 起動中で操作できないときの案内。

        ゲームモードでは Steam を終了できないので「終了してください」は
        実行できない指示になる。Desktop Mode へ切り替えてもらう。
        """
        if steam.session_kind() == steam.SESSION_GAMING:
            return (
                f"ゲームモードでは{action}できません。"
                " ゲームモードでは Steam を終了できないため、"
                " Desktop Mode に切り替えて操作してください。"
            )
        return (
            f"Steam が起動中のため{action}できません。"
            " 終了してからやり直してください"
            " (起動中に書き換えても Steam の終了時に上書きされます)。"
        )

    def steam_state(self) -> dict[str, Any]:
        """Steam が動いているかだけを返す軽い問い合わせ。

        画面の「Steam を終了しました」から押されて、実際のプロセスを見に行く。
        起動中は ``config.vdf`` / ``shortcuts.vdf`` への書き込みが Steam 終了時に
        上書きされてしまうため、その間の操作は塞ぐ。

        ゲームモードでは Steam を終了できない (Steam 自身がセッションで、
        このツールもそこから起動されている) ので、案内を変えられるように
        セッション種別も返す。
        """
        session = steam.session_kind()
        return {
            "running": steam.is_steam_running(),
            "session": session,
            "can_quit_steam": session != steam.SESSION_GAMING,
        }

    # -- カバーの作り直し ---------------------------------------------

    def cover_task(self) -> dict[str, Any]:
        """作り直しの進み具合。"""
        with self._lock:
            return dict(self._cover_task)

    def rebuild_covers(self) -> dict[str, Any]:
        """登録済みゲームのライブラリ画像を、今の設定どおりに作り直す。

        「カバーにタイトルを埋め込む」を切り替えたあと、登録し直さずに
        反映させるための入口。作るだけでなく、**使わなくなった画像は引き上げる**
        ので、設定を戻せば元の見た目に戻る。

        ストア画像は作品フォルダに残してあるものを使うため、たいていは通信が要らない。
        """
        # userdata が無ければ始める前に断る
        userdata = self._userdata()

        with self._lock:
            if self._cover_task["running"]:
                raise UiError("すでに作り直しています。")
            self._cover_task = _new_cover_task()
            self._cover_task["running"] = True

        worker = threading.Thread(
            target=self._rebuild_covers, args=(userdata,), daemon=True
        )
        worker.start()
        return self.cover_task()

    def _rebuild_covers(self, userdata: Path) -> None:
        try:
            self._rebuild_covers_inner(userdata)
        except Exception as error:  # 担当スレッドは何があっても畳む
            with self._lock:
                self._cover_task["message"] = f"作り直しに失敗しました: {error}"
        finally:
            with self._lock:
                self._cover_task["running"] = False

    def _rebuild_covers_inner(self, userdata: Path) -> None:
        current = self.state()
        _app_ids, _names, foreign = self.registered_shortcuts()

        targets = [
            entry
            for entry in current.works.values()
            if entry.steam_app_id
            and (entry.steam_title or entry.title) not in foreign
        ]

        with self._lock:
            self._cover_task["total"] = len(targets)

        config_dirs = steam.find_user_config_dirs(userdata)
        slots = list(self.config.steam_grid_slots)
        # 設定から外したスロットは、置いてあるものを引き上げる
        unused = [slot for slot in steam.GRID_SLOTS if slot != steam.LOGO_SLOT and slot not in slots]

        for entry in targets:
            written, removed, note = 0, 0, ""
            try:
                if not self.config.steam_grid_images:
                    for config_dir in config_dirs:
                        removed += len(
                            steam.remove_grid_images(config_dir, entry.steam_app_id)
                        )
                    note = "画像なしに戻した"
                else:
                    written, removed, note = self._rebuild_one(
                        entry, config_dirs, slots, unused
                    )
            except Exception as error:
                note = f"失敗: {error}"

            with self._lock:
                self._cover_task["done"] += 1
                self._cover_task["written"] += written
                self._cover_task["removed"] += removed
                if note.startswith("失敗"):
                    self._cover_task["errors"].append(
                        f"{entry.id} {entry.steam_title or entry.title}: {note}"
                    )

        with self._lock:
            task = self._cover_task
            task["message"] = (
                f"{task['done']} 件を見直し、画像 {task['written']} 枚を書き、"
                f"{task['removed']} 枚を引き上げました。"
                " Steam を起動し直すと表示に反映されます。"
            )

    def _rebuild_one(
        self,
        entry: state.InstalledWork,
        config_dirs: list[Path],
        slots: list[str],
        unused: list[str],
    ) -> tuple[int, int, str]:
        """1 件ぶんの画像を今の設定に合わせる。"""
        image = self.grid_image(entry)
        title = entry.steam_title or entry.title
        cover_image, logo = cover.artwork(self.config, title, image, entry.executable)

        written, removed = 0, 0

        for config_dir in config_dirs:
            if unused:
                removed += len(
                    steam.remove_grid_images(config_dir, entry.steam_app_id, slots=unused)
                )

            written += len(
                steam.apply_artwork(
                    config_dir,
                    entry.steam_app_id,
                    image=image,
                    cover_image=cover_image,
                    logo=logo,
                    slots=slots,
                )
            )

            # 「設定しない」を選んだときだけ引き上げる。
            #
            # 作れなかっただけの場合に消してはいけない。rsvg-convert が無い環境や
            # アイコンを持たない exe では作れず、そこで作り直すと、別の環境で
            # 付けたロゴを黙って剥がしてしまう。
            if not logo and self.config.steam_logo_source == "none":
                removed += len(
                    steam.remove_grid_images(
                        config_dir, entry.steam_app_id, slots=[steam.LOGO_SLOT]
                    )
                )

        if not image:
            return written, removed, "元の画像が無い"
        return written, removed, "作り直した"

    def stop_steam(self) -> dict[str, Any]:
        """Steam に終了してもらい、落ちるまで待つ。

        ``shortcuts.vdf`` などは Steam が抱えていて終了時に書き戻す。手で
        終わらせてもらう代わりに、ここから頼む。

        **ゲームモードでは行わない。** Steam 自身がセッションで、このツールも
        そこから起動されているため、終了するとツールごと落ちる。
        """
        if steam.session_kind() == steam.SESSION_GAMING:
            raise UiError(
                "ゲームモードでは Steam を終了できません。"
                " 終了するとこのツールごと落ちます。"
                " Desktop Mode に切り替えて操作してください。"
            )

        if not steam.is_steam_running():
            return {"running": False, "message": "Steam は起動していません。"}

        try:
            stopped = steam.shutdown_steam(
                config_module.steam_userdata_path(self.config),
                configured=self.config.steam_executable,
            )
        except steam.SteamError as error:
            raise UiError(str(error)) from error

        if stopped:
            return {"running": False, "message": "Steam を終了しました。"}

        return {
            "running": True,
            # 終了を頼んでも効かない環境がある。Ubuntu のコンテナで試したところ、
            # 指示は届いている ("command line was forwarded" がログに出る) のに
            # Steam 側が畳まなかった。無理に kill すると shortcuts.vdf を
            # 書き戻す前に死ぬので、手で閉じてもらうよう案内する。
            "message": "Steam がまだ終了していません。"
                       " 起動中のゲームがあれば、先に終了してください。"
                       " 環境によっては終了の指示が効かないことがあります。"
                       " その場合は Steam のメニュー（Steam → 終了）から閉じて、"
                       "「Steam を終了しました」を押してください。",
        }

    # -- 取得したファイルの削除 ------------------------------------------

    def delete_preview(self, work_id: str) -> dict[str, Any]:
        """削除する前に、何が消えて何が残るかを知らせる。

        DLC を他の作品に重ねてある場合、**重ねたファイルは相手側に残る**。
        記録だけ消えると利用者が相手の状態を追えなくなるので、先に伝える。
        """
        current = self.state()
        entry = self._require_entry(work_id, current)

        applied = []
        for item in current.links:
            if item.source_id != work_id or not item.applied_at:
                continue
            other = current.get(item.target_id)
            applied.append({
                "work_id": item.target_id,
                "title": other.title if other else item.target_id,
            })

        cache = self.config.download_path / work_id
        backups = link.backup_dirs_for(entry)
        backup_bytes = sum(_directory_size(path) for path in backups)

        return {
            "work_id": work_id,
            "title": entry.title,
            "directory": str(entry.path),
            "size_label": download.format_size(_directory_size(entry.path)),
            "cache_label": download.format_size(_directory_size(cache)),
            "registered": entry.steam_app_id is not None,
            # DLC を重ねられたときの退避。一緒に消えるので、先に見せておく。
            "backup_count": len(backups),
            "backup_label": download.format_size(backup_bytes),
            # ここに載る作品には、この DLC のファイルが残ったままになる
            "applied_to": applied,
        }

    def _discard_backups(self, entry: state.InstalledWork) -> tuple[int, int]:
        """その作品が上書きされたときの退避を片付ける。

        戻す先の作品ごと消すので、控えだけ残しても使い道がない。**その作品を
        名指しした退避だけ**を消し、他の作品のものには触れない。

        消す場所はインストール先の中に限る。設定を書き換えて別の場所を
        指させても、そこを掃除してしまわないようにするため。
        """
        backups = link.backup_dirs_for(entry)
        if not backups:
            return 0, 0

        roots = [root.resolve() for root in self.config.install_paths]
        freed, count = 0, 0

        for path in backups:
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not any(root in resolved.parents for root in roots):
                continue

            freed += _directory_size(resolved)
            shutil.rmtree(resolved, ignore_errors=True)
            if not resolved.exists():
                count += 1

        # 空になった入れ物は残さない
        root = link.backup_root_dir(entry)
        try:
            if root.is_dir() and not any(root.iterdir()):
                root.rmdir()
        except OSError:
            pass

        return freed, count

    def delete_work(self, work_id: str) -> dict[str, Any]:
        """展開したファイルとキャッシュを消し、記録からも外す。

        **Steam に登録済みのものは消さない。** 消すとライブラリから起動できない
        ショートカットだけが残るため、先に登録を解除してもらう。
        """
        current = self.state()
        entry = self._require_entry(work_id, current)

        if entry.steam_app_id is not None:
            raise UiError(
                f"{entry.title} は Steam に登録されています。"
                " 先に「解除」してから削除してください。"
            )

        target = entry.path
        removed_bytes = 0
        removed = False

        if target.is_dir():
            resolved = target.resolve()
            # 展開先として設定した場所の中にあるものだけを消す。設定を書き換えて
            # 別の場所を指させても、そこを消してしまわないようにする。
            roots = [root.resolve() for root in self.config.install_paths]
            if not any(root == resolved or root in resolved.parents for root in roots):
                raise UiError(
                    f"インストール先の外にあるため削除しません: {resolved}"
                )
            if resolved in roots:
                raise UiError(f"インストール先そのものは削除できません: {resolved}")

            removed_bytes = _directory_size(resolved)
            shutil.rmtree(resolved, ignore_errors=True)
            removed = not resolved.exists()
            if not removed:
                raise UiError(f"削除しきれませんでした: {resolved}")

        # ダウンロードしたアーカイブも片付ける。既定では展開後に消えているが、
        # 「アーカイブを残す」設定や、途中で失敗した取得の分が残ることがある。
        cache = self.config.download_path / work_id
        cached_bytes = 0
        if cache.is_dir():
            cached_bytes = _directory_size(cache)
            shutil.rmtree(cache, ignore_errors=True)

        # DLC を重ねられたときの退避も一緒に消す。
        #
        # 退避は作品フォルダの隣 (インストール先の直下) にあるので、作品を
        # 消しただけでは残り続ける。戻す相手が無くなった控えなので連れていく。
        backup_bytes, backup_count = self._discard_backups(entry)

        # この作品に関わる DLC の結び付けも記録から外す。
        # 既に重ねたファイルは相手側に残るので、それは伝える。
        dropped = [
            item for item in current.links
            if item.source_id == work_id or item.target_id == work_id
        ]
        left_behind = sorted({
            (current.get(item.target_id).title
             if current.get(item.target_id) else item.target_id)
            for item in dropped
            if item.source_id == work_id and item.applied_at
        })
        current.links = [item for item in current.links if item not in dropped]

        current.forget(work_id)
        current.save()

        freed = removed_bytes + cached_bytes + backup_bytes
        parts = [f"{entry.title} を削除しました"]
        if freed:
            parts.append(f"({download.format_size(freed)} を解放)")
        elif not target.is_dir():
            parts.append("(ファイルは既にありませんでした)")
        if backup_count:
            parts.append(f"DLC の退避 {backup_count} 件も片付けました。")
        if dropped:
            parts.append(f"DLC の結び付け {len(dropped)} 件も記録から外しました。")
        if left_behind:
            parts.append(
                "重ねたファイルは " + "、".join(left_behind) + " 側に残っています。"
            )

        return {
            "message": " ".join(parts),
            "freed": freed,
            "cache_freed": cached_bytes,
            "backup_freed": backup_bytes,
            "left_behind": left_behind,
        }

    # -- Proton の割り当て ----------------------------------------------

    def proton_json(self) -> dict[str, Any]:
        """登録済みゲームと、それぞれに割り当てられている Proton を並べる。

        書き込む先は Steam 自身が使う ``config.vdf`` の ``CompatToolMapping``
        で、設定画面から変えたときと同じ場所・同じ形。実機の 143 件はすべて
        ``name`` / ``config`` / ``priority`` の同じ形をしていた。
        """
        userdata = self._userdata()

        current = self.state()
        tools = steam.list_compat_tools(userdata)
        known = {item["value"]: item["label"] for item in tools}

        games = []
        for entry in current.works.values():
            if entry.steam_app_id is None or not entry.path.is_dir():
                continue
            assigned = steam.get_compat_tool(userdata, entry.steam_app_id) or ""
            prefix = proton.prefix_path(userdata, entry.steam_app_id)
            games.append({
                "work_id": entry.id,
                "title": entry.steam_title or entry.title,
                "maker": entry.maker,
                "app_id": entry.steam_app_id,
                "tool": assigned,
                # 一覧に無い名前が入っていることもある (消した GE-Proton など)
                "tool_label": known.get(assigned, assigned or "（未設定）"),
                "missing_tool": bool(assigned and assigned not in known),
                "has_prefix": prefix.is_dir(),
            })

        games.sort(key=lambda item: item["title"])
        return {
            "games": games,
            "tools": tools,
            "default_tool": self.config.steam_compat_tool,
            "steam_running": steam.is_steam_running(),
            "config_path": str(steam.compat_config_path(userdata)),
        }

    def set_proton(self, work_id: str, tool: str) -> dict[str, Any]:
        """1 本の Proton 割り当てを変える。

        **Steam が起動中は変更しない。** Steam は ``config.vdf`` を抱えたまま
        動き、終了時に書き戻すので、動作中に書き換えても捨てられる。黙って
        失敗するのが一番困るため、ここで断る。
        """
        userdata = self._userdata()

        if steam.is_steam_running():
            raise UiError(self._steam_blocked("Proton の変更が"))

        entry = self._require_entry(work_id)
        if entry.steam_app_id is None:
            raise UiError(f"{entry.title} は Steam に登録されていません。")

        tool = tool.strip()
        if not tool:
            raise UiError("使用する Proton を選んでください。")

        allowed = {item["value"] for item in steam.list_compat_tools(userdata)}
        if tool not in allowed:
            raise UiError(f"'{tool}' は使える互換ツールの一覧にありません。")

        try:
            changed = steam.set_compat_tool(userdata, entry.steam_app_id, tool)
        except steam.SteamError as error:
            raise UiError(str(error)) from error

        return {
            "message": (
                f"{entry.steam_title or entry.title} を {tool} にしました。"
                if changed
                else f"{entry.steam_title or entry.title} は既に {tool} でした。"
            ),
            "changed": changed,
            "tool": tool,
        }

    # -- DLC の結び付け ------------------------------------------------

    def link_candidates(self, work_id: str) -> dict[str, Any]:
        """重ね先の候補を返す。

        既定では同じ出品者の導入済み作品だけを出す。実ライブラリで確認した
        3 組はいずれも出品者が一致していた。ただし例外に備えて全件も返し、
        画面側で切り替えられるようにしておく。
        """
        current = self.state()
        entry = self._require_entry(work_id, current)

        def describe(other: state.InstalledWork) -> dict[str, Any]:
            return {
                "id": other.id,
                "title": other.title,
                "maker": other.maker,
                "directory": str(other.path),
                "same_maker": bool(entry.maker and other.maker == entry.maker),
            }

        others = [
            describe(other)
            for other in sorted(current.works.values(), key=lambda w: w.title)
            if other.id != work_id and other.path.is_dir()
        ]
        return {
            "work_id": work_id,
            "title": entry.title,
            "maker": entry.maker,
            "directory": str(entry.path),
            "candidates": others,
            "same_maker_count": sum(1 for item in others if item["same_maker"]),
        }

    def browse(self, work_id: str, relative: str = "") -> dict[str, Any]:
        """作品ディレクトリの中を見る。コピー元・コピー先を選ぶのに使う。"""
        current = self.state()
        entry = self._require_installed(work_id, current)

        try:
            here = link.resolve_inside(entry, relative)
        except link.LinkError as error:
            raise UiError(str(error)) from error
        if not here.is_dir():
            raise UiError(f"フォルダではありません: {relative}")

        directories, files = [], []
        for child in sorted(here.iterdir(), key=lambda p: p.name.lower()):
            item = str(child.relative_to(entry.path)).replace("\\", "/")
            if child.is_dir():
                directories.append({"name": child.name, "relative": item})
            else:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = 0
                files.append({
                    "name": child.name, "relative": item,
                    "size_label": download.format_size(size),
                })

        parent = str(Path(relative).parent).replace("\\", "/") if relative else None
        return {
            "work_id": work_id,
            "relative": relative,
            "parent": "" if parent == "." else parent,
            "directories": directories,
            "files": files[:400],
        }

    def links_json(self) -> dict[str, Any]:
        current = self.state()
        items = []
        for item in current.links:
            source = current.get(item.source_id)
            target = current.get(item.target_id)
            items.append({
                # 添字ではなく key で指す。1 件外すと以降の添字がずれるので、
                # 画面を読み直す前に 2 つ目を押すと別の結び付きに当たってしまう。
                "key": item.key,
                "source_id": item.source_id,
                "target_id": item.target_id,
                "source_title": source.title if source else item.source_id,
                "target_title": target.title if target else item.target_id,
                "source_path": item.source_path,
                "target_path": item.target_path,
                "mode": item.mode,
                "applied_at": api.format_datetime(item.applied_at) if item.applied_at else None,
                "has_backup": bool(item.backup_dir),
                "note": item.note,
            })
        return {"links": items}

    def _build_link(self, values: dict[str, Any]) -> state.LinkRecord:
        source_id = str(values.get("source_id") or "").strip()
        target_id = str(values.get("target_id") or "").strip()
        if not source_id or not target_id:
            raise UiError("コピー元とコピー先の両方を選んでください。")
        if source_id == target_id:
            raise UiError("同じ作品どうしは結び付けられません。")

        mode = str(values.get("mode") or "copy")
        if mode not in ("copy", "symlink"):
            raise UiError("コピー方法の指定が不正です。")

        return state.LinkRecord(
            source_id=source_id,
            target_id=target_id,
            source_path=str(values.get("source_path") or "").strip().strip("/"),
            target_path=str(values.get("target_path") or "").strip().strip("/"),
            mode=mode,
            note=str(values.get("note") or "").strip(),
        )

    def preview_link(self, values: dict[str, Any]) -> dict[str, Any]:
        """適用前の下見。何個が上書きされるかを見せる。"""
        item = self._build_link(values)
        try:
            result = link.plan(self.state(), item)
        except link.LinkError as error:
            raise UiError(str(error)) from error

        return {
            "summary": result.summary(),
            "overwrites": result.overwrites[:200],
            "additions": result.additions[:200],
            "overwrite_count": len(result.overwrites),
            "addition_count": len(result.additions),
            "total_label": download.format_size(result.total_bytes),
            "source": str(result.source),
            "target": str(result.target),
        }

    def apply_link(self, values: dict[str, Any]) -> dict[str, Any]:
        item = self._build_link(values)
        current = self.state()
        try:
            result = link.apply(current, item, backup=bool(values.get("backup", True)))
        except link.LinkError as error:
            raise UiError(str(error)) from error

        # 同じ関係が既にあれば置き換える
        current.links = [
            existing for existing in current.links if existing.key != item.key
        ]
        current.links.append(item)
        current.save()

        return {
            "message": f"適用しました。{result.summary()}",
            "backup_dir": item.backup_dir,
        }

    @staticmethod
    def _find_link(current: state.State, key: str) -> state.LinkRecord:
        for item in current.links:
            if item.key == key:
                return item
        raise UiError("その結び付けは見つかりません。画面を開き直してください。")

    def revert_link(self, key: str) -> dict[str, Any]:
        current = self.state()
        item = self._find_link(current, key)
        try:
            message = link.revert(current, item)
        except link.LinkError as error:
            raise UiError(str(error)) from error
        current.save()
        return {"message": message}

    def remove_link(self, key: str) -> dict[str, Any]:
        """記録だけ消す。既に重ねたファイルはそのまま。"""
        current = self.state()
        removed = self._find_link(current, key)
        current.links = [item for item in current.links if item is not removed]
        current.save()
        return {
            "message": f"{removed.source_id} → {removed.target_id} の記録を消しました。"
                       " 既に重ねたファイルはそのままです。"
        }

    # -- パッチ (exe を実行して当てるもの) ------------------------------

    def patches(self, work_id: str) -> dict[str, Any]:
        """パッチとして実行できる exe を並べる。

        1 つの作品に 12 個の exe が入っていて、
        フォルダで区別する作りがある。相対パスをそのまま見せる。
        """
        current = self.state()
        entry = self._require_installed(work_id, current)

        found = proton.find_patches(entry.path, applied=entry.applied_patches)
        targets = [
            {"id": other.id, "title": other.title,
             "same_maker": bool(entry.maker and other.maker == entry.maker),
             "registered": other.steam_app_id is not None}
            for other in current.works.values()
            if other.path.is_dir()
        ]
        # 同じ出品者を先に出す。当てる先はまず同じサークルの本編なので、
        # 五十音順のまま出すと関係ない作品が初期選択になってしまう。
        targets.sort(key=lambda item: (not item["same_maker"], item["title"]))
        return {
            "work_id": work_id,
            "title": entry.title,
            "directory": str(entry.path),
            "patches": [
                {"relative": item.relative, "size_label": download.format_size(item.size),
                 "applied": item.applied}
                for item in found
            ],
            "targets": targets,
        }

    def run_patch(self, work_id: str, relative: str, target_id: str) -> dict[str, Any]:
        """パッチ exe を、当てる先のゲームのプレフィックスで実行する。

        当てる先を別に指定できるのは、パッチが本編とは別の作品として売られて
        いることがあるため (特典セットとして別売りされる形がある)。
        """
        current = self.state()
        source = current.get(work_id)
        target = current.get(target_id)
        if source is None or not source.path.is_dir():
            raise UiError(f"{work_id} はまだ展開されていません。")
        if target is None or not target.path.is_dir():
            raise UiError(f"当てる先 {target_id} が導入されていません。")
        if target.steam_app_id is None:
            raise UiError(
                f"{target.title} はまだ Steam に登録されていません。"
                " プレフィックスは登録して一度起動すると作られます。"
            )

        userdata = self._userdata()

        try:
            exe = link.resolve_inside(source, relative)
        except link.LinkError as error:
            raise UiError(str(error)) from error
        if not exe.is_file():
            raise UiError(f"実行ファイルが見つかりません: {relative}")

        try:
            result = proton.run_exe(
                userdata,
                target.steam_app_id,
                exe,
                working_dir=target.path,
                fallback_tool=self.config.steam_compat_tool,
            )
        except proton.ProtonError as error:
            raise UiError(str(error)) from error

        if result.ok and relative not in source.applied_patches:
            source.applied_patches.append(relative)
            current.save()

        return {
            "message": result.summary(),
            "ok": result.ok,
            "returncode": result.returncode,
            "proton": result.proton,
            "prefix": result.prefix,
            "output": (result.stdout + result.stderr)[-3000:],
        }

    # -- Steam --------------------------------------------------------

    def steam_status(self) -> dict[str, Any]:
        userdata = config_module.steam_userdata_path(self.config)
        session = steam.session_kind()
        return {
            "userdata": str(userdata) if userdata else None,
            "running": steam.is_steam_running(),
            "session": session,
            # ゲームモードでは Steam を終了する手段が無い
            "can_quit_steam": session != steam.SESSION_GAMING,
        }

    def register_steam(
        self,
        work_id: str,
        executable: str,
        title: str,
        launch_options: str | None = None,
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise UiError("登録するタイトルを入力してください。")

        current = self.state()
        entry = self._require_entry(work_id, current)

        target = Path(executable)
        if not target.is_file():
            raise UiError(f"実行ファイルが見つかりません: {target}")

        # 展開先の外の実行ファイルを登録させない
        try:
            target.resolve().relative_to(entry.path.resolve())
        except ValueError:
            raise UiError(
                f"{work_id} の展開先の外にある実行ファイルは登録できません: {target}"
            ) from None

        userdata = self._userdata()
        if steam.is_steam_running():
            raise UiError(self._steam_blocked("登録"))

        # 改名や exe の変更で AppID が変わるので、古い登録と画像を先に片付ける
        previous_title = entry.steam_title or entry.title
        previous_id = entry.steam_app_id
        new_id = steam.build_shortcut(title, target).app_id
        if previous_id != new_id:
            steam.unregister(userdata, previous_title, app_id=previous_id)

        image = self.grid_image(entry) if self.config.steam_grid_images else None

        # 同名のショートカットが別環境のものとして存在する場合、ここで登録すると
        # 名前が同じという理由でそちらを書き換えてしまう。
        self._refuse_if_foreign(entry, "登録")

        options = (
            self.config.steam_launch_options
            if launch_options is None
            else launch_options
        ).strip()

        result = install.register_to_steam(
            self.config, userdata, title, target, image, launch_options=options
        )

        entry.executable = str(target)
        entry.steam_title = title
        entry.steam_app_id = result.app_id
        current.works[work_id] = entry
        current.save()

        return {
            "work_id": work_id,
            "app_id": result.app_id,
            "title": title,
            "executable": str(target),
            "created": result.created,
            "updated": [str(path) for path in result.updated],
            "backups": [str(path) for path in result.backups],
            "images": [str(path) for path in result.images],
        }

    def grid_image(self, entry: state.InstalledWork) -> bytes | None:
        """ライブラリの表紙・背景に使う画像を取ってくる。

        一度取ったものは作品フォルダに残してあるので、まずそちらを見る。
        カバーを作り直すだけなら通信が要らない。

        取れなくても登録自体は続けたいので、失敗しても例外にしない。
        """
        saved = cover.cached_image(entry.path)
        if saved is not None:
            return saved

        url = entry.image_url
        if not url:
            works, _unavailable = self.library()
            work = next((item for item in works if item.id == entry.id), None)
            url = work.image_url if work else None
        if not url:
            return None

        try:
            response = self.client().request(url)
        except api.DlsiteApiError:
            return None

        if not 200 <= response.status < 300 or not response.body:
            return None

        cover.store_image(entry.path, response.body)
        return response.body

    def unregister_steam(self, work_id: str) -> dict[str, Any]:
        current = self.state()
        entry = self._require_entry(work_id, current)

        userdata = self._userdata()
        if steam.is_steam_running():
            raise UiError(self._steam_blocked("解除"))

        self._refuse_if_foreign(entry, "解除")

        result = steam.unregister(
            userdata, entry.steam_title or entry.title, app_id=entry.steam_app_id
        )
        entry.steam_app_id = None
        current.works[work_id] = entry
        current.save()

        return {
            "work_id": work_id,
            "updated": [str(path) for path in result.updated],
            "images": [str(path) for path in result.images],
        }

    # -- ダウンロード -------------------------------------------------

    def jobs_json(self) -> dict[str, Any]:
        with self._lock:
            # 順番待ちには「あと何番目か」を入れてから渡す
            waiting = sorted(
                (job for job in self._jobs.values() if job.status == "queued"),
                key=lambda job: job.id,
            )
            for position, job in enumerate(waiting, start=1):
                job.queue_position = position
            for job in self._jobs.values():
                if job.status != "queued":
                    job.queue_position = None

            jobs = [job.to_json() for job in self._jobs.values()]
            queued = len(waiting)
            running = sum(1 for job in self._jobs.values() if job.status == "running")

        jobs.sort(key=lambda job: job["id"], reverse=True)
        return {"jobs": jobs, "queued": queued, "running": running}

    def _ensure_worker(self) -> None:
        """順番待ちを捌く担当を 1 本だけ動かす。

        同時に何本も走らせない。以前は要求のたびにスレッドを起こしていたため、
        一括取得を押すと表示中の未取得すべて (実ライブラリで 18 本・60 GiB) が
        いっせいに走り、DLsite にも回線にも負担をかけていた。

        起動の判断は ``_worker_active`` で行い、担当が「もう仕事が無い」と
        決める処理と同じ錠の中で見る。別々に見ると、担当が終わりかけている
        隙に積まれた分が誰にも拾われず、永久に待ち続けることになる。
        """
        with self._lock:
            if self._worker_active:
                return
            self._worker_active = True

        self._worker = threading.Thread(target=self._work_queue, daemon=True)
        self._worker.start()

    def _next_job(self) -> "tuple[Job, api.Work, Path] | None":
        """次に処理するものを取り出す。無ければ担当を終わらせる。"""
        with self._lock:
            waiting = sorted(
                (job for job in self._jobs.values() if job.status == "queued"),
                key=lambda job: job.id,
            )
            for job in waiting:
                if job.cancel_requested:
                    # 走り出す前に取り消されたもの。何も作っていないので印だけ付ける
                    job.status = "cancelled"
                    job.message = "順番待ちのうちに取り消しました。"
                    job.finished_at = time.time()
                    self._queued_args.pop(job.id, None)
                    continue
                job.status = "running"
                job.started_at = time.time()
                work, target_root = self._queued_args[job.id]
                return job, work, target_root

            # 仕事が無いことの確認と、担当を降りる判断を同じ錠の中で行う
            self._worker_active = False
            return None

    def _work_queue(self) -> None:
        """押された順に 1 本ずつ処理する。"""
        while True:
            picked = self._next_job()
            if picked is None:
                return
            job, work, target_root = picked
            try:
                self._run_download(job, work, target_root)
            finally:
                with self._lock:
                    self._queued_args.pop(job.id, None)

    def start_download(self, work_id: str, install_dir: str | None = None) -> dict[str, Any]:
        works, _unavailable = self.library()
        work = next((item for item in works if item.id == work_id), None)
        if work is None:
            raise UiError(f"ライブラリに {work_id} がありません。")

        try:
            target_root = self.config.resolve_install_dir(install_dir)
        except ValueError as error:
            raise UiError(str(error)) from None

        with self._lock:
            for job in self._jobs.values():
                if job.work_id == work_id and job.status in ("running", "queued"):
                    # 同じ作品を二重に積まない
                    return {"job": job.to_json(), "already_running": True}

            job = Job(id=self._next_job_id, work_id=work.id, title=work.title)
            self._jobs[job.id] = job
            self._queued_args[job.id] = (work, target_root)
            self._next_job_id += 1

        # 実際に走らせるのは担当スレッド。ここでは順番に積むだけ。
        self._ensure_worker()
        return {"job": job.to_json(), "already_running": False}

    def cancel_download(self, job_id: int) -> dict[str, Any]:
        """走っているダウンロードに中止を指示する。"""
        with self._lock:
            job = self._jobs.get(job_id)

        if job is None:
            raise UiError(f"ジョブ {job_id} がありません。")

        if job.status == "queued":
            # まだ何も始めていないので、その場で取り下げる
            with self._lock:
                job.status = "cancelled"
                job.cancel_requested = True
                job.message = "順番待ちのうちに取り消しました。"
                job.finished_at = time.time()
                self._queued_args.pop(job.id, None)
            return {"job": job.to_json()}

        if job.status != "running":
            raise UiError("そのジョブは既に終わっています。")
        if not job.cancellable:
            raise UiError("展開中は中止できません。終わるまでお待ちください。")

        job.cancel_requested = True
        job.current = "中止しています"
        return {"job": job.to_json()}

    def _run_download(self, job: Job, work: api.Work, target_root: Path) -> None:
        """取得を実行する。手順は :mod:`dlsite_deck.install` にあり、ここは
        進捗の見せ方と中止の受け渡しだけを担う。"""
        result = install.InstallResult(work_id=work.id)

        def progress(name: str, written: int, total: int | None) -> None:
            job.current = name
            job.written = written
            job.total = total
            # 通信の合間に中止を拾う。ここが唯一の割り込み点。
            if job.cancel_requested:
                raise install.Cancelled()

        def on_phase(phase: str) -> None:
            job.phase = phase
            if phase == install.PHASE_EXTRACT:
                # 展開に入ると中止できなくなるので、その直前で最後の確認をする
                if job.cancel_requested:
                    raise install.Cancelled()
                job.current = "展開中"

        try:
            # 同時に複数を落とすと帯域も進捗表示も破綻するので直列化する
            with self._download_lock:
                current = self.state()
                install.install_work(
                    self.client(),
                    self.config,
                    work,
                    target_root,
                    current,
                    progress=progress,
                    on_phase=on_phase,
                    result=result,
                )
                job.cache_dir = result.cache_dir
                job.created_output = result.created_output

                if result.extracted:
                    current.save()
                    job.message = f"展開しました: {result.output_dir}"
                    if result.freed:
                        job.message += (
                            f"（アーカイブ {download.format_size(result.freed)} を削除）"
                        )
                else:
                    job.message = (
                        "展開できる形式ではないため、ダウンロードのみ行いました: "
                        f"{result.cache_dir}"
                    )

                if result.serial_numbers:
                    serials = " / ".join(
                        f"{label}: {value}" for label, value in result.serial_numbers
                    )
                    job.message += f"（シリアルコード {serials}）"

            job.status = "done"
        except install.Cancelled:
            job.status = "cancelled"
            job.current = ""
            job.message = "中止しました（" + install.discard_partial(result) + "）"
        except Exception as error:  # UI に出すので握って記録する
            job.status = "error"
            job.message = str(error) or error.__class__.__name__
            traceback.print_exc()
        finally:
            job.finished_at = time.time()


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# 経路
# ----------------------------------------------------------------------
#
# 以前は if/elif を積み上げていたが、経路が 28 本まで増えて見通しが悪くなった。
# 「どの URL がどれを呼ぶか」だけの表にしてある。


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


#: GET。``(Backend, クエリ)`` を受けて、そのまま JSON にする値を返す。
GET_ROUTES: dict[str, Callable[["Backend", dict[str, list[str]]], Any]] = {
    "/api/library": lambda b, q: b.library_json(refresh=_q(q, "refresh") == "1"),
    "/api/executables": lambda b, q: b.executables(_q(q, "work_id")),
    "/api/jobs": lambda b, q: b.jobs_json(),
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
}

#: POST。``(Backend, 本文)`` を受ける。
POST_ROUTES: dict[str, Callable[["Backend", dict[str, Any]], Any]] = {
    "/api/download": lambda b, p: b.start_download(
        _s(p, "work_id"),
        str(p["install_dir"]) if p.get("install_dir") else None,
    ),
    "/api/download/cancel": lambda b, p: b.cancel_download(int(p.get("job_id", 0))),
    "/api/steam/register": lambda b, p: b.register_steam(
        _s(p, "work_id"),
        _s(p, "executable"),
        _s(p, "title"),
        launch_options=(
            None if p.get("launch_options") is None else str(p["launch_options"])
        ),
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
    "/api/patch/run": lambda b, p: b.run_patch(
        _s(p, "work_id"), _s(p, "relative"), _s(p, "target_id")
    ),
    "/api/config": _save_config,
}


class Handler(BaseHTTPRequestHandler):
    backend: Backend  # サーバー生成時に差し込む

    server_version = "dlsite_deck"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # 既定の実装は毎リクエストを stderr に出すので黙らせる
        pass

    # -- 送信 ---------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        encoding = None

        # ライブラリ一覧は 80KB 前後になる。圧縮すると 1 割程度に縮み、
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
        # ローカル専用なので外部への埋め込みや参照を許さない
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

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise UiError("要求が大きすぎます。")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise UiError(f"要求を解釈できませんでした: {error}") from error

    # -- 経路 ---------------------------------------------------------

    def _dispatch(self, route: Callable[[], None]) -> None:
        """経路の処理を包み、失敗を UI に返せる形に揃える。

        GET と POST で同じ後始末が要る。別々に書くと片方だけ直して
        食い違うので、例外の扱いはここ一箇所にまとめてある。
        """
        try:
            route()
        except _DISCONNECTED:
            # 相手が切った後にエラー応答を書こうとすると二重に失敗する
            return
        except UiError as error:
            self._send_error_json(
                str(error), kind=error.kind, steps=error.steps
            )
        except Exception as error:  # 予期しない失敗も UI に返す
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


def _is_registered(
    entry: state.InstalledWork | None,
    shortcuts: "tuple[set[int], set[str], set[str]] | None",
) -> bool | None:
    """この作品が実際に Steam に登録されているか。

    ``None`` は「判定できない」。Steam が見つからない環境で「未登録」と
    言い切ると、登録済みのものを未登録として扱ってしまう。

    AppID だけでなく名前でも照合する。AppID は exe と名前から算出するので、
    Steam 側で改名されると記録した値と合わなくなるため。
    """
    if entry is None:
        return False
    if shortcuts is None:
        # 記録は残っているが、実際に登録されているかは確かめられない
        return None if entry.steam_app_id is None else True

    app_ids, names, _foreign = shortcuts
    if entry.steam_app_id is not None and entry.steam_app_id in app_ids:
        return True
    return (entry.steam_title or entry.title) in names


def _is_foreign(
    entry: state.InstalledWork | None,
    shortcuts: "tuple[set[int], set[str], set[str]] | None",
) -> bool:
    """同じ名前のショートカットが、別環境のものとして存在するか。

    この状態でこちらから登録すると、名前が同じという理由で**別環境の登録を
    書き換えてしまう**。触らせないために印を付ける。
    """
    if entry is None or shortcuts is None:
        return False
    _app_ids, _names, foreign = shortcuts
    return (entry.steam_title or entry.title) in foreign


#: 取り外せる媒体が置かれがちな場所
_REMOVABLE_ROOTS = ("/run/media", "/media", "/mnt")


#: 既定の並びの段。手を動かす必要がある順。
#:
#: 画面の「状態」順とは意図が違うので別に持つ。あちらは状態の重さの順、
#: こちらは「次に何をすればよいか」の順。
_DEFAULT_RANK = {
    "unregistered": 0,  # 取得したのに Steam に載せていない。次にやること
    "outdated": 1,      # 更新が出ている
    "missing": 2,       # まだ取得していない
    "done": 3,          # 登録済み / 更新不明。やることは無い
    "record_only": 4,   # 記録だけ残っている (SD カードが外れている等)
    "foreign": 5,       # 別環境と重複。こちらからは触れないので最後
}


def _default_rank(item: dict[str, Any]) -> int:
    """既定の並びで、その作品がどの段に入るか。"""
    if item["steam_foreign"]:
        return _DEFAULT_RANK["foreign"]
    if item["installed"]:
        # Steam 登録の話はゲームだけ。マンガや音声を「未登録」に混ぜない。
        if item["category"] == "game" and item["steam_registered"] is False:
            return _DEFAULT_RANK["unregistered"]
        if item["outdated"]:
            return _DEFAULT_RANK["outdated"]
        return _DEFAULT_RANK["done"]
    if item["status_label"] == "記録のみ":
        return _DEFAULT_RANK["record_only"]
    return _DEFAULT_RANK["missing"]


def _is_under(path: Path, root: Path) -> bool:
    """``path`` が ``root`` の下にあるか。実在しなくても文字列で判断する。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _looks_removable(path: Path) -> bool:
    """取り外せる媒体の上にありそうか。

    SteamDeck の SD カードは ``/run/media/deck/<ラベル>`` に来る。確実な判定では
    ないので、あくまで「カードが外れているのでは」と促すためだけに使う。
    """
    text = str(path).replace("\\", "/")
    return any(text.startswith(root) for root in _REMOVABLE_ROOTS)


def _directory_size(path: Path) -> int:
    """ディレクトリ以下の合計バイト数。読めないものは飛ばす。"""
    if not path.is_dir():
        return 0

    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).stat().st_size
            except OSError:
                pass
    return total


def _is_loopback(host: str) -> bool:
    """自分自身にだけ開いているか。"""
    return host in ("127.0.0.1", "localhost", "::1", "") or host.startswith("127.")


#: 応答の Server ヘッダに入れている名前。自分の実体かどうかの判別に使う。
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
        # 繋がらない = 誰も使っていない
        return "free"

    # 繋がった時点で「誰かが使っている」ことは確定。あとは相手が自分かどうか。
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

    2 つ動くと state.json を同時に書き替えうるので、見つけたら知らせる。
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


def serve(
    cfg: config_module.Config,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Web UI を起動する。Ctrl-C で終了。

    起動できなかった場合は、理由を出して 1 を返す（例外は投げない）。
    """
    url = f"http://{host if host else '127.0.0.1'}:{port}/"

    # 起動前の確認。塞がっていれば、何が使っているかを見てから諦める。
    occupied = probe_port(host, port)
    if occupied == "ours":
        print(f"既に起動しています: {url}", file=sys.stderr)
        print("  ブラウザでそのまま開けます。", file=sys.stderr)
        print("  止めるには、動いている方の端末で Ctrl-C を押してください。", file=sys.stderr)
        return 1
    if occupied == "other":
        print(f"ポート {port} は別のプログラムが使っています。", file=sys.stderr)
        print(f"  別のポートを指定してください: --port {port + 1}", file=sys.stderr)
        return 1

    # ポートは空いていても、別ポートで動いている同居インスタンスは危ない。
    # state.json を同時に書き替えると記録が壊れる。
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
    handler = type("BoundHandler", (Handler,), {"backend": backend})

    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as error:
        # 確認から実際の bind までの間に取られることもある
        print(f"待ち受けを開始できませんでした: {host}:{port}", file=sys.stderr)
        print(f"  {error}", file=sys.stderr)
        print(f"  別のポートを指定してください: --port {port + 1}", file=sys.stderr)
        return 1

    # 画面の「終了」から畳めるようにする
    backend._server = server

    url = f"http://{host}:{server.server_port}/"

    if not _is_loopback(host):
        # 認証機構が無いので、外部に開くと購入履歴の閲覧もダウンロード操作も
        # 誰でもできてしまう。止めはしないが、黙って開かない。
        print()
        print("=" * 70, file=sys.stderr)
        print(
            f"警告: {host} で待ち受けます。このツールに認証機構はありません。",
            file=sys.stderr,
        )
        print(
            "      到達できる相手は誰でも、購入済み作品の一覧を見て、",
            file=sys.stderr,
        )
        print(
            "      ダウンロードや Steam への登録を実行できます。",
            file=sys.stderr,
        )
        print(
            "      信頼できないネットワークでは 127.0.0.1 のまま使ってください。",
            file=sys.stderr,
        )
        print("=" * 70, file=sys.stderr)
        print()

    print(f"Web UI を起動しました: {url}")
    print("終了するには画面右上の「終了」を押すか、Ctrl-C を押してください。")

    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        server.serve_forever()
        # 画面の「終了」から畳んだ場合はここに来る
        print("終了しました。")
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
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
