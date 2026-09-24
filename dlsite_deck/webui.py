"""ローカル Web UI の中身。画面から頼まれた処理を実際に行う。

SteamDeck のデスクトップモードはタッチ操作が主なので、一覧の絞り込みや実行ファイルの
選択はブラウザ上で行えたほうが扱いやすい。標準ライブラリだけで組んであり、
追加の依存はない。

ここにあるのは処理の中身 (:class:`Backend`) と、設定画面の組み立て方。
HTTP の受け答えと、他のサイトやプログラムからの要求を締め出す仕組み (合言葉・接続の上限・
自動終了など) は :mod:`dlsite_deck.server` が受け持つ。
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

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
    local,
    proton,
    steam,
)
from .paths import is_within

if TYPE_CHECKING:
    # 型の注釈にだけ使う。server は webui を読み込むので、実行時には読み込まない。
    from http.server import ThreadingHTTPServer

    from .server import IdleWatch

#: 設定画面の組み立て方。順番がそのまま画面の並びになる。
CONFIG_FIELDS = [
    {"key": "download_dir", "label": "キャッシュ（アーカイブ置き場）", "type": "path",
     "locked": True,
     "help": "ダウンロードしたアーカイブの一時置き場。展開後は既定で削除する"},
    {"key": "install_dirs", "label": "インストール先の一覧", "type": "list",
     "help": "1 行に 1 つ。ダウンロード時にこの中から選ぶ。"
             " 追加できるのは承認済みの場所だけ"},
    {"key": "install_dir", "label": "既定のインストール先", "type": "install_default",
     "help": "上の一覧から選ぶ。ダウンロード画面で最初に選択された状態になる"},
    {"key": "runtime_dirs", "label": "共通パッチ置き場", "type": "list",
     "help": "1 行に 1 つ。ランタイムやコーデックのインストーラを置く場所。ここにある exe と"
             " msi は、どのゲームにも「パッチ」から実行できる。隠しディレクトリ（. で始まる"
             "もの）とその中は指定できない"},
    {"key": "import_dirs", "label": "自由登録の取り込み元", "type": "list",
     "help": "1 行に 1 つ。自由登録で取り込むアーカイブやディレクトリを置く場所。画面では"
             "この中だけをたどって選ぶ。取り込んだあとのアーカイブは、画面から削除できる。"
             "隠しディレクトリ（. で始まるもの）とその中は指定できない"},
    {"key": "state_file", "label": "状態ファイル", "type": "path", "locked": True,
     "help": "導入済みの記録"},
    {"key": "cookie_source", "label": "Cookie の取得元", "type": "choice",
     "choices": [{"value": "firefox", "label": "Firefox から読む"},
                 {"value": "manual", "label": "書き出したファイルから読む"}]},
    {"key": "cookie_file", "label": "Cookie ファイル", "type": "path", "locked": True,
     "help": "取得元が「書き出したファイル」のときに読む", "depends": "cookie_source=manual"},
    {"key": "directory_template", "label": "展開先の名前", "type": "text",
     "help": "{romaji} と {id} が使える"},
    {"key": "long_vowel", "label": "長音符「ー」の扱い", "type": "choice",
     "choices": [{"value": "repeat", "label": "母音を重ねる（スーパー→suupaa）"},
                 {"value": "drop", "label": "捨てる（スーパー→supa）"}]},
    {"key": "strip_versions", "label": "名前からバージョン番号を除去", "type": "bool"},
    {"key": "flatten_single_root", "label": "単一ディレクトリを展開先直下に引き上げる",
     "type": "bool"},
    {"key": "delete_archives", "label": "展開後にアーカイブを削除", "type": "bool"},
    {"key": "include_non_games", "label": "一括取得でゲーム以外も対象にする", "type": "bool"},
    {"key": "register_to_steam", "label": "ダウンロード後に自動で Steam 登録",
     "type": "bool"},
    {"key": "steam_userdata_dir", "label": "Steam の userdata", "type": "path",
     "locked": True, "help": "空なら自動検出"},
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
             " 元の画像は作品ディレクトリに残してあるので、通常は再ダウンロードしない"},
    {"key": "steam_compat_tool", "label": "登録時に割り当てる Proton", "type": "compat_tool",
     "help": "新しく登録するゲームに適用する。既に割り当て済みのものは変更しない"},
    {"key": "steam_executable", "label": "Steam の実行ファイル", "type": "path",
     "locked": True,
     "help": "「Steam を終了する」で使う。既定は SteamDeck の場所。"
             " ここに無ければ PATH と Steam のディレクトリからも探す"},
    {"key": "steam_launch_options", "label": "既定の起動オプション", "type": "text",
     "locked": True,
     "help": "新しく登録するゲームに付ける。日本語の文字化けを避けるには"
             " ロケールを渡す。例: LANG=ja_JP.UTF-8 ~/locales/run.sh %command%"},
    {"key": "tool_dirs", "label": "展開ツールを探す場所", "type": "list",
     "locked": True,
     "help": "7z や unrar、rsvg-convert が PATH に無い場合のディレクトリ"},
    {"key": "exclude", "label": "対象外にする作品 ID", "type": "list",
     "help": "1 行に 1 つ"},
    {"key": "idle_shutdown_minutes", "label": "使っていないときに自動で終了する",
     "type": "minutes",
     "choices": [{"value": 0, "label": "終了しない"},
                 {"value": 10, "label": "10 分"},
                 {"value": 30, "label": "30 分"},
                 {"value": 60, "label": "1 時間"},
                 {"value": 120, "label": "2 時間"}],
     "help": "画面を閉じてからこの時間がたつと終了する。画面を開いている間と、"
             "ダウンロードや展開の途中は終了しない。次の起動から反映される"},
]

#: 画面や API からは変えさせない設定。``config.json`` を直接書き換えたときだけ変わる。
#:
#: どれも「場所」か「動かすプログラム」を指す。API を呼べるプロセスがこれを
#: 変えられると、ツールに任意のプログラムを実行させられる。たとえば Flatpak の
#: アプリはホームディレクトリを読めなくてもネットワーク越しにここへは届くので、
#: 自分の書ける場所を ``steam_executable`` や ``tool_dirs`` に入れて実行させたり、
#: ``download_dir`` に入れてアーカイブを自分の手元に落とさせたりできてしまう。
#: 起動オプションも同じで、任意のコマンドを Steam に登録できる。
#: ``acknowledged_paths`` は画面に出していないが、同じ理由で受け付けない。
#:
#: ``install_dirs`` は画面から変えられるが、承認済みの場所に限る
#: (:meth:`Backend._check_install_dirs`)。``import_dirs`` と ``runtime_dirs`` は、
#: 利用者が自分で置き場所を決めて使うものなので画面から変えられる。ただし隠し
#: ディレクトリなど、他のアプリの持ち物がある場所は断る (:func:`_check_user_dir`)。
LOCKED_CONFIG_KEYS = frozenset(
    [field_def["key"] for field_def in CONFIG_FIELDS if field_def.get("locked")]
    + ["acknowledged_paths"]
)

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


class UiError(RuntimeError):
    """画面からの操作が失敗した。"""

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
    """表紙の作り直しの進み具合の初期値。"""
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
    """ダウンロードや自由登録の取り込みのような、時間のかかる処理 1 件。"""

    id: int
    work_id: str
    title: str
    #: "queued" | "running" | "done" | "error" | "cancelled"
    status: str = "queued"
    #: "download" | "extract" | "copy"。ダウンロード中しか途中で止められない。
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
    #: 元からあったディレクトリは片付けの対象にしない。
    created_output: Path | None = None
    #: 順番待ちの位置 (1 が次)。表示のために外から入れる。
    queue_position: int | None = None
    #: 自由登録の取り込みで作る展開先。同じ場所への取り込みを二重に積まないために持つ。
    output: Path | None = None

    @property
    def cancellable(self) -> bool:
        """今この瞬間、中止を受け付けられるか。

        順番待ちのものは何も始めていないので、いつでも取り消せる。
        走っている分は、ダウンロード中だけ受け付ける。展開は外部コマンドに
        委ねられていて、自由登録のコピーも作業用ディレクトリから名前を変えるまでが
        ひと続きなので、途中で安全に止められない。
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
    """画面からの要求を、実際の処理につなぐ。

    HTTP ハンドラは要求ごとに別スレッドで動くので、状態を触る箇所はロックで守る。
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
        #: 順番待ちの処理。走り出すまで持っておく。
        self._queued_args: dict[int, Callable[[Job], None]] = {}
        #: 順番待ちを処理する担当。同時に 1 本だけ。
        self._worker: threading.Thread | None = None
        #: 担当が動いているか。起動の判断は必ず _lock の中で行う。
        self._worker_active = False
        #: 終了の要求を受けたときに止める待ち受け。serve() が差し込む。
        self._server: ThreadingHTTPServer | None = None
        self._next_job_id = 1
        #: 同時に走らせるダウンロードは 1 件に絞る
        self._download_lock = threading.Lock()
        #: 表紙の作り直しの進み具合
        self._cover_task: dict[str, Any] = _new_cover_task()
        #: 使われていないかの見張り。serve() が差し込む。
        self.idle: IdleWatch | None = None

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
        ここに寄せてそろえる。
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
        呼び出し側は「分からない」として扱うこと (分からないのに断定しない)。

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
        # 同じ段の中はタイトル順 (画面の他の並べ替えとそろえる)。
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
        一覧を取りに行くとき、もう一度つなぎ直さずに済む。
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
                # Play 側を張り直せたかどうかは、使う人が意識する必要がないので
                # 画面では区別しない。
                if client.validate_session() or client.refresh_session():
                    session_state = {"ok": True, "detail": "有効", "purchases": None}
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
        """コマンドラインの ``check`` と同じ内容を、画面向けに返す。

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
        """設定の現在値と、画面が編集欄を組み立てるための定義を返す。"""
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
            # インストール先の一覧に画面から追加できる場所 (承認済みのものだけ)
            "install_dir_choices": sorted(self._allowed_install_dirs().values()),
            # 「縦長カバーにタイトルを載せる」を入れてよい環境か。
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

        minutes = merged.get("idle_shutdown_minutes")
        allowed_minutes = {
            choice["value"]
            for field_def in CONFIG_FIELDS if field_def["key"] == "idle_shutdown_minutes"
            for choice in field_def["choices"]
        }
        if isinstance(minutes, bool) or minutes not in allowed_minutes:
            raise UiError(
                "自動で終了するまでの時間は "
                + " / ".join(str(value) for value in sorted(allowed_minutes))
                + " 分から選んでください。"
            )

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

    def _allowed_install_dirs(self) -> dict[str, str]:
        """画面からインストール先の一覧に入れてよい場所。

        正規化した形をキーに、書き込むときの表記を値にして返す。

        * 端末で承認した場所 (``acknowledged_paths``)
        * プロジェクトの ``games/`` (既定のインストール先)
        * いま一覧に入っている場所 (``config.json`` に書かれたもの)

        プロジェクトの中を丸ごと許すと、ツール自身のプログラムが入った
        ``dlsite_deck/`` までインストール先にできてしまうので、``games/`` に限る。
        """
        allowed: dict[str, str] = {}
        for raw in (
            *self.config.acknowledged_paths,
            str(config_module.PROJECT_ROOT / "games"),
            *self.config.install_dirs,
        ):
            if raw and raw.strip():
                allowed.setdefault(_normalized_dir(raw), raw.strip())
        return allowed

    def _check_install_dirs(self, values: dict[str, Any]) -> dict[str, Any]:
        """インストール先の一覧を、承認済みの場所だけに限る。

        他のプログラムが自分の読み書きできる場所をインストール先にすると、
        ゲームを手元に落とさせて持ち出したり、exe を差し替えたりできる。
        承認は端末からしか増やせないので、承認済みの場所から選ぶだけなら安全。

        比べるのは**場所そのもの**。「承認済みの場所の中」を許すと
        ``<承認済み>/../..`` のような指定が通ってしまう。正規化してから
        完全一致で比べ、書き込むときは承認したときの表記にそろえる。
        """
        allowed = self._allowed_install_dirs()

        dirs = values.get("install_dirs")
        if isinstance(dirs, list):
            refused, cleaned = [], []
            for item in dirs:
                text = str(item).strip()
                if not text:
                    continue
                key = _normalized_dir(text)
                if key in allowed:
                    cleaned.append(allowed[key])
                else:
                    refused.append(text)
            if refused:
                raise UiError(
                    f"{', '.join(refused)} はインストール先に追加できません。"
                    " 画面から追加できるのは、承認済みの場所だけです。"
                    f" 新しい場所は {config_module.default_config_path()} の"
                    " install_dirs に書き、端末からツールを一度起動して承認してください。"
                )
            values["install_dirs"] = cleaned

        # 既定の選択も同じ表記にそろえる (末尾の / の有無などで一覧と食い違わないように)
        default = values.get("install_dir")
        if isinstance(default, str) and default.strip():
            key = _normalized_dir(default)
            if key in allowed:
                values["install_dir"] = allowed[key]

        return values

    def save_config(self, values: dict[str, Any]) -> dict[str, Any]:
        """画面から届いた設定を検証して書き出す。"""
        known = set(config_module.Config.__dataclass_fields__)
        unknown = sorted(set(values) - known)
        if unknown:
            raise UiError(f"知らない設定項目です: {', '.join(unknown)}")

        # 場所とプログラムを指す設定は、ここからは変えさせない (LOCKED_CONFIG_KEYS)。
        # 画面はこれらを送らないが、今と同じ値なら受け流す。
        current = asdict(self.config)
        changed = sorted(
            key for key in set(values) & LOCKED_CONFIG_KEYS
            if values[key] != current[key]
        )
        if changed:
            raise UiError(
                f"{', '.join(changed)} は画面からは変更できません。"
                f" {config_module.default_config_path()} を直接編集してください。"
            )

        values = self._check_install_dirs(dict(values))
        for key in USER_DIR_KEYS:
            if key in values and values[key] != current[key]:
                values[key] = _check_user_dirs(key, values[key])

        merged = current
        merged.update(values)
        self._validate_config(merged)

        # 切り替えたときだけ知らせる。使えない環境で毎回知らせても煩わしいだけ。
        warnings = []
        if merged.get("steam_cover_title") and not self.config.steam_cover_title:
            problem = cover.title_support_problem()
            if problem:
                warnings.append("カバーにタイトルを載せられません。" + problem)

        updated = config_module.Config(**merged)

        # 以前はここでプロジェクト外の場所を承認済みにしていたが、やめた。場所は画面から
        # 自由には変えられなくなった (インストール先は承認済みの場所から選ぶだけ) ので、
        # 承認は端末から起動したときに利用者が行う。
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

    def _current_launch_options(self, entry: state.InstalledWork) -> str | None:
        """既に登録されているショートカットの起動オプションを読む。

        まだ登録されていなければ ``None``。空の起動オプションで登録済みの
        場合 (``""``) と区別するため。
        """
        return self._launch_options_by_name().get(entry.steam_title or entry.title)

    def _launch_options_by_name(self) -> dict[str, str]:
        """登録済みのショートカットの起動オプションを、登録名ごとに集める。

        同じ名前が複数あれば最初のものを使う。Steam が見つからなければ空。
        """
        userdata = config_module.steam_userdata_path(self.config)
        if userdata is None:
            return {}

        found: dict[str, str] = {}
        for config_dir in steam.find_user_config_dirs(userdata):
            document = steam.load_shortcuts(config_dir / "shortcuts.vdf")
            for existing in document.get("shortcuts", {}).values():
                if not isinstance(existing, dict):
                    continue
                name = str(steam.get_field(existing, "AppName") or "")
                found.setdefault(name, str(steam.get_field(existing, "LaunchOptions") or ""))
        return found

    def _launch_options_for(self, entry: state.InstalledWork) -> tuple[str, str]:
        """登録するときに付く起動オプションと、その出どころ。

        **画面からは指定させない。** 起動オプションには任意のコマンドを書けるので、
        API を呼べるプロセスに渡すと、次にゲームを遊んだときにそのコマンドが動く。
        既に登録があれば Steam 側の値を引き継ぎ (名前を変えて登録し直す場合も)、
        無ければ ``config.json`` の既定値を使う。どちらも利用者が自分で書いたもの。
        """
        current = self._current_launch_options(entry)
        if current is not None:
            return current, "steam"
        return self.config.steam_launch_options, "config"

    def executables(self, work_id: str) -> dict[str, Any]:
        entry = self._require_installed(work_id)

        candidates = archive.executable_candidates(entry.path)
        launch_options, launch_source = self._launch_options_for(entry)
        return {
            "work_id": work_id,
            "directory": str(entry.path),
            "title": entry.title,
            "steam_title": entry.steam_title or entry.title,
            # 「実行」の画面で、どの Proton 環境で動くかを見せるために使う
            "app_id": entry.steam_app_id,
            "selected": entry.executable,
            # 表示するだけ。登録のときに画面から送っても使わない。
            "launch_options": launch_options,
            "launch_options_source": launch_source,
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

    def is_busy(self) -> bool:
        """止めてはいけない処理が残っているか。自動で終了するかの判断に使う。"""
        with self._lock:
            if any(job.status in ("running", "queued") for job in self._jobs.values()):
                return True
            return bool(self._cover_task.get("running"))

    def ping(self) -> dict[str, Any]:
        """画面からの合図。開いている間は使われているとみなす。"""
        return {"ok": True, "idle_shutdown_minutes": self.config.idle_shutdown_minutes}

    def shutdown(self) -> dict[str, Any]:
        """待ち受けを止めて、プロセスを終わらせる。

        ``server.shutdown()`` は要求を処理しているスレッドから呼ぶと自分の完了を
        待って固まる。応答を返しきってから別のスレッドで呼ぶ必要がある。

        進行中のダウンロードがあるかは呼び出し側 (画面) が確認して尋ねる。ここまで
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
            # 応答が相手に届くだけの間を置いてから止める
            time.sleep(0.4)
            server.shutdown()

        threading.Thread(target=stop, daemon=True).start()
        return {"message": "終了します。", "abandoned": abandoned}

    # -- 置き場の確認 ----------------------------------------------------

    def path_warnings(self) -> dict[str, Any]:
        """設定した置き場が実在するかを確かめる。

        SteamDeck ではインストール先を SD カードに置くのが普通で、カードが外れていたり
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
        実行できない指示になる。デスクトップモードへ切り替えてもらう。
        """
        if steam.session_kind() == steam.SESSION_GAMING:
            return (
                f"ゲームモードでは{action}できません。"
                " ゲームモードでは Steam を終了できないため、"
                " デスクトップモードに切り替えて操作してください。"
            )
        return (
            f"Steam が起動中のため{action}できません。"
            " 終了してからやり直してください"
            " (起動中に書き換えても Steam の終了時に上書きされます)。"
        )

    def _require_steam_stopped(self, action: str) -> None:
        """Steam が起動中なら、書き換えずに断る。

        Steam は shortcuts.vdf と config.vdf を抱えたまま動き、終了時に書き戻す。
        起動中に書き換えても捨てられるので、書き換える操作はここを通す。
        """
        if steam.is_steam_running():
            raise UiError(self._steam_blocked(action))

    def steam_state(self) -> dict[str, Any]:
        """Steam が動いているかだけを返す軽い問い合わせ。

        画面の「Steam を終了しました」を押したときに、実際のプロセスを見に行く。
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

    # -- 表紙の作り直し -----------------------------------------------

    def cover_task(self) -> dict[str, Any]:
        """作り直しの進み具合。"""
        with self._lock:
            return dict(self._cover_task)

    def rebuild_covers(self) -> dict[str, Any]:
        """登録済みゲームのライブラリ画像を、今の設定どおりに作り直す。

        「縦長カバーにタイトルを載せる」を切り替えたあと、登録し直さずに
        反映させるための入口。作るだけでなく、**使わなくなった画像は削除する**
        ので、設定を戻せば元の見た目に戻る。

        ストア画像は作品ディレクトリに残してあるものを使うため、たいていは通信が要らない。
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
        except Exception as error:  # 担当スレッドの中の失敗は、ここですべて受け止めて記録する
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
        slots, unused = self._grid_slot_plan()

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

    def _grid_slot_plan(self) -> tuple[list[str], list[str]]:
        """画像を置くスロットと、置いてあれば削除するスロット (設定から外したもの)。

        ロゴは別の設定 (steam_logo_source) で決めるので、どちらにも入れない。
        """
        slots = list(self.config.steam_grid_slots)
        unused = [
            slot for slot in steam.GRID_SLOTS
            if slot != steam.LOGO_SLOT and slot not in slots
        ]
        return slots, unused

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

            # 「設定しない」を選んだときだけ削除する。
            #
            # 作れなかっただけの場合に消してはいけない。rsvg-convert が無い環境や
            # アイコンを持たない exe では作れず、そこで作り直すと、別の環境で
            # 付けたロゴを知らないうちに消してしまう。
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
        """Steam に終了してもらい、終了するまで待つ。

        ``shortcuts.vdf`` などは Steam が抱えていて終了時に書き戻す。手で
        終わらせてもらう代わりに、ここから頼む。

        **ゲームモードでは行わない。** Steam 自身がセッションで、このツールも
        そこから起動されているため、終了するとツールも一緒に終了する。
        """
        if steam.session_kind() == steam.SESSION_GAMING:
            raise UiError(
                "ゲームモードでは Steam を終了できません。"
                " 終了するとこのツールごと落ちます。"
                " デスクトップモードに切り替えて操作してください。"
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
            # 終了を頼んでも終了しないことがある。Steam がサインイン画面のまま
            # だと、指示は届いている ("command line was forwarded" がログに出る)
            # のに終了しなかった (Ubuntu のコンテナで確認)。無理に kill すると
            # shortcuts.vdf を書き戻す前に止まるので、手で閉じてもらうよう案内する。
            "message": "Steam がまだ終了していません。"
                       " 起動中のゲームがあれば、先に終了してください。"
                       " 環境によっては終了の指示が効かないことがあります。"
                       " その場合は Steam のメニュー（Steam → 終了）から閉じて、"
                       "「Steam を終了しました」を押してください。",
        }

    # -- 作品の削除 ------------------------------------------------------

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
            # 自由登録の作品は DLsite から取り直せない。確認の文言を変えるために返す。
            "origin": entry.origin,
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

        戻す先の作品ごと消すので、退避したファイルだけ残しても使い道がない。**その作品を
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
            # インストール先そのものではなく、その中にあるものだけを消す
            if not any(is_within(resolved, root, strict=True) for root in roots):
                continue

            freed += _directory_size(resolved)
            shutil.rmtree(resolved, ignore_errors=True)
            if not resolved.exists():
                count += 1

        # 空になったディレクトリは残さない
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
            # インストール先として設定した場所の中にあるものだけを消す。設定を書き換えて
            # 別の場所を指させても、そこを消してしまわないようにする。
            roots = [root.resolve() for root in self.config.install_paths]
            if not any(is_within(resolved, root) for root in roots):
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
        # 「展開後にアーカイブを削除」を切った場合や、途中で失敗した取得の分が残ることがある。
        cache = self.config.download_path / work_id
        cached_bytes = 0
        if cache.is_dir():
            cached_bytes = _directory_size(cache)
            shutil.rmtree(cache, ignore_errors=True)

        # DLC を重ねられたときの退避も一緒に消す。
        #
        # 退避は作品ディレクトリの隣 (インストール先の直下) にあるので、作品を
        # 消しただけでは残り続ける。戻す相手が無くなったので一緒に消す。
        backup_bytes, backup_count = self._discard_backups(entry)

        # この作品に関わる DLC の重ね合わせも記録から外す。
        # 既に重ねたファイルは相手側に残るので、それは伝える。
        dropped = [
            item for item in current.links
            if item.source_id == work_id or item.target_id == work_id
        ]
        # ファイルが残るのは、この作品を DLC として適用済みの相手だけ。この作品が
        # 重ねられた側なら、重ねたファイルは作品と一緒に消えている。
        applied = [
            item for item in dropped
            if item.source_id == work_id and item.applied_at
        ]
        left_behind = sorted({
            (current.get(item.target_id).title
             if current.get(item.target_id) else item.target_id)
            for item in applied
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
        if applied:
            parts.append(f"{len(applied)} 件の適用済みDLCのファイルはそのまま残っています。")

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
        で、Steam の設定画面から変えたときと同じ場所・同じ形。実機の 143 件はすべて
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
        """1 つのゲームの Proton の割り当てを変える。

        **Steam が起動中は変更しない。** Steam は ``config.vdf`` を抱えたまま
        動き、終了時に書き戻すので、動作中に書き換えても捨てられる。黙って
        失敗するのが一番困るため、ここで断る。
        """
        userdata = self._userdata()

        self._require_steam_stopped("Proton の変更が")

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

    # -- DLC の重ね合わせ ----------------------------------------------

    def link_candidates(self, work_id: str) -> dict[str, Any]:
        """重ねる相手の候補を返す。

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
            raise UiError(f"ディレクトリではありません: {relative}")

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
                # 画面を読み直す前に 2 つ目を押すと別の重ね合わせに当たってしまう。
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
            raise UiError("同じ作品同士をDLCとして扱うことはできません。")

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
        raise UiError("DLCを適用した情報が見つかりませんでした。画面を開き直してください。")

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
        """パッチとして実行できるものを並べる。

        出どころは 2 つ。**この作品の中**の exe (1 つの作品に 12 個入っていて、
        ディレクトリで区別する作りがある) と、**共通パッチ置き場**に置いた
        ランタイムやコーデックのインストーラ。どちらも相対パスをそのまま見せる。

        「実行済み」は当てた先のゲームごとに記録しているので、当てる先ごとに
        実行済みの鍵を返し、画面で選び直したときに印を付け直す。
        """
        current = self.state()
        entry = self._require_installed(work_id, current)

        # 当てる先のゲームの起動オプション。画面で見せ、既定ではパッチにも付ける。
        try:
            launch = self._launch_options_by_name()
        except steam.SteamError:
            launch = {}

        targets = [
            {"id": other.id, "title": other.title,
             "launch_options": launch.get(other.steam_title or other.title),
             "same_maker": bool(entry.maker and other.maker == entry.maker),
             # 両方に出品者があって違うときだけ。自由登録の作品には出品者が無い。
             "other_maker": bool(entry.maker and other.maker and other.maker != entry.maker),
             "registered": other.steam_app_id is not None,
             "installed": other.installed_patches}
            for other in current.works.values()
            if other.path.is_dir()
        ]
        # 同じ出品者を先に出す。当てる先はまず同じサークルの本編なので、
        # タイトル順のまま出すと関係ない作品が初期選択になってしまう。
        targets.sort(key=lambda item: (not item["same_maker"], item["title"]))

        runtimes = []
        for index, root in enumerate(self.config.runtime_paths):
            found = proton.find_patches(root, keep_runtimes=True) if root.is_dir() else []
            runtimes.append({
                "index": index,
                "path": str(root),
                "exists": root.is_dir(),
                "patches": [
                    {"relative": item.relative,
                     "size_label": download.format_size(item.size),
                     "key": _runtime_key(item.path)}
                    for item in found
                ],
            })

        return {
            "work_id": work_id,
            "title": entry.title,
            "directory": str(entry.path),
            "patches": [
                {"relative": item.relative, "size_label": download.format_size(item.size),
                 "key": _work_patch_key(work_id, item.relative)}
                for item in proton.find_patches(entry.path)
            ],
            "runtimes": runtimes,
            "targets": targets,
        }

    def run_patch(
        self,
        work_id: str,
        relative: str,
        target_id: str,
        source: str = "work",
        root: str = "",
        args: str = "",
        use_launch_options: bool = True,
    ) -> dict[str, Any]:
        """パッチの exe (か msi) を、当てる先のゲームのプレフィックスで実行する。

        当てる先を別に指定できるのは、パッチが本編とは別の作品として売られて
        いることがあるため (特典セットとして別売りされる形がある)。

        ``source`` が ``"runtime"`` なら、共通パッチ置き場の ``root`` 番目の中から選ぶ。
        ``args`` はインストーラに渡す引数 (``/quiet`` など)。シェルは通さず、
        空白で区切って (引用符でまとめられる) そのまま渡す。

        ``use_launch_options`` が立っていれば (既定)、当てる先のゲームに Steam で
        設定してある起動オプションを付けて実行する。ゲームと同じ言語設定で動くので、
        日本語のインストーラが文字化けしない。``%command%`` の無い起動オプションは
        使わない (Steam ではゲームへの引数になる形で、パッチに付けると意味が変わる)。
        """
        current = self.state()
        target = current.get(target_id)
        if target is None or not target.path.is_dir():
            raise UiError(f"当てる先 {target_id} が導入されていません。")
        self._require_prefix(target)
        arguments = _split_args(args)

        if source == "runtime":
            try:
                _base, exe = local.resolve_in(
                    self.config.runtime_paths, root, relative, "共通パッチ置き場"
                )
            except local.LocalError as error:
                raise UiError(str(error)) from error
            key = _runtime_key(exe)
        elif source == "work":
            owner = current.get(work_id)
            if owner is None or not owner.path.is_dir():
                raise UiError(f"{work_id} はまだ展開されていません。")
            try:
                exe = link.resolve_inside(owner, relative)
            except link.LinkError as error:
                raise UiError(str(error)) from error
            key = _work_patch_key(work_id, relative)
        else:
            raise UiError("実行するものの出どころが正しくありません。")

        # パッチは当てる先のゲームのディレクトリで動かす (そこへ書き込むものが多い)
        response, result = self._run_in_prefix(
            target, exe, relative, arguments, use_launch_options, working_dir=target.path
        )
        if result.ok and key not in target.installed_patches:
            target.installed_patches.append(key)
            current.save()
        response["key"] = key
        return response

    def run_game_exe(
        self,
        work_id: str,
        relative: str,
        args: str = "",
        use_launch_options: bool = True,
    ) -> dict[str, Any]:
        """ゲームの中の exe を、そのゲームの Proton 環境 (同じ AppID) で実行する。

        設定用のアプリ (``Config.exe`` など) がゲーム本体と別になっていることが多い。
        Steam には本体しか登録しないので、それ以外はここから動かす。パッチと違い、
        何度でも起動するものなので「実行済み」は記録しない。
        """
        entry = self._require_installed(work_id)
        self._require_prefix(entry)
        arguments = _split_args(args)
        try:
            exe = link.resolve_inside(entry, relative)
        except link.LinkError as error:
            raise UiError(str(error)) from error
        # Steam がゲームを起動するときと同じく、exe のあるディレクトリで動かす。
        # 設定用のアプリは、隣にある設定ファイルを相対パスで読むことが多い。
        response, _result = self._run_in_prefix(
            entry, exe, relative, arguments, use_launch_options, working_dir=exe.parent
        )
        return response

    @staticmethod
    def _require_prefix(target: state.InstalledWork) -> None:
        """Proton の環境 (プレフィックス) を持てるか。Steam の登録 (AppID) に結び付く。"""
        if target.steam_app_id is None:
            raise UiError(
                f"{target.title} はまだ Steam に登録されていません。"
                " プレフィックスは登録して一度起動すると作られます。"
            )

    def _run_in_prefix(
        self,
        target: state.InstalledWork,
        exe: Path,
        relative: str,
        arguments: list[str],
        use_launch_options: bool,
        working_dir: Path,
    ) -> "tuple[dict[str, Any], proton.RunResult]":
        """``target`` の Proton 環境で ``exe`` を実行し、画面に返す内容と結果を返す。

        パッチの実行と、ゲームの中の exe の実行で共通。``use_launch_options`` が
        立っていれば、``target`` に Steam で設定してある起動オプションを付ける
        (``%command%`` の無いものは付けない)。
        """
        if not exe.is_file():
            raise UiError(f"実行ファイルが見つかりません: {relative}")
        if not exe.name.lower().endswith(proton.PATCH_SUFFIXES):
            raise UiError("実行できるのは exe か msi だけです。")

        userdata = self._userdata()

        launch_options, launch_note = "", ""
        if use_launch_options:
            try:
                configured = (self._current_launch_options(target) or "").strip()
            except steam.SteamError:
                configured = ""
            if configured and proton.COMMAND_MARK in configured:
                launch_options = configured
            elif configured:
                launch_note = (
                    f"起動オプションに {proton.COMMAND_MARK} が無いため、"
                    "起動オプションは使いませんでした。"
                )

        try:
            result = proton.run_exe(
                userdata,
                target.steam_app_id,
                exe,
                working_dir=working_dir,
                args=arguments,
                fallback_tool=self.config.steam_compat_tool,
                launch_options=launch_options,
            )
        except proton.ProtonError as error:
            raise UiError(str(error)) from error

        return {
            "message": result.summary() + (" " + launch_note if launch_note else ""),
            "ok": result.ok,
            # 実際に付けた起動オプション。付けなかったなら空。
            "launch_options": result.launch_options,
            "returncode": result.returncode,
            "proton": result.proton,
            "prefix": result.prefix,
            "output": (result.stdout + result.stderr)[-3000:],
        }, result

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
    ) -> dict[str, Any]:
        """Steam に登録する。

        起動オプションは引数に取らない (:meth:`_launch_options_for`)。
        """
        title = title.strip()
        if not title:
            raise UiError("登録するタイトルを入力してください。")

        current = self.state()
        entry = self._require_entry(work_id, current)

        target = Path(executable)
        if not target.is_file():
            raise UiError(f"実行ファイルが見つかりません: {target}")

        # 作品ディレクトリの外の実行ファイルを登録させない
        try:
            target.resolve().relative_to(entry.path.resolve())
        except ValueError:
            raise UiError(
                f"{work_id} の展開先の外にある実行ファイルは登録できません: {target}"
            ) from None

        userdata = self._userdata()
        self._require_steam_stopped("登録")

        # 古い登録を片付ける前に読む。名前を変えて登録し直す場合も、Steam で
        # 設定してあった起動オプションを引き継ぐため。
        options, _source = self._launch_options_for(entry)

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

        # 既にある登録の起動オプションには触れない。付けるのは新しく作る登録だけ。
        result = install.register_to_steam(
            self.config, userdata, title, target, image,
            launch_options=options.strip(), keep_launch_options=True,
            # 自由登録で選んだ Proton。無ければ設定の既定値。
            compat_tool=entry.compat_tool,
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
        """ライブラリの表紙・背景に使う画像を取得する。

        一度取ったものは作品ディレクトリに残してあるので、まずそちらを見る。
        表紙を作り直すだけなら通信が要らない。

        取れなくても登録自体は続けたいので、失敗しても例外にしない。
        """
        saved = cover.cached_image(entry.path)
        if saved is not None:
            return saved

        # 自由登録の作品は DLsite に無い。画像は利用者が選んだものだけを使う。
        if entry.is_local:
            return None

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
        self._require_steam_stopped("解除")

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

    # -- 自由登録 -------------------------------------------------------

    def local_json(self) -> dict[str, Any]:
        """自由登録の一覧と、新規登録の画面を組み立てるための情報。

        DLsite には問い合わせない。ログインしていなくても使えるようにするため。
        """
        current = self.state()
        shortcuts = self.registered_shortcuts()
        roots = self._import_roots()

        items = []
        for entry in current.works.values():
            if not entry.is_local:
                continue
            installed = entry.path.is_dir()
            parts = (
                local.archive_parts(Path(entry.source))
                if entry.source and entry.source_kind == "archive"
                else []
            )
            items.append({
                "id": entry.id,
                "title": entry.title,
                "directory": entry.directory,
                "installed": installed,
                "status_label": "導入済み" if installed else "記録のみ",
                "steam_registered": _is_registered(entry, shortcuts),
                "steam_foreign": _is_foreign(entry, shortcuts),
                "steam_title": entry.steam_title or entry.title,
                "steam_app_id": entry.steam_app_id,
                "installed_at": (
                    api.format_datetime(entry.installed_at) if entry.installed_at else ""
                ),
                "installed_sort": entry.installed_at or "",
                "source": entry.source or "",
                "source_kind": entry.source_kind or "",
                # アーカイブが取り込み元に残っているか。残っていれば画面から削除できる。
                "archive_exists": bool(parts),
                "archive_label": download.format_size(
                    sum(part.stat().st_size for part in parts)
                ),
                "archive_deletable": bool(parts) and all(
                    _inside_any(part, roots) for part in parts
                ),
                "compat_tool": entry.compat_tool,
                "has_image": installed and cover.has_cached_image(entry.path),
            })

        items.sort(key=lambda item: item["title"].strip())
        return {
            "items": items,
            "roots": local.roots_json(self.config),
            "install_dirs": self.config.install_dirs,
            "install_dir": self.config.install_dir,
            "compat_tools": self._compat_tools(),
            "default_tool": self.config.steam_compat_tool,
            "steam": self.steam_status(),
        }

    def _import_roots(self) -> list[Path]:
        """実在する取り込み元。リンクを解いた形。"""
        return [root.resolve() for root in self.config.import_paths if root.is_dir()]

    def local_browse(self, root: str, relative: str, mode: str) -> dict[str, Any]:
        """取り込み元の中を見る。取り込むものや、表紙の画像を選ぶのに使う。"""
        if mode not in ("source", "image"):
            raise UiError("見る目的の指定が正しくありません。")
        try:
            return local.browse(self.config, root, relative, mode)
        except local.LocalError as error:
            raise UiError(str(error)) from error

    def local_names(self, title: str) -> dict[str, Any]:
        """ゲーム名から作るディレクトリ名の候補。ラジオボタンの横に見せる。"""
        title = title.strip()
        if not title:
            return {"title": "", "romaji": ""}
        return {
            "title": local.title_directory_name(title),
            "romaji": local.romaji_directory_name(title, self.config),
        }

    def _check_compat_tool(self, tool: str) -> str:
        """選ばれた Proton が使えるものか確かめる。空は「割り当てない」。"""
        tool = tool.strip()
        if not tool:
            return ""
        allowed = {item["value"] for item in self._compat_tools()}
        if tool not in allowed:
            raise UiError(f"'{tool}' は使える互換ツールの一覧にありません。")
        return tool

    def start_local_import(self, values: dict[str, Any]) -> dict[str, Any]:
        """自由登録の取り込みを順番待ちに積む。

        コピーや展開には時間がかかるので、ダウンロードと同じ担当スレッドで 1 本ずつ行う。
        """
        title = str(values.get("title") or "").strip()
        if not title:
            raise UiError("ゲーム名を入力してください。")

        try:
            name = local.directory_name(
                str(values.get("name_mode") or ""), title,
                str(values.get("name") or ""), self.config,
            )
            base, source = local.resolve(
                self.config, values.get("root"), str(values.get("path") or "")
            )
        except local.LocalError as error:
            raise UiError(str(error)) from error

        if source == base:
            # 取り込み元の中身すべて (他のゲームのアーカイブも) をコピーすることになる
            raise UiError("取り込み元そのものは取り込めません。その中のディレクトリを選んでください。")
        if source.is_dir():
            kind = "directory"
        elif source.is_file() and local.is_archive(source):
            kind = "archive"
            problem = local.first_part_problem(source)
            if problem:
                raise UiError(problem)
        else:
            raise UiError("取り込むアーカイブかディレクトリを選んでください。")

        try:
            target_root = self.config.resolve_install_dir(values.get("install_dir") or None)
        except ValueError as error:
            raise UiError(str(error)) from None

        compat_tool = self._check_compat_tool(str(values.get("compat_tool") or ""))

        output = target_root / name
        if output.exists():
            raise UiError(f"{output} は既にあります。別のディレクトリ名にしてください。")
        # 取り込むディレクトリの中へコピーすると、コピーしたものをまたコピーし続ける
        if kind == "directory" and is_within(output.resolve(), source):
            raise UiError("取り込むディレクトリの中には展開できません。")

        with self._lock:
            pending = [
                job for job in self._jobs.values() if job.status in ("running", "queued")
            ]
            if any(job.output == output for job in pending):
                raise UiError(f"{output} へは、すでに取り込みを待っています。")
            work_id = self.state().next_local_id(job.work_id for job in pending)

            job = Job(
                id=self._next_job_id, work_id=work_id, title=title,
                phase="copy", output=output,
            )
            spec = {
                "work_id": work_id, "title": title, "source": source, "kind": kind,
                "output": output, "compat_tool": compat_tool,
            }
            self._jobs[job.id] = job
            self._queued_args[job.id] = lambda job: self._run_local_import(job, spec)
            self._next_job_id += 1

        self._ensure_worker()
        return {"job": job.to_json(), "work_id": work_id}

    def _run_local_import(self, job: Job, spec: dict[str, Any]) -> None:
        """取り込みを実行する。失敗したら、この回で作ったものだけを片付ける。"""
        source: Path = spec["source"]
        output: Path = spec["output"]
        created = False
        try:
            if output.exists():
                raise UiError(f"{output} は既にあります。別のディレクトリ名にしてください。")

            if spec["kind"] == "directory":
                job.phase = "copy"
                job.current = "コピー中"
                job.total = local.directory_size(source)
                download.ensure_space(output.parent, job.total)

                def progress(copied: int) -> None:
                    job.written = copied

                created = True
                removed = local.copy_directory(source, output, progress=progress)
            else:
                job.phase = "extract"
                job.current = "展開中"
                parts = local.archive_parts(source)
                # 展開後の大きさは分からないので、ダウンロードと同じく倍を見ておく
                download.ensure_space(
                    output.parent, 2 * sum(part.stat().st_size for part in parts)
                )
                created = True
                removed = local.extract_archive(source, output, self.config).removed_links

            executables = archive.find_executables(output)
            entry = state.InstalledWork(
                id=spec["work_id"],
                title=spec["title"],
                directory=str(output),
                installed_at=datetime.now(timezone.utc).isoformat(),
                work_type="game",
                executable=str(executables[0]) if executables else None,
                origin=state.ORIGIN_LOCAL,
                source=str(source),
                source_kind=spec["kind"],
                compat_tool=spec["compat_tool"],
            )
            current = self.state()
            current.works[entry.id] = entry
            current.save()

            job.current = ""
            job.status = "done"
            job.message = f"取り込みました: {output}"
            if removed:
                job.message += (
                    f"（外を指すリンクと絶対パスのリンク {len(removed)} 個を取り除きました）"
                )
        except Exception as error:  # 画面に出すので、例外を受け止めて記録する
            job.status = "error"
            job.current = ""
            job.message = str(error) or error.__class__.__name__
            if created and output.is_dir():
                shutil.rmtree(output, ignore_errors=True)
            traceback.print_exc()
        finally:
            job.finished_at = time.time()

    def _require_local(
        self, work_id: str, current: "state.State | None" = None
    ) -> state.InstalledWork:
        """自由登録の作品の記録を引く。DLsite の作品なら断る。"""
        entry = self._require_entry(work_id, current)
        if not entry.is_local:
            raise UiError(f"{entry.title} は自由登録の作品ではありません。")
        return entry

    def delete_local_archive(self, work_id: str) -> dict[str, Any]:
        """取り込んだあとのアーカイブを、取り込み元から削除する。

        分割されたものは続きのファイルもまとめて消す。消すのは**取り込み元の中にある
        もの**だけ。設定を書き換えて取り込み元から外した場所のものは消さない。
        """
        entry = self._require_local(work_id)
        if entry.source_kind != "archive" or not entry.source:
            raise UiError(f"{entry.title} はアーカイブから取り込んだものではありません。")

        parts = local.archive_parts(Path(entry.source))
        if not parts:
            raise UiError(f"アーカイブは既にありません: {entry.source}")

        roots = self._import_roots()
        outside = [str(part) for part in parts if not _inside_any(part, roots)]
        if outside:
            raise UiError("取り込み元の外にあるため削除しません: " + ", ".join(outside))

        freed = 0
        for part in parts:
            size = part.stat().st_size
            part.unlink()
            freed += size

        return {
            "message": (
                f"{entry.title} のアーカイブ {len(parts)} 個"
                f"（{download.format_size(freed)}）を削除しました。"
            ),
            "freed": freed,
            "removed": [str(part) for part in parts],
        }

    def set_local_image(self, work_id: str, root: str, relative: str) -> dict[str, Any]:
        """自由登録の作品に、表紙・背景に使う画像を設定する。

        選んだ画像は作品ディレクトリに写しておく (DLsite の作品の画像と同じ置き場)。
        Steam に登録済みなら、ライブラリの画像もその場で作り直す。
        """
        current = self.state()
        entry = self._require_local(work_id, current)
        if not entry.path.is_dir():
            raise UiError(f"{entry.title} のディレクトリが見つかりません: {entry.path}")

        try:
            _base, picked = local.resolve(self.config, root, relative)
        except local.LocalError as error:
            raise UiError(str(error)) from error
        if not picked.is_file() or not local.is_image(picked):
            raise UiError("PNG か JPEG の画像を選んでください。")
        if picked.stat().st_size > local.IMAGE_LIMIT:
            raise UiError("画像が大きすぎます（20 MiB まで）。")

        image = picked.read_bytes()
        if not cover.is_supported_image(image):
            raise UiError("PNG か JPEG の画像ではありません。")
        if cover.store_image(entry.path, image) is None:
            raise UiError(f"画像を保存できませんでした: {entry.path}")

        message = f"{entry.title} の画像を設定しました。"
        if entry.steam_app_id is None:
            return {"message": message + " Steam に登録すると、ライブラリの表紙・背景に使われます。"}

        userdata = config_module.steam_userdata_path(self.config)
        if (
            userdata is not None
            and self.config.steam_grid_images
            and not _is_foreign(entry, self.registered_shortcuts())
        ):
            slots, unused = self._grid_slot_plan()
            self._rebuild_one(entry, steam.find_user_config_dirs(userdata), slots, unused)
            message += " Steam を起動し直すと、ライブラリの表示に反映されます。"
        return {"message": message}

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
        """順番待ちを処理する担当を 1 本だけ動かす。

        同時に何本も走らせない。以前は要求のたびにスレッドを起こしていたため、
        一括取得を押すと表示中の未取得すべて (実ライブラリで 18 本・60 GiB) が
        いっせいに走り、DLsite にも回線にも負担をかけていた。

        起動の判断は ``_worker_active`` で行い、担当が「もう仕事が無い」と
        決める処理と同じロックの中で見る。別々に見ると、担当が終わりかけている
        隙に積まれた分が誰にも処理されず、永久に待ち続けることになる。
        """
        with self._lock:
            if self._worker_active:
                return
            self._worker_active = True

        self._worker = threading.Thread(target=self._work_queue, daemon=True)
        self._worker.start()

    def _next_job(self) -> "tuple[Job, Callable[[Job], None]] | None":
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
                return job, self._queued_args[job.id]

            # 仕事が無いことの確認と、担当を降りる判断を同じロックの中で行う
            self._worker_active = False
            return None

    def _work_queue(self) -> None:
        """押された順に 1 本ずつ処理する。"""
        while True:
            picked = self._next_job()
            if picked is None:
                return
            job, run = picked
            try:
                run(job)
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
            self._queued_args[job.id] = (
                lambda job: self._run_download(job, work, target_root)
            )
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
            raise UiError("展開やコピーの途中は中止できません。終わるまでお待ちください。")

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
            # 通信の合間に中止の指示を確かめる。ここが唯一の割り込み点。
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
            # 同時に複数をダウンロードすると帯域も進捗表示も破綻するので、1 本ずつにする
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
        except Exception as error:  # 画面に出すので、例外を受け止めて記録する
            job.status = "error"
            job.message = str(error) or error.__class__.__name__
            traceback.print_exc()
        finally:
            job.finished_at = time.time()


def _normalized_dir(value: str) -> str:
    """場所を比べるための形。``..`` や末尾の区切り、``~`` を解いておく。

    シンボリックリンクは辿らない。承認は「利用者が書いたその場所」に対して
    行われたものなので、同じ表記かどうかだけを見る。
    """
    return os.path.normcase(os.path.normpath(os.path.expanduser(value.strip())))


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
    "unregistered": 0,  # 取得したのに Steam に登録していない。次にやること
    "outdated": 1,      # 更新が出ている
    "missing": 2,       # まだ取得していない
    "done": 3,          # 登録済み / 更新不明。やることは無い
    "record_only": 4,   # 記録だけ残っている (SD カードが外れているなど)
    "foreign": 5,       # 別環境と重複。こちらからは触れないので最後
}


