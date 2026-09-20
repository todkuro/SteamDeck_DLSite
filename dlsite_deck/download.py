"""レジューム対応のダウンロード処理。

作品によっては数 GB になるので、途中で切れても ``.part`` から再開できるように
している。SteamDeck の Wi-Fi 環境では実際にこれが効く。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .api import AlreadyCompleteError, DlsiteApiError, DlsiteClient, DownloadFile

CHUNK_SIZE = 1024 * 256


@dataclass
class DownloadResult:
    """1 ファイルのダウンロード結果。"""

    path: Path
    size: int
    resumed: bool
    skipped: bool


# 型エイリアスは実行時に評価されるので、古い Python でも動く書き方にしておく
ProgressCallback = Callable[[str, int, Optional[int]], None]


def download_file(
    client: DlsiteClient,
    file: DownloadFile,
    target_dir: Path,
    fallback_name: str,
    progress: ProgressCallback | None = None,
    resume: bool = True,
) -> DownloadResult:
    """1 ファイルをダウンロードする。``.part`` があれば続きから取得する。"""
    target_dir.mkdir(parents=True, exist_ok=True)

    # ファイル名はレスポンスヘッダで確定するので、まず 0 バイト目を軽く開く
    part_path: Path | None = None
    offset = 0

    probe = client.open_stream(file.url)
    try:
        name = _filename_from_response(probe, file.url) or fallback_name
        total = _content_length(probe)
    finally:
        probe.close()

    final_path = target_dir / name
    part_path = target_dir / (name + ".part")

    if final_path.exists() and total is not None and final_path.stat().st_size == total:
        if progress:
            progress(name, total, total)
        return DownloadResult(path=final_path, size=total, resumed=False, skipped=True)

    if resume and part_path.exists():
        offset = part_path.stat().st_size
        if total is not None and offset >= total:
            # 取り切っていたが rename 前に落ちたケース
            part_path.replace(final_path)
            if progress:
                progress(name, total, total)
            return DownloadResult(path=final_path, size=total, resumed=True, skipped=False)
    else:
        offset = 0

    try:
        response = client.open_stream(file.url, start=offset or None)
    except AlreadyCompleteError:
        if part_path.exists():
            part_path.replace(final_path)
        size = final_path.stat().st_size if final_path.exists() else 0
        if progress:
            progress(name, size, size)
        return DownloadResult(path=final_path, size=size, resumed=True, skipped=False)

    try:
        resumed = response.status == 206 and offset > 0
        if not resumed:
            # サーバーが Range を無視した場合は先頭から取り直す
            offset = 0
            total = _content_length(response)
        elif total is None:
            total = _content_length(response)
            if total is not None:
                total += offset

        mode = "ab" if resumed else "wb"
        written = offset if resumed else 0

        with open(part_path, mode) as handle:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                if progress:
                    progress(name, written, total)
    finally:
        response.close()

    if total is not None and written < total:
        raise DlsiteApiError(
            f"{name} のダウンロードが途中で終わりました ({written}/{total} バイト)。"
            "再実行すると続きから再開します。"
        )

    part_path.replace(final_path)
    return DownloadResult(path=final_path, size=written, resumed=resumed, skipped=False)


def download_plan_files(
    client: DlsiteClient,
    work_id: str,
    files: list[DownloadFile],
    target_dir: Path,
    progress: ProgressCallback | None = None,
    resume: bool = True,
) -> list[Path]:
    """ダウンロードプランのファイルをすべて取得し、順序を保った一覧を返す。"""
    paths = []
    for index, file in enumerate(files, start=1):
        fallback = (
            f"{work_id}.zip"
            if file.kind == "direct"
            else f"{work_id}.part{file.number or index}"
        )
        result = download_file(
            client, file, target_dir, fallback, progress=progress, resume=resume
        )
        paths.append(result.path)
    return paths


def _content_length(response) -> int | None:
    raw = response.headers.get("Content-Length")
    if raw and raw.isdigit():
        return int(raw)
    return None


_FILENAME_STAR = re.compile(r"filename\*\s*=\s*([^;]+)", re.IGNORECASE)
_FILENAME = re.compile(r"""filename\s*=\s*("([^"]*)"|[^;]+)""", re.IGNORECASE)


