"""Steam のライブラリ表紙とロゴを、タイトル入りで作る。

Steam のストア製品はカバーにタイトルロゴが焼き込まれているが、DLsite の作品画像には
それが無い。そのままではライブラリの一覧でどれがどのゲームか分からないので、
作品画像を Steam が想定する縦長 (2:3) に切り出し、タイトルを載せたものを作る。

描画は ``rsvg-convert`` に任せる。SteamOS に最初から入っているコマンドで、
フォントの選択 (Noto Sans CJK JP) と日本語の字形はこれが面倒を見る。
標準ライブラリだけでは TrueType の字形を起こせないため、ここだけは外部に頼る。

**このコマンドが無い環境では静かに諦める** (``None`` を返す)。画像はあくまで飾りで、
登録そのものは成立させたいため。
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import archive, winicon

#: Steam が想定する縦長カバーの寸法
COVER_WIDTH = 600
COVER_HEIGHT = 900

#: 1 行に収める文字数 (全角換算)。フォントサイズはここから決まる。
CHARS_PER_LINE = 10

#: 左右の余白。これを除いた幅に CHARS_PER_LINE 文字が収まる。
SIDE_PADDING = 24.0

#: 文字を載せる白い下地の不透明度
PLATE_OPACITY = 0.55

#: 黒縁の太さ (フォントサイズに対する比)
STROKE_RATIO = 0.18

#: 使うフォント。実体の選択は fontconfig に任せる。
FONT_FAMILY = "Noto Sans CJK JP, Noto Sans, sans-serif"

#: 文字ブロックが高くなりすぎないようにする上限 (カバー高さに対する比)
MAX_TEXT_BLOCK = 0.45

#: rsvg-convert が固まった場合に諦めるまでの秒数
TIMEOUT = 30


#: 取得したストア画像を作品ディレクトリに残す名前。
#:
#: 表紙を作り直すたびに DLsite から取得し直すのは無駄で、ログインが切れていると
#: そもそも取得できない。作品ディレクトリに置いておけば、設定を切り替えて作り直すだけなら
#: 通信が要らなくなる。作品を削除すればディレクトリごと消えるので後始末も要らない。
CACHE_STEM = ".dlsite-store-image"

#: 残す可能性のある拡張子。片付けと探索の両方で使う。
CACHE_EXTENSIONS = (".png", ".jpg")


def cache_path(directory: Path, image: bytes) -> Path | None:
    """その画像を残すならどのパスになるか。"""
    mime = _mime(image)
    if mime is None:
        return None
    return Path(directory) / (CACHE_STEM + (".png" if mime == "image/png" else ".jpg"))


def cached_image(directory: Path | None) -> bytes | None:
    """作品ディレクトリに残してあるストア画像。無ければ ``None``。"""
    if directory is None:
        return None

    for extension in CACHE_EXTENSIONS:
        path = Path(directory) / (CACHE_STEM + extension)
        try:
            if path.is_file():
                data = path.read_bytes()
                # 書きかけや壊れたものは使わない
                if _mime(data) is not None:
                    return data
        except OSError:
            continue
    return None


def store_image(directory: Path | None, image: bytes) -> Path | None:
    """ストア画像を作品ディレクトリに残す。失敗しても呼び出し側を止めない。"""
    if directory is None or not image:
        return None

    target = cache_path(directory, image)
    if target is None:
        return None

    try:
        if not target.parent.is_dir():
            return None
        # 拡張子が変わった場合に古いものを残さない
        for extension in CACHE_EXTENSIONS:
            other = target.parent / (CACHE_STEM + extension)
            if other != target:
                other.unlink(missing_ok=True)
        target.write_bytes(image)
    except OSError:
        return None
    return target


def _rsvg() -> str | None:
    """``rsvg-convert`` の場所。設定した tool_dirs も PATH より先に見る。"""
    for directory in archive.EXTRA_TOOL_DIRS:
        found = shutil.which("rsvg-convert", path=directory)
        if found:
            return found
    return shutil.which("rsvg-convert")


def available() -> bool:
    """描画のコマンドがあるか。"""
    return _rsvg() is not None


@lru_cache(maxsize=1)
def can_draw_japanese() -> bool:
    """日本語の字形を持つフォントがあるか。

    rsvg は fontconfig でフォントを選ぶので、fontconfig に直接尋ねる。

    描いて見比べる手は**使えない**。字形が無い文字は「豆腐」になるが、最近の
    豆腐は枠の中に符号位置の 16 進を並べた絵なので、**文字ごとに違う絵になる**。
    実機で確かめたところ、日本語フォントを外した状態でも「あ」と私用領域の
    文字は別の絵になり、区別が付かなかった。

    ``fc-list`` が無い環境では判断できないので、**描ける前提で通す**。
    描けるものを勝手に止めるより、間違っていたら利用者がオフにするほうがよい。
    """
    if not available():
        return False

    executable = shutil.which("fc-list")
    if executable is None:
        return True

    try:
        result = subprocess.run(
            [executable, ":lang=ja", "family"], capture_output=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return True

    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


@lru_cache(maxsize=1)
def title_supported() -> bool:
    """表紙にタイトルを入れられる環境か。

    描画のコマンドと日本語フォントの両方が要る。**どちらかが欠けていれば
    既定では入れない** (豆腐が並んだ表紙を作ってしまわないように)。
    """
    return available() and can_draw_japanese()


def title_support_problem() -> str:
    """入れられない理由。入れられるなら空文字。

    「使えません」だけでは手の打ちようがないので、何が足りないかまで返す。
    """
    if not available():
        return (
            "描画に使う rsvg-convert が見つかりません。"
            " このままでは元の画像がそのまま使われます。"
            "（SteamOS には標準で入っています。"
            " 他の環境では librsvg2-bin / librsvg などを入れるか、"
            "設定の「展開ツールを探す場所」に置き場所を足してください）"
        )
    if not can_draw_japanese():
        return (
            "日本語のフォントが見つかりません。"
            " このまま有効にすると、タイトルが豆腐（□）で並んだカバーになります。"
            "（Noto Sans CJK JP などを入れてください）"
        )
    return ""


def char_width(char: str) -> float:
    """全角を 1.0、半角を 0.5 として数える。

    曖昧幅 (``A``) は日本語のタイトルに混じる記号なので全角として扱う。
    """
    return 1.0 if unicodedata.east_asian_width(char) in ("W", "F", "A") else 0.5


def text_width(text: str) -> float:
    return sum(char_width(char) for char in text)


def wrap(title: str, limit: float = CHARS_PER_LINE) -> list[str]:
    """全角換算で ``limit`` 文字ずつに折り返す。

    単語の切れ目は見ない。日本語のタイトルが主で、そもそも空白が無いため。
    """
    lines: list[str] = []
    current, used = "", 0.0
    for char in title:
        width = char_width(char)
        if used + width > limit and current:
            lines.append(current)
            current, used = "", 0.0
        current += char
        used += width
    if current:
        lines.append(current)
    return lines or [""]


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _text(line: str, x: float, y: float, size: float) -> str:
    """白抜き・黒縁の 1 行。

    ``paint-order="stroke"`` で縁を先に描く。こうしないと縁が字の内側を削る。
    """
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle"'
        f' font-family="{FONT_FAMILY}" font-size="{size:.2f}" font-weight="700"'
        f' fill="#ffffff" stroke="#000000" stroke-width="{size * STROKE_RATIO:.2f}"'
        f' paint-order="stroke" stroke-linejoin="round">{_escape(line)}</text>'
    )


def _render(svg: str, width: int, height: int) -> bytes | None:
    """SVG を PNG にする。失敗しても例外にしない。"""
    executable = _rsvg()
    if executable is None:
        return None

    try:
        # -o は付けない。rsvg-convert は "-" をファイル名として扱うため、
        # 標準出力に出させるには省略する必要がある。
        result = subprocess.run(
            [executable, "-w", str(width), "-h", str(height)],
            input=svg.encode("utf-8"),
            capture_output=True,
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0 or not result.stdout.startswith(b"\x89PNG"):
        return None
    return result.stdout


def _mime(image: bytes) -> str | None:
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image[:2] == b"\xff\xd8":
        return "image/jpeg"
    return None


def cover_svg(image: bytes, title: str, mime: str) -> str:
    """作品画像を中心から 2:3 に切り出し、下部にタイトルを載せた SVG。

    切り出しは ``preserveAspectRatio="xMidYMid slice"`` に任せる。短い辺に合わせて
    拡大し、はみ出した分を中心基準で捨てる、という意味になる。
    """
    data = base64.b64encode(image).decode("ascii")

    size = (COVER_WIDTH - SIDE_PADDING * 2) / CHARS_PER_LINE
    lines = wrap(title)

    # 行数が多いと表紙が文字で埋まってしまうので、その場合だけ文字を縮める
    line_height = size * 1.2
    if len(lines) * line_height > COVER_HEIGHT * MAX_TEXT_BLOCK:
        size *= COVER_HEIGHT * MAX_TEXT_BLOCK / (len(lines) * line_height)
        line_height = size * 1.2

    margin = size * 0.45
    plate_height = len(lines) * line_height + margin * 2
    plate_y = COVER_HEIGHT - plate_height - margin

    spans = "\n".join(
        _text(line, COVER_WIDTH / 2, plate_y + margin + line_height * (index + 0.8), size)
        for index, line in enumerate(lines)
    )

    return (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink"'
        f' width="{COVER_WIDTH}" height="{COVER_HEIGHT}"'
        f' viewBox="0 0 {COVER_WIDTH} {COVER_HEIGHT}">'
        f'<rect width="{COVER_WIDTH}" height="{COVER_HEIGHT}" fill="#101014"/>'
        f'<image x="0" y="0" width="{COVER_WIDTH}" height="{COVER_HEIGHT}"'
        ' preserveAspectRatio="xMidYMid slice"'
        f' xlink:href="data:{mime};base64,{data}"/>'
        f'<rect x="{margin:.1f}" y="{plate_y:.1f}"'
        f' width="{COVER_WIDTH - margin * 2:.1f}" height="{plate_height:.1f}"'
        f' rx="{size * 0.25:.1f}" fill="#ffffff" fill-opacity="{PLATE_OPACITY}"/>'
        f"{spans}</svg>"
    )


def logo_svg(title: str) -> tuple[str, int, int]:
    """タイトルだけの透過画像。折り返さない 1 行。"""
    size = (COVER_WIDTH - SIDE_PADDING * 2) / CHARS_PER_LINE
    margin = size * 0.45

    width = max(1, int(text_width(title) * size + margin * 2))
    height = max(1, int(size * 1.2 + margin * 2))

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"'
        f' width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect x="0" y="0" width="{width}" height="{height}"'
        f' rx="{size * 0.25:.1f}" fill="#ffffff" fill-opacity="{PLATE_OPACITY}"/>'
        f"{_text(title, width / 2, margin + size * 0.96, size)}</svg>"
    )
    return svg, width, height


def build_cover(image: bytes, title: str) -> bytes | None:
    """タイトル入りの縦長カバーを PNG で返す。作れなければ ``None``。"""
    if not image or not title.strip():
        return None

    mime = _mime(image)
    if mime is None:
        return None

    return _render(cover_svg(image, title, mime), COVER_WIDTH, COVER_HEIGHT)


def build_logo(title: str) -> bytes | None:
    """タイトルだけの透過 PNG を返す。作れなければ ``None``。"""
    if not title.strip():
        return None

    svg, width, height = logo_svg(title)
    return _render(svg, width, height)


def artwork(
    config: Any,
    title: str,
    image: bytes | None,
    executable: Path | str | None = None,
) -> tuple[bytes | None, bytes | None]:
    """設定に従って、縦長カバーとロゴを用意する。

    コマンドラインと Web UI で同じ判断をするための入口。作れなかったものは ``None`` を返し、
    呼び出し側はその分を単に設定しない。
    """
    cover_image = None
    if image and getattr(config, "steam_cover_title", False):
        cover_image = build_cover(image, title)

    source = getattr(config, "steam_logo_source", "none")
    logo = None
    if source == "title":
        logo = build_logo(title)
    elif source == "exe" and executable:
        logo = winicon.extract_largest_icon(Path(executable))

    return cover_image, logo