def _default_rank(item: dict[str, Any]) -> int:
    """既定の並びで、その作品がどの段に入るか。"""
    if item["steam_foreign"]:
        return _DEFAULT_RANK["foreign"]
    if item["installed"]:
        # Steam 登録の話はゲームだけ。マンガやボイスを「未登録」に混ぜない。
        if item["category"] == "game" and item["steam_registered"] is False:
            return _DEFAULT_RANK["unregistered"]
        if item["outdated"]:
            return _DEFAULT_RANK["outdated"]
        return _DEFAULT_RANK["done"]
    if item["status_label"] == "記録のみ":
        return _DEFAULT_RANK["record_only"]
    return _DEFAULT_RANK["missing"]


#: 画面から自由に変えられる「置き場」の設定と、その呼び名
USER_DIR_KEYS = {"import_dirs": "自由登録の取り込み元", "runtime_dirs": "共通パッチ置き場"}


def _check_user_dirs(key: str, values: Any) -> list[str]:
    """画面から届いた置き場の一覧を確かめる。1 つでもおかしければ断る。"""
    if not isinstance(values, list):
        raise UiError(f"{USER_DIR_KEYS[key]}は一覧で指定してください。")
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in cleaned:
            _check_user_dir(USER_DIR_KEYS[key], text)
            cleaned.append(text)
    return cleaned


