"""登録済みゲームの Proton プレフィックス内で exe を動かす。

``patch.exe`` を実行して当てる形のパッチがある。Steam に登録した
非 Steam ゲームにも専用のプレフィックスが作られる (実機では 137 件中 135 件に
存在した。残りは未起動のもの) ので、そこを指定すれば Windows 用のパッチを
そのまま実行できる。

**そのゲームに割り当てられている Proton を使うこと。** 違う版で実行すると
プレフィックスが変換され、これは元に戻せない。実機の割り当ては
proton_experimental / GE-Proton10-10 / Proton-GE Latest / proton_7 などが
混在していたので、決め打ちにはできない。ここで自動的に引き当てる。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import steam


class ProtonError(RuntimeError):
    """Proton の実行に関する失敗。"""


def steam_root(userdata: Path) -> Path:
    """userdata の位置から Steam の本体ディレクトリを求める。"""
    return userdata.parent


def prefix_path(userdata: Path, app_id: int) -> Path:
    """ゲームのプレフィックス。Steam が初回起動時に作る。"""
    unsigned = app_id if app_id > 0 else app_id + 2**32
    return steam_root(userdata) / "steamapps" / "compatdata" / str(unsigned)


def _official_proton_dir(root: Path, key: str) -> Path | None:
    """``proton_experimental`` のような設定名から実体を探す。

    設定側は ``proton_8`` / ``proton_experimental`` / ``proton_hotfix`` のような
    表記で、フォルダ側は ``Proton 8.0`` / ``Proton - Experimental`` /
    ``Proton Hotfix``。数字とそれ以外で付き方が違うので両方見る。
    """
    common = root / "steamapps" / "common"
    if not common.is_dir():
        return None

    suffix = key.replace("proton_", "", 1).strip()
    if not suffix:
        return None

    def usable(directory: Path) -> Path | None:
        return directory if (directory / "proton").is_file() else None

    if suffix in ("experimental", "exp"):
        return usable(common / "Proton - Experimental")

    # 数字なら "Proton 8.0" のように版を合わせる
    if suffix[0].isdigit():
        for directory in sorted(common.glob("Proton *")):
            name = directory.name.replace("Proton ", "", 1).strip()
            if name == suffix or name.startswith(suffix + "."):
                found = usable(directory)
                if found is not None:
                    return found
        return None

    # hotfix のような名前。区切りの違いを均して突き合わせる
    def flatten(text: str) -> str:
        return text.lower().replace("-", "").replace("_", "").replace(" ", "")

    wanted = flatten(suffix)
    for directory in sorted(common.glob("Proton*")):
        name = flatten(directory.name.replace("Proton", "", 1))
        if name == wanted:
            found = usable(directory)
            if found is not None:
                return found
    return None


def resolve_proton(userdata: Path, app_id: int, fallback: str = "") -> Path:
    """このゲームに割り当てられている Proton の ``proton`` を返す。

    割り当てが無ければ ``fallback``、それも無ければ Experimental を使う。
    """
    root = steam_root(userdata)
    key = steam.get_compat_tool(userdata, app_id) or fallback or "proton_experimental"

    # 独自に入れた GE-Proton など
    custom = root / "compatibilitytools.d" / key
    if (custom / "proton").is_file():
        return custom / "proton"

    official = _official_proton_dir(root, key)
    if official is not None:
        return official / "proton"

    # 名前が合わなくても Experimental があれば使えるようにしておく
    experimental = root / "steamapps" / "common" / "Proton - Experimental" / "proton"
    if experimental.is_file():
        return experimental

    raise ProtonError(
        f"'{key}' に対応する Proton が見つかりませんでした。"
        " Steam でこのゲームの互換ツールを確認してください。"
    )


@dataclass
class PatchExe:
    """当てられるパッチ 1 件。"""

    #: ゲームディレクトリからの相対パス
    relative: str
    #: 実体
    path: Path
    size: int = 0
    #: 既に実行したか
    applied: bool = False

    @property
    def label(self) -> str:
        """どのパッチか分かる表示名。

        ``追加パッチ/<キャラ名>/patch.exe`` のように
        フォルダで区別する作りが多いので、フォルダ名まで見せる。
        """
        return self.relative


#: パッチではないと分かっているもの
_NOT_PATCH = (
    "unins", "uninstall", "setup_dx", "dxsetup", "vcredist", "directx",
    "dotnet", "oalinst", "redist", "crashhandler", "crashreport",
)


def find_patches(directory: Path, applied: list[str] | None = None) -> list[PatchExe]:
    """ディレクトリ以下の exe を、パッチ候補として列挙する。

    どれがパッチかは中身からは分からないので、絞り込みすぎない。明らかに
    パッチでないもの (アンインストーラや再頒布ランタイム) だけ外す。
    """
    done = set(applied or ())
    found: list[PatchExe] = []

    if not directory.is_dir():
        return found

    for base, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.lower().endswith(".exe"):
                continue
            lowered = name.lower()
            if any(mark in lowered for mark in _NOT_PATCH):
                continue
            full = Path(base) / name
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            relative = str(full.relative_to(directory)).replace("\\", "/")
            found.append(
                PatchExe(relative=relative, path=full, size=size, applied=relative in done)
            )

    found.sort(key=lambda item: item.relative)
    return found


@dataclass
class RunResult:
    """実行の結果。"""

    exe: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    proton: str = ""
    prefix: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def summary(self) -> str:
        if self.ok:
            return f"{self.exe} を実行しました。"
        return f"{self.exe} が終了コード {self.returncode} で終わりました。"


def run_exe(
    userdata: Path,
    app_id: int,
    exe: Path,
    working_dir: Path | None = None,
    args: list[str] | None = None,
    timeout: float = 1800.0,
    fallback_tool: str = "",
) -> RunResult:
    """ゲームのプレフィックスで exe を実行する。

    インストーラ形式のパッチは対話操作を求めることがある。その場合は画面が
    出るので、SteamDeck ならデスクトップモードで操作することになる。
    """
    if not exe.is_file():
        raise ProtonError(f"実行ファイルが見つかりません: {exe}")

    proton = resolve_proton(userdata, app_id, fallback=fallback_tool)
    prefix = prefix_path(userdata, app_id)

    if not prefix.is_dir():
        # 初回起動前だと存在しない。Proton が作るので致命ではないが、
        # Steam が使うものと中身がずれる可能性は伝えておきたい。
        prefix.mkdir(parents=True, exist_ok=True)

    environment = dict(os.environ)
    environment["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root(userdata))
    environment["STEAM_COMPAT_DATA_PATH"] = str(prefix)

    command = [str(proton), "run", str(exe)] + list(args or [])
    try:
        completed = subprocess.run(
            command,
            cwd=str(working_dir or exe.parent),
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ProtonError(
            f"{exe.name} が {timeout:.0f} 秒で終わりませんでした。"
            " 対話操作を求めている可能性があります。"
        ) from error
    except OSError as error:
        raise ProtonError(f"{exe.name} を実行できませんでした: {error}") from error

    return RunResult(
        exe=exe.name,
        returncode=completed.returncode,
        stdout=(completed.stdout or "")[-4000:],
        stderr=(completed.stderr or "")[-4000:],
        proton=str(proton),
        prefix=str(prefix),
    )
