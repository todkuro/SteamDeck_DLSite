"""日本語タイトルをヘボン式ローマ字のディレクトリ名に変換する。

仮名の変換は標準ライブラリだけで完結する。漢字の読みは辞書が要るため、
``pykakasi`` が入っていればそれを使い、無ければ英語タイトル → 仮名部分のみの
変換 → 作品 ID の順にフォールバックする。どの経路を通ったかは
:func:`romanize_work` の戻り値で分かるようにしてある。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 拗音・外来音を含む 2〜3 文字の並びは先に引き当てる必要があるため、
# 表はカタカナに正規化したうえで最長一致で走査する。
_DIGRAPHS = {
    "キャ": "kya", "キュ": "kyu", "キョ": "kyo",
    "ギャ": "gya", "ギュ": "gyu", "ギョ": "gyo",
    "シャ": "sha", "シュ": "shu", "ショ": "sho", "シェ": "she",
    "ジャ": "ja", "ジュ": "ju", "ジョ": "jo", "ジェ": "je",
    "チャ": "cha", "チュ": "chu", "チョ": "cho", "チェ": "che",
    "ヂャ": "ja", "ヂュ": "ju", "ヂョ": "jo",
    "ニャ": "nya", "ニュ": "nyu", "ニョ": "nyo",
    "ヒャ": "hya", "ヒュ": "hyu", "ヒョ": "hyo",
    "ビャ": "bya", "ビュ": "byu", "ビョ": "byo",
    "ピャ": "pya", "ピュ": "pyu", "ピョ": "pyo",
    "ミャ": "mya", "ミュ": "myu", "ミョ": "myo",
    "リャ": "rya", "リュ": "ryu", "リョ": "ryo",
    "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo", "フュ": "fyu",
    "ヴァ": "va", "ヴィ": "vi", "ヴェ": "ve", "ヴォ": "vo", "ヴュ": "vyu",
    "ウィ": "wi", "ウェ": "we", "ウォ": "wo",
    "ティ": "ti", "ディ": "di", "トゥ": "tu", "ドゥ": "du",
    "ツァ": "tsa", "ツィ": "tsi", "ツェ": "tse", "ツォ": "tso",
    "クァ": "kwa", "クィ": "kwi", "クェ": "kwe", "クォ": "kwo",
    "グァ": "gwa",
    "イェ": "ye",
}

_MONOGRAPHS = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヰ": "i", "ヱ": "e", "ヲ": "o",
    "ヴ": "vu",
    # 単独で現れた小書き仮名
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
    "ャ": "ya", "ュ": "yu", "ョ": "yo", "ヮ": "wa",
}

_VOWELS = set("aiueo")
# ヘボン式では撥音「ン」は b/m/p の前で m になる
_BILABIALS = set("bmp")

_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")


@dataclass(frozen=True)
class RomanizationResult:
    """ローマ字化の結果と、どの経路で得たか。"""

    text: str
    #: "english" | "pykakasi" | "kana" | "fallback"
    source: str
    #: 読みを解決できない漢字が残ったか
    incomplete: bool = False


def to_katakana(text: str) -> str:
    """ひらがなをカタカナに寄せ、全角英数を半角にする。"""
    # NFKC で全角英数字・記号を半角に畳む
    text = unicodedata.normalize("NFKC", text)
    return "".join(
        chr(ord(char) + 0x60) if "ぁ" <= char <= "ゖ" else char for char in text
    )


def kana_to_romaji(text: str, long_vowel: str = "repeat") -> str:
    """仮名列をヘボン式ローマ字に変換する。仮名以外はそのまま残す。

    ``long_vowel`` は長音符「ー」の扱い。``"repeat"`` は直前の母音を繰り返し、
    ``"drop"`` は捨てる。
    """
    source = to_katakana(text)
    out: list[str] = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        # 促音: 次の音の頭子音を重ねる。ch の前だけは t を重ねるのがヘボン式。
        if char == "ッ":
            following = _next_romaji(source, index + 1, long_vowel)
            if following:
                head = following[0]
                out.append("t" if following.startswith("ch") else head if head not in _VOWELS else "")
            index += 1
            continue

        # 撥音: 後続音で n / n' / m を使い分ける
        if char == "ン":
            following = _next_romaji(source, index + 1, long_vowel)
            if following and following[0] in _BILABIALS:
                out.append("m")
            elif following and (following[0] in _VOWELS or following[0] == "y"):
                out.append("n'")
            else:
                out.append("n")
            index += 1
            continue

        if char == "ー":
            previous = out[-1][-1] if out and out[-1] else ""
            if long_vowel == "repeat" and previous in _VOWELS:
                out.append(previous)
            index += 1
            continue

        digraph = source[index : index + 2]
        if digraph in _DIGRAPHS:
            out.append(_DIGRAPHS[digraph])
            index += 2
            continue

        if char in _MONOGRAPHS:
            out.append(_MONOGRAPHS[char])
            index += 1
            continue

        # 仮名以外 (英字・数字・記号・漢字) はそのまま通す
        out.append(char)
        index += 1

    return "".join(out)


def _next_romaji(source: str, index: int, long_vowel: str) -> str:
    """促音・撥音の判定のために、直後 1 音だけを先読みして変換する。"""
    if index >= len(source):
        return ""
    digraph = source[index : index + 2]
    if digraph in _DIGRAPHS:
        return _DIGRAPHS[digraph]
    char = source[index]
    if char in _MONOGRAPHS:
        return _MONOGRAPHS[char]
    # 仮名以外が続く場合は子音扱いしない
    return char.lower() if char.isascii() and char.isalpha() else ""


def has_kanji(text: str) -> bool:
    return bool(_KANJI.search(text))


def has_japanese(text: str) -> bool:
    return bool(_KANJI.search(text) or _KANA.search(text))


def _pykakasi_romanize(text: str) -> str | None:
    """pykakasi が使えるなら漢字込みでヘボン式に変換する。"""
    try:
        import pykakasi  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        converter = pykakasi.kakasi()
        segments = converter.convert(text)
    except Exception:  # pykakasi の版差による失敗は握って諦める
        return None

    return " ".join(segment.get("hepburn", "") for segment in segments if segment.get("hepburn"))


def romanize(text: str, long_vowel: str = "repeat") -> RomanizationResult:
    """任意の文字列をローマ字化する。"""
    text = text.strip()
    if not text:
        return RomanizationResult(text="", source="fallback", incomplete=True)

    if not has_japanese(text):
        # もともと英字タイトル
        return RomanizationResult(text=text, source="english")

    converted = _pykakasi_romanize(text)
    if converted:
        return RomanizationResult(text=converted, source="pykakasi")

    converted = kana_to_romaji(text, long_vowel=long_vowel)
    return RomanizationResult(
        text=converted,
        source="kana",
        incomplete=has_kanji(converted),
    )


def romanize_work(
    title: str,
    english_title: str | None = None,
    work_id: str = "",
    long_vowel: str = "repeat",
) -> RomanizationResult:
    """作品タイトルをディレクトリ名向けにローマ字化する。

    漢字を含むタイトルは pykakasi が無いと読みを解決できないため、
    その場合は DLsite が持つ英語タイトルを優先して使う。
    """
    if has_japanese(title):
        converted = _pykakasi_romanize(title)
        if converted:
            return RomanizationResult(text=converted, source="pykakasi")

        if english_title and not has_japanese(english_title):
            return RomanizationResult(text=english_title, source="english")

        result = romanize(title, long_vowel=long_vowel)
        if result.incomplete:
            # 漢字が残るなら読める名前にならないので作品 ID を混ぜる
            remainder = sanitize(_KANJI.sub("", result.text))
            text = f"{remainder}_{work_id}".strip("_") if remainder else work_id
            return RomanizationResult(text=text or work_id, source="fallback", incomplete=True)
        return result

    if title:
        return RomanizationResult(text=title, source="english")

    return RomanizationResult(text=work_id, source="fallback", incomplete=True)


# ファイル名に使えない文字と、詰めたい記号
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SEPARATORS = re.compile(r"[\s_\-–—~･・、。！!？?,，．.（）()\[\]【】「」『』\"'’”:;+=&#%@$^`{}]+")


def sanitize(name: str, max_length: int = 96) -> str:
    """ディレクトリ名として安全な ASCII 寄りの文字列に整える。"""
    name = unicodedata.normalize("NFKC", name)
    name = _UNSAFE.sub(" ", name)
    # 記号・空白の連続をアンダースコア 1 個にまとめる
    name = _SEPARATORS.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_. ")

    if len(name) > max_length:
        name = name[:max_length].rstrip("_. ")

    return name


def directory_name(
    title: str,
    english_title: str | None = None,
    work_id: str = "",
    template: str = "{romaji}",
    long_vowel: str = "repeat",
) -> str:
    """作品の展開先ディレクトリ名を決める。

    ``template`` では ``{romaji}`` と ``{id}`` が使える。
    """
    result = romanize_work(
        title, english_title=english_title, work_id=work_id, long_vowel=long_vowel
    )
    romaji = sanitize(result.text) or work_id
    name = sanitize(template.format(romaji=romaji, id=work_id))
    return name or work_id or "untitled"
