"""アーカイブの判定・展開と、展開後のディレクトリ整形。

DLsite の配布物は概ね次の 2 形態:

* 単一の ZIP
* 旧来の分割自己展開 RAR (先頭が ``.exe``、続きが ``.bin`` / ``.part2`` 等)

ZIP は標準ライブラリで扱える。RAR は標準ライブラリでは展開できないため、
``bsdtar`` / ``7z`` / ``unar`` / ``unrar`` のうち入っているものを使う。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# 展開に使える外部コマンドを優先度順に並べる。
# 分割 (マルチボリューム) RAR を確実に扱えるものを先に置く。libarchive 系は
# 分割 RAR を読めないことがあるため後ろに回す。SteamOS には bsdtar が標準で入る。
RAR_TOOLS = (
    ("unar", ["-quiet", "-force-overwrite", "-output-directory", "{dest}", "{archive}"]),
    ("7zz", ["x", "-y", "-o{dest}", "{archive}"]),
    ("7z", ["x", "-y", "-o{dest}", "{archive}"]),
    ("unrar", ["x", "-y", "{archive}", "{dest}/"]),
    ("bsdtar", ["-x", "-f", "{archive}", "-C", "{dest}"]),
    # Windows 10 以降と macOS の tar は bsdtar だが、Linux の tar は GNU tar で
    # RAR を読めない。中身を確認したうえでのみ採用する。
    ("tar", ["-x", "-f", "{archive}", "-C", "{dest}"]),
)

#: 実体が libarchive かどうかを確かめてから使うコマンド
_NEEDS_LIBARCHIVE_CHECK = {"tar"}

#: PATH に無い展開ツールを探す追加ディレクトリ。設定または環境変数で足せる。
EXTRA_TOOL_DIRS: list[str] = [
    directory
    for directory in os.environ.get("DLSITE_DECK_TOOL_DIRS", "").split(os.pathsep)
    if directory
]


def add_tool_dirs(directories: Iterable[str]) -> None:
    """展開ツールを探すディレクトリを追加する。PATH より優先して見る。"""
    for directory in directories:
        if directory and directory not in EXTRA_TOOL_DIRS:
            EXTRA_TOOL_DIRS.append(directory)


class ArchiveError(RuntimeError):
    """展開に失敗した。"""


@dataclass
class ArchivePlan:
    """ダウンロード済みファイル群の扱い方。"""

    #: "zip" | "split_rar" | "passthrough"
    kind: str
    files: list[Path] = field(default_factory=list)

    @property
    def is_extractable(self) -> bool:
        return self.kind in ("zip", "split_rar")


@dataclass
class ExtractResult:
    """展開結果。"""

    output_dir: Path
    entries: list[Path]
    flattened: bool
    renamed: list[tuple[Path, Path]] = field(default_factory=list)


def plan_archive(files: list[Path]) -> ArchivePlan:
    """ファイル群からアーカイブ種別を判定する。"""
    files = [path for path in files if path.is_file()]
    if not files:
        return ArchivePlan(kind="passthrough", files=[])

    ordered = sorted(files, key=_split_sort_key)

    if len(ordered) == 1 and ordered[0].suffix.lower() == ".zip":
        return ArchivePlan(kind="zip", files=ordered)

    # 分割 ZIP (.zip + .z01...) は zipfile では扱えないので RAR と同じ経路に流す
    if ordered[0].suffix.lower() == ".exe":
        return ArchivePlan(kind="split_rar", files=ordered)

    if len(ordered) > 1 and ordered[0].suffix.lower() == ".zip":
        return ArchivePlan(kind="split_rar", files=ordered)

    return ArchivePlan(kind="passthrough", files=ordered)


# "part2" / "bin3" / ".r01" / ".z02" / 末尾の ".2" からパート番号を読む
_PART_NUMBER = re.compile(
    r"(?:part|bin|vol)[._-]?(\d+)|\.(?:r|z)(\d+)$|\.(\d+)$", re.IGNORECASE
)


def _split_sort_key(path: Path) -> tuple[int, int, str]:
    """分割ファイルを本来の順序に並べる。

    自己展開の先頭ボリューム (``.exe``) を必ず最初に置き、残りはパート番号の
    **数値**で並べる。名前順のままだと ``part10`` が ``part2`` より前に来る。
    """
    name = path.name.lower()

    if path.suffix.lower() == ".exe":
        return (0, 0, name)

    match = _PART_NUMBER.search(name)
    if match:
        number = next(group for group in match.groups() if group is not None)
        return (1, int(number), name)

    # 番号を読み取れないものは後ろに回す
    return (2, 0, name)


def extract(
    plan: ArchivePlan,
    output_dir: Path,
    flatten_single_root: bool = True,
    strip_versions: bool = True,
) -> ExtractResult:
    """アーカイブを ``output_dir`` に展開する。

    いったんステージング領域に展開してから中身を移すことで、途中で失敗しても
    展開先が中途半端な状態にならないようにしている。単一フォルダだけを含む
    アーカイブは、そのフォルダの中身を展開先直下に引き上げる。
    """
    if not plan.is_extractable:
        raise ArchiveError(
            "展開できる形式ではありません: "
            + ", ".join(path.name for path in plan.files)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(output_dir)))

    try:
        if plan.kind == "zip":
            _extract_zip(plan.files[0], staging)
        else:
            _extract_split_rar(plan.files, staging)

        root, flattened = _content_root(staging, flatten_single_root)
        entries = _move_contents(root, output_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    renamed: list[tuple[Path, Path]] = []
    if strip_versions:
        renamed = strip_version_names(output_dir)

    return ExtractResult(
        output_dir=output_dir,
        entries=sorted(output_dir.iterdir()),
        flattened=flattened,
        renamed=renamed,
    )


def _extract_zip(archive: Path, staging: Path) -> None:
    """ZIP を展開する。日本語ファイル名の文字化けにも対処する。"""
    try:
        with zipfile.ZipFile(archive) as zip_file:
            for info in zip_file.infolist():
                name = _decode_zip_name(info)
                target = _safe_join(staging, name)

                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(info) as source, open(target, "wb") as destination:
                    shutil.copyfileobj(source, destination)

                # 実行ビットが立っているエントリは保持する (Linux 版同梱ゲーム用)
                mode = info.external_attr >> 16
                if mode & 0o111:
                    target.chmod(target.stat().st_mode | 0o111)
    except zipfile.BadZipFile as error:
        raise ArchiveError(f"ZIP を展開できませんでした: {archive.name} ({error})") from error


#: UTF-8 フラグが無い ZIP のファイル名を読むときに試す文字コード。
#:
#: UTF-8 を先に試す。cp932 はほとんどのバイト列を「解釈できてしまう」ので、
#: 先に試すと本当は UTF-8 だった名前を化けさせる。逆に cp932 の名前は UTF-8 として
#: 不正になることがほとんどなので、UTF-8 を先に置いても取りこぼさない。
#: (実際の DLsite の ZIP は全エントリがフラグ無しの cp932 で、UTF-8 では解釈不可)
ZIP_NAME_ENCODINGS = ("utf-8", "cp932")


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """ZIP エントリ名を復元する。

    UTF-8 フラグが無い ZIP は zipfile が cp437 として読むため、
    国内製アーカイブでよくある cp932 などを推定して読み直す。
    ``unzip -O cp932`` に相当する処理。
    """
    if info.flag_bits & 0x800:
        return info.filename

    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename

    for encoding in ZIP_NAME_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return info.filename


def _extract_split_rar(files: list[Path], staging: Path) -> None:
    """分割自己展開アーカイブを外部ツールで展開する。

    先頭ファイルは ``.exe`` (SFX) なので、展開ツールが中身を RAR と認識できるよう
    一時的に ``.rar`` へ改名し、終わったら必ず元に戻す。
    """
    first = files[0]
    tool = _find_rar_tool()
    if tool is None:
        raise ArchiveError(
            "分割アーカイブの展開に必要なコマンドが見つかりません。"
            "bsdtar / 7z / unar / unrar のいずれかを用意してください "
            "(SteamDeck では `flatpak install flathub org.gnome.FileRoller` など)。"
        )

    name, executable, template = tool
    renamed = first.suffix.lower() == ".exe"
    working = first.with_suffix(".rar") if renamed else first

    if renamed:
        if working.exists():
            raise ArchiveError(f"作業用ファイルが既に存在します: {working}")
        first.rename(working)

    try:
        command = [executable] + [
            part.format(archive=str(working), dest=str(staging)) for part in template
        ]
        result = subprocess.run(
            command,
            cwd=str(working.parent),
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:500]
            raise ArchiveError(
                f"{name} による展開に失敗しました (終了コード {result.returncode}): {detail}"
            )
    finally:
        if renamed and working.exists():
            working.rename(first)


def _find_rar_tool() -> tuple[str, str, list[str]] | None:
    """使える展開コマンドを ``(表示名, 実行パス, 引数テンプレート)`` で返す。"""
    for name, template in RAR_TOOLS:
        for executable in _tool_candidates(name):
            if name in _NEEDS_LIBARCHIVE_CHECK and not _is_libarchive(executable):
                continue
            return name, executable, template
    return None


def _tool_candidates(name: str) -> list[str]:
    """コマンド名から実行パスの候補を挙げる。"""
    candidates = []

    # 明示指定されたディレクトリを PATH より先に見る
    for directory in EXTRA_TOOL_DIRS:
        found = shutil.which(name, path=directory)
        if found:
            candidates.append(found)

    found = shutil.which(name)
    if found:
        candidates.append(found)

    # Windows では PATH 上の tar が GNU tar (Git 同梱) のことがあるので、
    # OS 標準の bsdtar も候補に入れる。
    if name == "tar" and sys.platform == "win32":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        bundled = Path(system_root) / "System32" / "tar.exe"
        if bundled.is_file():
            candidates.append(str(bundled))

    return candidates


def _is_libarchive(executable: str) -> bool:
    """``tar`` の実体が bsdtar (libarchive) かどうか。"""
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    output = (result.stdout + result.stderr).lower()
    return "bsdtar" in output or "libarchive" in output


def _safe_join(base: Path, entry: str) -> Path:
    """アーカイブ内のパスが展開先の外に出ないことを保証する。"""
    # Windows で作られたアーカイブは区切りがバックスラッシュのことがある
    entry = entry.replace("\\", "/")
    segments = entry.split("/")

    # 親参照・絶対パス・ドライブ指定はいずれも展開先の外を指しうるので受け付けない
    if ".." in segments:
        raise ArchiveError(f"アーカイブ内のパスが展開先の外を指しています: {entry!r}")
    if entry.startswith("/"):
        raise ArchiveError(f"アーカイブ内のパスが絶対パスです: {entry!r}")
    if _is_absolute_part(segments[0]):
        raise ArchiveError(f"アーカイブ内のパスがドライブを指しています: {entry!r}")

    parts = [part for part in segments if part not in ("", ".")]
    if not parts:
        raise ArchiveError(f"アーカイブ内のパスが不正です: {entry!r}")

    target = base.joinpath(*parts).resolve()
    root = base.resolve()
    if target != root and root not in target.parents:
        raise ArchiveError(f"アーカイブ内のパスが展開先の外を指しています: {entry!r}")
    return target


def _is_absolute_part(part: str) -> bool:
    # "C:" のようなドライブ指定を弾く
    return len(part) == 2 and part[1] == ":" and part[0].isalpha()


def _content_root(staging: Path, flatten_single_root: bool) -> tuple[Path, bool]:
    """展開先直下に置くべきディレクトリを決める。

    中身が単一ディレクトリだけなら、その中身を引き上げて二重の入れ子を避ける。
    """
    if not flatten_single_root:
        return staging, False

    current = staging
    flattened = False

    while True:
        entries = [entry for entry in current.iterdir() if not _is_junk(entry)]
        if len(entries) == 1 and entries[0].is_dir():
            current = entries[0]
            flattened = True
            continue
        return current, flattened


_JUNK_NAMES = {".ds_store", "thumbs.db", "__macosx", "desktop.ini"}


def _is_junk(path: Path) -> bool:
    return path.name.lower() in _JUNK_NAMES


def _move_contents(root: Path, output_dir: Path) -> list[Path]:
    """ステージングの中身を展開先へ移す。既存の同名は上書きする。"""
    moved = []
    for entry in sorted(root.iterdir()):
        if _is_junk(entry):
            continue

        target = output_dir / entry.name
        if target.exists():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

        shutil.move(str(entry), str(target))
        moved.append(target)

    return moved


# ----------------------------------------------------------------------
# バージョン番号の除去
# ----------------------------------------------------------------------

# "_ver1.05" "-v2" " Ver.1.2.3" "(ver1.0)" のような明示的なバージョン表記
_VERSION_MARKED = re.compile(
    r"""
    [\s_\-]*                    # 直前の区切り
    [(\[（【]?                   # 括弧開き (任意)
    (?:ver|version|v|rev)       # バージョンマーカー
    [\s._\-]?                   # 区切り
    \d+(?:[._]\d+)*             # 数字列
    [a-z]?                      # 1.0a のような枝番
    [)\]）】]?                   # 括弧閉じ (任意)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# マーカー無しで末尾に付く "_1.05" 形式。ドットを含むものだけを対象にして誤爆を抑える。
