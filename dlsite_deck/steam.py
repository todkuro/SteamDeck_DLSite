"""展開したゲームを Steam の非 Steam ゲームとして登録する。

``userdata/<id>/config/shortcuts.vdf`` はバイナリ VDF。標準ライブラリだけで
読み書きできる形式なので自前で実装する。

重要: Steam は起動中 shortcuts.vdf をメモリ上に保持しており、終了時に書き戻す。
書き換えは **Steam を終了してから** 行うこと。この確認は呼び出し側で行う。
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# バイナリ VDF のトークン
_MAP = 0x00
_STRING = 0x01
_INT32 = 0x02
_END = 0x08


class SteamError(RuntimeError):
    """Steam 連携に関する失敗。"""


# ----------------------------------------------------------------------
# バイナリ VDF
# ----------------------------------------------------------------------


def parse_binary_vdf(data: bytes) -> dict[str, Any]:
    """バイナリ VDF を辞書に読み解く。"""
    result, offset = _parse_map(data, 0)
    return result


def _parse_map(data: bytes, offset: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}

    while offset < len(data):
        token = data[offset]
        offset += 1

        if token == _END:
            return result, offset

        key, offset = _read_cstring(data, offset)

        if token == _MAP:
            value, offset = _parse_map(data, offset)
        elif token == _STRING:
            value, offset = _read_cstring(data, offset)
        elif token == _INT32:
            (value,) = struct.unpack_from("<i", data, offset)
            offset += 4
        else:
            raise SteamError(f"shortcuts.vdf に未知のトークンがあります: 0x{token:02x}")

        result[key] = value

    return result, offset


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        raise SteamError("shortcuts.vdf が途中で終わっています。")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def serialize_binary_vdf(value: dict[str, Any]) -> bytes:
    """辞書をバイナリ VDF に書き戻す。"""
    return _serialize_map_body(value) + bytes([_END])


def _serialize_map_body(mapping: dict[str, Any]) -> bytes:
    chunks = bytearray()

    for key, item in mapping.items():
        encoded_key = key.encode("utf-8") + b"\x00"

        if isinstance(item, dict):
            chunks.append(_MAP)
            chunks += encoded_key
            chunks += _serialize_map_body(item)
            chunks.append(_END)
        elif isinstance(item, bool):
            chunks.append(_INT32)
            chunks += encoded_key
            chunks += struct.pack("<i", 1 if item else 0)
        elif isinstance(item, int):
            chunks.append(_INT32)
            chunks += encoded_key
            chunks += struct.pack("<i", _to_signed32(item))
        else:
            chunks.append(_STRING)
            chunks += encoded_key
            chunks += str(item).encode("utf-8") + b"\x00"

    return bytes(chunks)


def _to_signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


# ----------------------------------------------------------------------
# ショートカット
# ----------------------------------------------------------------------


@dataclass
class Shortcut:
    """非 Steam ゲーム 1 件。"""

    app_name: str
    exe: str
    start_dir: str
    launch_options: str = ""
    icon: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def app_id(self) -> int:
        return generate_app_id(self.exe, self.app_name)

    def to_vdf(self) -> dict[str, Any]:
        # キーの綴りは実際に Steam が書き出すものに合わせてある
        # (appname / exe / openvr は小文字。VDF のキーは大小を区別しないが、
        #  差分を出さないためにそろえておく)
        return {
            "appid": _to_signed32(self.app_id),
            "appname": self.app_name,
            "exe": self.exe,
            "StartDir": self.start_dir,
            "icon": self.icon,
            "ShortcutPath": "",
            "LaunchOptions": self.launch_options,
            "IsHidden": 0,
            "AllowDesktopConfig": 1,
            "AllowOverlay": 1,
            "openvr": 0,
            "Devkit": 0,
            "DevkitGameID": "",
            "DevkitOverrideAppID": 0,
            "LastPlayTime": 0,
            "FlatpakAppID": "",
            "tags": {str(index): tag for index, tag in enumerate(self.tags)},
        }


def generate_app_id(exe: str, app_name: str) -> int:
    """Steam が非 Steam ゲームに割り当てるのと同じ AppID を求める。

    Steam は ``Exe`` と ``AppName`` の連結の CRC32 に最上位ビットを立てた値を使う。
    """
    checksum = zlib.crc32((exe + app_name).encode("utf-8")) & 0xFFFFFFFF
    return checksum | 0x80000000


def quote_path(path: Path | str) -> str:
    """Steam が期待する引用符付きのパス表記にする。"""
    text = str(path)
    return text if text.startswith('"') else f'"{text}"'


def find_user_config_dirs(userdata: Path) -> list[Path]:
    """userdata 配下の各ユーザーの config ディレクトリを返す。"""
    if not userdata.is_dir():
        raise SteamError(f"Steam の userdata が見つかりません: {userdata}")

    dirs = []
    for entry in sorted(userdata.iterdir()):
        # "0" は匿名ユーザーなので除く
        if not entry.is_dir() or not entry.name.isdigit() or entry.name == "0":
            continue
        config = entry / "config"
        if config.is_dir():
            dirs.append(config)

    if not dirs:
        raise SteamError(f"Steam のユーザーが見つかりません: {userdata}")
    return dirs


def load_shortcuts(path: Path) -> dict[str, Any]:
    """shortcuts.vdf を読む。無ければ空の構造を返す。"""
    if not path.is_file():
        return {"shortcuts": {}}

    try:
        data = path.read_bytes()
    except OSError as error:
        raise SteamError(f"shortcuts.vdf を読めませんでした: {path} ({error})") from error

    if not data:
        return {"shortcuts": {}}

    parsed = parse_binary_vdf(data)
    if "shortcuts" not in parsed:
        parsed["shortcuts"] = {}
    return parsed


def save_shortcuts(path: Path, document: dict[str, Any], backup: bool = True) -> Path | None:
    """shortcuts.vdf を書き戻す。既存があれば控えを残す。"""
    path.parent.mkdir(parents=True, exist_ok=True)

    backup_path = None
    if backup and path.is_file():
        backup_path = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup_path)

    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_bytes(serialize_binary_vdf(document))
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise SteamError(f"shortcuts.vdf を書けませんでした: {path} ({error})") from error

    return backup_path


def _find_key(entry: dict[str, Any], name: str) -> str | None:
    """VDF のキーは大小を区別しない。実際の綴りを探して返す。"""
    lowered = name.lower()
    for key in entry:
        if key.lower() == lowered:
            return key
    return None


def get_field(entry: dict[str, Any], name: str, default: Any = None) -> Any:
    """ショートカットの項目を大小を無視して取り出す。"""
    key = _find_key(entry, name)
    return entry[key] if key is not None else default


def _set_field(entry: dict[str, Any], name: str, value: Any) -> None:
    """既存キーの綴りを保ったまま値を差し替える。"""
    entry[_find_key(entry, name) or name] = value



#: Windows のドライブレター付きパス。`C:\...` と `"C:/...` の両方に当たる。
_WINDOWS_PATH = re.compile(r'^"?[A-Za-z]:[\\/]')


def shortcut_platform(exe: str) -> str:
    """ショートカットの exe パスから、どの環境のものかを見分ける。

    Steam はデバイス間で非 Steam ゲームの情報を見せることがある。他環境の
    エントリをこちらのものと取り違えると、動かないものを「登録済み」と扱ったり、
    同じ名前という理由だけで**別環境の登録を書き換えたり消したり**しかねない。

    戻り値は ``"windows"`` / ``"posix"`` / ``"unknown"``。
    """
    text = (exe or "").strip()
    if not text:
        return "unknown"
    if _WINDOWS_PATH.match(text):
        return "windows"
    if text.lstrip('"').startswith("/"):
        return "posix"
    return "unknown"


def is_foreign_shortcut(entry: dict[str, Any]) -> bool:
    """このエントリが、今動いている環境とは別のプラットフォームのものか。

    判別できないものは「別環境のもの」とはみなさない。確証が持てるときだけ
    弾く方が、誤って自分の登録を無視してしまう事故を避けられる。
    """
    kind = shortcut_platform(str(get_field(entry, "Exe") or ""))
    if kind == "unknown":
        return False
    here = "windows" if sys.platform == "win32" else "posix"
    return kind != here

def upsert_shortcut(
    document: dict[str, Any],
    shortcut: Shortcut,
    set_launch_options: bool = False,
) -> tuple[int, bool]:
    """ショートカットを追加、または同じ AppName のものを差し替える。

    既存を差し替える場合は識別と起動に関わる項目だけを書き換え、``icon`` や
    ``sortas`` など利用者が Steam 上で設定した内容には触れない。

    ``LaunchOptions`` は ``set_launch_options`` を指示したときだけ書き換える。
    日本語のゲームはロケールを渡さないと文字化けするため、ツールから設定
    できる必要がある。一方で黙って消すと Steam 上での設定が失われるので、
    明示されたときに限る。

    戻り値は ``(AppID, 新規追加かどうか)``。
    """
    shortcuts = document.setdefault("shortcuts", {})

    for existing in shortcuts.values():
        if not isinstance(existing, dict):
            continue
        if get_field(existing, "AppName") != shortcut.app_name:
            continue

        _set_field(existing, "appid", _to_signed32(shortcut.app_id))
        _set_field(existing, "exe", shortcut.exe)
        _set_field(existing, "StartDir", shortcut.start_dir)
        if set_launch_options:
            _set_field(existing, "LaunchOptions", shortcut.launch_options)
        return shortcut.app_id, False

    next_index = 0
    while str(next_index) in shortcuts:
        next_index += 1
    shortcuts[str(next_index)] = shortcut.to_vdf()
    return shortcut.app_id, True


def remove_shortcut(document: dict[str, Any], app_name: str) -> bool:
    """AppName でショートカットを消し、添字を振り直す。"""
    shortcuts = document.get("shortcuts", {})
    remaining = [
        value
        for value in shortcuts.values()
        if not (isinstance(value, dict) and get_field(value, "AppName") == app_name)
    ]

    if len(remaining) == len(shortcuts):
        return False

    document["shortcuts"] = {str(index): value for index, value in enumerate(remaining)}
    return True


def is_steam_running() -> bool:
    """Steam が起動中か大まかに判定する。

    起動中に shortcuts.vdf を書き換えても Steam 終了時に上書きされてしまうため、
    登録前にこれで確認する。
    """
    if sys.platform == "win32":
        return _is_steam_running_windows()

    proc = Path("/proc")
    if not proc.is_dir():
        return False

    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if command in ("steam", "steamwebhelper"):
            return True
    return False


def steam_executable(
    userdata: Path | None = None, configured: str = ""
) -> Path | None:
    """Steam 本体の実行ファイルを探す。

    設定で指定された場所を最優先する。既定は SteamDeck の ``/usr/bin/steam``
    だが、そこに無い環境 (Windows など) のために PATH と Steam のディレクトリも見る。
    設定が的外れでも動くようにしておきたいので、無ければ黙って次を試す。
    """
    if configured:
        candidate = Path(configured)
        if candidate.is_file():
            return candidate

    found = shutil.which("steam")
    if found:
        return Path(found)

    if userdata is not None:
        name = "steam.exe" if sys.platform == "win32" else "steam"
        candidate = userdata.parent / name
        if candidate.is_file():
            return candidate
    return None


def shutdown_steam(
    userdata: Path | None = None,
    timeout: float = 90.0,
    configured: str = "",
) -> bool:
    """Steam 自身に終了してもらい、終了するまで待つ。

    ``-shutdown`` は Steam 自身に後片付けをさせてから終わらせる。``pkill`` で
    強制終了すると ``shortcuts.vdf`` などを書き戻す前に止まることがあるので使わない。

    終了したら ``True``。時間内に終了しなければ ``False``。
    """
    executable = steam_executable(userdata, configured)
    if executable is None:
        raise SteamError(
            "Steam の実行ファイルが見つかりませんでした。"
            " 設定の steam_executable で場所を指定するか、手で終了してください。"
        )

    try:
        subprocess.run(
            [str(executable), "-shutdown"],
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        # 指示そのものは届いている見込みなので、終了するかどうかで判断する
        pass
    except OSError as error:
        raise SteamError(f"Steam を終了できませんでした: {error}") from error

    # 指示はすぐ戻るが、実際に終わるまでには間がある
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_steam_running():
            return True
        time.sleep(0.5)
    return False


#: セッションの種別
SESSION_GAMING = "gaming"
SESSION_DESKTOP = "desktop"
SESSION_UNKNOWN = "unknown"


def _running_commands() -> set[str]:
    """今動いているプロセス名を集める。"""
    proc = Path("/proc")
    if not proc.is_dir():
        return set()

    found = set()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            found.add(
                (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
            )
        except OSError:
            continue
    return found


#: デスクトップモードを受け持つもの (SteamOS は Plasma)
_DESKTOP_COMMANDS = frozenset(
    {"plasmashell", "kwin_wayland", "kwin_x11", "startplasma-way", "startplasma-wayland"}
)


def session_from_commands(commands: set[str]) -> str:
    """動いているプロセス名からセッション種別を決める。

    ゲームモードは ``gamescope`` が、デスクトップモードは Plasma が受け持つ。
    実機のデスクトップモードでは gamescope が動いていないことを確認済み。
    """
    if not commands:
        return SESSION_UNKNOWN
    if any(name.startswith("gamescope") for name in commands):
        return SESSION_GAMING
    if commands & _DESKTOP_COMMANDS:
        return SESSION_DESKTOP
    return SESSION_UNKNOWN


def session_kind() -> str:
    """SteamOS のどちらのセッションで動いているか。

    ゲームモードでは **Steam を終了できない**。Steam 自身がセッションであり、
    このツールも Steam から起動されているため、終了すればツールも一緒に終了する。
    つまり ``shortcuts.vdf`` / ``config.vdf`` を書き換える操作は、ゲームモードでは
    どうやっても行えない。「Steam を終了してください」という案内はそこでは
    実行不可能な指示になるので、区別して案内を変えるために使う。
    """
    if sys.platform == "win32":
        # Windows にはゲームモードが無いので、常に終了できる。
        return SESSION_DESKTOP
    return session_from_commands(_running_commands())


def _is_steam_running_windows() -> bool:
    """Windows での判定。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        # 判定できないときは「起動中かもしれない」と見なして安全側に倒す
        return True

    return "steam.exe" in result.stdout.lower()