def _filename_from_response(response, url: str) -> str | None:
    """Content-Disposition か URL からファイル名を決める。"""
    disposition = response.headers.get("Content-Disposition", "")

    match = _FILENAME_STAR.search(disposition)
    if match:
        value = match.group(1).strip().strip('"')
        # RFC 5987: charset'lang'percent-encoded-value
        parts = value.split("'", 2)
        if len(parts) == 3:
            charset, _lang, encoded = parts
            try:
                return _safe_name(
                    urllib.parse.unquote(encoded, encoding=charset or "utf-8", errors="replace")
                )
            except LookupError:
                pass
        return _safe_name(urllib.parse.unquote(value))

    match = _FILENAME.search(disposition)
    if match:
        value = (match.group(2) or match.group(1)).strip()
        if value:
            return _safe_name(urllib.parse.unquote(value))

    path = urllib.parse.urlparse(response.geturl() or url).path
    name = Path(urllib.parse.unquote(path)).name
    return _safe_name(name) if name and "." in name else None


_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_name(name: str) -> str:
    name = _UNSAFE_NAME.sub("_", name).strip().strip(".")
    return name or "download.bin"


def format_size(size: int | None) -> str:
    """バイト数を読みやすい単位にする。"""
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


class ConsoleProgress:
    """端末に 1 行で進捗を出す。パイプ越しのときは静かにする。"""

    def __init__(self, stream=None, interval: float = 0.2) -> None:
        self.stream = stream or sys.stderr
        self.interval = interval
        self._last = 0.0
        self._current = ""
        self.enabled = self.stream.isatty()

    def __call__(self, name: str, written: int, total: int | None) -> None:
        if not self.enabled:
            return

        now = time.monotonic()
        complete = total is not None and written >= total
        if not complete and now - self._last < self.interval and name == self._current:
            return

        self._last = now
        self._current = name

        if total:
            ratio = min(1.0, written / total)
            bar_width = 24
            filled = int(bar_width * ratio)
            bar = "#" * filled + "-" * (bar_width - filled)
            text = (
                f"  {_ellipsize(name, 34)} [{bar}] {ratio * 100:5.1f}% "
                f"{format_size(written)}/{format_size(total)}"
            )
        else:
            text = f"  {_ellipsize(name, 34)} {format_size(written)}"

        width = shutil.get_terminal_size((100, 24)).columns - 1
        self.stream.write("\r" + text[:width].ljust(width))
        if complete:
            self.stream.write("\n")
        self.stream.flush()

    def finish(self) -> None:
        if self.enabled and self._current:
            self.stream.write("\n")
            self.stream.flush()
            self._current = ""


def _ellipsize(text: str, width: int) -> str:
    if len(text) <= width:
        return text.ljust(width)
    return text[: width - 3] + "..."


def free_space(path: Path) -> int | None:
    """展開先の空き容量 (バイト)。取得できなければ None。"""
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        return shutil.disk_usage(target).free
    except OSError:
        return None


def ensure_space(path: Path, needed: int, margin: float = 1.2) -> None:
    """展開ぶんも含めて空き容量が足りるか確認する。"""
    available = free_space(path)
    if available is None:
        return
    required = int(needed * margin)
    if available < required:
        raise DlsiteApiError(
            f"{path} の空き容量が足りません "
            f"(必要 約{format_size(required)} / 空き {format_size(available)})。"
        )


def cleanup_partials(directory: Path) -> list[Path]:
    """途中で放置された .part を列挙する (削除はしない)。"""
    if not directory.is_dir():
        return []
    return sorted(directory.glob("**/*.part"))


def remove_files(paths: list[Path]) -> None:
    """ダウンロード済みアーカイブを片付ける。"""
    for path in paths:
        try:
            if path.is_file():
                os.remove(path)
        except OSError:
            pass


def discard_cache(paths: list[Path], directory: Path | None = None) -> int:
    """展開が終わったアーカイブを消し、空になった置き場も片付ける。

    戻り値は解放したバイト数。
    """
    freed = 0
    for path in paths:
        try:
            if path.is_file():
                freed += path.stat().st_size
        except OSError:
            pass

    remove_files(paths)

    if directory is not None:
        try:
            # 中身が残っている場合 (取り残した .part など) は消さない
            directory.rmdir()
        except OSError:
            pass

    return freed
