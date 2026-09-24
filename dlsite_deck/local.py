"""「自由登録」: DLsite 以外のゲームを取り込む。

利用者が用意したアーカイブかディレクトリを、インストール先へ展開 (またはコピー) して
記録に載せる。載せたあとは DLsite の作品と同じように、Steam への登録やパッチの実行が
できる。

**取り込めるのは、設定の取り込み元 (``import_dirs``) の中だけ。** 取り込むものの場所を
そのまま受け取ると、API を呼べるプロセスが Firefox の Cookie などを「ゲーム」として
取り込めてしまう。場所は番号 (何番目の取り込み元か) と、その中の相対パスで受け取り、
リンクを解いたうえで取り込み元の中にあることを確かめる。取り込み元そのものは設定画面から
変えられるが、隠しディレクトリは指定させない (``webui._check_user_dir``)。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import archive, config as config_module, download, romaji
from .paths import is_within

#: 取り込めるアーカイブ。分割されたものは先頭のファイルを選んでもらう。
ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z")
#: 表紙に使える画像。Steam のライブラリ画像は PNG か JPEG。
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
#: 表紙に使う画像の大きさの上限。これより大きいものは画像でない可能性が高い。
IMAGE_LIMIT = 20 * 1024 * 1024
#: 1 つのディレクトリで見せる数の上限
BROWSE_LIMIT = 400

#: 連番で分割されたアーカイブ (``game.7z.001`` など) の番号
_NUMBERED_PART = re.compile(r"\.(\d{3})$")
#: RAR の分割 (``game.part1.rar`` など) の番号
_RAR_PART = re.compile(r"\.part(\d+)\.rar$", re.IGNORECASE)

#: ディレクトリ名に使えない文字。Windows で使えないものもそろえて断る。
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class LocalError(RuntimeError):
    """自由登録の操作が失敗した。"""


# ----------------------------------------------------------------------
# 取り込み元
# ----------------------------------------------------------------------


def roots_json(cfg: config_module.Config) -> list[dict[str, Any]]:
    """取り込み元の一覧。画面ではこの番号で場所を指す。"""
    return [
        {"index": index, "path": str(path), "exists": path.is_dir()}
        for index, path in enumerate(cfg.import_paths)
    ]


def resolve(cfg: config_module.Config, root: Any, relative: str) -> tuple[Path, Path]:
    """取り込み元の番号と相対パスから、実際の場所を求める。

    戻り値は ``(取り込み元, 指した場所)``。どちらもリンクを解いた形。
    指した場所が取り込み元の外に出る (``..`` やリンクを辿る) なら断る。
    """
    return resolve_in(cfg.import_paths, root, relative, "取り込み元")


def resolve_in(
    roots: list[Path], root: Any, relative: str, label: str
) -> tuple[Path, Path]:
    """承認済みの場所の番号と、その中の相対パスから、実際の場所を求める。

    自由登録の取り込み元と、共通パッチ置き場で共通に使う。``label`` は案内の文言に
    入れる場所の呼び名。
    """
    try:
        index = int(root)
    except (TypeError, ValueError):
        raise LocalError(f"{label}の指定が正しくありません。") from None

    if not 0 <= index < len(roots):
        raise LocalError(f"{label}の指定が正しくありません。画面を開き直してください。")

    base = roots[index]
    if not base.is_dir():
        raise LocalError(f"{label}が見つかりません: {base}")
    base = base.resolve()

    relative = (relative or "").strip().replace("\\", "/")
    if relative.startswith("/") or re.match(r"^[A-Za-z]:", relative):
        raise LocalError(f"{label}の中の場所を指定してください。")

    target = (base / relative).resolve() if relative else base
    if not is_within(target, base):
        raise LocalError(f"{label}の外は指定できません。")
    return base, target


def relative_to_root(path: Path, base: Path) -> str:
    """取り込み元からの相対パス。画面とのやり取りに使う。"""
    if path == base:
        return ""
    return str(path.relative_to(base)).replace("\\", "/")


# ----------------------------------------------------------------------
# アーカイブ
# ----------------------------------------------------------------------


def is_archive(path: Path) -> bool:
    """取り込めるアーカイブに見えるか (分割の 2 つ目以降も含む)。"""
    name = path.name.lower()
    return name.endswith(ARCHIVE_SUFFIXES) or bool(_NUMBERED_PART.search(name))


def is_image(path: Path) -> bool:
    return path.name.lower().endswith(IMAGE_SUFFIXES)


def first_part_problem(path: Path) -> str | None:
    """分割アーカイブの 2 つ目以降を選んでいたら、その旨を返す。

    展開ツールは先頭から続きを探すので、途中のファイルからは展開できない。
    """
    name = path.name
    numbered = _NUMBERED_PART.search(name)
    if numbered and int(numbered.group(1)) != 1:
        return f"分割アーカイブの先頭（{name[:numbered.start()]}.001）を選んでください。"
    part = _RAR_PART.search(name)
    if part and int(part.group(1)) != 1:
        return f"分割アーカイブの先頭（{name[:part.start()]}.part1.rar）を選んでください。"
    return None


def archive_parts(first: Path) -> list[Path]:
    """アーカイブを構成するファイル。先頭と、同じディレクトリにある続き。

    展開後にアーカイブを削除するとき、続きのファイルだけが残らないように使う。
    続きかどうかは名前の形だけで判断し、別のアーカイブを巻き込まないよう
    先頭と同じ名前の部分を持つものに限る。
    """
    directory = first.parent
    name = first.name
    patterns: list[re.Pattern[str]] = []

    numbered = _NUMBERED_PART.search(name)
    rar_part = _RAR_PART.search(name)
    if numbered:
        # game.7z.001 → game.7z.002 …
        patterns.append(re.compile(re.escape(name[:numbered.start()]) + r"\.\d{3}$", re.IGNORECASE))
    elif rar_part:
        # game.part1.rar → game.part2.rar …
        patterns.append(
            re.compile(re.escape(name[:rar_part.start()]) + r"\.part\d+\.rar$", re.IGNORECASE)
        )
    else:
        stem, suffix = os.path.splitext(name)
        if suffix.lower() == ".zip":
            # 分割 ZIP: game.zip + game.z01 …
            patterns.append(re.compile(re.escape(stem) + r"\.z\d{2}$", re.IGNORECASE))
        elif suffix.lower() == ".rar":
            # 古い形の分割 RAR: game.rar + game.r00 …
            patterns.append(re.compile(re.escape(stem) + r"\.r\d{2}$", re.IGNORECASE))

    parts = [first] if first.is_file() else []
    if patterns and directory.is_dir():
        for child in sorted(directory.iterdir()):
            if child == first or not child.is_file():
                continue
            if any(pattern.fullmatch(child.name) for pattern in patterns):
                parts.append(child)
    return parts


def archive_plan(first: Path) -> archive.ArchivePlan:
    """展開のしかた。単一の ZIP は標準ライブラリで、それ以外は外部ツールで展開する。"""
    parts = archive_parts(first)
    if first.suffix.lower() == ".zip" and parts == [first]:
        return archive.ArchivePlan(kind="zip", files=[first])
    # 外部ツールには先頭だけを渡す。続きはツールが自分で探す。
    return archive.ArchivePlan(kind="external", files=[first])


# ----------------------------------------------------------------------
# 取り込み元を見る
# ----------------------------------------------------------------------


def browse(cfg: config_module.Config, root: Any, relative: str, mode: str) -> dict[str, Any]:
    """取り込み元の中を見る。

    ``mode`` が ``"source"`` ならアーカイブを、``"image"`` なら画像を選べるものとして
    印を付ける。ディレクトリはどちらでもたどれる。
    """
    base, here = resolve(cfg, root, relative)
    if not here.is_dir():
        raise LocalError(f"ディレクトリではありません: {relative}")

    directories, files = [], []
    try:
        children = sorted(here.iterdir(), key=lambda p: p.name.lower())
    except OSError as error:
        raise LocalError(f"読めませんでした: {here} ({error})") from error

    for child in children[:BROWSE_LIMIT]:
        # 外を指すリンクも名前は並ぶが、たどったり選んだりすると resolve() で断る
        item = relative_to_root(child, base)
        if child.is_dir():
            directories.append({"name": child.name, "relative": item})
            continue
        try:
            size = child.stat().st_size
        except OSError:
            size = 0
        selectable = is_archive(child) if mode == "source" else is_image(child)
        files.append({
            "name": child.name,
            "relative": item,
            "size_label": download.format_size(size),
            "selectable": selectable,
        })

    current = relative_to_root(here, base)
    parent = None
    if current:
        parent = relative_to_root(here.parent, base)
    return {
        "root": int(root),
        "root_path": str(base),
        "relative": current,
        "parent": parent,
        "directories": directories,
        "files": files,
        "truncated": len(children) > BROWSE_LIMIT,
    }


# ----------------------------------------------------------------------
# ディレクトリ名
# ----------------------------------------------------------------------


def title_directory_name(title: str) -> str:
    """ゲーム名をそのままディレクトリ名に使う形。使えない文字は _ に置き換える。"""
    name = unicodedata.normalize("NFC", title)
    name = _FORBIDDEN.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(". ")
    return name[:96].rstrip(". ")


def romaji_directory_name(title: str, cfg: config_module.Config) -> str:
    """ゲーム名をローマ字にした形。ライブラリの作品と同じ変換を使う。"""
    return romaji.directory_name(title, template="{romaji}", long_vowel=cfg.long_vowel)


def check_directory_name(name: str) -> str:
    """利用者が入力したディレクトリ名を確かめる。"""
    name = (name or "").strip()
    if not name:
        raise LocalError("ディレクトリ名を入力してください。")
    if name in (".", "..") or _FORBIDDEN.search(name):
        raise LocalError(
            'ディレクトリ名に使えない文字が含まれています（/ \\ : * ? " < > | は使えません）。'
        )
    if name.endswith((".", " ")):
        raise LocalError("ディレクトリ名の末尾に . や空白は使えません。")
    if len(name) > 120:
        raise LocalError("ディレクトリ名が長すぎます（120 文字まで）。")
    return name


def directory_name(mode: str, title: str, custom: str, cfg: config_module.Config) -> str:
    """ラジオボタンの選択に応じたディレクトリ名。"""
    if mode == "title":
        name = title_directory_name(title)
    elif mode == "romaji":
        name = romaji_directory_name(title, cfg)
    elif mode == "custom":
        name = custom
    else:
        raise LocalError("ディレクトリ名の決め方を選んでください。")
    return check_directory_name(name)


# ----------------------------------------------------------------------
# 取り込む
# ----------------------------------------------------------------------


def directory_size(path: Path) -> int:
    """ディレクトリ以下の合計バイト数。リンクは辿らない。"""
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(base) / name).lstat().st_size
            except OSError:
                pass
    return total


def copy_directory(
    source: Path, destination: Path, progress: Callable[[int], None] | None = None
) -> list[str]:
    """ディレクトリを丸ごとコピーする。取り除いたリンクの一覧を返す。

    いったん隣の作業用ディレクトリにコピーしてから名前を変える。途中で失敗しても、
    中途半端なディレクトリが展開先に残らない。

    リンクはリンクのままコピーし、コピー先の外を指すものと絶対パスのものは取り除く
    (アーカイブを展開したときと同じ扱い)。
    """
    if destination.exists():
        raise LocalError(f"既にあります: {destination}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(destination.parent)))
    copied = 0

    def copy(src: str, dst: str) -> str:
        nonlocal copied
        result = shutil.copy2(src, dst)
        try:
            copied += os.path.getsize(dst)
        except OSError:
            pass
        if progress:
            progress(copied)
        return result

    try:
        work = staging / "copy"
        shutil.copytree(source, work, symlinks=True, copy_function=copy)
        removed = archive.drop_escaping_links(work)
        work.rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return removed


def extract_archive(
    source: Path, destination: Path, cfg: config_module.Config
) -> archive.ExtractResult:
    """アーカイブを展開する。単一のディレクトリの引き上げなどは設定に従う。"""
    return archive.extract(
        archive_plan(source),
        destination,
        flatten_single_root=cfg.flatten_single_root,
        strip_versions=cfg.strip_versions,
    )