def build_shortcut(
    app_name: str,
    executable: Path,
    launch_options: str = "",
    tags: list[str] | None = None,
) -> Shortcut:
    """展開済みゲームからショートカットを組み立てる。"""
    executable = executable.resolve()
    return Shortcut(
        app_name=app_name,
        exe=quote_path(executable),
        start_dir=quote_path(executable.parent),
        launch_options=launch_options,
        tags=tags or ["DLsite"],
    )


# ----------------------------------------------------------------------
# ライブラリの画像 (grid)
# ----------------------------------------------------------------------

# Steam は非 Steam ゲームの画像を userdata/<id>/config/grid/ に置く。
# ファイル名は shortcuts.vdf の appid を**符号なし**で表した値。
#   <appid>.<ext>        横長カプセル (460x215)
#   <appid>p.<ext>       縦長カバー (600x900)  ← ライブラリの表紙
#   <appid>_hero.<ext>   背景 (1920x620)
#   <appid>_logo.png     ロゴ (透過 PNG)
GRID_SLOTS = {
    "capsule": "",
    "cover": "p",
    "hero": "_hero",
    "logo": "_logo",
}

#: ロゴは透過を保つため PNG のみ
LOGO_SLOT = "logo"

#: 縦長カバー。タイトル入りの画像を作る場合、ここだけ差し替える
COVER_SLOT = "cover"

