"""コマンドラインインターフェース。

``python3 -m dlsite_deck <サブコマンド>`` で使う。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import (
    __version__,
    api,
    archive,
    category,
    config as config_module,
    cookies,
    cover,
    download,
    session,
    install,
    server,
    state,
    steam,
)

LOGIN_INSTRUCTIONS = session.LOGIN_INSTRUCTIONS


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "handler", None):
        parser.print_help()
        return 1

    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        return 130
    except (
        api.DlsiteApiError,
        archive.ArchiveError,
        cookies.CookieError,
        steam.SteamError,
        RuntimeError,
    ) as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dlsite_deck",
        description="DLsite 購入済み作品を SteamDeck にダウンロード・展開する",
    )
    parser.add_argument("--version", action="version", version=f"dlsite_deck {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="設定ファイルのパス (既定: プロジェクト直下の config.json)",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="プロジェクト外への書き込み確認を省略する",
    )

    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="環境とセッションを確認する")
    check.set_defaults(handler=cmd_check)

    login = sub.add_parser("login", help="ログイン手順を表示する")
    login.set_defaults(handler=cmd_login)

    init = sub.add_parser("init", help="設定ファイルを作成する")
    init.set_defaults(handler=cmd_init)

    serve = sub.add_parser("serve", help="ローカル Web UI を起動する")
    serve.add_argument("--host", default="127.0.0.1", help="待ち受けアドレス")
    serve.add_argument("--port", type=int, default=8765, help="待ち受けポート")
    serve.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    serve.set_defaults(handler=cmd_serve)

    listing = sub.add_parser("list", help="購入済みライブラリを一覧する")
    listing.add_argument(
        "--category",
        choices=[item.key for item in category.CATEGORIES],
        help="表示するカテゴリ (既定: ゲームのみ)",
    )
    listing.add_argument("--all", action="store_true", help="ゲーム以外の作品も表示する")
    listing.add_argument("--updates", action="store_true", help="更新があるものだけ表示する")
    listing.add_argument("--installed", action="store_true", help="導入済みだけ表示する")
    listing.add_argument("--missing", action="store_true", help="未取得だけ表示する")
    listing.set_defaults(handler=cmd_list)

    fetch = sub.add_parser("download", help="作品をダウンロードして展開する")
    fetch.add_argument("ids", nargs="*", help="作品 ID (RJ/VJ...)")
    fetch.add_argument("--all", action="store_true", help="未取得の作品をすべて取得する")
    fetch.add_argument("--updates", action="store_true", help="更新がある作品を取り直す")
    fetch.add_argument("--force", action="store_true", help="導入済みでも取り直す")
    fetch.add_argument(
        "--install-dir",
        help="展開先 (設定した一覧のどれか。既定は設定の既定インストール先)",
    )
    fetch.add_argument("--keep-archives", action="store_true", help="展開後もアーカイブを残す")
    fetch.add_argument("--no-extract", action="store_true", help="展開せずダウンロードのみ行う")
    fetch.add_argument("--dry-run", action="store_true", help="対象を表示するだけで実行しない")
    fetch.set_defaults(handler=cmd_download)

    adopt = sub.add_parser(
        "adopt", help="手作業で展開済みのディレクトリをツールの管理下に取り込む"
    )
    adopt.add_argument(
        "mapping",
        type=Path,
        help='対応表の JSON。[{"directory": "...", "work_id": "RJ..."}, ...] の形式',
    )
    adopt.add_argument("--dry-run", action="store_true", help="確認だけで記録しない")
    adopt.set_defaults(handler=cmd_adopt)

    register = sub.add_parser("steam", help="導入済みゲームを Steam に登録する")
    register.add_argument("ids", nargs="*", help="作品 ID (省略時は導入済みすべて)")
    register.add_argument("--remove", action="store_true", help="登録を解除する")
    register.add_argument(
        "--exe",
        help="登録する実行ファイル (作品 ID を 1 件だけ指定したときに使える)",
    )
    register.add_argument(
        "--title",
        help="Steam に登録する名称 (既定: DLsite のタイトル)",
    )
    register.add_argument(
        "--choose",
        action="store_true",
        help="実行ファイルと名称を対話的に選ぶ",
    )
    register.add_argument(
        "--no-images",
        action="store_true",
        help="ライブラリの表紙・背景を設定しない",
    )
    register.add_argument(
        "--images-only",
        action="store_true",
        help="shortcuts.vdf は変更せず、既存の登録に表紙・背景・ロゴだけを設定する"
        "（既に別名で登録済みのゲーム向け）",
    )
    register.add_argument("--dry-run", action="store_true", help="対象を表示するだけで実行しない")
    register.set_defaults(handler=cmd_steam)

    return parser


# ----------------------------------------------------------------------
# 共通処理
# ----------------------------------------------------------------------


def _load_config(args) -> config_module.Config:
    cfg = config_module.load(args.config)
    # 展開ツールの探索先は archive 側が持つので、設定を読んだ時点で渡しておく
    archive.add_tool_dirs(cfg.tool_dirs)
    return cfg


def _confirm_external_paths(args, cfg: config_module.Config) -> bool:
    """プロジェクト外に書き込む設定なら確認を取る。

    一度承認したパスは設定に記録し、以後は聞かない。デスクトップのショートカットや
    Steam から起動したときは端末が無く、確認に答えられないため。
    """
    pending = config_module.unacknowledged_paths(cfg)
    if not pending:
        return True

    print("次のパスはプロジェクトディレクトリの外にあります:")
    for path in pending:
        print(f"  {path}")

    if args.yes:
        print("--yes が指定されたので続行し、以後は確認しません。")
    elif sys.stdin.isatty():
        answer = input("これらへの書き込みを許可しますか? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            return False
    else:
        print(
            "対話的に確認できません。一度端末から実行して承認するか、"
            "--yes を付けてください。",
            file=sys.stderr,
        )
        return False

    config_module.acknowledge(cfg, pending)
    config_module.save(cfg, args.config)
    return True


# セッションの用意は session に一本化してある
_load_cookies = session.load_cookies
_build_client = session.build_client


def _fetch_library(
    client: api.DlsiteClient, cfg: config_module.Config
) -> tuple[list[api.Work], list[str]]:
    library = client.library()
    excluded = set(cfg.exclude)
    works = [work for work in library.works if work.id not in excluded]
    return works, library.unavailable


def _select(
    statuses: list[state.LibraryStatus],
    ids: list[str],
    take_all: bool,
    updates_only: bool,
    include_non_games: bool,
) -> list[state.LibraryStatus]:
    """コマンドライン指定から対象を絞る。"""
    if ids:
        wanted = {value.upper() for value in ids}
        selected = [item for item in statuses if item.work.id.upper() in wanted]
        missing = wanted - {item.work.id.upper() for item in selected}
        if missing:
            raise RuntimeError(
                "ライブラリに見つからない作品 ID があります: " + ", ".join(sorted(missing))
            )
        return selected

    pool = statuses if include_non_games else [item for item in statuses if item.work.is_game]

    if updates_only:
        return [item for item in pool if item.outdated]
    if take_all:
        return [item for item in pool if not item.installed]
    return []


# ----------------------------------------------------------------------
# サブコマンド
# ----------------------------------------------------------------------


def cmd_login(args) -> int:
    print(LOGIN_INSTRUCTIONS)
    return 0


def cmd_init(args) -> int:
    path = args.config or config_module.default_config_path()
    if path.is_file():
        print(f"設定ファイルは既にあります: {path}")
        return 0

    saved = config_module.save(config_module.Config(), path)
    print(f"設定ファイルを作成しました: {saved}")
    print("展開先などを変更する場合はこのファイルを編集してください。")
    return 0


def cmd_serve(args) -> int:
    cfg = _load_config(args)

    if not _confirm_external_paths(args, cfg):
        return 1

    return server.serve(
        cfg,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )


def cmd_check(args) -> int:
    cfg = _load_config(args)
    ok = True

    print(f"dlsite_deck {__version__}")
    print(f"Python      : {sys.version.split()[0]}")
    print(f"設定        : {args.config or config_module.default_config_path()}")
    print()

    print("[パス]")
    for label, path in (
        ("ダウンロード", cfg.download_path),
        ("展開先", cfg.install_path),
        ("状態ファイル", cfg.state_path),
    ):
        location = "プロジェクト内" if config_module.is_inside_project(path) else "プロジェクト外"
        print(f"  {label:<12}: {path}  ({location})")

    available = download.free_space(cfg.install_path)
    if available is not None:
        print(f"  空き容量    : {download.format_size(available)}")
    print()

    print("[展開ツール]")
    print("  ZIP         : 標準ライブラリで対応")
    rar_tool = archive._find_rar_tool()
    if rar_tool:
        print(f"  分割 RAR    : {rar_tool[0]} を使用 ({rar_tool[1]})")
    else:
        print(
            "  分割 RAR    : 未検出 "
            "(bsdtar / 7z / unar / unrar のいずれかがあれば旧形式にも対応)"
        )
    print()

    print("[ローマ字変換]")
    try:
        import pykakasi  # noqa: F401

        print("  pykakasi    : あり (漢字タイトルもローマ字化できる)")
    except ImportError:
        print(
            "  pykakasi    : なし "
            "(漢字タイトルは英語タイトルか作品 ID にフォールバックする)"
        )
        print("                入れる場合: pip install --user pykakasi")
    print()

    print("[Cookie / セッション]")
    try:
        found = _load_cookies(cfg)
    except cookies.CookieError as error:
        print(f"  取得失敗    : {error}")
        print()
        print(LOGIN_INSTRUCTIONS)
        return 1

    if not found:
        print("  取得件数    : 0 件")
        print()
        print(LOGIN_INSTRUCTIONS)
        return 1

    print(f"  取得件数    : {len(found)} 件")
    print(cookies.summarize(found))

    client = api.DlsiteClient(cookies.to_cookiejar(found))
    if client.validate_session():
        count = client.content_count()
        print(f"  セッション  : 有効 (購入 {count.get('user', '?')} 件)")
    elif client.refresh_session():
        count = client.content_count()
        print(f"  セッション  : 有効 (購入 {count.get('user', '?')} 件)")
        print("                Play 側セッションを再取得しました")
    else:
        print("  セッション  : 無効または期限切れ")
        print()
        print(LOGIN_INSTRUCTIONS)
        ok = False

    print()
    print("[Steam]")
    userdata = config_module.steam_userdata_path(cfg)
    if userdata:
        print(f"  userdata    : {userdata}")
        if steam.is_steam_running():
            print("  状態        : 起動中 (登録する前に Steam を終了すること)")
    else:
        print("  userdata    : 未検出 (Steam 連携は使えない)")

    return 0 if ok else 1


def cmd_list(args) -> int:
    cfg = _load_config(args)
    client = _build_client(cfg)
    current = state.State.load(cfg.state_path)

    works, unavailable = _fetch_library(client, cfg)
    statuses = state.build_status(works, current)

    # 既定はゲームのみ。--category で切り替え、--all で全カテゴリ。
    if args.category:
        statuses = [item for item in statuses if item.work.category == args.category]
    elif not args.all:
        statuses = [item for item in statuses if item.work.is_game]
    if args.updates:
        statuses = [item for item in statuses if item.outdated]
    if args.installed:
        statuses = [item for item in statuses if item.installed]
    if args.missing:
        statuses = [item for item in statuses if not item.installed]

    if not statuses:
        print("該当する作品はありません。")
        return 0

    statuses.sort(key=lambda item: (not item.outdated, item.installed, item.work.id))

    width = shutil.get_terminal_size((100, 24)).columns
    title_width = max(20, width - 48)
    show_category = bool(args.all and not args.category)

    header = f"{'ID':<12} {'状態':<8} "
    if show_category:
        header += f"{'カテゴリ':<14} "
    print(header + "タイトル")
    print("-" * min(width - 1, 100))

    for item in statuses:
        title = item.work.title
        if len(title) > title_width:
            title = title[: title_width - 1] + "…"
        line = f"{item.work.id:<12} {item.label:<8} "
        if show_category:
            line += f"{item.work.category_label:<14} "
        print(line + title)

    outdated = sum(1 for item in statuses if item.outdated)
    installed = sum(1 for item in statuses if item.installed)
    print()
    print(f"合計 {len(statuses)} 件 / 導入済み {installed} 件 / 更新あり {outdated} 件")
    if outdated:
        print("更新を取り込むには: python3 -m dlsite_deck download --updates")
    if unavailable:
        print()
        print(
            f"次の {len(unavailable)} 件は購入履歴にありますが Play API が扱いません "
            "(ブラウザ専用作品や取り扱い終了分):"
        )
        print("  " + ", ".join(unavailable))
    return 0


def cmd_download(args) -> int:
    cfg = _load_config(args)

    if not _confirm_external_paths(args, cfg):
        return 1

    client = _build_client(cfg)
    current = state.State.load(cfg.state_path)

    works, _unavailable = _fetch_library(client, cfg)
    statuses = state.build_status(works, current)

    if args.ids:
        targets = _select(statuses, args.ids, False, False, True)
    else:
        targets = _select(
            statuses,
            [],
            args.all,
            args.updates,
            cfg.include_non_games,
        )

    if args.force and args.ids:
        pass  # 明示指定は常に対象
    elif not args.force:
        targets = [item for item in targets if not item.installed or item.outdated]

    if not targets:
        print("取得対象がありません。--all / --updates / 作品 ID を指定してください。")
        return 0

    print(f"対象 {len(targets)} 件:")
    for item in targets:
        print(f"  {item.work.id}  {item.label:<8} {item.work.title}")
    print()

    if args.dry_run:
        print("--dry-run のため実行しません。")
        return 0

    try:
        install_root = cfg.resolve_install_dir(args.install_dir)
    except ValueError as error:
        raise RuntimeError(str(error)) from None

    if args.install_dir:
        print(f"展開先: {install_root}")
        print()

    progress = download.ConsoleProgress()
    failures: list[tuple[str, str]] = []

    for index, item in enumerate(targets, start=1):
        work = item.work
        print(f"[{index}/{len(targets)}] {work.id} {work.title}")

        try:
            _process_work(
                client=client,
                cfg=cfg,
                current=current,
                work=work,
                progress=progress,
                install_root=install_root,
                keep_archives=args.keep_archives,
                extract=not args.no_extract,
            )
            current.save()
        except (api.DlsiteApiError, archive.ArchiveError, OSError) as error:
            progress.finish()
            print(f"  失敗: {error}", file=sys.stderr)
            failures.append((work.id, str(error)))
            continue

    if failures:
        print()
        print(f"{len(failures)} 件が失敗しました:", file=sys.stderr)
        for work_id, message in failures:
            print(f"  {work_id}: {message}", file=sys.stderr)
        return 1

    print()
    print("完了しました。")
    return 0


def _process_work(
    client: api.DlsiteClient,
    cfg: config_module.Config,
    current: state.State,
    work: api.Work,
    progress: download.ConsoleProgress,
    install_root: Path,
    keep_archives: bool,
    extract: bool,
) -> None:
    """1 作品のダウンロードから展開・記録までを行う。

    手順そのものは :mod:`dlsite_deck.install` にあり、ここは端末への表示だけを担う。
    """
    result = install.install_work(
        client,
        cfg,
        work,
        install_root,
        current,
        progress=progress,
        extract=extract,
        keep_archives=keep_archives,
    )
    progress.finish()

    if result.serial_numbers:
        print("  シリアルコード:")
        for label, value in result.serial_numbers:
            print(f"    {label}: {value}")

    if not extract:
        print(f"  ダウンロード完了: {result.cache_dir}")
        return

    if not result.extracted:
        print(
            f"  展開できる形式ではないため、ダウンロードのみ行いました: {result.cache_dir}",
            file=sys.stderr,
        )
        return

    print(f"  展開先: {result.output_dir}")
    if result.flattened:
        print("    (単一ディレクトリを展開先直下に引き上げました)")
    for before, after in result.renamed:
        print(f"    バージョン除去: {before.name} -> {after.name}")
    for link in result.removed_links:
        print(f"    外を指すリンクを取り除きました: {link}")
    if result.executable:
        print(f"    実行ファイル: {result.executable.relative_to(result.output_dir)}")

    if cfg.register_to_steam and result.executable and result.entry:
        _register_one(
            cfg, current, result.entry, result.executable, dry_run=False, client=client
        )

    if result.freed:
        print(f"    アーカイブ {download.format_size(result.freed)} を削除しました")


def cmd_steam_images(args, cfg: config_module.Config, userdata: Path) -> int:
    """既存の Steam 登録に、表紙・背景・ロゴだけを設定する。

    ツールを使う前に自分で登録したショートカットは、DLsite のタイトルとは違う名前が
    付いていることが多い。名前で照合すると重複を作ってしまうので、**exe のパスが
    どの作品のディレクトリに入っているか**で対応付ける。``shortcuts.vdf`` は読むだけで
    書き換えないので、AppID もプレイ時間も Proton の割り当ても保たれる。
    """
    current = state.State.load(cfg.state_path)
    entries = list(current.works.values())
    if args.ids:
        wanted = {value.upper() for value in args.ids}
        entries = [entry for entry in entries if entry.id.upper() in wanted]

    # 既存のショートカットを exe のパスで引けるようにする
    shortcuts: list[tuple[Path, int, str]] = []
    config_dirs = steam.find_user_config_dirs(userdata)
    for config_dir in config_dirs:
        document = steam.load_shortcuts(config_dir / "shortcuts.vdf")
        for entry in document.get("shortcuts", {}).values():
            if not isinstance(entry, dict):
                continue
            exe = (steam.get_field(entry, "exe") or "").strip('"')
            app_id = steam.get_field(entry, "appid")
            name = steam.get_field(entry, "AppName") or ""
            if exe and app_id is not None:
                shortcuts.append((Path(exe), int(app_id) & 0xFFFFFFFF, name))

    print(f"既存の Steam 登録: {len(shortcuts)} 件")

    client: api.DlsiteClient | None = None
    if not args.no_images:
        try:
            client = _build_client(cfg)
            _fill_missing_image_urls(client, cfg, entries)
        except (api.DlsiteApiError, cookies.CookieError) as error:
            print(f"  画像を取得できません: {error}", file=sys.stderr)

    matched = unmatched = 0
    installed = 0

    for entry in entries:
        directory = entry.path.resolve()
        found = None
        for exe, app_id, name in shortcuts:
            try:
                exe.resolve().relative_to(directory)
            except (ValueError, OSError):
                continue
            found = (app_id, name)
            break

        if found is None:
            unmatched += 1
            continue

        app_id, name = found
        matched += 1
        entry.steam_app_id = app_id
        entry.steam_title = name

        if args.dry_run:
            print(f"  {entry.id}  「{name}」 (AppID {app_id})")
            continue

        image = _fetch_grid_image(entry, client) if not args.no_images else None
        cover_image, logo = cover.artwork(cfg, name, image, entry.executable)

        written: list[Path] = []
        for config_dir in config_dirs:
            written += steam.apply_artwork(
                config_dir,
                app_id,
                image=image,
                cover_image=cover_image,
                logo=logo,
                slots=cfg.steam_grid_slots,
            )

        if written:
            installed += 1
        print(f"  {entry.id}  「{name}」 画像 {len(written)} 枚")

    if not args.dry_run:
        current.save()

    print()
    print(f"対応付いた: {matched} 件 / 見つからなかった: {unmatched} 件")
    if not args.dry_run:
        print(f"画像を設定した: {installed} 件")
        print("Steam を再起動すると表示に反映されます。")
    return 0


def cmd_adopt(args) -> int:
    """既に展開済みのディレクトリを、ダウンロードせずに導入済みとして記録する。

    ツールを使う前に手作業で展開したゲーム群を管理下に移すための入口。
    ファイルには一切触れず、``state.json`` に記録するだけ。
    """
    cfg = _load_config(args)

    try:
        payload = json.loads(args.mapping.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"対応表を読めませんでした: {args.mapping} ({error})") from None

    if not isinstance(payload, list):
        raise RuntimeError("対応表は一覧形式で指定してください。")

    client = _build_client(cfg)
    works, _unavailable = _fetch_library(client, cfg)
    catalogue = {work.id.upper(): work for work in works}

    current = state.State.load(cfg.state_path)
    adopted = 0
    skipped: list[str] = []

    for row in payload:
        directory = Path(str(row.get("directory", ""))).expanduser()
        work_id = str(row.get("work_id", "")).upper()

        if not directory.is_dir():
            skipped.append(f"{directory}: ディレクトリがありません")
            continue

        work = catalogue.get(work_id)
        if work is None:
            skipped.append(f"{directory}: ライブラリに {work_id} がありません")
            continue

        existing = current.get(work.id)
        if existing and Path(existing.directory) != directory:
            skipped.append(
                f"{directory}: {work_id} は既に {existing.directory} として記録済み"
            )
            continue

        executables = archive.find_executables(directory)
        executable = executables[0] if executables else None

        if args.dry_run:
            print(f"  {work.id}  {work.title}")
            print(f"    {directory}")
            print(f"    実行ファイル: {executable.name if executable else '(見つからず)'}")
        else:
            current.record(work, directory, executable=executable)
        adopted += 1

    if not args.dry_run:
        current.save()

    print()
    print(f"取り込み{'（確認のみ）' if args.dry_run else ''}: {adopted} 件")
    if skipped:
        print(f"見送り: {len(skipped)} 件")
        for line in skipped[:20]:
            print(f"  {line}")
        if len(skipped) > 20:
            print(f"  ... 他 {len(skipped) - 20} 件")
    return 0


def cmd_steam(args) -> int:
    cfg = _load_config(args)
    current = state.State.load(cfg.state_path)

    userdata = config_module.steam_userdata_path(cfg)
    if userdata is None:
        raise steam.SteamError(
            "Steam の userdata が見つかりません。"
            "設定の steam_userdata_dir で明示してください。"
        )

    # 画像だけなら shortcuts.vdf を触らないので、Steam の起動中でも安全に行える
    if args.images_only:
        return cmd_steam_images(args, cfg, userdata)

    if not args.yes and not config_module.is_inside_project(userdata):
        print(f"Steam の設定ファイルを書き換えます: {userdata}")
        if not sys.stdin.isatty():
            print("対話的に確認できません。--yes を付けてください。", file=sys.stderr)
            return 1
        if input("続行しますか? [y/N]: ").strip().lower() not in ("y", "yes"):
            return 1

    if steam.is_steam_running():
        print(
            "Steam が起動中です。終了してから実行してください "
            "(起動中に書き換えても Steam 終了時に上書きされます)。",
            file=sys.stderr,
        )
        return 1

    entries = list(current.works.values())
    if args.ids:
        wanted = {value.upper() for value in args.ids}
        entries = [entry for entry in entries if entry.id.upper() in wanted]

    if not args.remove:
        entries = [entry for entry in entries if entry.executable]

    if not entries:
        print("対象がありません。先に download を実行してください。")
        return 0

    if (args.exe or args.title) and len(entries) != 1:
        raise steam.SteamError(
            "--exe / --title は作品 ID を 1 件だけ指定したときに使えます。"
        )

    for entry in entries:
        action = "解除" if args.remove else "登録"
        print(f"  {action}: {entry.id}  {entry.steam_title or entry.title}")

    if args.dry_run:
        print("--dry-run のため実行しません。")
        return 0

    # 画像の取得にだけセッションが要る。要らないなら接続しない。
    client: api.DlsiteClient | None = None
    if not args.remove and cfg.steam_grid_images and not args.no_images:
        try:
            client = _build_client(cfg)
            _fill_missing_image_urls(client, cfg, entries)
        except (api.DlsiteApiError, cookies.CookieError) as error:
            print(f"  画像取得用のセッションを用意できませんでした: {error}", file=sys.stderr)

    changed = 0

    for entry in entries:
        if args.remove:
            result = steam.unregister(
                userdata, entry.steam_title or entry.title, app_id=entry.steam_app_id
            )
            if result.updated or result.images:
                entry.steam_app_id = None
                changed += 1
                for path in result.updated:
                    print(f"  更新: {path}")
                if result.images:
                    print(f"    画像 {len(result.images)} 件を削除")
            continue

        executable, title = _resolve_registration(entry, args)
        if executable is None:
            continue

        # 改名や exe の変更で AppID が変わるので、古い登録と画像を先に片付ける
        previous_title = entry.steam_title or entry.title
        new_id = steam.build_shortcut(title, executable).app_id
        if entry.steam_app_id != new_id:
            steam.unregister(userdata, previous_title, app_id=entry.steam_app_id)

        image = None
        if cfg.steam_grid_images and not args.no_images:
            image = _fetch_grid_image(entry, client)

        result = install.register_to_steam(cfg, userdata, title, executable, image)
        entry.executable = str(executable)
        entry.steam_title = title
        entry.steam_app_id = result.app_id
        changed += 1

        print(f"  {entry.id}: 「{title}」として登録 (AppID {result.app_id})")
        print(f"    実行ファイル: {executable}")
        if result.images:
            print(f"    表紙・背景: {len(result.images)} 件を設定")
        elif cfg.steam_grid_images and not args.no_images:
            print("    表紙・背景: 画像を取得できなかったので未設定", file=sys.stderr)
        for path in result.backups:
            print(f"    控え: {path}")

    current.save()
    print(f"{changed} 件を反映しました。Steam を起動して確認してください。")
    return 0


def _fill_missing_image_urls(
    client: api.DlsiteClient,
    cfg: config_module.Config,
    entries: list[state.InstalledWork],
) -> None:
    """画像 URL が記録されていない古い記録を、ライブラリから補う。"""
    missing = [entry for entry in entries if not entry.image_url]
    if not missing:
        return

    works, _unavailable = _fetch_library(client, cfg)
    urls = {work.id: work.image_url for work in works}
    for entry in missing:
        entry.image_url = urls.get(entry.id)


def _fetch_grid_image(
    entry: state.InstalledWork, client: api.DlsiteClient | None
) -> bytes | None:
    """作品画像を取得する。失敗しても登録自体は続ける。

    一度取ったものは作品ディレクトリに残してあるので、まずそちらを見る。
    """
    saved = cover.cached_image(entry.path)
    if saved is not None:
        return saved

    image = session.fetch_image(client, entry.image_url)
    if image:
        cover.store_image(entry.path, image)
    return image


def _resolve_registration(entry: state.InstalledWork, args) -> tuple[Path | None, str]:
    """登録する実行ファイルと名称を決める。

    ``--exe`` / ``--title`` の指定があればそれを使い、``--choose`` なら対話的に
    選ばせる。どちらも無ければ従来どおり自動検出の第一候補と DLsite のタイトル。
    """
    default_title = entry.steam_title or entry.title

    if args.exe:
        executable = Path(args.exe).expanduser()
        if not executable.is_file():
            raise steam.SteamError(f"実行ファイルが見つかりません: {executable}")
        return executable, (args.title or default_title)

    candidates = archive.executable_candidates(entry.path)
    if not candidates:
        print(f"  {entry.id}: 実行ファイルが見つからないので飛ばします。")
        return None, default_title

    if not args.choose:
        chosen = Path(entry.executable) if entry.executable else candidates[0].path
        return chosen, (args.title or default_title)

    if not sys.stdin.isatty():
        raise steam.SteamError("--choose は対話的な端末でのみ使えます。")

    print(f"\n  {entry.id} {entry.title}")
    print("  登録する実行ファイルを選んでください:")
    for index, item in enumerate(candidates, start=1):
        note = " ← ゲーム本体でない可能性" if item.suspicious else ""
        print(
            f"    {index}) {item.relative}"
            f"  ({download.format_size(item.size)}){note}"
        )

    executable = candidates[0].path
    answer = input(f"  番号 [1-{len(candidates)}] (既定 1): ").strip()
    if answer:
        try:
            executable = candidates[int(answer) - 1].path
        except (ValueError, IndexError):
            raise steam.SteamError(f"選択が正しくありません: {answer}") from None

    title = args.title or default_title
    entered = input(f"  Steam に登録する名称 (既定「{title}」): ").strip()
    return executable, (entered or title)


def _register_one(
    cfg: config_module.Config,
    current: state.State,
    entry: state.InstalledWork,
    executable: Path,
    dry_run: bool,
    client: api.DlsiteClient | None = None,
) -> None:
    """ダウンロード直後の自動 Steam 登録。"""
    userdata = config_module.steam_userdata_path(cfg)
    if userdata is None:
        print("    Steam の userdata が見つからないため登録を省略しました。", file=sys.stderr)
        return

    if steam.is_steam_running():
        print(
            "    Steam が起動中のため登録を省略しました "
            "(終了後に `steam` サブコマンドで登録できます)。",
            file=sys.stderr,
        )
        return

    title = entry.steam_title or entry.title
    image = _fetch_grid_image(entry, client) if cfg.steam_grid_images else None
    result = install.register_to_steam(cfg, userdata, title, executable, image)
    entry.steam_title = title
    entry.steam_app_id = result.app_id
    note = f"（表紙・背景 {len(result.images)} 件）" if result.images else ""
    print(f"    Steam に「{title}」として登録しました{note}。")
