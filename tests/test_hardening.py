"""一般的な攻撃手段への備え。

* **HTTPS 以外で通信しないこと。** DLsite のログインを保つ Cookie
  (``__DLsite_SID`` など) は、DLsite 側の設定で Secure 指定が無い。http で
  DLsite に接続すると暗号化されずに送られる。DLsite から受け取った URL や
  リダイレクト先に http が混ざっても、平文では送らない。``file:`` などは開かない。
* **展開したゲームの中の、外を指すシンボリックリンクを辿らないこと。** 分割 RAR を
  展開する外部ツールはリンクをそのまま作ることがある。DLC の重ね合わせでそれを
  辿ると、作品フォルダの外を読んだり書いたりしてしまう。

リンクの試験は、シンボリックリンクを作れない環境 (権限の無い Windows) では飛ばす。
Linux で必ず走らせること。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from email.message import Message
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import api, archive, link, state  # noqa: E402


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as name:
        try:
            os.symlink(name, os.path.join(name, "probe"), target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


CAN_SYMLINK = _can_symlink()
needs_symlink = unittest.skipUnless(CAN_SYMLINK, "この環境ではシンボリックリンクを作れません")


# ---------------------------------------------------------------------------
# HTTPS だけで通信すること
# ---------------------------------------------------------------------------


class SecureUrlTest(unittest.TestCase):
    def test_https_is_kept(self):
        url = "https://www.dlsite.com/home/download/=/product_id/RJ1.html"
        self.assertEqual(api.secure_url(url), url)

    def test_http_is_upgraded(self):
        self.assertEqual(
            api.secure_url("http://www.dlsite.com/a/b?c=1#d"),
            "https://www.dlsite.com/a/b?c=1#d",
        )

    def test_http_upgrade_keeps_case_insensitive_scheme(self):
        self.assertTrue(api.secure_url("HTTP://img.dlsite.jp/x.jpg").startswith("https://"))

    def test_other_schemes_are_refused(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi",
                    "javascript:alert(1)", "//www.dlsite.com/x", "/relative/path", ""):
            with self.subTest(url=url):
                with self.assertRaises(api.DlsiteApiError):
                    api.secure_url(url)


class OpenerTest(unittest.TestCase):
    """通信の部品そのものが、HTTPS 以外を扱えないこと (入口の確認をすり抜けても)。"""

    def setUp(self):
        self.opener = api._https_only_opener(CookieJar())
        self.kinds = {type(h).__name__ for h in self.opener.handlers}

    def test_only_https_is_wired(self):
        self.assertIn("HTTPSHandler", self.kinds)
        for unwanted in ("HTTPHandler", "FileHandler", "FTPHandler", "DataHandler"):
            self.assertNotIn(unwanted, self.kinds)

    def test_file_urls_cannot_be_opened(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"secret")
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(urllib.error.URLError):
            self.opener.open(Path(handle.name).as_uri())

    def test_http_urls_cannot_be_opened(self):
        with self.assertRaises(urllib.error.URLError):
            # 接続する前に「扱えない形式」で断られる
            self.opener.open("http://127.0.0.1:9/")


class _Recorder:
    """通信の代わりに、開こうとした URL を記録する。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _redirect(url, location):
    headers = Message()
    headers["Location"] = location
    return urllib.error.HTTPError(url, 302, "Found", headers, io.BytesIO(b""))


class _Ok:
    status = 200
    headers = Message()

    def geturl(self):
        return "https://example.invalid/"

    def read(self, *args):
        return b""

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.client = api.DlsiteClient(CookieJar())

    def test_request_upgrades_http(self):
        recorder = _Recorder([_Ok()])
        self.client._opener = recorder
        self.client.request("http://www.dlsite.com/home/x")
        self.assertEqual(recorder.urls, ["https://www.dlsite.com/home/x"])

    def test_request_refuses_file(self):
        recorder = _Recorder([])
        self.client._opener = recorder
        with self.assertRaises(api.DlsiteApiError):
            self.client.request("file:///etc/passwd")
        self.assertEqual(recorder.urls, [], "開こうとしてしまった")

    def test_download_link_over_http_is_upgraded(self):
        recorder = _Recorder([_Ok()])
        self.client._opener = recorder
        self.client.open_stream("http://www.dlsite.com/home/download/=/product_id/RJ1.html")
        self.assertTrue(recorder.urls[0].startswith("https://"))

    def test_redirect_to_http_is_upgraded(self):
        """リダイレクトで http に落とされても、平文では送らない。"""
        start = "https://www.dlsite.com/home/download/=/product_id/RJ1.html"
        recorder = _Recorder([
            _redirect(start, "http://download.dlsite.com/file.zip"),
            _Ok(),
        ])
        self.client._opener = recorder
        self.client.open_stream(start)
        self.assertEqual(recorder.urls[1], "https://download.dlsite.com/file.zip")

    def test_redirect_to_file_is_refused(self):
        start = "https://www.dlsite.com/home/download/=/product_id/RJ1.html"
        recorder = _Recorder([_redirect(start, "file:///home/deck/.ssh/id_rsa")])
        self.client._opener = recorder
        with self.assertRaises(api.DlsiteApiError):
            self.client.open_stream(start)
        self.assertEqual(len(recorder.urls), 1, "リダイレクト先を開こうとした")


