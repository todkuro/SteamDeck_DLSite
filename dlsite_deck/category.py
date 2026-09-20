"""作品種別 (``work_type``) をカテゴリにまとめる。

DLsite の ``work_type`` は細かいので、一覧の絞り込みに使いやすい粒度に畳む。
既定の表示はゲームのみで、それ以外はカテゴリを切り替えて表示する。
"""

from __future__ import annotations

from dataclasses import dataclass

#: カテゴリの識別子
GAME = "game"
MANGA = "manga"
CG = "cg"
NOVEL = "novel"
VOICE = "voice"
MUSIC = "music"
VIDEO = "video"
TOOL = "tool"
OTHER = "other"


@dataclass(frozen=True)
class Category:
    """一覧の絞り込みに使うカテゴリ。"""

    key: str
    label: str


#: 表示順に並べたカテゴリ一覧。UI のプルダウンはこの順で並べる。
CATEGORIES: tuple[Category, ...] = (
    Category(GAME, "ゲーム"),
    Category(MANGA, "マンガ"),
    Category(CG, "CG・イラスト"),
    Category(NOVEL, "ノベル"),
    Category(VOICE, "ボイス・ASMR"),
    Category(MUSIC, "音楽"),
    Category(VIDEO, "動画"),
    Category(TOOL, "ツール・素材"),
    Category(OTHER, "その他"),
)

CATEGORY_LABELS = {category.key: category.label for category in CATEGORIES}

#: ``work_type`` からカテゴリへの対応。
#: 出典は DLsite の作品種別コード。ここに無いものは file_type で判定する。
WORK_TYPE_CATEGORIES = {
    # ゲーム
    "GAM": GAME,  # ゲーム (総称)
    "ACN": GAME,  # アクション
    "QIZ": GAME,  # クイズ
    "ADV": GAME,  # アドベンチャー
    "RPG": GAME,
    "TBL": GAME,  # テーブル
    "DNV": GAME,  # デジタルノベル
    "SLN": GAME,  # シミュレーション
    "TYP": GAME,  # タイピング
    "STG": GAME,  # シューティング
    "PZL": GAME,  # パズル
    # 読み物
    "MNG": MANGA,
    "ICG": CG,
    "NRE": NOVEL,
    # 音もの
    "SOU": VOICE,  # ボイス・ASMR
    "AMT": VOICE,
    "MUS": MUSIC,
    # 映像
    "MOV": VIDEO,
    # 制作物
    "IMT": TOOL,  # 素材
    "TOL": TOOL,  # ツール・アクセサリ
}

#: ``work_type`` が "ETC" (その他) の作品を ``file_type`` で振り分ける。
#: 実ライブラリでは ETC がイラスト集に使われていた。
FILE_TYPE_CATEGORIES = {
    "IME": CG,  # 画像
    "ET1": CG,
    "PDF": MANGA,
    "MP3": VOICE,
    "WAV": VOICE,
    "MP4": VIDEO,
    "MOV": VIDEO,
}


def classify(work_type: str | None, file_type: str | None = None) -> str:
    """作品種別からカテゴリを決める。"""
    if work_type:
        category = WORK_TYPE_CATEGORIES.get(work_type.upper())
        if category:
            return category

    if file_type:
        category = FILE_TYPE_CATEGORIES.get(file_type.upper())
        if category:
            return category

    return OTHER


def label(key: str) -> str:
    """カテゴリの表示名。"""
    return CATEGORY_LABELS.get(key, key)


def is_valid(key: str) -> bool:
    return key in CATEGORY_LABELS