def _check_user_dir(label: str, text: str) -> None:
    """取り込み元・共通パッチ置き場にしてよい場所か。

    どちらも利用者が決める場所なので画面から変えられるが、合言葉を知った
    プログラムに悪用されないよう、他のアプリの持ち物がある場所は断る。

    * 取り込み元に ``~/.mozilla`` を指定されると、Cookie のファイルを「ゲーム」
      として取り込まれてしまう。
    * 共通パッチ置き場に Flatpak のアプリが書ける ``~/.var/app/...`` を指定されると、
      そのアプリが置いた exe をゲームの Proton で実行させられてしまう。

    どちらも隠しディレクトリ (``.`` で始まる名前) の中にあるので、それを断る。
    Windows では ``AppData`` が同じ役割の場所。リンクは解いてから判断する。
    ``/`` やホームディレクトリそのもの (中に隠しディレクトリを含む) と、ツール自身の
    プログラムの置き場も断る。
    """
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        raise UiError(f"{label}は絶対パスで指定してください: {text}")
    if not path.is_dir():
        raise UiError(f"{label}に指定したディレクトリが見つかりません: {text}")

    resolved = path.resolve()
    hidden = [
        part for part in resolved.parts
        if part.startswith(".") or part.lower() == "appdata"
    ]
    if hidden:
        raise UiError(
            f"{label}に、隠しディレクトリ（{hidden[0]}）の中は指定できません: {text}"
            "（他のアプリのデータが置かれる場所のため）"
        )

    project = config_module.PROJECT_ROOT.resolve()
    forbidden = {Path(resolved.anchor), Path.home().resolve(), project, project / "dlsite_deck"}
    if resolved in forbidden or is_within(resolved, project / "dlsite_deck"):
        raise UiError(f"{label}には、その場所そのものは指定できません: {text}")


