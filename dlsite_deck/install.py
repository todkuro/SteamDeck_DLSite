"""1 作品を取得して展開し、記録するまでの一連の流れ。

コマンドラインと Web UI の両方から使う。以前はそれぞれが同じ手順を持っており、
片方だけ直して食い違う (実際に展開先の決め方がずれていた) 事故があったため、
手順そのものはここに一本化してある。

呼び出し側との違いは「経過をどう見せるか」と「中止をどう伝えるか」だけなので、
そこだけを差し込めるようにしている。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import api, archive, config as config_module, cover, download, romaji, state, steam


class Cancelled(Exception):
    """利用者が中止を指示した。"""


#: 進行段階。``"download"`` の間だけ中止を受け付けられる。
PHASE_DOWNLOAD = "download"
PHASE_EXTRACT = "extract"


@dataclass
class InstallResult:
    """取得の結果。"""

    work_id: str
    #: ダウンロードしたアーカイブ
    archives: list[Path] = field(default_factory=list)
    #: アーカイブの置き場
    cache_dir: Path | None = None
    #: 展開先。展開しなかった場合は None。
    output_dir: Path | None = None
    #: このジョブが新しく作った展開先 (中止・失敗時に消してよい場所)
    created_output: Path | None = None
    #: 見つかった実行ファイル
    executable: Path | None = None
    #: 単一ディレクトリを引き上げたか
    flattened: bool = False
    #: バージョン番号を除いた名前の対応
    renamed: list[tuple[Path, Path]] = field(default_factory=list)
    #: 展開先の外を指していたので取り除いたリンク
    removed_links: list[str] = field(default_factory=list)
    #: シリアルコード
    serial_numbers: list[tuple[str, str]] = field(default_factory=list)
    #: 展開できる形式だったか
    extracted: bool = False
    #: 片付けたアーカイブのバイト数
    freed: int = 0
    #: state.json に記録した内容
    entry: state.InstalledWork | None = None


def resolve_output_dir(
    cfg: config_module.Config,
    work: api.Work,
    root: Path,
    current: state.State,
) -> Path:
    """作品の展開先を決める。

    導入済みのものは記録された場所を使い続ける。別の展開先を選んでも複製を
    増やさないため。名前が他作品とぶつかる場合は作品 ID を足して避ける。
    """
    entry = current.get(work.id)
    if entry and entry.path.is_dir():
        return entry.path

    name = romaji.directory_name(
        work.title,
        english_title=work.english_title,
        work_id=work.id,
        template=cfg.directory_template,
        long_vowel=cfg.long_vowel,
    )
    candidate = root / name

    if not candidate.exists():
        return candidate

    # 空でも、別の作品の展開先として記録されている場所は使わない
    used_by_other = any(
        other.id != work.id and Path(other.directory) == candidate
        for other in current.works.values()
    )
    if not used_by_other and not any(candidate.iterdir()):
        return candidate

    return root / f"{name}_{work.id}"


#: 進捗の通知。``(ファイル名, 取得済み, 全体)``
ProgressCallback = Callable[[str, int, "int | None"], None]
#: 段階が変わったときの通知。``"download"`` / ``"extract"``
PhaseCallback = Callable[[str], None]


def install_work(
    client: api.DlsiteClient,
    cfg: config_module.Config,
    work: api.Work,
    install_root: Path,
    current: state.State,
    progress: ProgressCallback | None = None,
    on_phase: PhaseCallback | None = None,
    extract: bool = True,
    keep_archives: bool = False,
    result: InstallResult | None = None,
) -> InstallResult:
    """1 作品を取得し、展開して記録する。

    ``progress`` は中止の割り込み点でもある。中止したいときは、この中から
    :class:`Cancelled` を送出すればよい。呼び出し側は例外を受けて後始末をする。

    ``result`` を渡すとその場で埋めていく。中断したときも呼び出し側が
    「どこまで作ったか」を見て片付けられるようにするため。
    """
    if result is None:
        result = InstallResult(work_id=work.id)
    else:
        result.work_id = work.id

    if on_phase:
        on_phase(PHASE_DOWNLOAD)

    plan = client.download_plan(work.id)
    result.serial_numbers = plan.serial_numbers

    archive_dir = cfg.download_path / work.id
    result.cache_dir = archive_dir
    if work.content_size:
        # 展開ぶんも要るので倍を見ておく
        download.ensure_space(archive_dir, work.content_size * 2)

    result.archives = download.download_plan_files(
        client, work.id, plan.files, archive_dir, progress=progress
    )

    if not extract:
        return result

    archive_plan = archive.plan_archive(result.archives)
    if not archive_plan.is_extractable:
        return result

    if on_phase:
        # ここから先は外部コマンドに委ねる部分があり、途中で安全に止められない
        on_phase(PHASE_EXTRACT)

    output_dir = resolve_output_dir(cfg, work, install_root, current)
    result.output_dir = output_dir
    if not output_dir.exists():
        result.created_output = output_dir

    extracted = archive.extract(
        archive_plan,
        output_dir,
        flatten_single_root=cfg.flatten_single_root,
        strip_versions=cfg.strip_versions,
    )
    result.extracted = True
    result.flattened = extracted.flattened
    result.renamed = extracted.renamed
    result.removed_links = extracted.removed_links

    executables = archive.find_executables(output_dir)
    result.executable = executables[0] if executables else None

    result.entry = current.record(
        work,
        output_dir,
        executable=result.executable,
        serial_numbers=plan.serial_numbers,
    )

    if cfg.delete_archives and not keep_archives:
        result.freed = download.discard_cache(result.archives, archive_dir)

    return result


def register_to_steam(
    cfg: config_module.Config,
    userdata: Path,
    title: str,
    executable: Path,
    image: bytes | None,
    launch_options: str | None = None,
    keep_launch_options: bool = False,
    compat_tool: str | None = None,
) -> steam.RegistrationResult:
    """設定どおりの画像を添えて Steam に登録する。

    ``compat_tool`` を渡すと、設定の既定値の代わりにその Proton を割り当てる
    (自由登録で、ゲームごとに選んだもの)。

    設定から何を作って何を渡すかの組み立ては、コマンドラインにも Web UI にも同じものが要る。
    ここに置いていないと、項目が増えるたびに 3 か所を直して回ることになる
    (``cover_image`` を足したときに実際そうなった)。
    """
    cover_image, logo = cover.artwork(cfg, title, image, executable)
    return steam.register(
        userdata,
        title,
        executable,
        launch_options=launch_options,
        image=image,
        image_slots=cfg.steam_grid_slots,
        logo=logo,
        compat_tool=cfg.steam_compat_tool if compat_tool is None else compat_tool,
        cover_image=cover_image,
        keep_launch_options=keep_launch_options,
    )


def discard_partial(result: InstallResult) -> str:
    """中止・失敗したときに、その回で作ったものだけを片付ける。

    既にあった展開先は消さない。ダウンロードし直す途中で止めたときに、導入済みの
    ゲームまで消してしまわないため。
    """
    freed = 0
    if result.cache_dir and result.cache_dir.is_dir():
        files = [path for path in sorted(result.cache_dir.glob("*")) if path.is_file()]
        freed = download.discard_cache(files, result.cache_dir)

    removed_output = False
    if result.created_output and result.created_output.is_dir():
        shutil.rmtree(result.created_output, ignore_errors=True)
        removed_output = not result.created_output.exists()

    parts = []
    if freed:
        parts.append(f"取得済みの {download.format_size(freed)} を削除")
    if removed_output:
        parts.append("展開先を削除")
    return "、".join(parts) if parts else "削除するものはありませんでした"
