"""exe からのアイコン取り出しのテスト。

実物の exe に頼らず、最小限の PE ファイルを組み立てて検証する。
"""

from __future__ import annotations

import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dlsite_deck import winicon  # noqa: E402


def make_dib(width: int, height: int, bit_count: int, pixels: bytes, mask: bytes = b"") -> bytes:
    """アイコン用の DIB を組み立てる。height は XOR 画像の高さ。"""
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height * 2,  # XOR + AND
        1,
        bit_count,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return header + pixels + mask


def make_pe(resources: dict[int, list[bytes]]) -> bytes:
    """指定した種別のリソースだけを持つ最小の PE を作る。"""
    # リソースセクションを組み立てる (種別 -> 名前 -> 言語 の 3 階層)
    section_rva = 0x1000

    payloads = []
    for entries in resources.values():
        payloads.extend(entries)

    # 各階層の大きさを先に決める
    type_dir = 16 + 8 * len(resources)
    name_dirs = sum(16 + 8 * len(entries) for entries in resources.values())
    lang_dirs = sum(16 + 8 for entries in resources.values() for _ in entries)
    data_entries = 16 * len(payloads)

    name_base = type_dir
    lang_base = name_base + name_dirs
    entry_base = lang_base + lang_dirs
    payload_base = entry_base + data_entries

    blob = bytearray()

    # 種別ディレクトリ
    blob += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(resources))
    cursor_name = name_base
    for resource_type, entries in resources.items():
        blob += struct.pack("<II", resource_type, cursor_name | 0x80000000)
        cursor_name += 16 + 8 * len(entries)

    # 名前ディレクトリ
    cursor_lang = lang_base
    for entries in resources.values():
        blob += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, len(entries))
        for index in range(len(entries)):
            blob += struct.pack("<II", index + 1, cursor_lang | 0x80000000)
            cursor_lang += 16 + 8

    # 言語ディレクトリ
    cursor_entry = entry_base
    for entries in resources.values():
        for _ in entries:
            blob += struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1)
            blob += struct.pack("<II", 0x0411, cursor_entry)
            cursor_entry += 16

    # データエントリ
    cursor_payload = payload_base
    for payload in payloads:
        blob += struct.pack("<IIII", section_rva + cursor_payload, len(payload), 0, 0)
        cursor_payload += len(payload)

    for payload in payloads:
        blob += payload

    resource_section = bytes(blob)

    # PE ヘッダ
    pe_offset = 0x80
    optional_size = 96 + 16 * 8
    headers = bytearray(b"\x00" * pe_offset)
    headers[0:2] = b"MZ"
    headers[0x3C:0x40] = struct.pack("<I", pe_offset)

    coff = struct.pack("<4sHHIIIHH", b"PE\0\0", 0x8664, 1, 0, 0, 0, optional_size, 0)

    optional = bytearray(b"\x00" * optional_size)
    optional[0:2] = struct.pack("<H", 0x10B)  # PE32
    optional[92:96] = struct.pack("<I", 16)  # NumberOfRvaAndSizes
    optional[96 + 2 * 8 : 96 + 2 * 8 + 8] = struct.pack(
        "<II", section_rva, len(resource_section)
    )

    section_start = pe_offset + len(coff) + optional_size + 40
    raw_offset = ((section_start // 512) + 1) * 512
    section = struct.pack(
        "<8sIIIIIIHHI",
        b".rsrc\0\0\0",
        len(resource_section),
        section_rva,
        len(resource_section),
        raw_offset,
        0,
        0,
        0,
        0,
        0x40000040,
    )

    data = bytearray(headers + coff + bytes(optional) + section)
    data += b"\x00" * (raw_offset - len(data))
    data += resource_section
    return bytes(data)


class PeParsingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_picks_the_largest_icon(self):
        small = make_dib(2, 2, 32, b"\x00\x00\xff\xff" * 4)
        large = make_dib(4, 4, 32, b"\x00\xff\x00\xff" * 16)
        path = self._write("a.exe", make_pe({winicon.RT_ICON: [small, large]}))

        icons = winicon.list_icons(path)
        self.assertEqual([(i.width, i.height) for i in icons], [(4, 4), (2, 2)])

    def test_ignores_other_resource_types(self):
        icon = make_dib(2, 2, 32, b"\x00\x00\xff\xff" * 4)
        path = self._write(
            "b.exe", make_pe({winicon.RT_GROUP_ICON: [b"junk"], winicon.RT_ICON: [icon]})
        )
        self.assertEqual(len(winicon.list_icons(path)), 1)

    def test_no_resources(self):
        path = self._write("c.exe", make_pe({}))
        self.assertEqual(winicon.list_icons(path), [])
        self.assertIsNone(winicon.extract_largest_icon(path))

    def test_not_a_pe_file(self):
        path = self._write("d.exe", b"not a pe file at all")
        with self.assertRaises(winicon.IconError):
            winicon.list_icons(path)
        # 上位は例外ではなく None を受け取る
        self.assertIsNone(winicon.extract_largest_icon(path))


class ConversionTest(unittest.TestCase):
    def _decode(self, png: bytes) -> tuple[int, int, bytes]:
        """PNG を検証しつつ RGBA の並びに戻す。"""
        self.assertEqual(png[:8], winicon.PNG_SIGNATURE)
        position = 8
        idat = b""
        width = height = 0

        while position < len(png):
            (length,) = struct.unpack_from(">I", png, position)
            tag = png[position + 4 : position + 8]
            body = png[position + 8 : position + 8 + length]
            (crc,) = struct.unpack_from(">I", png, position + 8 + length)
            self.assertEqual(crc, zlib.crc32(tag + body) & 0xFFFFFFFF, f"{tag} の CRC")

            if tag == b"IHDR":
                width, height, depth, color_type = struct.unpack(">IIBB", body[:10])
                self.assertEqual((depth, color_type), (8, 6), "8bit RGBA であること")
            elif tag == b"IDAT":
                idat += body
            position += 12 + length

        raw = zlib.decompress(idat)
        self.assertEqual(len(raw), height * (1 + width * 4))

        pixels = bytearray()
        for y in range(height):
            start = y * (1 + width * 4)
            self.assertEqual(raw[start], 0, "フィルタは 0 のみ")
            pixels += raw[start + 1 : start + 1 + width * 4]
        return width, height, bytes(pixels)

    def test_png_icon_passes_through(self):
        source = winicon._write_png(1, 1, b"\x01\x02\x03\x04")
        icon = winicon.IconImage(1, 1, 32, source)
        self.assertIs(winicon.to_png(icon), source)

    def test_32bpp_keeps_alpha_and_channel_order(self):
        # DIB は BGRA、かつ下から上に並ぶ
        bottom = bytes((0x11, 0x22, 0x33, 0x44))  # B,G,R,A
        top = bytes((0xAA, 0xBB, 0xCC, 0xFF))
        icon = winicon.IconImage(1, 2, 32, make_dib(1, 2, 32, bottom + top))

        width, height, pixels = self._decode(winicon.to_png(icon))
        self.assertEqual((width, height), (1, 2))
        # 出力は上から下。RGBA に並び替わっていること。
        self.assertEqual(pixels[0:4], bytes((0xCC, 0xBB, 0xAA, 0xFF)))
        self.assertEqual(pixels[4:8], bytes((0x33, 0x22, 0x11, 0x44)))

    def test_24bpp_uses_and_mask_for_transparency(self):
        # 2x1 の 24bpp。行は 4 バイト境界に揃える。
        row = bytes((0x11, 0x22, 0x33, 0x44, 0x55, 0x66)) + b"\x00\x00"
        # AND マスク: 左の画素だけ透過
        mask = bytes((0b10000000,)) + b"\x00\x00\x00"
        icon = winicon.IconImage(2, 1, 24, make_dib(2, 1, 24, row, mask))

        _width, _height, pixels = self._decode(winicon.to_png(icon))
        self.assertEqual(pixels[3], 0, "マスクされた画素は透過")
        self.assertEqual(pixels[7], 255, "マスクされていない画素は不透明")
        self.assertEqual(pixels[4:7], bytes((0x66, 0x55, 0x44)), "BGR -> RGB")

    def test_8bpp_uses_palette(self):
        palette = bytes((0x10, 0x20, 0x30, 0)) + bytes((0x40, 0x50, 0x60, 0))
        header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 8, 0, 0, 0, 0, 2, 0)
        pixels = bytes((0, 1, 0, 0))  # 1 行 = 2 画素 + 詰め物
        mask = b"\x00\x00\x00\x00"
        icon = winicon.IconImage(2, 1, 8, header + palette + pixels + mask)

        _width, _height, decoded = self._decode(winicon.to_png(icon))
        self.assertEqual(decoded[0:3], bytes((0x30, 0x20, 0x10)))
        self.assertEqual(decoded[4:7], bytes((0x60, 0x50, 0x40)))

    def test_unsupported_depth(self):
        icon = winicon.IconImage(1, 1, 16, make_dib(1, 1, 16, b"\x00\x00"))
        with self.assertRaises(winicon.IconError):
            winicon.to_png(icon)


if __name__ == "__main__":
    unittest.main()
