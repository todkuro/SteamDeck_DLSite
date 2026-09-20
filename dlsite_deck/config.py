"""設定の読み書きと、プロジェクト外パスの保護。

引継ぎ仕様の制限に合わせ、プロジェクトディレクトリの外へ書き込む場合は
呼び出し側で明示的な確認を要求できるようにしている (:func:`is_inside_project`)。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cover

#: パッケージの 1 つ上がプロジェクトルート
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILENAME = "config.json"

#: Linux で新しい登録に当てる互換ツール。DLsite のゲームは Windows 向け。
DEFAULT_COMPAT_TOOL = "proton_experimental"


def default_compat_tool() -> str:
    """登録時に割り当てる Proton の既定値。

    Windows では割り当てない。exe がそのまま動くので不要なうえ、当ててしまうと
    実体の無いツールを指す記述が ``config.vdf`` に残る。
    """
    return "" if sys.platform == "win32" else DEFAULT_COMPAT_TOOL


@dataclass
class Config:
    """ツール全体の設定。"""

    #: アーカイブの一時置き場 (キャッシュ)。展開後は既定で削除する。
    download_dir: str = str(PROJECT_ROOT / "downloads")
    #: 既定の展開先。この下に作品ごとのローマ字ディレクトリを作る。
    #: 必ず install_dirs のどれかと一致する。
    install_dir: str = str(PROJECT_ROOT / "games")
    #: 選べる展開先の一覧。ダウンロード時にこの中から選ぶ。
    install_dirs: list[str] = field(
        default_factory=lambda: [str(PROJECT_ROOT / "games")]
    )
    #: 状態ファイル
    state_file: str = str(PROJECT_ROOT / "state.json")
    #: Cookie の取得元。"firefox" または "manual"
    cookie_source: str = "firefox"
    #: cookie_source が "manual" のときに読むファイル
    cookie_file: str = str(PROJECT_ROOT / "cookies.txt")
    #: 展開先ディレクトリ名のテンプレート。{romaji} と {id} が使える。
    directory_template: str = "{romaji}"
    #: 長音符の扱い。"repeat" で母音を重ね、"drop" で捨てる。
    long_vowel: str = "repeat"
    #: 展開後にアーカイブを削除するか
    delete_archives: bool = True
    #: 展開後にバージョン番号を名前から除去するか
    strip_versions: bool = True
    #: 単一フォルダのみのアーカイブを展開先直下に引き上げるか
    flatten_single_root: bool = True
    #: ゲーム以外の作品種別も対象に含めるか
    include_non_games: bool = False
    #: Steam の非 Steam ゲームとして自動登録するか
    register_to_steam: bool = False
    #: Steam のユーザーデータ置き場 (未指定なら自動検出)
    steam_userdata_dir: str = ""
    #: Steam 登録時に DLsite の作品画像をライブラリの表紙・背景として設定するか
    steam_grid_images: bool = True
    #: 画像を設定するスロット。DLsite の画像は 560x420 (4:3) で、Steam が想定する
    #: 比率 (カバー 2:3 / 背景 3:1 / 横長 2.14:1) とは違うため、どこに使うかは選べる。
    #: "capsule" 横長カプセル / "cover" 縦長カバー / "hero" 背景
    steam_grid_slots: list[str] = field(
        default_factory=lambda: ["capsule", "cover", "hero"]
    )
    #: 縦長カバーを作るとき、作品画像を中心から 2:3 に切り出してタイトルを載せるか。
    #:
    #: Steam のストア製品はカバーにタイトルが焼き込まれているが、DLsite の画像には
    #: 無いため、そのままではライブラリ一覧でどれがどのゲームか分からない。
    #:
    #: **既定は環境で決まる。** 描画に使う rsvg-convert と日本語フォントが揃って
    #: いれば有効、どちらかが欠けていれば無効にする。揃っていない環境で有効に
    #: しても、豆腐 (□) が並んだカバーができるだけで嬉しくないため。
    #: 設定ファイルに書いてあればそちらが優先される。
    steam_cover_title: bool = field(default_factory=lambda: cover.title_supported())
    #: ロゴ画像 (背景に重ねて出る) に何を使うか。
    #: "title" タイトルの透過 PNG / "exe" exe の一番大きなアイコン / "none" 設定しない
    steam_logo_source: str = "title"
    #: 登録時に割り当てる互換ツール (Proton)。空なら割り当てない。
    #:
    #: DLsite のゲームは Windows 向けなので、Linux では既定で Proton Experimental を
    #: 当てる。**Windows では既定を空にする。** そこでは exe がそのまま動くうえ、
    #: 実体の無い互換ツールを指す ``CompatToolMapping`` を ``config.vdf`` に
    #: 書き込んでしまう (節が無ければ作るため、Linux 専用の節を新設することになる)。
    steam_compat_tool: str = field(default_factory=lambda: default_compat_tool())
    #: Steam 本体の実行ファイル。「Steam を終了する」で使う。
    #:
    #: 既定は SteamDeck の場所。ここに無い環境 (Windows など) では、PATH と
    #: Steam のフォルダからも探すので、たいていは既定のままでよい。
    steam_executable: str = "/usr/bin/steam"
    #: 登録時に入れる Steam の起動オプション。空なら設定しない。
    #:
    #: 日本語のゲームは、ロケールが無いと表示もファイル名も文字化けする。
    #: SteamOS には ja_JP.UTF-8 が入っていないので、ロケールを用意して
    #: ``LANG=ja_JP.UTF-8 ~/locales/run.sh %command%`` のように渡す必要がある。
    #: 環境によって用意の仕方が違うため、既定は空にして各自で設定してもらう。
    steam_launch_options: str = ""
    #: 利用者が書き込みを承認したプロジェクト外のパス。
    #: 一度承認したものは再確認しない。端末の無い起動 (デスクトップの
    #: ショートカットや Steam の非 Steam ゲーム) では確認に答えられないため。
    acknowledged_paths: list[str] = field(default_factory=list)
    #: 展開ツール (7z / unar / unrar 等) を探す追加ディレクトリ。PATH より優先する。
    tool_dirs: list[str] = field(default_factory=list)
    #: 除外する作品 ID
    exclude: list[str] = field(default_factory=list)

    @property
    def download_path(self) -> Path:
        return Path(self.download_dir).expanduser()

    def __post_init__(self) -> None:
        # 既定の展開先が一覧に無いと、選択欄に出せず設定として矛盾する
        self.install_dirs = [
            directory for directory in dict.fromkeys(self.install_dirs) if directory.strip()
        ]
        if not self.install_dirs:
            self.install_dirs = [self.install_dir]
        if self.install_dir not in self.install_dirs:
            self.install_dirs.insert(0, self.install_dir)

    @property
    def install_path(self) -> Path:
        """既定の展開先。"""
        return Path(self.install_dir).expanduser()

    @property
    def install_paths(self) -> list[Path]:
        """選べる展開先すべて。"""
        return [Path(directory).expanduser() for directory in self.install_dirs]

    def resolve_install_dir(self, requested: str | None) -> Path:
        """要求された展開先を検証して返す。

        設定にある場所しか受け付けない。UI からは一覧で選ばせるので、
        ここで弾かれるのは設定を書き換えた直後などに限られる。
        """
        if not requested:
            return self.install_path

        for directory in self.install_dirs:
            if directory == requested:
                return Path(directory).expanduser()

        raise ValueError(
            f"設定にない展開先です: {requested}"
            "（設定画面でインストール先を追加してください）"
        )

    @property
    def state_path(self) -> Path:
        return Path(self.state_file).expanduser()

    @property
    def cookie_path(self) -> Path:
        return Path(self.cookie_file).expanduser()


def default_config_path() -> Path:
    """設定ファイルの既定の位置。環境変数で差し替えできる。"""
    override = os.environ.get("DLSITE_DECK_CONFIG")
    if override:
        return Path(override).expanduser()
    return PROJECT_ROOT / CONFIG_FILENAME


def load(path: Path | None = None) -> Config:
    """設定を読む。無ければ既定値を返す。"""
    path = path or default_config_path()
    if not path.is_file():
        return Config()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"設定ファイルを読めませんでした: {path} ({error})") from error

    # 古い設定ファイルからの移行。steam_logo_from_exe (真偽値) は
    # steam_logo_source (何を使うか) に置き換わった。
    if "steam_logo_from_exe" in payload:
        legacy = payload.pop("steam_logo_from_exe")
        payload.setdefault("steam_logo_source", "exe" if legacy else "none")

    known = {field_name for field_name in Config.__dataclass_fields__}
    unknown = sorted(set(payload) - known)
    if unknown:
        raise RuntimeError(
            f"設定ファイルに未知の項目があります: {', '.join(unknown)} ({path})"
        )

    return Config(**payload)


def save(config: Config, path: Path | None = None) -> Path:
    """設定を書き出す。"""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def is_inside_project(path: Path) -> bool:
    """パスがプロジェクトディレクトリの内側か。"""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        return False

    root = PROJECT_ROOT.resolve()
    return resolved == root or root in resolved.parents


def unacknowledged_paths(config: Config) -> list[Path]:
    """プロジェクト外で、まだ承認されていないパス。"""
    approved = set(config.acknowledged_paths)
    return [path for path in external_paths(config) if str(path) not in approved]


def acknowledge(config: Config, paths: list[Path]) -> None:
    """指定したパスを承認済みとして覚える。"""
    approved = list(config.acknowledged_paths)
    for path in paths:
        if str(path) not in approved:
            approved.append(str(path))
    config.acknowledged_paths = approved


def external_paths(config: Config) -> list[Path]:
    """書き込み対象のうち、プロジェクトの外にあるものを列挙する。"""
    candidates = [config.download_path, config.state_path, *config.install_paths]
    if config.register_to_steam:
        candidates.append(steam_userdata_path(config))

    seen: dict[str, Path] = {}
    for path in candidates:
        if path and not is_inside_project(path):
            seen.setdefault(str(path), path)
    return list(seen.values())


def steam_userdata_path(config: Config) -> Path | None:
    """Steam の userdata ディレクトリを求める。

    本番は SteamOS だが、Windows での検証用に既定インストール先と
    レジストリに記録されたインストール先も見る。
    """
    if config.steam_userdata_dir:
        return Path(config.steam_userdata_dir).expanduser()

    for candidate in _steam_userdata_candidates():
        if candidate.is_dir():
            return candidate
    return None


def _steam_userdata_candidates() -> list[Path]:
    home = Path.home()

    if sys.platform == "win32":
        candidates = []
        registry_path = _steam_path_from_registry()
        if registry_path:
            candidates.append(registry_path / "userdata")
        for program_files in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(program_files)
            if base:
                candidates.append(Path(base) / "Steam" / "userdata")
        return candidates

    return [
        home / ".steam" / "steam" / "userdata",
        home / ".local" / "share" / "Steam" / "userdata",
        home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam" / "userdata",
    ]


def _steam_path_from_registry() -> Path | None:
    """Windows のレジストリから Steam のインストール先を読む。"""
    try:
        import winreg
    except ImportError:
        return None

    for root, key_path, value_name in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(root, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, value_name)
        except OSError:
            continue
        if value:
            return Path(str(value))
    return None
