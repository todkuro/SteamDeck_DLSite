"""Windows 実行ファイルからアイコンを取り出す。

Steam のロゴ画像は PNG しか受け付けないので、exe のリソースにある一番大きな
アイコンを探して PNG として書き出す。標準ライブラリだけで完結させるため、
PE ファイルの解析と PNG の組み立ても自前で行う。

対応しているアイコンの中身:

* PNG (Vista 以降の 256x256 アイコンはこの形式) — そのまま使う
* DIB 32bpp (BGRA) — アルファをそのまま使う
* DIB 24bpp / 8bpp / 4bpp / 1bpp — AND マスクを透過に変換する
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

#: リソースの種別
RT_ICON = 3
RT_GROUP_ICON = 14

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class IconError(RuntimeError):
    """アイコンを取り出せなかった。"""


@dataclass
class IconImage:
    """exe から取り出したアイコン 1 枚。"""

    width: int
    height: int
    bit_count: int
    #: リソースに入っていた生データ (PNG か DIB)
    data: bytes

    @property
    def is_png(self) -> bool:
        return self.data[:8] == PNG_SIGNATURE

    @property
    def pixels(self) -> int:
        return self.width * self.height


# ----------------------------------------------------------------------
# PE の解析
# ----------------------------------------------------------------------


class _PeFile:
    """リソースを読むのに必要な範囲だけの PE パーサー。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.sections: list[tuple[int, int, int, int]] = []
        self.resource_rva = 0
        self._parse_headers()

    def _parse_headers(self) -> None:
        data = self.data
        if data[:2] != b"MZ":
            raise IconError("Windows 実行ファイルではありません。")

        (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
        if data[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise IconError("PE ヘッダーが見つかりません。")

        coff = pe_offset + 4
        (section_count,) = struct.unpack_from("<H", data, coff + 2)
        (optional_size,) = struct.unpack_from("<H", data, coff + 16)

        optional = coff + 20
        (magic,) = struct.unpack_from("<H", data, optional)
        # PE32 は 96 バイト目、PE32+ は 112 バイト目からデータディレクトリ
        if magic == 0x10B:
            directories = optional + 96
        elif magic == 0x20B:
            directories = optional + 112
        else:
            raise IconError(f"未知の PE 形式です: 0x{magic:04x}")

        (directory_count,) = struct.unpack_from("<I", data, directories - 4)
        if directory_count > 2:
            (self.resource_rva,) = struct.unpack_from("<I", data, directories + 2 * 8)

        section_table = optional + optional_size
        for index in range(section_count):
            base = section_table + index * 40
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
                "<II II", data, base + 8
            )
            self.sections.append((virtual_address, virtual_size, raw_size, raw_offset))

    def offset_of(self, rva: int) -> int:
        """RVA をファイル内オフセットに直す。"""
        for virtual_address, virtual_size, raw_size, raw_offset in self.sections:
            size = max(virtual_size, raw_size)
            if virtual_address <= rva < virtual_address + size:
                return raw_offset + (rva - virtual_address)
        raise IconError(f"RVA {rva:#x} を含むセクションがありません。")


def _leaf_offsets(pe: _PeFile, root: int, offset: int, depth: int = 0):
    """リソースディレクトリを辿り、末端のデータエントリ位置を列挙する。"""
    if depth > 8:  # 壊れたファイルで無限に潜らないための保険
        return

    data = pe.data
    named, ids = struct.unpack_from("<HH", data, offset + 12)

    for index in range(named + ids):
        entry = offset + 16 + index * 8
        _name_value, data_value = struct.unpack_from("<II", data, entry)

        if data_value & 0x80000000:
            yield from _leaf_offsets(
                pe, root, root + (data_value & 0x7FFFFFFF), depth + 1
            )
        else:
            yield root + data_value


def _resource_entries(pe: _PeFile, resource_type: int) -> list[bytes]:
    """指定した種別のリソースの中身をすべて返す。"""
    if not pe.resource_rva:
        return []

    root = pe.offset_of(pe.resource_rva)
    data = pe.data
    named, ids = struct.unpack_from("<HH", data, root + 12)

    entries: list[bytes] = []

    for index in range(named + ids):
        entry = root + 16 + index * 8
        name_value, data_value = struct.unpack_from("<II", data, entry)
        if name_value & 0x80000000 or name_value != resource_type:
            continue
        if not data_value & 0x80000000:
            continue

        # 種別 -> 名前 -> 言語 の 3 階層をたどる
        for position in _leaf_offsets(pe, root, root + (data_value & 0x7FFFFFFF)):
            payload_rva, size = struct.unpack_from("<II", data, position)
            start = pe.offset_of(payload_rva)
            entries.append(data[start : start + size])

    return entries


# ----------------------------------------------------------------------
# アイコンの取り出し
# ----------------------------------------------------------------------


def _describe(payload: bytes) -> IconImage | None:
    """アイコンの中身から寸法を読む。"""
    if payload[:8] == PNG_SIGNATURE:
        if len(payload) < 24:
            return None
        width, height = struct.unpack_from(">II", payload, 16)
        return IconImage(width=width, height=height, bit_count=32, data=payload)

    if len(payload) < 40:
        return None

    header_size, width, height, _planes, bit_count = struct.unpack_from(
        "<IiiHH", payload, 0
    )
    if header_size < 40:
        return None

    # DIB の高さは XOR 画像と AND マスクの合計になっている
    return IconImage(
        width=width, height=abs(height) // 2, bit_count=bit_count, data=payload
    )


def list_icons(path: Path) -> list[IconImage]:
    """exe に入っているアイコンを大きい順に返す。"""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise IconError(f"実行ファイルを読めませんでした: {path} ({error})") from error

    pe = _PeFile(data)
    icons = []
    for payload in _resource_entries(pe, RT_ICON):
        image = _describe(payload)
        if image and image.width > 0 and image.height > 0:
            icons.append(image)

    # 面積が同じなら色数が多いほうを優先する
    icons.sort(key=lambda icon: (icon.pixels, icon.bit_count), reverse=True)
    return icons


def extract_largest_icon(path: Path) -> bytes | None:
    """一番大きなアイコンを PNG にして返す。見つからなければ None。"""
    try:
        icons = list_icons(path)
    except IconError:
        return None

    for icon in icons:
        try:
            return to_png(icon)
        except IconError:
            continue
    return None


def to_png(icon: IconImage) -> bytes:
    """アイコンを PNG (RGBA) に変換する。"""
    if icon.is_png:
        return icon.data
    return _write_png(icon.width, icon.height, _dib_to_rgba(icon))


# ----------------------------------------------------------------------
# DIB -> RGBA
# ----------------------------------------------------------------------


def _dib_to_rgba(icon: IconImage) -> bytes:
    """DIB を上から下に並んだ RGBA の並びに変換する。"""
    data = icon.data
    (header_size,) = struct.unpack_from("<I", data, 0)
    width, height, bit_count = icon.width, icon.height, icon.bit_count

    if bit_count not in (1, 4, 8, 24, 32):
        raise IconError(f"未対応の色深度です: {bit_count}bpp")

    # パレット (色数が指定されていなければ色深度から決まる)
    (colors_used,) = struct.unpack_from("<I", data, 32)
    if bit_count <= 8:
        palette_size = colors_used or (1 << bit_count)
    else:
        palette_size = colors_used

    palette_start = header_size
    palette = []
    for index in range(palette_size):
        base = palette_start + index * 4
        blue, green, red, _reserved = data[base : base + 4]
        palette.append((red, green, blue))

    xor_start = palette_start + palette_size * 4
    xor_stride = ((width * bit_count + 31) // 32) * 4
    and_stride = ((width + 31) // 32) * 4
    and_start = xor_start + xor_stride * height

    has_and_mask = len(data) >= and_start + and_stride * height
    rows = []

    for y in range(height):
        # DIB は下から上に並ぶ
        source = height - 1 - y
        xor_row = xor_start + xor_stride * source
        and_row = and_start + and_stride * source
        row = bytearray()

        for x in range(width):
            if bit_count == 32:
                base = xor_row + x * 4
                blue, green, red, alpha = data[base : base + 4]
            elif bit_count == 24:
                base = xor_row + x * 3
                blue, green, red = data[base : base + 3]
                alpha = 255
            else:
                index = _palette_index(data, xor_row, x, bit_count)
                red, green, blue = palette[index] if index < len(palette) else (0, 0, 0)
                alpha = 255

            if bit_count != 32 and has_and_mask:
                # AND マスクのビットが立っている画素は透過
                byte = data[and_row + (x >> 3)]
                if byte & (0x80 >> (x & 7)):
                    alpha = 0

            row += bytes((red, green, blue, alpha))

        rows.append(bytes(row))

    # 32bpp なのにアルファがすべて 0 の古いアイコンは不透明として扱う
    if bit_count == 32 and not any(row[3::4].strip(b"\x00") for row in rows):
        rows = [_force_opaque(row, and_start, and_stride, width, height, index, data)
                for index, row in enumerate(rows)]

    return b"".join(rows)


def _force_opaque(
    row: bytes,
    and_start: int,
    and_stride: int,
    width: int,
    height: int,
    y: int,
    data: bytes,
) -> bytes:
    """アルファが空の 32bpp アイコンを AND マスクで補う。"""
    out = bytearray(row)
    source = height - 1 - y
    and_row = and_start + and_stride * source
    has_mask = len(data) >= and_row + and_stride

    for x in range(width):
        transparent = False
        if has_mask:
            byte = data[and_row + (x >> 3)]
            transparent = bool(byte & (0x80 >> (x & 7)))
        out[x * 4 + 3] = 0 if transparent else 255

    return bytes(out)


def _palette_index(data: bytes, row_start: int, x: int, bit_count: int) -> int:
    if bit_count == 8:
        return data[row_start + x]
    if bit_count == 4:
        byte = data[row_start + (x >> 1)]
        return (byte >> 4) if x % 2 == 0 else (byte & 0x0F)
    byte = data[row_start + (x >> 3)]
    return (byte >> (7 - (x & 7))) & 1


# ----------------------------------------------------------------------
# PNG の書き出し
# ----------------------------------------------------------------------


def _write_png(width: int, height: int, rgba: bytes) -> bytes:
    """RGBA の並びから PNG を組み立てる。"""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        # フィルタ種別 0 (なし)
        raw.append(0)
        raw += rgba[y * stride : (y + 1) * stride]

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