# ---------------------------------------------------------------------------
# 外を指すシンボリックリンク
# ---------------------------------------------------------------------------


@needs_symlink
class DropEscapingLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("secret", encoding="utf-8")
        self.root = base / "staging"
        (self.root / "game" / "lib").mkdir(parents=True)
        (self.root / "game" / "lib" / "libfoo.so.1").write_bytes(b"ELF")

    def _link(self, name, target, directory=False):
        path = self.root / name
        os.symlink(str(target), str(path), target_is_directory=directory)
        return path

    def test_absolute_link_outside_is_removed(self):
        path = self._link("game/steal", self.outside / "secret.txt")
        removed = archive.drop_escaping_links(self.root)
        self.assertFalse(path.is_symlink())
        self.assertEqual(removed, ["game/steal"])

    def test_relative_climb_is_removed(self):
        path = self._link("game/up", "../../outside/secret.txt")
        archive.drop_escaping_links(self.root)
        self.assertFalse(path.is_symlink())

    def test_directory_link_outside_is_removed(self):
        path = self._link("game/Data", self.outside, directory=True)
        archive.drop_escaping_links(self.root)
        self.assertFalse(path.is_symlink())
        self.assertTrue((self.outside / "secret.txt").exists(), "リンク先まで消した")

    def test_dangling_link_outside_is_removed(self):
        path = self._link("game/gone", "/nonexistent/place")
        archive.drop_escaping_links(self.root)
        self.assertFalse(path.is_symlink())

    def test_link_through_link_is_removed(self):
        """中を指すリンクを経由して外に出るものも見逃さない。"""
        self._link("game/hop", self.outside, directory=True)
        path = self._link("game/lib/via", "../hop/secret.txt")
        archive.drop_escaping_links(self.root)
        self.assertFalse(path.is_symlink())

    def test_inside_links_are_kept(self):
        """Linux 版のゲームにある、中を指すリンクは残す。"""
        path = self._link("game/lib/libfoo.so", "libfoo.so.1")
        self.assertEqual(archive.drop_escaping_links(self.root), [])
        self.assertTrue(path.is_symlink())

    def test_extract_runs_it(self):
        """展開の流れの中で、移す前に取り除かれること。"""
        out = Path(self.tmp.name) / "out"
        zip_path = Path(self.tmp.name) / "a.zip"
        with zipfile.ZipFile(zip_path, "w") as handle:
            handle.writestr("game/readme.txt", "hi")

        outside = self.outside

        def extract_with_link(archive_path, staging):
            original(archive_path, staging)
            os.symlink(str(outside), str(staging / "game" / "evil"),
                       target_is_directory=True)

        original = archive._extract_zip
        with mock.patch.object(archive, "_extract_zip", extract_with_link):
            result = archive.extract(archive.plan_archive([zip_path]), out)

        self.assertEqual(result.removed_links, ["game/evil"])
        self.assertFalse(any(p.is_symlink() for p in out.rglob("*")))


def _state_with(root: Path, works: dict[str, dict[str, str]]) -> state.State:
    current = state.State(root / "state.json")
    for work_id, files in works.items():
        directory = root / work_id
        directory.mkdir(parents=True, exist_ok=True)
        for relative, body in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        current.works[work_id] = state.InstalledWork(
            id=work_id, title=work_id, directory=str(directory))
    return current


