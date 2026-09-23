"""よその Web ページからの要求を断ること。

127.0.0.1 で待ち受けていても、同じ端末のブラウザで開いた Web ページは
ここへ要求を送れる。以前は出どころを一切見ていなかったので、どのサイトからでも
作品の削除や Steam の終了を実行でき、DNS リバインディングを使えば購入履歴を
読んだうえで、起動オプションに任意のコマンドを入れた登録までできた。

次の 4 つで断る。

* Host が IP アドレスか localhost であること (DNS リバインディング)
* Origin が付いていれば自分と同じであること
* API に起動ごとの合言葉が付いていること
* POST が application/json であること (事前確認なしで送れる形を断つ)
"""

from __future__ import annotations

import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlsite_deck import server as deck_server  # noqa: E402
from tests import support  # noqa: E402

TOKEN = "secret-for-test"


class FakeBackend:
    def __init__(self):
        self.calls = []

    def status_json(self):
        self.calls.append("status")
        return {"ok": True}

    def delete_work(self, work_id):
        self.calls.append(("delete", work_id))
        return {"message": "deleted"}


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        handler = deck_server.make_handler(self.backend, token=TOKEN)
        self.server = support.start_server(self, handler)
        self.port = self.server.server_address[1]

    def _request(self, path, *, body=None, headers=None, token=True,
                 host=None, content_type="application/json"):
        """1 回だけ要求を出す (接続が切られたら support が出し直す)。"""
        sent = dict(headers or {})
        if token:
            sent[deck_server.TOKEN_HEADER] = TOKEN
        if host is not None:
            sent["Host"] = host
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            if content_type:
                sent["Content-Type"] = content_type

        url = f"http://127.0.0.1:{self.port}{path}"
        return support.open_with_retry(urllib.request.Request(url, data=data, headers=sent))

    # -- 通るもの -------------------------------------------------------

    def test_the_page_itself_gets_through(self):
        status, _ = self._request("/")
        self.assertEqual(status, 200)

    def test_a_proper_api_call_gets_through(self):
        status, body = self._request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_a_proper_post_gets_through(self):
        status, _ = self._request("/api/delete", body={"work_id": "RJ1"})
        self.assertEqual(status, 200)
        self.assertIn(("delete", "RJ1"), self.backend.calls)

    def test_same_origin_is_accepted(self):
        status, _ = self._request(
            "/api/status", headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_localhost_and_ipv6_names_are_accepted(self):
        for host in (f"localhost:{self.port}", f"[::1]:{self.port}"):
            with self.subTest(host=host):
                status, _ = self._request("/api/status", host=host)
                self.assertEqual(status, 200)

    # -- 合言葉 ---------------------------------------------------------

    def test_the_page_does_not_carry_the_token(self):
        """画面を取得しても合言葉は得られないこと。

        画面に埋め込んでいた頃は、ブラウザではないプロセスが / を取得するだけで
        合言葉を読めた。今は URL の # の後ろで渡すので、サーバーは画面に入れない。
        """
        _, body = self._request("/", token=False)
        self.assertNotIn(TOKEN.encode(), body)

    def test_no_token_is_refused(self):
        status, body = self._request("/api/status", token=False)
        self.assertEqual(status, 403)
        self.assertIn("#t=", json.loads(body)["error"], "開き直し方を案内していない")
        self.assertEqual(self.backend.calls, [])

    def test_a_wrong_token_is_refused(self):
        status, _ = self._request(
            "/api/status", token=False, headers={deck_server.TOKEN_HEADER: "guess"})
        self.assertEqual(status, 403)
        self.assertEqual(self.backend.calls, [])

    def test_each_launch_gets_a_new_token(self):
        first = deck_server.make_handler(self.backend).token
        second = deck_server.make_handler(self.backend).token
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_a_handler_without_a_token_refuses_everything(self):
        """合言葉を差し込み忘れても、素通しにはならないこと。

        空の合言葉と、合言葉ヘッダーを付けない要求が「一致」してはいけない。
        """
        handler = type("Bare", (deck_server.Handler,), {"backend": self.backend})
        bare = support.start_server(self, handler)

        self.port = bare.server_address[1]
        status, _ = self._request("/api/status", token=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.backend.calls, [])

    # -- 事前確認なしで送れる POST ----------------------------------------

    def test_text_plain_post_is_refused(self):
        """よそのページが事前確認なしで送れる形。合言葉があっても断る。"""
        status, _ = self._request(
            "/api/delete", body={"work_id": "RJ1"}, content_type="text/plain")
        self.assertEqual(status, 403)
        self.assertEqual(self.backend.calls, [], "削除まで届いてしまった")

    def test_form_post_is_refused(self):
        status, _ = self._request(
            "/api/delete", body={"work_id": "RJ1"},
            content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 403)
        self.assertEqual(self.backend.calls, [])

    def test_json_with_charset_is_fine(self):
        status, _ = self._request(
            "/api/delete", body={"work_id": "RJ1"},
            content_type="application/json; charset=utf-8")
        self.assertEqual(status, 200)

    # -- Origin ---------------------------------------------------------

    def test_another_site_is_refused(self):
        status, body = self._request(
            "/api/delete", body={"work_id": "RJ1"},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertIn("別のサイト", json.loads(body)["error"])
        self.assertEqual(self.backend.calls, [])

    def test_null_origin_is_refused(self):
        """サンドボックスの iframe などは Origin: null を送る。"""
        status, _ = self._request("/api/status", headers={"Origin": "null"})
        self.assertEqual(status, 403)

    def test_another_port_is_another_origin(self):
        status, _ = self._request(
            "/api/status", headers={"Origin": f"http://127.0.0.1:{self.port + 1}"})
        self.assertEqual(status, 403)

    # -- Host (DNS リバインディング) ---------------------------------------

    def test_a_domain_name_is_refused(self):
        """向け直された攻撃者のドメイン。合言葉も Origin も揃っていても断る。"""
        host = f"rebind.evil.example:{self.port}"
        status, _ = self._request(
            "/api/status", host=host, headers={"Origin": f"http://{host}"})
        self.assertEqual(status, 403)
        self.assertEqual(self.backend.calls, [])

    def test_a_domain_name_cannot_read_the_page(self):
        """画面が読めると合言葉が漏れる。画面にも Host の確認を効かせる。"""
        status, body = self._request(
            "/", token=False, host=f"rebind.evil.example:{self.port}")
        self.assertEqual(status, 403)
        self.assertNotIn(TOKEN.encode(), body)

    def test_the_wrong_port_is_refused(self):
        status, _ = self._request("/api/status", host=f"127.0.0.1:{self.port + 1}")
        self.assertEqual(status, 403)

    def test_the_server_keeps_working_after_a_refusal(self):
        """断った要求の本文を読み残して、次の要求が壊れないこと。"""
        self._request("/api/delete", body={"work_id": "RJ1"}, content_type="text/plain")
        status, _ = self._request("/api/status")
        self.assertEqual(status, 200)


class HostCheckTest(unittest.TestCase):
    def test_accepted(self):
        for value in ("127.0.0.1:8765", "localhost:8765", "LOCALHOST:8765",
                      "[::1]:8765", "192.168.1.10:8765"):
            with self.subTest(value=value):
                self.assertTrue(deck_server._is_direct_host(value, 8765))

    def test_refused(self):
        for value in ("", "evil.example:8765", "127.0.0.1.nip.io:8765",
                      "localhost.evil.example:8765", "127.0.0.1:8766",
                      "127.0.0.1", "localhost:abc", "[::1]x", "::1"):
            with self.subTest(value=value):
                self.assertFalse(deck_server._is_direct_host(value, 8765))

    def test_port_80_may_be_omitted(self):
        self.assertTrue(deck_server._is_direct_host("127.0.0.1", 80))


class AppUrlTest(unittest.TestCase):
    """ツールが開く URL に合言葉が付くこと。"""

    def test_token_goes_after_the_hash(self):
        url = deck_server.app_url("127.0.0.1", 8765, "abc")
        self.assertEqual(url, "http://127.0.0.1:8765/#t=abc")

    def test_empty_host_means_loopback(self):
        self.assertTrue(deck_server.app_url("", 8765, "abc").startswith("http://127.0.0.1:8765/"))

    def test_the_token_is_never_sent_to_the_server(self):
        """# の後ろは要求に含まれない。ブラウザもそう扱うが、ここでも確かめる。"""
        import urllib.parse

        parsed = urllib.parse.urlsplit(deck_server.app_url("127.0.0.1", 8765, "abc"))
        self.assertNotIn("abc", parsed.path + parsed.query)
        self.assertEqual(parsed.fragment, "t=abc")


class PageSendsTheTokenTest(unittest.TestCase):
    """画面の JS が合言葉を付けて呼んでいること。付け忘れると画面が全滅する。"""

    def test_the_page_reads_and_sends_it(self):
        self.assertIn("location.hash", deck_server.PAGE)
        self.assertIn(f'.get("{deck_server.TOKEN_FRAGMENT}")', deck_server.PAGE)
        self.assertIn(deck_server.TOKEN_HEADER, deck_server.PAGE)
        self.assertIn("withToken(options)", deck_server.PAGE)

    def test_the_page_has_no_token_slot(self):
        """画面に合言葉を差し込む場所が残っていないこと (差し込み直さないため)。"""
        self.assertNotIn("dlsite-deck-token", deck_server.PAGE)


if __name__ == "__main__":
    unittest.main()