#: パッチに渡す引数の長さの上限
MAX_PATCH_ARGS = 1000


def _split_args(text: str) -> list[str]:
    """画面で入力された引数を分ける。シェルは通さない。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) > MAX_PATCH_ARGS:
        raise UiError(f"引数が長すぎます（{MAX_PATCH_ARGS} 文字まで）。")
    if any(ord(char) < 0x20 for char in text):
        raise UiError("引数に改行や制御文字は使えません。")
    try:
        # Windows のパス (C:\...) の \ を消さないよう、posix=False で分ける
        parts = shlex.split(text, posix=False)
    except ValueError as error:
        raise UiError(f"引数を読み取れません（引用符の対応を確認してください）: {error}") from None
    # 引用符でまとめた部分は、囲みの引用符を外して渡す
    return [
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part
        for part in parts
    ]


def _work_patch_key(work_id: str, relative: str) -> str:
    """作品の中の exe を「どのゲームに当てたか」の記録に使う鍵。"""
    return f"work:{work_id}:{relative}"


def _runtime_key(path: Path) -> str:
    """共通パッチ置き場の exe を記録に使う鍵。置き場の順番が変わっても同じになるよう、場所で持つ。"""
    return f"runtime:{path.resolve()}"


def _inside_any(path: Path, roots: list[Path]) -> bool:
    """``path`` が、どれかの ``roots`` の中 (そのものは含まない) にあるか。リンクは解く。"""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(is_within(resolved, root, strict=True) for root in roots)


def _is_under(path: Path, root: Path) -> bool:
    """``path`` が ``root`` の下にあるか。実在しなくても文字列で判断する。"""
    return is_within(path, root)


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