@needs_symlink
class LinkGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.outside = self.root / "outside"
        self.outside.mkdir()
        (self.outside / "cookies.sqlite").write_text("secret", encoding="utf-8")
        self.state = _state_with(self.root, {
            "BASE": {"Game.exe": "原本"},
            "DLC": {"Game.exe": "DLC 版"},
        })

    def test_source_link_to_outside_is_refused(self):
        """コピー元のリンクを辿って、外のファイルを読み出さないこと。"""
        os.symlink(str(self.outside / "cookies.sqlite"), str(self.root / "DLC" / "steal"))
        item = link.Link(source_id="DLC", target_id="BASE")
        with self.assertRaises(link.LinkError):
            link.plan(self.state, item)
        with self.assertRaises(link.LinkError):
            link.apply(self.state, item)
        self.assertFalse((self.root / "BASE" / "steal").exists(), "外のファイルを持ち込んだ")
        self.assertEqual((self.root / "BASE" / "Game.exe").read_text(encoding="utf-8"), "原本")

    def test_target_directory_link_to_outside_is_refused(self):
        """コピー先のリンクを辿って、外に書き込まないこと。"""
        (self.root / "DLC" / "Data").mkdir()
        (self.root / "DLC" / "Data" / "evil.desktop").write_text("x", encoding="utf-8")
        os.symlink(str(self.outside), str(self.root / "BASE" / "Data"), target_is_directory=True)

        item = link.Link(source_id="DLC", target_id="BASE")
        with self.assertRaises(link.LinkError):
            link.apply(self.state, item)
        self.assertFalse((self.outside / "evil.desktop").exists(), "外に書き込んだ")

    def test_target_file_link_to_outside_is_refused(self):
        """上書きされるファイルがリンクだと、退避のために外を読んでしまう。"""
        (self.root / "BASE" / "Game.exe").unlink()
        os.symlink(str(self.outside / "cookies.sqlite"), str(self.root / "BASE" / "Game.exe"))
        with self.assertRaises(link.LinkError):
            link.plan(self.state, link.Link(source_id="DLC", target_id="BASE"))

    def test_inside_links_are_fine(self):
        os.symlink("Game.exe", str(self.root / "DLC" / "alias.exe"))
        link.apply(self.state, link.Link(source_id="DLC", target_id="BASE"))
        self.assertEqual((self.root / "BASE" / "Game.exe").read_text(encoding="utf-8"), "DLC 版")

    def test_revert_does_not_write_outside(self):
        item = link.Link(source_id="DLC", target_id="BASE")
        link.apply(self.state, item)

        # 重ねたあとで、書き戻し先が外を指すリンクに差し替えられた
        (self.root / "BASE" / "Game.exe").unlink()
        os.symlink(str(self.outside / "cookies.sqlite"), str(self.root / "BASE" / "Game.exe"))

        with self.assertRaises(link.LinkError):
            link.revert(self.state, item)
        self.assertEqual(
            (self.outside / "cookies.sqlite").read_text(encoding="utf-8"), "secret",
            "外のファイルを書き換えた")


@needs_symlink
class SymlinkModeRevertTest(unittest.TestCase):
    """「シンボリックリンクにする」で重ねたものを、書き戻し・重ね直しできること。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = _state_with(self.root, {
            "BASE": {"Game.exe": "原本"},
            "DLC": {"Game.exe": "DLC 版"},
        })
        self.item = link.Link(source_id="DLC", target_id="BASE", mode="symlink")
        self.base = self.root / "BASE" / "Game.exe"
        self.dlc = self.root / "DLC" / "Game.exe"

    def _assert_restored(self):
        self.assertFalse(self.base.is_symlink(), "リンクが残った")
        self.assertEqual(self.base.read_text(encoding="utf-8"), "原本")
        self.assertEqual(self.dlc.read_text(encoding="utf-8"), "DLC 版", "DLC 側を上書きした")

    def test_revert_restores_original(self):
        link.apply(self.state, self.item)
        self.assertTrue(self.base.is_symlink())
        link.revert(self.state, self.item)
        self._assert_restored()

    def test_reapply_then_revert(self):
        link.apply(self.state, self.item)
        link.apply(self.state, self.item)
        self.assertTrue(self.base.is_symlink())
        link.revert(self.state, self.item)
        self._assert_restored()

    def test_link_to_other_work_is_still_refused(self):
        """コピー元以外の作品を指すリンクは、ツールが置いたものではない。"""
        other = self.root / "OTHER"
        other.mkdir()
        (other / "secret").write_text("secret", encoding="utf-8")
        link.apply(self.state, self.item)
        self.base.unlink()
        os.symlink(str(other / "secret"), str(self.base))
        with self.assertRaises(link.LinkError):
            link.revert(self.state, self.item)
        with self.assertRaises(link.LinkError):
            link.apply(self.state, self.item)
        self.assertEqual((other / "secret").read_text(encoding="utf-8"), "secret")


if __name__ == "__main__":
    unittest.main()
