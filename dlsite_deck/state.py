"""導入済み作品の記録と、バージョンアップ検出。

記録には、DLsite から取得した作品と、「自由登録」で取り込んだ DLsite 以外の
ゲーム (``origin`` が ``"local"``) の両方が入る。更新の検出は DLsite の作品だけ。

DLsite Play の作品メタデータには ``upgrade_date`` があり、これが導入時より
新しければ更新が出ている。バージョン番号そのものは API から取れないため、
この日時を「入れてあるバージョン」の代わりに使う。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .api import Work

STATE_VERSION = 1

#: DLsite のライブラリから取得した作品
ORIGIN_DLSITE = "dlsite"
#: 「自由登録」で取り込んだ作品 (DLsite 以外のゲーム)
ORIGIN_LOCAL = "local"
#: 自由登録の作品 ID の頭。DLsite の作品 ID (RJ…) とぶつからない形にする。
LOCAL_ID_PREFIX = "LOCAL-"


@dataclass
class InstalledWork:
    """導入済み作品 1 件の記録。"""

    id: str
    title: str
    directory: str
    #: 導入時点の upgrade_date (ISO 8601)。更新判定の基準。
    upgrade_date: str | None = None
    #: 導入時点のタイトル内バージョン表記。upgrade_date が無い作品の予備。
    version: str | None = None
    installed_at: str | None = None
    maker: str | None = None
    work_type: str | None = None
    #: Steam に登録した実行ファイル
    executable: str | None = None
    #: Steam に登録した名称。既定は DLsite のタイトルだが登録時に変更できる。
    steam_title: str | None = None
    #: Steam ショートカットの AppID
    steam_app_id: int | None = None
    #: 作品画像の取得元 URL (表紙・背景に使ったもの)
    image_url: str | None = None
    #: シリアルコードが必要な作品は、そのコードをここに記録する
    serial_numbers: list[list[str]] = field(default_factory=list)
    #: このゲームに当てたパッチ。どの exe を実行したかを、出どころ付きの鍵で持つ
    #: (``work:<作品ID>:<相対パス>`` か ``runtime:<共通パッチ置き場の中の場所>``)。
    #:
    #: 以前は ``applied_patches`` として**パッチの側**に「実行したか」だけを持って
    #: いたが、どのゲームに当てたかが分からず、1 つを多くのゲームに入れるランタイムには
    #: 使えなかった。古い記録は読み込まない (捨てる)。
    installed_patches: list[str] = field(default_factory=list)
    #: どこから来た作品か。"dlsite" は DLsite のライブラリ、"local" は「自由登録」で
    #: 取り込んだもの (DLsite 以外のゲーム)。
    origin: str = ORIGIN_DLSITE
    #: 自由登録で取り込んだ元の場所 (アーカイブかディレクトリ)
    source: str | None = None
    #: 取り込んだ元の種類。"archive" か "directory"。
    source_kind: str | None = None
    #: Steam に登録するときに割り当てる Proton。None なら設定の既定値を使う。
    compat_tool: str | None = None

    @property
    def path(self) -> Path:
        return Path(self.directory)

    @property
    def is_local(self) -> bool:
        """「自由登録」で取り込んだ作品か。DLsite に問い合わせてはいけない。"""
        return self.origin == ORIGIN_LOCAL


@dataclass
class LinkRecord:
    """DLC と本編の重ね合わせ 1 件。

    「どの作品のどのパスを、どの作品のどのパスへ」の 4 項目で持つ。DLC の
    重ね方は作品によって方向も対象も違うため、これだけの自由度が要る。
    詳しい経緯と実例は :mod:`dlsite_deck.link` を見ること。
    """

    source_id: str
    target_id: str
    #: コピー元の相対パス ("" なら作品ディレクトリ全体)
    source_path: str = ""
    #: コピー先の相対パス ("" なら作品ディレクトリの直下)
    target_path: str = ""
    #: "copy" か "symlink"
    mode: str = "copy"
    #: 適用した日時 (ISO 8601)。未適用なら None。
    applied_at: str | None = None
    #: 退避したファイルの置き場
    backup_dir: str | None = None
    #: 覚え書き
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.source_path}->{self.target_id}:{self.target_path}"


class State:
    """``state.json`` の読み書き。"""

    def __init__(
        self,
        path: Path,
        works: dict[str, InstalledWork] | None = None,
        links: list["LinkRecord"] | None = None,
    ) -> None:
        self.path = path
        self.works: dict[str, InstalledWork] = works or {}
        #: DLC と本編の重ね合わせ。詳しくは link モジュールを見ること。
        self.links: list[LinkRecord] = links or []

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.is_file():
            return cls(path)

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"状態ファイルを読めませんでした: {path} ({error})") from error

        version = payload.get("version", 0)
        if version > STATE_VERSION:
            raise RuntimeError(
                f"状態ファイルの形式が新しすぎます (version {version}): {path}"
            )

        known = set(InstalledWork.__dataclass_fields__)
        works = {}
        for entry in payload.get("works", []):
            filtered = {key: value for key, value in entry.items() if key in known}
            if filtered.get("id"):
                works[filtered["id"]] = InstalledWork(**filtered)

        link_fields = set(LinkRecord.__dataclass_fields__)
        links = []
        for entry in payload.get("links", []):
            picked = {k: v for k, v in entry.items() if k in link_fields}
            if picked.get("source_id") and picked.get("target_id"):
                links.append(LinkRecord(**picked))

        return cls(path, works, links)

    def save(self) -> None:
        """一時ファイル経由で原子的に書き出す。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "works": [asdict(work) for work in sorted(self.works.values(), key=lambda w: w.id)],
            "links": [asdict(item) for item in self.links],
        }

        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(handle.name, self.path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------

    def get(self, work_id: str) -> InstalledWork | None:
        return self.works.get(work_id)

    def record(
        self,
        work: Work,
        directory: Path,
        executable: Path | None = None,
        serial_numbers: Iterable[tuple[str, str]] = (),
        steam_app_id: int | None = None,
    ) -> InstalledWork:
        """導入完了を記録する。"""
        existing = self.works.get(work.id)
        entry = InstalledWork(
            id=work.id,
            title=work.title,
            directory=str(directory),
            upgrade_date=work.updated_at.isoformat() if work.updated_at else None,
            version=work.version,
            installed_at=datetime.now(timezone.utc).isoformat(),
            maker=work.maker,
            work_type=work.work_type,
            executable=str(executable) if executable else None,
            image_url=work.image_url,
            steam_title=existing.steam_title if existing else None,
            steam_app_id=steam_app_id
            if steam_app_id is not None
            else (existing.steam_app_id if existing else None),
            serial_numbers=[[label, value] for label, value in serial_numbers],
        )
        self.works[work.id] = entry
        return entry

    def next_local_id(self, reserved: Iterable[str] = ()) -> str:
        """自由登録の作品に振る、まだ使われていない ID。

        ``reserved`` には、順番待ちなどでまだ記録に載っていない ID を渡す。
        今ある番号の最大の次を振る。作品を消すと、その作品の退避なども一緒に
        片付くので、あとで同じ番号を振り直しても混ざらない。
        """
        taken = set(self.works) | set(reserved)
        numbers = [
            int(item[len(LOCAL_ID_PREFIX):])
            for item in taken
            if item.startswith(LOCAL_ID_PREFIX) and item[len(LOCAL_ID_PREFIX):].isdigit()
        ]
        return f"{LOCAL_ID_PREFIX}{max(numbers, default=0) + 1:04d}"

    def forget(self, work_id: str) -> InstalledWork | None:
        return self.works.pop(work_id, None)

    def is_installed(self, work_id: str) -> bool:
        entry = self.works.get(work_id)
        return bool(entry and entry.path.is_dir())


def needs_update(entry: InstalledWork | None, work: Work) -> bool:
    """作品に更新が出ているか。

    まず ``upgrade_date`` で比べる。DLsite がこれを返さない作品も多いので
    (実ライブラリでは 193 件中 77 件)、その場合はタイトルに書かれたバージョン
    表記で比べる。どちらも無ければ判定できないので更新扱いにしない。
    """
    if entry is None:
        return False

    if work.updated_at is not None and entry.upgrade_date:
        try:
            installed = datetime.fromisoformat(entry.upgrade_date)
        except ValueError:
            return False
        if installed.tzinfo is None:
            installed = installed.replace(tzinfo=timezone.utc)
        return work.updated_at > installed

    # upgrade_date が使えないときの予備手段
    if work.version and entry.version and work.version != entry.version:
        return True

    return False


@dataclass
class LibraryStatus:
    """ライブラリ 1 件の現在の状態。"""

    work: Work
    entry: InstalledWork | None

    @property
    def installed(self) -> bool:
        return self.entry is not None and self.entry.path.is_dir()

    @property
    def outdated(self) -> bool:
        return self.installed and needs_update(self.entry, self.work)

    @property
    def update_unknown(self) -> bool:
        """更新の有無を判定できないか。

        DLsite は ``upgrade_date`` を返さない作品が多く (実ライブラリでは
        193 件中 77 件)、タイトルの版表記も当てにならない (同じライブラリで
        版が取れたのは 3 件、しかもすべて upgrade_date も持っていた)。
        判定できないものを「導入済み」とだけ出すと、更新が無いのか
        分からないのかの区別が付かない。
        """
        if not self.installed:
            return False
        if self.work.updated_at is not None and self.entry and self.entry.upgrade_date:
            return False
        if self.work.version and self.entry and self.entry.version:
            return False
        return True

    @property
    def label(self) -> str:
        if self.outdated:
            return "更新あり"
        if self.installed:
            return "更新不明" if self.update_unknown else "導入済み"
        if self.entry is not None:
            return "記録のみ"
        return "未取得"


def build_status(works: Iterable[Work], state: State) -> list[LibraryStatus]:
    """ライブラリと状態を突き合わせる。"""
    return [LibraryStatus(work=work, entry=state.get(work.id)) for work in works]