_VERSION_BARE = re.compile(r"[\s_\-]+\d+\.\d+(?:\.\d+)*[a-z]?$", re.IGNORECASE)


def strip_version(name: str, is_dir: bool = False) -> str:
    """ファイル名・フォルダ名からバージョン番号を取り除く。"""
    stem, extension = (name, "") if is_dir else _split_extension(name)

    cleaned = _VERSION_MARKED.sub("", stem)
    cleaned = _VERSION_BARE.sub("", cleaned)
    cleaned = re.sub(r"[\s_\-]+$", "", cleaned).strip()
    # 括弧だけが残った場合を掃除する
    cleaned = re.sub(r"[(\[（【]\s*[)\]）】]", "", cleaned).strip()

    # 全部消えてしまうなら元の名前を尊重する
    return (cleaned + extension) if cleaned else name


def _split_extension(name: str) -> tuple[str, str]:
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    return stem, "." + extension


#: バージョン番号を取り除く対象の拡張子。実行ファイルとフォルダのみを触る。
VERSION_STRIP_SUFFIXES = {".exe", ".bat", ".sh", ".x86", ".x86_64", ".swf", ".jar"}


def strip_version_names(root: Path) -> list[tuple[Path, Path]]:
    """展開先のフォルダ名と実行ファイル名からバージョン番号を除去する。

    深い階層から処理することで、親を改名した後に子のパスが変わる問題を避ける。
    """
    renamed: list[tuple[Path, Path]] = []
    targets: list[Path] = []

    for current, directories, filenames in os.walk(root, topdown=False):
        base = Path(current)
        targets.extend(base / name for name in filenames)
        targets.extend(base / name for name in directories)

    for path in targets:
        is_dir = path.is_dir()
        if not is_dir and path.suffix.lower() not in VERSION_STRIP_SUFFIXES:
            continue

        new_name = strip_version(path.name, is_dir=is_dir)
        if new_name == path.name:
            continue

        target = path.with_name(new_name)
        if target.exists():
            # 同名が既にあるなら改名しない (中身を壊さないことを優先)
            continue

        try:
            path.rename(target)
        except OSError:
            continue
        renamed.append((path, target))

    return renamed


