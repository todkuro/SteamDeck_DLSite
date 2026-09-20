"""ブラウザの Cookie ストアから DLsite のセッション Cookie を取り出す。

DLsite のセッション Cookie は HttpOnly なので ``document.cookie`` からは読めない
(検証済み)。ブラウザが自前で持つ Cookie データベースを直接読むことで、2FA を含む
通常のログインをユーザーにそのまま行ってもらいつつ、確立済みのセッションだけを
借りる。ツールは認証情報に一切触れない。

現状 Firefox 系 (native / Flatpak / Snap / Windows) に対応する。Chromium 系は
値が OS のキーリングで暗号化されており標準ライブラリだけでは復号できないため、
``load_manual`` によるエクスポート済み Cookie の読み込みをフォールバックとする。
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar
from pathlib import Path

# セッションの確立に関わるホスト。これらのドメインに属する Cookie をすべて拾う。
DLSITE_DOMAIN_SUFFIX = "dlsite.com"

#: ブラウザから借りない Cookie。
#:
#: ``play_session`` はブラウザが play.dlsite.com を開いたときに作られるが、これを
#: 持ち込むとダウンロード API が 500 を返す (実機で確認済み)。こちらは
#: :meth:`DlsiteClient.refresh_session` で自前のセッションを張るので、
#: ブラウザ側のものは捨てて構わない。
BORROWED_COOKIE_DENYLIST = frozenset({"play_session"})

#: ログイン手順。CLI と Web UI の両方から同じ文面を出す。
LOGIN_STEPS = (
    "SteamDeck の Desktop Mode で Firefox を開く",
    "https://www.dlsite.com/ にアクセスし、ID・パスワード・2FA コードで"
    "いつもどおりログインする（このツールは入力に一切関与しない）",
    "続けて https://play.dlsite.com/ を一度開く。"
    "Play 側のセッションはここで確立されるので、この手順は省略しないこと",
    "Firefox は開いたままで構わない。この画面で「再確認」を押す",
)

LOGIN_NOTE = (
    "このツールはブラウザの Cookie データベースを読むだけで、"
    "ID・パスワード・2FA コードを受け取ることも保存することもない。"
)


def login_instructions(last_step: str = "このツールを実行する") -> str:
    """端末向けのログイン手順を組み立てる。"""
    steps = list(LOGIN_STEPS)
    steps[-1] = f"Firefox は開いたままで構わない。{last_step}"
    body = "\n".join(f"  {index}. {step}" for index, step in enumerate(steps, start=1))
    return f"DLsite へのログイン手順\n\n{body}\n\n{LOGIN_NOTE}\n"


class CookieError(RuntimeError):
    """Cookie の抽出に失敗した。"""


@dataclass(frozen=True)
class RawCookie:
    """ブラウザから取り出した 1 件の Cookie。"""

    host: str
    name: str
    value: str
    path: str
    expires: int | None
    secure: bool
    http_only: bool

    @property
    def domain(self) -> str:
        return self.host

    def is_expired(self, now: float | None = None) -> bool:
        if not self.expires:
            return False
        return self.expires <= (now if now is not None else time.time())


@dataclass(frozen=True)
class FirefoxProfile:
    """検出した Firefox プロファイル。"""

    name: str
    path: Path
    install: str
    default: bool

    @property
    def cookie_db(self) -> Path:
        return self.path / "cookies.sqlite"


def _candidate_firefox_roots() -> list[Path]:
    """環境ごとの Firefox プロファイル置き場を列挙する。"""
    home = Path.home()
    roots: list[Path] = []

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            roots.append(Path(appdata) / "Mozilla" / "Firefox")
    else:
        # Firefox は近年プロファイルを XDG の場所へ移した。実測では
        # Flatpak 版 155.0.1 が ~/.mozilla、deb 版 156.0 が ~/.config/mozilla
        # を使っていた。版や配布形態で変わるので**両方を見る**。
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        flatpak = home / ".var" / "app" / "org.mozilla.firefox"

        roots.extend(
            [
                # 通常のネイティブインストール
                config_home / "mozilla" / "firefox",
                home / ".mozilla" / "firefox",
                # Flatpak 版 (SteamDeck ではこれが既定)。
                # サンドボックス内の XDG_CONFIG_HOME は ~/.var/app/<id>/config。
                flatpak / "config" / "mozilla" / "firefox",
                flatpak / ".config" / "mozilla" / "firefox",
                flatpak / ".mozilla" / "firefox",
                # Snap 版
                home / "snap" / "firefox" / "common" / ".config" / "mozilla" / "firefox",
                home / "snap" / "firefox" / "common" / ".mozilla" / "firefox",
                # 各種フォーク
                config_home / "librewolf",
                home / ".var" / "app" / "io.gitlab.librewolf-community" / ".librewolf",
                home / ".librewolf",
            ]
        )

    env_root = os.environ.get("DLSITE_DECK_FIREFOX_ROOT")
    if env_root:
        roots.insert(0, Path(env_root))

    return [root for root in roots if root.is_dir()]


def discover_firefox_profiles() -> list[FirefoxProfile]:
    """cookies.sqlite を持つ Firefox プロファイルを新しい順に返す。"""
    profiles: list[FirefoxProfile] = []

    for root in _candidate_firefox_roots():
        defaults = _read_installs_defaults(root)
        ini_path = root / "profiles.ini"

        if ini_path.is_file():
            parser = configparser.ConfigParser()
            # profiles.ini はセクション名の大小を保持したいので既定の変換を切る
            parser.optionxform = str  # type: ignore[assignment]
            try:
                parser.read(ini_path, encoding="utf-8")
            except (OSError, configparser.Error):
                parser = configparser.ConfigParser()

            for section in parser.sections():
                if not section.lower().startswith("profile"):
                    continue
                raw_path = parser[section].get("Path")
                if not raw_path:
                    continue
                is_relative = parser[section].get("IsRelative", "1") == "1"
                path = (root / raw_path) if is_relative else Path(raw_path)
                if not (path / "cookies.sqlite").is_file():
                    continue
                profiles.append(
                    FirefoxProfile(
                        name=parser[section].get("Name", path.name),
                        path=path,
                        install=str(root),
                        default=(
                            parser[section].get("Default", "0") == "1"
                            or str(path) in defaults
                        ),
                    )
                )

        # profiles.ini が壊れている / 存在しない場合の総当たり
        if not profiles:
            for path in sorted(root.glob("*/cookies.sqlite")):
                profiles.append(
                    FirefoxProfile(
                        name=path.parent.name,
                        path=path.parent,
                        install=str(root),
                        default=False,
                    )
                )

    # 既定プロファイルを優先し、次に cookies.sqlite の更新時刻が新しい順
    profiles.sort(
        key=lambda profile: (
            profile.default,
            _mtime(profile.cookie_db),
        ),
        reverse=True,
    )
    return profiles


def _read_installs_defaults(root: Path) -> set[str]:
    """installs.ini に記録された既定プロファイルの絶対パス集合を返す。"""
    ini_path = root / "installs.ini"
    if not ini_path.is_file():
        return set()

    parser = configparser.ConfigParser()
    parser.optionxform = str  # type: ignore[assignment]
    try:
        parser.read(ini_path, encoding="utf-8")
    except (OSError, configparser.Error):
        return set()

    defaults = set()
    for section in parser.sections():
        raw_path = parser[section].get("Default")
        if raw_path:
            defaults.add(str(root / raw_path))
    return defaults


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _open_readonly_copy(db_path: Path) -> tuple[sqlite3.Connection, tempfile.TemporaryDirectory]:
    """起動中の Firefox にロックされていても読めるよう、DB を一時領域に複写して開く。

    呼び出し側は接続を閉じたあと TemporaryDirectory も cleanup すること。
    """
    tmp = tempfile.TemporaryDirectory(prefix="dlsite-deck-cookies-")
    target = Path(tmp.name) / db_path.name

    try:
        shutil.copy2(db_path, target)
        # WAL 未チェックポイントの分も取り込む
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, target.with_name(target.name + suffix))
        connection = sqlite3.connect(str(target))
    except (OSError, sqlite3.Error) as error:
        tmp.cleanup()
        raise CookieError(f"Cookie データベースを開けませんでした: {db_path} ({error})") from error

    return connection, tmp


def load_firefox(
    profile: FirefoxProfile | None = None,
    domain_suffix: str = DLSITE_DOMAIN_SUFFIX,
) -> list[RawCookie]:
    """Firefox の cookies.sqlite から対象ドメインの Cookie を取り出す。"""
    if profile is None:
        profiles = discover_firefox_profiles()
        if not profiles:
            raise CookieError(
                "Firefox のプロファイルが見つかりませんでした。"
                "Firefox で一度 DLsite にログインしてから再実行するか、"
                "DLSITE_DECK_FIREFOX_ROOT でプロファイル置き場を指定してください。"
            )
        profile = profiles[0]

    if not profile.cookie_db.is_file():
        raise CookieError(f"cookies.sqlite がありません: {profile.cookie_db}")

    connection, tmp = _open_readonly_copy(profile.cookie_db)
    try:
        rows = connection.execute(
            """
            SELECT host, name, value, path, expiry, isSecure, isHttpOnly
            FROM moz_cookies
            """
        ).fetchall()
    except sqlite3.Error as error:
        raise CookieError(f"moz_cookies の読み取りに失敗しました: {error}") from error
    finally:
        connection.close()
        tmp.cleanup()

    return _filter_domain(
        (
            RawCookie(
                host=str(host or ""),
                name=str(name or ""),
                value=str(value or ""),
                path=str(path or "/"),
                # Firefox の expiry は秒。セッション Cookie は 0。
                expires=int(expiry) if expiry else None,
                secure=bool(secure),
                http_only=bool(http_only),
            )
            for host, name, value, path, expiry, secure, http_only in rows
        ),
        domain_suffix,
    )


def load_manual(path: Path, domain_suffix: str = DLSITE_DOMAIN_SUFFIX) -> list[RawCookie]:
    """手動エクスポートした Cookie を読み込む (フォールバック経路)。

    ブラウザ拡張が吐く JSON 配列形式と、Netscape 形式の cookies.txt に対応する。
    """
    if not path.is_file():
        raise CookieError(f"Cookie ファイルがありません: {path}")

    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise CookieError(f"Cookie ファイルが空です: {path}")

    cookies = (
        _parse_manual_json(text, path)
        if text[0] in "[{"
        else _parse_netscape(text)
    )
    return _filter_domain(cookies, domain_suffix)


def _parse_manual_json(text: str, path: Path) -> list[RawCookie]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise CookieError(f"Cookie JSON を解釈できませんでした: {path} ({error})") from error

    # 拡張によっては {"cookies": [...]} で包む
    if isinstance(payload, dict):
        payload = payload.get("cookies", [])
    if not isinstance(payload, list):
        raise CookieError(f"Cookie JSON の形式が想定外です: {path}")

    cookies = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        host = entry.get("domain") or entry.get("host")
        if not name or not host:
            continue
        expires = entry.get("expirationDate") or entry.get("expires") or entry.get("expiry")
        cookies.append(
            RawCookie(
                host=str(host),
                name=str(name),
                value=str(entry.get("value", "")),
                path=str(entry.get("path") or "/"),
                expires=int(float(expires)) if expires else None,
                secure=bool(entry.get("secure", True)),
                http_only=bool(entry.get("httpOnly", False)),
            )
        )
    return cookies


def _parse_netscape(text: str) -> list[RawCookie]:
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        # "#HttpOnly_" 接頭辞つきの行は有効なデータなので落とさない
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
            http_only = True
        elif not line or line.startswith("#"):
            continue
        else:
            http_only = False

        fields = line.split("\t")
        if len(fields) < 7:
            continue
        host, _include_subdomains, path, secure, expires, name, value = fields[:7]
        cookies.append(
            RawCookie(
                host=host,
                name=name,
                value=value,
                path=path or "/",
                expires=int(expires) if expires and expires != "0" else None,
                secure=secure.upper() == "TRUE",
                http_only=http_only,
            )
        )
    return cookies


def _filter_domain(cookies, domain_suffix: str) -> list[RawCookie]:
    """対象ドメインかつ未期限切れの Cookie だけに絞る。"""
    suffix = domain_suffix.lstrip(".").lower()
    now = time.time()
    kept = []

    for cookie in cookies:
        host = cookie.host.lstrip(".").lower()
        if host != suffix and not host.endswith("." + suffix):
            continue
        if cookie.is_expired(now):
            continue
        if cookie.name in BORROWED_COOKIE_DENYLIST:
            continue
        kept.append(cookie)

    return kept


def to_cookiejar(cookies: list[RawCookie]) -> CookieJar:
    """抽出結果を urllib が使える CookieJar に変換する。"""
    jar = CookieJar()

    for raw in cookies:
        # 先頭ドットの有無でサブドメインへ送るかが決まる
        domain = raw.host if raw.host.startswith(".") else raw.host
        domain_specified = raw.host.startswith(".")
        jar.set_cookie(
            Cookie(
                version=0,
                name=raw.name,
                value=raw.value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain_specified,
                domain_initial_dot=domain_specified,
                path=raw.path or "/",
                path_specified=True,
                secure=raw.secure,
                expires=raw.expires,
                discard=raw.expires is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if raw.http_only else {},
                rfc2109=False,
            )
        )

    return jar


def summarize(cookies: list[RawCookie]) -> str:
    """診断用に、値を伏せた要約を返す。"""
    if not cookies:
        return "(Cookie なし)"

    by_host: dict[str, list[str]] = {}
    for cookie in cookies:
        by_host.setdefault(cookie.host, []).append(cookie.name)

    lines = []
    for host in sorted(by_host):
        names = ", ".join(sorted(by_host[host]))
        lines.append(f"  {host}: {names}")
    return "\n".join(lines)
