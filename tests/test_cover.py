"""タイトル入りカバーとロゴの組み立て。

rsvg-convert が無い環境でも走るよう、実際の描画を伴う確認は分けてある。
"""

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dlsite_deck import cover, steam  # noqa: E402

#: 1x1 の JPEG (SOI + ほんの少し)。中身は見ないので先頭バイトだけ意味がある。
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@dataclass
class FakeConfig:
    steam_cover_title: bool = True
    steam_logo_source: str = "title"


class WidthTest(unittest.TestCase):
    def test_full_width_counts_as_one(self):
        self.assertEqual(cover.char_width("あ"), 1.0)
        self.assertEqual(cover.char_width("漢"), 1.0)

    def test_half_width_counts_as_half(self):
        self.assertEqual(cover.char_width("a"), 0.5)
        self.assertEqual(cover.char_width("1"), 0.5)

    def test_ambiguous_treated_as_full_width(self):
        # 日本語のタイトルに混じる記号。全角として数えないと行が溢れる。
        self.assertEqual(cover.char_width("★"), 1.0)


class WrapTest(unittest.TestCase):
    def test_wraps_every_ten_full_width_chars(self):
        title = "あ" * 25
        self.assertEqual(cover.wrap(title), ["あ" * 10, "あ" * 10, "あ" * 5])

    def test_half_width_fits_twice_as_many(self):
        self.assertEqual(cover.wrap("a" * 20), ["a" * 20])

    def test_mixed_width(self):
        # 全角 5 + 半角 10 = ちょうど 10 文字ぶん
        self.assertEqual(cover.wrap("あいうえお" + "a" * 10), ["あいうえおaaaaaaaaaa"])

    def test_empty_title_yields_one_empty_line(self):
        self.assertEqual(cover.wrap(""), [""])

    def test_single_char_over_limit_is_not_dropped(self):
        self.assertEqual(cover.wrap("あ" * 11), ["あ" * 10, "あ"])


class SvgTest(unittest.TestCase):
    def test_cover_svg_has_center_crop(self):
        svg = cover.cover_svg(JPEG, "テスト", "image/jpeg")
        # 中心基準で切り出し、短い辺に合わせて拡大する指定
        self.assertIn('preserveAspectRatio="xMidYMid slice"', svg)
        self.assertIn(f'width="{cover.COVER_WIDTH}"', svg)
        self.assertIn(f'height="{cover.COVER_HEIGHT}"', svg)

    def test_cover_svg_embeds_image(self):
        svg = cover.cover_svg(JPEG, "テスト", "image/jpeg")
        self.assertIn("data:image/jpeg;base64,", svg)

    def test_text_is_white_with_black_outline(self):
        svg = cover.cover_svg(JPEG, "テスト", "image/jpeg")
        self.assertIn('fill="#ffffff"', svg)
        self.assertIn('stroke="#000000"', svg)
        # 縁を先に描かないと字の内側が削れる
        self.assertIn('paint-order="stroke"', svg)

    def test_plate_is_translucent_white(self):
        svg = cover.cover_svg(JPEG, "テスト", "image/jpeg")
        self.assertIn(f'fill-opacity="{cover.PLATE_OPACITY}"', svg)

    def test_ten_chars_fit_inside_the_padding(self):
        svg = cover.cover_svg(JPEG, "あ" * 10, "image/jpeg")
        size = (cover.COVER_WIDTH - cover.SIDE_PADDING * 2) / cover.CHARS_PER_LINE
        self.assertIn(f'font-size="{size:.2f}"', svg)
        # 10 文字ぶんの幅が、左右の余白を除いた幅に収まる
        self.assertLessEqual(size * 10, cover.COVER_WIDTH - cover.SIDE_PADDING * 2)

    def test_long_title_shrinks_to_fit(self):
        """行数が増えすぎたら文字を縮める。カバーが文字で埋まらないように。"""
        svg = cover.cover_svg(JPEG, "あ" * 200, "image/jpeg")
        size = (cover.COVER_WIDTH - cover.SIDE_PADDING * 2) / cover.CHARS_PER_LINE
        self.assertNotIn(f'font-size="{size:.2f}"', svg)

    def test_special_characters_are_escaped(self):
        svg = cover.cover_svg(JPEG, '<&">', "image/jpeg")
        self.assertIn("&lt;&amp;&quot;&gt;", svg)
        self.assertNotIn('<&">', svg)

    def test_logo_svg_is_one_line(self):
        svg, width, height = cover.logo_svg("あ" * 30)
        self.assertEqual(svg.count("<text"), 1)
        # 折り返さないぶん横に伸びる
        self.assertGreater(width, cover.COVER_WIDTH)
        self.assertLess(height, 200)

    def test_logo_width_follows_title_length(self):
        _svg, short, _h = cover.logo_svg("あい")
        _svg, long, _h = cover.logo_svg("あ" * 20)
        self.assertGreater(long, short)