# ----------------------------------------------------------------------
# 実行ファイルの検出 (Steam 登録用)
# ----------------------------------------------------------------------

#: ランチャーやアンインストーラなど、ゲーム本体でない可能性が高い名前
_NON_GAME_EXE = re.compile(
    # "unins" は Inno Setup が付ける unins000.exe に当てるため。
    # "uninst" だけだと外れる。
    r"(unins|setup|install|config|conf|readme|manual|update|patch|redist|"
    r"vcredist|dxsetup|crash|report|tool)",
    re.IGNORECASE,
)


@dataclass
class Executable:
    """展開先で見つかった実行ファイル 1 件。"""

    path: Path
    #: 展開先からの相対パス (表示用)
    relative: str
    size: int
    #: ランチャやアンインストーラらしい名前か
    suspicious: bool

    @property
    def label(self) -> str:
        note = "（ゲーム本体でない可能性）" if self.suspicious else ""
        return f"{self.relative}{note}"


def executable_candidates(root: Path) -> list[Executable]:
    """展開先の実行ファイルを、本体らしい順に詳細つきで返す。

    複数の exe を含む作品は珍しくないので、呼び出し側で選ばせられるように
    候補をすべて返す。並び順は :func:`find_executables` と同じ基準。
    """
    candidates = []

    for path in find_executables(root):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name

        # 名前だけでなく、どこに入っているかも見る。install 系のフォルダに
        # 埋まっているものは本体のこともあるが、付属物のことも
        # 多いので、選ぶ前に分かるようにしておく。
        suspicious = bool(_NON_GAME_EXE.search(path.name)) or any(
            _NON_GAME_EXE.search(part) for part in Path(relative).parts[:-1]
        )

        candidates.append(
            Executable(
                path=path,
                relative=relative,
                size=size,
                suspicious=suspicious,
            )
        )

    return candidates


