"""Firefox のプロファイル置き場の探索。

Firefox はプロファイルを XDG のディレクトリへ移した。実測:

  Flatpak 版 155.0.1 → ~/.mozilla/firefox
  deb 版     156.0   → ~/.config/mozilla/firefox

版と配布形態で変わるので、片方だけを見ていると
「プロファイルが見つかりません」で止まる。実際に止まった。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dlsite_deck import cookies  # noqa: E402


class RootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

        self.real_home = Path.home
        Path.home = staticmethod(lambda: self.home)
        self.addCleanup(setattr, Path, "home", self.real_home)

        self.real_platform = cookies.sys.platform
        cookies.sys.platform = "linux"
        self.addCleanup(setattr, cookies.sys, "platform", self.real_platform)

        for key in ("XDG_CONFIG_HOME", "DLSITE_DECK_FIREFOX_ROOT"):
            self.addCleanup(_restore_env, key, os.environ.get(key))
            os.environ.pop(key, None)

    def _make(self, *parts):
        path = self.home.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_xdg_location_is_searched(self):
        """deb 版 156.0 の場所。これを見ていなかったのが不具合だった。"""
        wanted = self._make(".config", "mozilla", "firefox")
        self.assertIn(wanted, cookies._candidate_firefox_roots())

    def test_classic_location_is_still_searched(self):
        wanted = self._make(".mozilla", "firefox")
        self.assertIn(wanted, cookies._candidate_firefox_roots())

    def test_both_at_once(self):
        """移行の途中は両方あることがある。取りこぼさない。"""
        new = self._make(".config", "mozilla", "firefox")
        old = self._make(".mozilla", "firefox")
        roots = cookies._candidate_firefox_roots()
        self.assertIn(new, roots)
        self.assertIn(old, roots)

    def test_flatpak_classic(self):
        wanted = self._make(".var", "app", "org.mozilla.firefox", ".mozilla", "firefox")
        self.assertIn(wanted, cookies._candidate_firefox_roots())

    def test_flatpak_xdg(self):
        """Flatpak の中では XDG_CONFIG_HOME が ~/.var/app/<id>/config になる。"""
        wanted = self._make(
            ".var", "app", "org.mozilla.firefox", "config", "mozilla", "firefox"
        )
        self.assertIn(wanted, cookies._candidate_firefox_roots())

    def test_snap_xdg(self):
        wanted = self._make(
            "snap", "firefox", "common", ".config", "mozilla", "firefox"
        )
        self.assertIn(wanted, cookies._candidate_firefox_roots())

    def test_xdg_config_home_is_honoured(self):
        custom = self.home / "elsewhere"
        (custom / "mozilla" / "firefox").mkdir(parents=True)
        os.environ["XDG_CONFIG_HOME"] = str(custom)

        self.assertIn(
            custom / "mozilla" / "firefox", cookies._candidate_firefox_roots()
        )

    def test_librewolf_xdg(self):
        wanted = self._make(".config", "librewolf")
        self.assertIn(wanted, cookies._candidate_firefox_roots())

    def test_missing_directories_are_dropped(self):
        # 1 つも作っていないので、何も返らない
        self.assertEqual(cookies._candidate_firefox_roots(), [])

    def test_explicit_override_comes_first(self):
        self._make(".config", "mozilla", "firefox")
        forced = self._make("forced")
        os.environ["DLSITE_DECK_FIREFOX_ROOT"] = str(forced)

        self.assertEqual(cookies._candidate_firefox_roots()[0], forced)

    def test_profile_is_found_in_the_xdg_location(self):
        """置き場所だけでなく、その中の profiles.ini まで辿れること。"""
        root = self._make(".config", "mozilla", "firefox")
        profile = root / "abc.default-release"
        profile.mkdir()
        (profile / "cookies.sqlite").write_bytes(b"")
        (root / "profiles.ini").write_text(
            "[Profile0]\nName=default-release\nIsRelative=1\n"
            "Path=abc.default-release\nDefault=1\n",
            encoding="utf-8",
        )

        found = cookies.discover_firefox_profiles()
        self.assertEqual([p.path for p in found], [profile])


def _restore_env(key, value):
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