class BuildTest(unittest.TestCase):
    def test_rejects_unknown_image_format(self):
        self.assertIsNone(cover.build_cover(b"GIF89a" + b"\x00" * 16, "テスト"))

    def test_blank_title_is_refused(self):
        self.assertIsNone(cover.build_cover(JPEG, "   "))
        self.assertIsNone(cover.build_logo(""))

    def test_no_image_is_refused(self):
        self.assertIsNone(cover.build_cover(b"", "テスト"))


class ArtworkTest(unittest.TestCase):
    """設定に応じて何を作るかの判断。描画そのものは差し替える。"""

    def setUp(self):
        self.calls = []
        self._cover, self._logo = cover.build_cover, cover.build_logo
        cover.build_cover = lambda image, title: self.calls.append(("cover", title)) or b"C"
        cover.build_logo = lambda title: self.calls.append(("logo", title)) or b"L"

    def tearDown(self):
        cover.build_cover, cover.build_logo = self._cover, self._logo

    def test_title_logo_and_cover(self):
        image, logo = cover.artwork(FakeConfig(), "タイトル", JPEG)
        self.assertEqual((image, logo), (b"C", b"L"))
        self.assertEqual(self.calls, [("cover", "タイトル"), ("logo", "タイトル")])

    def test_cover_disabled(self):
        image, logo = cover.artwork(FakeConfig(steam_cover_title=False), "T", JPEG)
        self.assertIsNone(image)
        self.assertEqual(logo, b"L")

    def test_logo_none(self):
        image, logo = cover.artwork(FakeConfig(steam_logo_source="none"), "T", JPEG)
        self.assertEqual(image, b"C")
        self.assertIsNone(logo)

    def test_no_source_image_means_no_cover(self):
        image, _logo = cover.artwork(FakeConfig(), "T", None)
        self.assertIsNone(image)

    def test_exe_source_does_not_build_title_logo(self):
        _image, logo = cover.artwork(
            FakeConfig(steam_logo_source="exe"), "T", JPEG, Path("__no_such_exe__")
        )
        self.assertIsNone(logo)
        self.assertNotIn(("logo", "T"), self.calls)


class RegisterSlotTest(unittest.TestCase):
    """タイトル入りカバーは縦長スロットにだけ使う。"""

    def test_cover_slot_constant(self):
        self.assertEqual(steam.COVER_SLOT, "cover")
        self.assertIn(steam.COVER_SLOT, steam.GRID_SLOTS)

    def test_cover_and_capsule_have_different_names(self):
        directory = Path("x")
        cover_paths = steam._grid_paths(directory, 1, steam.GRID_SLOTS["cover"])
        capsule_paths = steam._grid_paths(directory, 1, steam.GRID_SLOTS["capsule"])
        self.assertTrue(set(cover_paths).isdisjoint(capsule_paths))


class DefaultTest(unittest.TestCase):
    """描けない環境では、既定で埋め込まないこと。

    豆腐 (□) が並んだカバーを作ってしまわないようにする。
    """

    def setUp(self):
        self._supported = cover.title_supported
        self.addCleanup(self._restore)

    def _restore(self):
        cover.title_supported = self._supported

    def _default(self):
        from dlsite_deck import config as config_module

        return config_module.Config().steam_cover_title

    def test_off_when_it_cannot_be_drawn(self):
        cover.title_supported = lambda: False
        self.assertFalse(self._default())

    def test_on_when_it_can(self):
        cover.title_supported = lambda: True
        self.assertTrue(self._default())

    def test_the_setting_file_still_wins(self):
        """環境で決まるのは既定だけ。書いてあればそちらを使う。"""
        import json
        import tempfile
        from pathlib import Path as P

        from dlsite_deck import config as config_module

        cover.title_supported = lambda: False
        with tempfile.TemporaryDirectory() as name:
            path = P(name) / "config.json"
            path.write_text(json.dumps({"steam_cover_title": True}), encoding="utf-8")
            self.assertTrue(config_module.load(path).steam_cover_title)

    def test_needs_both_the_command_and_a_font(self):
        real_available, real_font = cover.available, cover.can_draw_japanese
        try:
            cover.title_supported.cache_clear()
            cover.available = lambda: True
            cover.can_draw_japanese = lambda: False
            self.assertFalse(cover.title_supported())

            cover.title_supported.cache_clear()
            cover.available = lambda: False
            cover.can_draw_japanese = lambda: True
            self.assertFalse(cover.title_supported())
        finally:
            cover.available, cover.can_draw_japanese = real_available, real_font
            cover.title_supported.cache_clear()

