"""DLC を本編に結び付けて、ファイルを重ねる。

DLsite の API には DLC を示す情報も、本編への参照も無い。実際に購入済みの
193 件を調べた範囲では ``work_type`` も ``purchase_type`` も本編と DLC で
同じ値だった。つまり **自動判別はできない**。ここでは利用者が関係を宣言し、
その通りに重ねることに徹する。

重ね方も作品によって違う。実際にライブラリにある 3 例:

* 本編 ← パッチ   … DLC 全体を本編の直下へ (168 個上書き)
* 本編 ← DLC     … DLC 全体を本編の直下へ (exe を含む 8 個上書き)
* 本編 ← 続編     … **本編の** ``<章>/Content`` を **続編の** ``Content`` へ

3 例目は方向が逆で、しかも本編の一部フォルダだけが対象になる。この違いを
吸収するため、関係は「どの作品のどのパスを、どの作品のどのパスへ」という
4 項目で持つ。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import state as state_module


#: 結び付きの記録そのものは state に置いてある (保存の都合)。
#: こちらの名前でも参照できるようにしておく。
Link = state_module.LinkRecord


class LinkError(RuntimeError):
    """結び付けの失敗。"""


def resolve_inside(entry: state_module.InstalledWork, relative: str) -> Path:
    """作品ディレクトリからの相対パスを、実際の場所に直す。

    作品ディレクトリの外を指す指定は受け付けない。``..`` を含む値が
    設定ファイル経由で紛れ込んでも、別の場所を壊さないようにするため。
    """
    root = entry.path.resolve()
    if not relative:
        return root

    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise LinkError(f"作品ディレクトリの外を指しています: {relative}")
    return candidate


@dataclass
class ApplyPlan:
    """適用したときに何が起きるかの下見。"""

    link: Link
    source: Path
    target: Path
    #: 上書きされる既存ファイル (コピー先からの相対パス)
    overwrites: list[str] = field(default_factory=list)
    #: 新しく置かれるファイル
    additions: list[str] = field(default_factory=list)
    #: 置くファイルの合計バイト数
    total_bytes: int = 0

    @property
    def is_directory(self) -> bool:
        return self.source.is_dir()

    def summary(self) -> str:
        return (
            f"{len(self.additions)} 個を追加、{len(self.overwrites)} 個を上書き "
            f"({self.total_bytes / 1024 / 1024:.1f} MiB)"
        )


def plan(current: state_module.State, item: Link) -> ApplyPlan:
    """適用前の下見。上書きされるものを数えて返す。

    実行より先にこれを見せる。上書きは元に戻せないため、利用者が件数を
    見てから決められるようにする。
    """
    src_entry = current.get(item.source_id)
    dst_entry = current.get(item.target_id)
    if src_entry is None or not src_entry.path.is_dir():
        raise LinkError(f"コピー元 {item.source_id} が導入されていません。")
    if dst_entry is None or not dst_entry.path.is_dir():
        raise LinkError(f"コピー先 {item.target_id} が導入されていません。")

    source = resolve_inside(src_entry, item.source_path)
    target = resolve_inside(dst_entry, item.target_path)
    if not source.exists():
        raise LinkError(f"コピー元が見つかりません: {source}")

    result = ApplyPlan(link=item, source=source, target=target)

    if source.is_file():
        destination = target / source.name if target.is_dir() else target
        entry = destination.name
        (result.overwrites if destination.exists() else result.additions).append(entry)
        result.total_bytes = source.stat().st_size
        return result

    for base, _dirs, files in os.walk(source):
        for name in files:
            full = Path(base) / name
            relative = full.relative_to(source)
            destination = target / relative
            text = str(relative).replace("\\", "/")
            (result.overwrites if destination.exists() else result.additions).append(text)
            try:
                result.total_bytes += full.stat().st_size
            except OSError:
                pass

    result.overwrites.sort()
    result.additions.sort()
    return result


#: 上書きされたファイルの退避先。作品フォルダの**隣**に置く。
#:
#: 中に置くと、重ねた相手を取り直したときに一緒に消えてしまう。
BACKUP_DIR_NAME = ".dlsite_backup"


def backup_root_dir(entry: state_module.InstalledWork) -> Path:
    """その作品の退避先がまとめて置かれる場所。"""
    return entry.path.parent / BACKUP_DIR_NAME


def _backup_dir(item: Link, target_entry: state_module.InstalledWork) -> Path:
    """その結び付き専用の退避先。**毎回おなじ場所**を返す。

    以前は適用のたびに日時で新しい世代を作っていたが、重ね直すたびに増える
    うえ、2 世代目以降の中身は「前回の DLC が当たったあとの姿」で、本当に
    戻したい手つかずの原本は最古の 1 つにしかなかった。結び付きごとに 1 つに
    固定し、控えるのを先勝ちにすることで (:func:`_place`)、ここには常に
    原本だけが入る。
    """
    digest = hashlib.sha1(item.key.encode("utf-8")).hexdigest()[:12]
    return backup_root_dir(target_entry) / f"{target_entry.id}-{digest}"


def backup_dirs_for(entry: state_module.InstalledWork) -> list[Path]:
    """その作品が上書きされたときの退避先を集める。

    退避先は ``<作品ID>-<印>`` という名前なので、作品 ID で引き当てられる。
    区切りの ``-`` まで見るので、``RJ1`` が ``RJ12-...`` を拾うことはない。
    日時で名付けていた頃の退避も同じ形なので、そのまま見つかる。
    """
    root = backup_root_dir(entry)
    if not root.is_dir():
        return []

    prefix = f"{entry.id}-"
    try:
        return sorted(
            path for path in root.iterdir()
            if path.is_dir() and path.name.startswith(prefix)
        )
    except OSError:
        return []


def apply(
    current: state_module.State,
    item: Link,
    backup: bool = True,
) -> ApplyPlan:
    """結び付けを実際に適用する。

    上書きされる既存ファイルは、既定で退避してから置き換える。DLC の適用は
    本体の exe まで差し替えることがあり (実物にそういう DLC があった)、
    やり直せる余地を残しておきたいため。

    同じ結び付けを重ねて適用しても退避は増えない。置き場は結び付きごとに
    1 つで、既に控えのあるファイルは控え直さない。
    """
    result = plan(current, item)
    target_entry = current.get(item.target_id)
    assert target_entry is not None  # plan() が確認済み

    backup_dir: Path | None = None
    if backup and result.overwrites:
        backup_dir = _backup_dir(item, target_entry)
        backup_dir.mkdir(parents=True, exist_ok=True)

    if result.source.is_file():
        destination = (
            result.target / result.source.name
            if result.target.is_dir()
            else result.target
        )
        _place(result.source, destination, backup_dir, destination.name, item.mode)
    else:
        for relative in result.overwrites + result.additions:
            source = result.source / relative
            if not source.is_file():
                continue
            _place(source, result.target / relative, backup_dir, relative, item.mode)

    item.applied_at = datetime.now(timezone.utc).isoformat()
    item.backup_dir = str(backup_dir) if backup_dir else None
    return result


def _place(
    source: Path,
    destination: Path,
    backup_dir: Path | None,
    relative: str,
    mode: str,
) -> None:
    """1 ファイルを置く。既存があれば先に退避する。"""
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() or destination.is_symlink():
        if backup_dir is not None:
            saved = backup_dir / relative
            # 先勝ち。2 回目以降の適用で控え直すと、原本が「1 回目の DLC が
            # 当たったあとの姿」に置き換わってしまう。最初の 1 つを守る。
            if not saved.exists():
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
        destination.unlink()

    if mode == "symlink":
        try:
            destination.symlink_to(source)
            return
        except OSError:
            # 対応していない場所ならコピーに落とす
            pass

    shutil.copy2(source, destination)


def revert(current: state_module.State, item: Link) -> str:
    """退避したファイルを書き戻す。

    退避には手つかずの原本だけが入っているので、何度重ね直したあとでも
    上書きされる前の姿に戻る。

    DLC が新しく置いたファイルまでは消さない。退避の中身だけではどれが DLC 由来か
    区別できず、消しすぎる方が害が大きいため。上書きされた分を元に戻すだけに留める。
    """
    if not item.backup_dir:
        return "退避したファイルがないため、書き戻せません。"

    backup_dir = Path(item.backup_dir)
    if not backup_dir.is_dir():
        return f"退避先が見つかりません: {backup_dir}"

    target_entry = current.get(item.target_id)
    if target_entry is None or not target_entry.path.is_dir():
        raise LinkError(f"コピー先 {item.target_id} が導入されていません。")
    target = resolve_inside(target_entry, item.target_path)

    restored = 0
    for base, _dirs, files in os.walk(backup_dir):
        for name in files:
            saved = Path(base) / name
            relative = saved.relative_to(backup_dir)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, destination)
            restored += 1

    item.applied_at = None
    return f"{restored} 個を書き戻しました。"