def find_executables(root: Path) -> list[Path]:
    """展開先からゲーム本体らしい実行ファイルを、それらしい順に返す。"""
    candidates: list[tuple[tuple[int, int, int], Path]] = []

    for current, _directories, filenames in os.walk(root):
        base = Path(current)
        try:
            parts = base.relative_to(root).parts
        except ValueError:
            parts = ()
        depth = len(parts) if parts or base == root else 99

        # 途中のフォルダ名に除外語があるか。
        #
        # 以前はこういう階層を丸ごと掘らずに捨てていたが、ゲーム本体が
        # そこに入っていることがある (実物に
        # InstallSet/InstallData/<本体>.exe という形があり、除外語 "install" に
        # 引っかかって候補が 1 つも出なかった)。捨てずに順位を下げるだけにする。
        buried = 1 if any(_NON_GAME_EXE.search(part) for part in parts) else 0

        for name in filenames:
            if not name.lower().endswith(".exe"):
                continue
            path = base / name
            try:
                size = path.stat().st_size
            except OSError:
                size = 0

            # 名前が紛らわしくない / 除外語のフォルダに埋まっていない /
            # 浅い / 大きい ものを優先する
            penalty = 1 if _NON_GAME_EXE.search(name) else 0
            candidates.append(((penalty, buried, depth, -size), path))

    candidates.sort(key=lambda item: item[0])
    return [path for _key, path in candidates]