class FontProbeTest(unittest.TestCase):
    """日本語フォントの有無を fontconfig に尋ねる。"""

    def setUp(self):
        self.real_available = cover.available
        self.real_which = cover.shutil.which
        self.real_run = cover.subprocess.run
        cover.available = lambda: True
        cover.can_draw_japanese.cache_clear()
        self.addCleanup(self._restore)

    def _restore(self):
        cover.available = self.real_available
        cover.shutil.which = self.real_which
        cover.subprocess.run = self.real_run
        cover.can_draw_japanese.cache_clear()

    def _answer(self, stdout, returncode=0):
        cover.shutil.which = lambda name, path=None: "/usr/bin/fc-list"

        class Result:
            pass

        result = Result()
        result.stdout, result.returncode = stdout, returncode
        cover.subprocess.run = lambda *a, **k: result

    def test_fonts_found(self):
        self._answer(b"Noto Sans CJK JP\n")
        self.assertTrue(cover.can_draw_japanese())

    def test_no_japanese_font(self):
        self._answer(b"")
        self.assertFalse(cover.can_draw_japanese())

    def test_whitespace_only_counts_as_none(self):
        self._answer(b"  \n \n")
        self.assertFalse(cover.can_draw_japanese())

    def test_without_fc_list_we_do_not_block(self):
        """判断できないなら通す。描けるものを勝手に止めない。"""
        cover.shutil.which = lambda name, path=None: None
        self.assertTrue(cover.can_draw_japanese())

    def test_fc_list_failing_does_not_block(self):
        self._answer(b"", returncode=1)
        self.assertTrue(cover.can_draw_japanese())

    def test_fc_list_blowing_up_does_not_block(self):
        cover.shutil.which = lambda name, path=None: "/usr/bin/fc-list"

        def explode(*a, **k):
            raise OSError("boom")

        cover.subprocess.run = explode
        self.assertTrue(cover.can_draw_japanese())

    def test_no_renderer_means_no(self):
        cover.available = lambda: False
        self.assertFalse(cover.can_draw_japanese())


class WarningTest(unittest.TestCase):
    """OFF → ON にしたとき、描けない環境なら知らせること。"""

    def setUp(self):
        import tempfile

        from dlsite_deck import config as config_module
        from dlsite_deck import webui

        self.webui = webui
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        cfg = config_module.Config()
        cfg.state_file = str(Path(self.tmp.name) / "state.json")
        cfg.steam_cover_title = False
        self.backend = webui.Backend(cfg)

        self._real = cover.title_support_problem
        self.addCleanup(self._restore)
        self._saved = config_module.save
        config_module.save = lambda updated, path=None: Path(self.tmp.name) / "config.json"
        self.addCleanup(setattr, config_module, "save", self._saved)

    def _restore(self):
        cover.title_support_problem = self._real

    def _save(self, **values):
        return self.backend.save_config(values)

    def test_warns_when_turning_it_on_without_support(self):
        cover.title_support_problem = lambda: "rsvg-convert がありません。"
        result = self._save(steam_cover_title=True)

        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("rsvg-convert", result["warnings"][0])

    def test_saves_anyway(self):
        """止めはしない。別の環境に持っていけば効くため。"""
        cover.title_support_problem = lambda: "無理です。"
        self._save(steam_cover_title=True)
        self.assertTrue(self.backend.config.steam_cover_title)

    def test_no_warning_when_it_is_supported(self):
        cover.title_support_problem = lambda: ""
        self.assertEqual(self._save(steam_cover_title=True)["warnings"], [])

    def test_no_warning_when_turning_it_off(self):
        cover.title_support_problem = lambda: "無理です。"
        self.assertEqual(self._save(steam_cover_title=False)["warnings"], [])

    def test_no_warning_when_it_was_already_on(self):
        """毎回言われても煩いだけ。切り替えたときだけ知らせる。"""
        self.backend.config.steam_cover_title = True
        cover.title_support_problem = lambda: "無理です。"
        self.assertEqual(self._save(steam_cover_title=True)["warnings"], [])

    def test_the_reason_says_what_is_missing(self):
        real_available, real_font = cover.available, cover.can_draw_japanese
        try:
            cover.available = lambda: False
            self.assertIn("rsvg-convert", cover.title_support_problem())

            cover.available = lambda: True
            cover.can_draw_japanese = lambda: False
            self.assertIn("フォント", cover.title_support_problem())

            cover.can_draw_japanese = lambda: True
            self.assertEqual(cover.title_support_problem(), "")
        finally:
            cover.available, cover.can_draw_japanese = real_available, real_font