#: Steam が読める画像の拡張子
GRID_EXTENSIONS = (".png", ".jpg", ".jpeg")


def grid_dir(config_dir: Path) -> Path:
    return config_dir / "grid"


def _grid_extension(data: bytes) -> str:
    """画像の中身から拡張子を決める。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    raise SteamError("PNG か JPEG 以外の画像は登録できません。")


def _grid_paths(directory: Path, app_id: int, suffix: str) -> list[Path]:
    """あるスロットについて、拡張子違いを含むすべてのパスを返す。"""
    stem = f"{app_id & 0xFFFFFFFF}{suffix}"
    return [directory / (stem + extension) for extension in GRID_EXTENSIONS]


def install_grid_images(
    config_dir: Path,
    app_id: int,
    image: bytes,
    slots: Iterable[str] = ("capsule", "cover", "hero"),
) -> list[Path]:
    """ライブラリの表紙・背景に使う画像を置く。

    同じスロットに拡張子違いが残っていると Steam がどちらを使うか読めないので、
    書き込む前に古いものを消す。
    """
    extension = _grid_extension(image)
    directory = grid_dir(config_dir)
    directory.mkdir(parents=True, exist_ok=True)

    written = []
    for slot in slots:
        if slot not in GRID_SLOTS:
            raise SteamError(f"知らない画像の種類です: {slot}")

        for path in _grid_paths(directory, app_id, GRID_SLOTS[slot]):
            path.unlink(missing_ok=True)

        target = directory / f"{app_id & 0xFFFFFFFF}{GRID_SLOTS[slot]}{extension}"
        target.write_bytes(image)
        written.append(target)

    return written


def apply_artwork(
    config_dir: Path,
    app_id: int,
    image: bytes | None = None,
    cover_image: bytes | None = None,
    logo: bytes | None = None,
    slots: Iterable[str] | None = None,
) -> list[Path]:
    """1 人ぶんの ``config/`` に画像一式を置く。置いたパスを返す。

    ``cover_image`` があれば**縦長カバーだけ**そちらを使う。タイトルを載せた
    カバーは 2:3 で作ってあり、横長カプセルや背景に流用すると比率が合わない。

    登録・作り直し・``--images-only`` の 3 経路が同じ判断をする必要があるので、
    ここに一本化してある。
    """
    requested = [
        slot
        for slot in (GRID_SLOTS if slots is None else slots)
        if slot != LOGO_SLOT
    ]
    plain = [slot for slot in requested if not (cover_image and slot == COVER_SLOT)]

    written: list[Path] = []
    if image and plain:
        written += install_grid_images(config_dir, app_id, image, slots=plain)

    if cover_image and COVER_SLOT in requested:
        written += install_grid_images(
            config_dir, app_id, cover_image, slots=[COVER_SLOT]
        )

    if logo:
        written += install_grid_images(config_dir, app_id, logo, slots=[LOGO_SLOT])

    return written


def remove_grid_images(
    config_dir: Path, app_id: int, slots: Iterable[str] | None = None
) -> list[Path]:
    """置いた画像を片付ける。

    ``slots`` を省くと全部消す (登録解除や改名のとき)。指定すればその種類だけ消す
    (設定を切り替えて、使わなくなった画像を削除するとき)。
    """
    directory = grid_dir(config_dir)
    if not directory.is_dir():
        return []

    wanted = list(GRID_SLOTS.values()) if slots is None else [
        GRID_SLOTS[slot] for slot in slots if slot in GRID_SLOTS
    ]

    removed = []
    for suffix in wanted:
        for path in _grid_paths(directory, app_id, suffix):
            if path.is_file():
                path.unlink()
                removed.append(path)
    return removed


# ----------------------------------------------------------------------
# 互換ツール (Proton) の割り当て
# ----------------------------------------------------------------------

# Steam は非 Steam ゲームに割り当てた Proton を
# <steam>/config/config.vdf の CompatToolMapping に持つ。AppID をキーにした
# テキスト形式の VDF で、他の設定と同居している。
#
# このファイルには利用者の設定が大量に入っているため、全体を読み直して書き戻すと
# 些細な差異で壊す危険がある。該当エントリの範囲だけを差し替える。

DEFAULT_COMPAT_PRIORITY = "250"


def compat_config_path(userdata: Path) -> Path:
    """userdata の位置から config.vdf を求める。"""
    return userdata.parent / "config" / "config.vdf"


def _official_proton_key(directory_name: str) -> str | None:
    """``Proton 8.0`` のようなディレクトリ名から、設定に書く名前を求める。

    Valve は ``Proton 8.0`` を ``proton_8``、``Proton 5.13`` を ``proton_513``
    のように綴る。Experimental と Hotfix だけ別扱い。
    """
    name = directory_name.strip()
    lowered = name.lower()

    if not lowered.startswith("proton"):
        return None
    if "experimental" in lowered:
        return "proton_experimental"
    if "hotfix" in lowered:
        return "proton_hotfix"

    match = re.search(r"(\d+)\.(\d+)", name)
    if not match:
        return None

    major, minor = match.group(1), match.group(2)
    return f"proton_{major}" if minor == "0" else f"proton_{major}{minor}"


def list_compat_tools(userdata: Path) -> list[dict[str, str]]:
    """使える互換ツールを ``{"value", "label"}`` の一覧で返す。

    公式 Proton は ``steamapps/common`` に、追加したものは
    ``compatibilitytools.d`` に入っている。設定に書く名前は両者で流儀が違う。
    """
    root = userdata.parent
    tools: dict[str, str] = {}

    # 追加した互換ツール (GE-Proton など)。ディレクトリ名がそのまま設定名になる。
    for base in (root / "compatibilitytools.d", root.parent / "compatibilitytools.d"):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and (entry / "compatibilitytool.vdf").is_file():
                tools.setdefault(entry.name, entry.name)

    # 公式 Proton
    for base in (root / "steamapps" / "common",):
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir():
                continue
            key = _official_proton_key(entry.name)
            if key:
                tools.setdefault(key, entry.name)

    ordered = sorted(
        tools.items(),
        # Experimental を先頭に、次に公式、最後に追加ツール
        key=lambda item: (
            0 if item[0] == "proton_experimental" else 1 if item[0].startswith("proton_") else 2,
            item[1],
        ),
    )
    return [{"value": value, "label": label} for value, label in ordered]


def _matching_brace(text: str, start: int) -> int:
    """``start`` の ``{`` に対応する ``}`` の次の位置を返す。"""
    depth = 0
    index = start
    in_string = False

    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1

    raise SteamError("config.vdf の括弧が閉じていません。")


def get_compat_tool(userdata: Path, app_id: int) -> str | None:
    """割り当てられている互換ツール名。未設定なら None。"""
    path = compat_config_path(userdata)
    if not path.is_file():
        return None

    text = path.read_text(encoding="utf-8", errors="replace")
    entry = _find_compat_entry(text, app_id)
    if entry is None:
        return None

    match = re.search(r'"name"\s*"([^"]*)"', entry[2])
    return match.group(1) if match else None


def _find_compat_entry(text: str, app_id: int) -> tuple[int, int, str] | None:
    """CompatToolMapping 内の該当エントリを (開始, 終了, 中身) で返す。"""
    anchor = text.find('"CompatToolMapping"')
    if anchor < 0:
        return None

    start = text.find("{", anchor)
    if start < 0:
        return None
    end = _matching_brace(text, start)
    block = text[start:end]

    key = f'"{app_id & 0xFFFFFFFF}"'
    position = block.find(key)
    if position < 0:
        return None

    brace = block.find("{", position)
    if brace < 0:
        return None
    close = _matching_brace(block, brace)
    return start + position, start + close, block[position:close]


def set_compat_tool(
    userdata: Path,
    app_id: int,
    tool: str,
    overwrite: bool = True,
) -> bool:
    """互換ツール (Proton) を割り当てる。変更したら True。

    ``overwrite`` が False なら、既に割り当てがある場合は触らない。
    Steam が起動中だと終了時に上書きされるので、呼び出し側で確認すること。
    """
    path = compat_config_path(userdata)
    if not path.is_file():
        raise SteamError(f"config.vdf が見つかりません: {path}")

    text = path.read_text(encoding="utf-8", errors="replace")
    found = _find_compat_entry(text, app_id)

    if found is not None:
        current = re.search(r'"name"\s*"([^"]*)"', found[2])
        if current and current.group(1) == tool:
            return False
        if current and current.group(1) and not overwrite:
            return False

        replaced = re.sub(
            r'("name"\s*")[^"]*(")',
            lambda match: match.group(1) + tool + match.group(2),
            found[2],
            count=1,
        )
        updated = text[: found[0]] + replaced + text[found[1] :]
    else:
        anchor = text.find('"CompatToolMapping"')
        if anchor < 0:
            # 入れたばかりの Steam にはこの節がまだ無い。作って入れる。
            #
            # 実機 (既に 143 件あった) では気付けず、素の Ubuntu に入れた
            # 新品の Steam で初めて出た。ここで諦めると、登録しても
            # Proton が割り当てられないまま黙って進んでしまう。
            updated = _insert_compat_section(text, app_id, tool)
            _write_with_backup(path, updated)
            return True

        start = text.find("{", anchor)
        # 既存のエントリに合わせた字下げにする
        indent = _detect_indent(text, start)
        entry = (
            f'\n{indent}"{app_id & 0xFFFFFFFF}"'
            f"\n{indent}{{"
            f'\n{indent}\t"name"\t\t"{tool}"'
            f'\n{indent}\t"config"\t\t""'
            f'\n{indent}\t"priority"\t\t"{DEFAULT_COMPAT_PRIORITY}"'
            f"\n{indent}}}"
        )
        updated = text[: start + 1] + entry + text[start + 1 :]

    _write_with_backup(path, updated)
    return True


#: config.vdf で CompatToolMapping が置かれる入れ子の道順
_COMPAT_SECTION_PATH = ("InstallConfigStore", "Software", "Valve", "Steam")


def _find_section(text: str, names: Iterable[str]) -> int | None:
    """入れ子をたどって、目的のブロックの ``{`` の位置を返す。

    同じ名前が他の場所にも出るので、上から順に範囲を狭めながら探す。
    """
    start, end = 0, len(text)
    brace = -1

    for name in names:
        pattern = re.compile(r'"' + re.escape(name) + r'"', re.IGNORECASE)
        match = pattern.search(text, start, end)
        if match is None:
            return None

        brace = text.find("{", match.end())
        if brace < 0 or brace >= end:
            return None

        closing = _matching_brace(text, brace)
        start, end = brace + 1, closing - 1

    return brace if brace >= 0 else None


def _insert_compat_section(text: str, app_id: int, tool: str) -> str:
    """CompatToolMapping ごと作って、最初の 1 件を入れる。"""
    brace = _find_section(text, _COMPAT_SECTION_PATH)
    if brace is None:
        raise SteamError(
            "config.vdf に CompatToolMapping がなく、作る場所も分かりませんでした。"
        )

    indent = _detect_indent(text, brace)
    inner = indent + "\t"
    section = (
        f'\n{indent}"CompatToolMapping"'
        f"\n{indent}{{"
        f'\n{inner}"{app_id & 0xFFFFFFFF}"'
        f"\n{inner}{{"
        f'\n{inner}\t"name"\t\t"{tool}"'
        f'\n{inner}\t"config"\t\t""'
        f'\n{inner}\t"priority"\t\t"{DEFAULT_COMPAT_PRIORITY}"'
        f"\n{inner}}}"
        f"\n{indent}}}"
    )
    return text[: brace + 1] + section + text[brace + 1 :]


def _detect_indent(text: str, brace: int) -> str:
    """``{`` の次の行の字下げを読み取る。"""
    line_end = text.find("\n", brace)
    if line_end < 0:
        return "\t\t\t\t\t"
    following = text[line_end + 1 : line_end + 40]
    indent = following[: len(following) - len(following.lstrip("\t "))]
    return indent or "\t\t\t\t\t"


def _write_with_backup(path: Path, text: str) -> Path:
    backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)

    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise SteamError(f"config.vdf を書けませんでした: {path} ({error})") from error
    return backup


@dataclass
class RegistrationResult:
    """登録・解除の結果。"""

    app_id: int | None
    #: 書き換えた shortcuts.vdf
    updated: list[Path] = field(default_factory=list)
    backups: list[Path] = field(default_factory=list)
    created: bool = False
    #: 置いた (または消した) 画像
    images: list[Path] = field(default_factory=list)
    #: 割り当てた互換ツール (Proton)
    compat_tool: str = ""


def register(
    userdata: Path,
    app_name: str,
    executable: Path,
    launch_options: str | None = None,
    image: bytes | None = None,
    image_slots: Iterable[str] | None = None,
    logo: bytes | None = None,
    compat_tool: str = "",
    cover_image: bytes | None = None,
    keep_launch_options: bool = False,
) -> RegistrationResult:
    """全ユーザーの shortcuts.vdf に登録する。

    ``app_name`` は呼び出し側が決める。DLsite のタイトルをそのまま使ってもよいし、
    利用者が付け替えた名前でもよい。``image`` を渡すとライブラリの表紙と背景にも
    使う。

    ``cover_image`` を渡した場合、縦長カバーだけはそちらを使う。タイトルを載せた
    カバーは 2:3 に作ってあり、横長カプセルや背景に流用すると比率が合わないため。

    ``keep_launch_options`` を立てると、``launch_options`` は新しく追加する登録に
    だけ使い、既にある登録の起動オプションには触れない。Web UI からの登録は
    こちらを使う (起動オプションを画面から変えさせないため)。
    """
    shortcut = build_shortcut(app_name, executable, launch_options=launch_options or "")
    result = RegistrationResult(app_id=shortcut.app_id)
    overwrite = launch_options is not None and not keep_launch_options

    for config_dir in find_user_config_dirs(userdata):
        path = config_dir / "shortcuts.vdf"
        document = load_shortcuts(path)
        # 起動オプションは、指示されたときだけ書き換える (空にする指示も含む)
        _app_id, created = upsert_shortcut(
            document, shortcut, set_launch_options=overwrite
        )
        backup = save_shortcuts(path, document)

        result.created = result.created or created
        result.updated.append(path)
        if backup:
            result.backups.append(backup)

        result.images.extend(
            apply_artwork(
                config_dir,
                shortcut.app_id,
                image=image,
                cover_image=cover_image,
                logo=logo,
                slots=image_slots,
            )
        )

    # DLsite のゲームは Windows 向けなので、最初から Proton を当てておく。
    # 既に割り当てがある場合は利用者の選択を尊重して触らない。
    if compat_tool:
        try:
            if set_compat_tool(userdata, shortcut.app_id, compat_tool, overwrite=False):
                result.compat_tool = compat_tool
        except SteamError:
            # 互換ツールを設定できなくても登録自体は成立している
            pass

    return result


def unregister(userdata: Path, app_name: str, app_id: int | None = None) -> RegistrationResult:
    """全ユーザーの shortcuts.vdf から登録を外す。

    ``app_id`` を渡すと、置いてあった表紙・背景も片付ける。AppID は名前と exe から
    決まるので、改名するときは**古い名前の AppID** を渡すこと。
    """
    result = RegistrationResult(app_id=app_id)

    for config_dir in find_user_config_dirs(userdata):
        path = config_dir / "shortcuts.vdf"
        document = load_shortcuts(path)
        removed = remove_shortcut(document, app_name)

        if removed:
            backup = save_shortcuts(path, document)
            result.updated.append(path)
            if backup:
                result.backups.append(backup)

        if app_id is not None:
            result.images.extend(remove_grid_images(config_dir, app_id))

    return result