class CacheTest(unittest.TestCase):
    """ストア画像を作品フォルダに残し、作り直しで再取得しないこと。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        written = cover.store_image(self.dir, JPEG)
        self.assertIsNotNone(written)
        self.assertEqual(written.name, cover.CACHE_STEM + ".jpg")
        self.assertEqual(cover.cached_image(self.dir), JPEG)

    def test_png_keeps_its_extension(self):
        written = cover.store_image(self.dir, PNG)
        self.assertEqual(written.name, cover.CACHE_STEM + ".png")

    def test_nothing_cached_yet(self):
        self.assertIsNone(cover.cached_image(self.dir))

    def test_missing_directory_is_not_an_error(self):
        self.assertIsNone(cover.cached_image(self.dir / "__no_such__"))
        self.assertIsNone(cover.store_image(self.dir / "__no_such__", JPEG))

    def test_none_directory_is_not_an_error(self):
        self.assertIsNone(cover.cached_image(None))
        self.assertIsNone(cover.store_image(None, JPEG))

    def test_changing_format_does_not_leave_the_old_one(self):
        cover.store_image(self.dir, JPEG)
        cover.store_image(self.dir, PNG)
        left = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(left, [cover.CACHE_STEM + ".png"])

    def test_corrupt_cache_is_ignored(self):
        (self.dir / (cover.CACHE_STEM + ".jpg")).write_bytes(b"not an image")
        self.assertIsNone(cover.cached_image(self.dir))

    def test_unknown_format_is_not_stored(self):
        self.assertIsNone(cover.store_image(self.dir, b"GIF89a"))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_name_starts_with_a_dot(self):
        """隠しファイルにしておく。ゲームのフォルダに紛れて見えないように。"""
        self.assertTrue(cover.CACHE_STEM.startswith("."))


class RemoveSlotTest(unittest.TestCase):
    """使わなくなった画像を種類ごとに引き上げられること。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name)
        self.grid = steam.grid_dir(self.config_dir)
        self.grid.mkdir(parents=True)
        for slot, suffix in steam.GRID_SLOTS.items():
            (self.grid / f"123{suffix}.png").write_bytes(PNG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_only_the_named_slot(self):
        removed = steam.remove_grid_images(self.config_dir, 123, slots=["logo"])
        self.assertEqual(len(removed), 1)
        left = sorted(p.name for p in self.grid.iterdir())
        self.assertNotIn("123_logo.png", left)
        self.assertIn("123p.png", left)

    def test_no_slots_given_removes_everything(self):
        steam.remove_grid_images(self.config_dir, 123)
        self.assertEqual(list(self.grid.iterdir()), [])

    def test_unknown_slot_is_ignored(self):
        removed = steam.remove_grid_images(self.config_dir, 123, slots=["nope"])
        self.assertEqual(removed, [])
        self.assertEqual(len(list(self.grid.iterdir())), len(steam.GRID_SLOTS))

    def test_other_apps_are_untouched(self):
        (self.grid / "999p.png").write_bytes(PNG)
        steam.remove_grid_images(self.config_dir, 123)
        self.assertEqual([p.name for p in self.grid.iterdir()], ["999p.png"])


@unittest.skipUnless(cover.available(), "rsvg-convert が無い")
class RenderTest(unittest.TestCase):
    """実際に描けるかどうか。rsvg-convert のある環境でだけ走る。"""

    def test_logo_renders_transparent_png(self):
        data = cover.build_logo("テスト")
        self.assertIsNotNone(data)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_cover_renders_at_expected_size(self):
        data = cover.build_cover(_sample_jpeg(), "テストタイトル")
        self.assertIsNotNone(data)
        # PNG の IHDR から寸法を読む
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        self.assertEqual((width, height), (cover.COVER_WIDTH, cover.COVER_HEIGHT))


def _sample_jpeg() -> bytes:
    """最小限の JPEG。rsvg (gdk-pixbuf) が読める必要がある。"""
    import base64

    # 1x1 のグレー JPEG
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
        "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
        "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
    )


if __name__ == "__main__":
    unittest.main()
