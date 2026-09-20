"""DLsite Play v3 API クライアント。

エンドポイントとダウンロード解決の手順 (direct / split / serial) は、非公式実装
``dlsite-manager`` の ``crates/dm-api`` から移植した。

    https://github.com/AcrylicShrimp/dlsite-manager
    Copyright (c) AcrylicShrimp / MIT License

Rust から Python へ書き直したもので、コードをそのまま複製してはいない。
ライセンスの全文と表示は同梱の LICENSE を参照。

公式に公開された仕様ではないため、DLsite 側の変更で予告なく壊れうる。

このクライアントはログインを行わない。確立済みセッションの Cookie を
:mod:`dlsite_deck.cookies` から受け取って使うだけなので、2FA の有無に影響されない。
"""

from __future__ import annotations

import gzip
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from typing import Any, Iterable, Iterator

from . import category as category_module

CONTENT_COUNT_URL = "https://play.dlsite.com/api/v3/content/count"
CONTENT_SALES_URL = "https://play.dlsite.com/api/v3/content/sales"
CONTENT_WORKS_URL = "https://play.dlsite.com/api/v3/content/works"
DOWNLOAD_URL = "https://play.dlsite.com/api/v3/download"
LOGIN_SKIP_URL = "https://www.dlsite.com/home/login/=/skip_register/1"
LOGIN_FINISH_URL = "https://www.dlsite.com/home/login/finish"
PUBLIC_PRODUCT_URL = "https://www.dlsite.com/home/api/=/product.json"
HOME_SERIAL_URL = "https://www.dlsite.com/home/serial/=/product_id/"

DEFAULT_WORKS_BATCH_LIMIT = 50
MAX_DOWNLOAD_REDIRECTS = 8
DOWNLOAD_PAGE_BODY_LIMIT = 512 * 1024

# ブラウザから借りたセッションを使うので、UA もブラウザ相当に揃えておく
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class DlsiteApiError(RuntimeError):
    """API 呼び出しに関する一般的な失敗。"""


class NotAuthorizedError(DlsiteApiError):
    """セッションが無効、または期限切れ。"""


class DownloadUnavailableError(DlsiteApiError):
    """その作品はダウンロードできない。"""


@dataclass
class RawResponse:
    """リダイレクトを追わずに受け取った生のレスポンス。"""

    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def location(self) -> str | None:
        raw = self.headers.get("location")
        if not raw:
            return None
        # 相対 Location を絶対化する
        return urllib.parse.urljoin(self.url, raw)

    def text(self, limit: int | None = None) -> str:
        body = self.body if limit is None else self.body[:limit]
        return body.decode("utf-8", errors="replace")


@dataclass
class Work:
    """ライブラリ上の 1 作品。"""

    id: str
    names: dict[str, str] = field(default_factory=dict)
    maker_names: dict[str, str] = field(default_factory=dict)
    work_type: str | None = None
    age_category: str | None = None
    registered_at: datetime | None = None
    published_at: datetime | None = None
    #: upgrade_date。バージョンアップ検出の基準になる。
    updated_at: datetime | None = None
    content_size: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        """表示用タイトル。日本語を優先する。"""
        for code in ("ja_JP", "en_US", "zh_TW", "zh_CN", "ko_KR"):
            if self.names.get(code):
                return self.names[code]
        return next(iter(self.names.values()), self.id)

    @property
    def english_title(self) -> str | None:
        """英字タイトル。ローマ字ディレクトリ名の第一候補に使う。"""
        return self.names.get("en_US") or None

    @property
    def maker(self) -> str | None:
        for code in ("ja_JP", "en_US"):
            if self.maker_names.get(code):
                return self.maker_names[code]
        return next(iter(self.maker_names.values()), None)

    @property
    def image_url(self) -> str | None:
        """ストアページの 1 枚目に出る画像 (560x420)。Steam の表紙に使う。"""
        files = self.raw.get("work_files")
        if not isinstance(files, dict):
            return None

        for key in ("main", "sam"):
            url = files.get(key)
            if url:
                return _absolute_url(str(url))
        return None

    @property
    def purchased_at(self) -> datetime | None:
        """購入日時。``library()`` が購入履歴から埋める。"""
        return _parse_datetime(self.raw.get("_purchased_at"))

    @property
    def version(self) -> str | None:
        """タイトルに書かれたバージョン表記。

        ``upgrade_date`` が無い作品 (実ライブラリでは 193 件中 77 件) でも、
        タイトルに版が書いてあれば更新の手がかりになる。
        """
        return extract_version(self.title)

    @property
    def file_type(self) -> str | None:
        """配布形式のコード。work_type が "ETC" のときの振り分けに使う。"""
        value = self.raw.get("file_type")
        return str(value) if value else None

    @property
    def category(self) -> str:
        """一覧の絞り込みに使うカテゴリ。"""
        return category_module.classify(self.work_type, self.file_type)

    @property
    def category_label(self) -> str:
        return category_module.label(self.category)

    @property
    def is_game(self) -> bool:
        """ゲーム系の作品種別か。"""
        return self.category == category_module.GAME


@dataclass
class Purchase:
    """購入履歴の 1 件。"""

    id: str
    purchased_at: datetime | None


@dataclass
class Library:
    """購入済みライブラリの取得結果。"""

    works: list[Work]
    #: 購入履歴にはあるが Play API がメタデータを返さない作品 ID
    unavailable: list[str] = field(default_factory=list)


@dataclass
class DownloadFile:
    """ダウンロード対象のファイル 1 件。"""

    url: str
    #: "direct" または "split"
    kind: str = "direct"
    #: 分割ダウンロード時のパート番号
    number: int | None = None


@dataclass
class DownloadPlan:
    """1 作品をダウンロードするために必要な情報一式。"""

    work_id: str
    files: list[DownloadFile]
    serial_numbers: list[tuple[str, str]] = field(default_factory=list)


class DlsiteClient:
    """DLsite Play の API を叩くクライアント。"""

    def __init__(
        self,
        cookie_jar: CookieJar,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 60.0,
    ) -> None:
        self.cookie_jar = cookie_jar
        self.user_agent = user_agent
        self.timeout = timeout
        self._works_batch_limit = DEFAULT_WORKS_BATCH_LIMIT
        # リダイレクトは Location を検査したいので自動追従させない
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            _NoRedirect(),
        )

    # ------------------------------------------------------------------
    # 低レベル HTTP
    # ------------------------------------------------------------------

    def request(
        self,
        url: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        body_limit: int | None = None,
    ) -> RawResponse:
        """リダイレクトを追わずに 1 回だけリクエストする。"""
        if params:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = url + separator + urllib.parse.urlencode(params)

        data = None
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "ja,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)

        request = urllib.request.Request(
            url, data=data, headers=request_headers, method=method
        )

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return _read_response(response, body_limit)
        except urllib.error.HTTPError as error:
            # 4xx/5xx も内容を見たいので例外にせず包んで返す
            with error:
                return _read_response(error, body_limit)
        except urllib.error.URLError as error:
            raise DlsiteApiError(f"{url} への接続に失敗しました: {error.reason}") from error

    def _json(self, response: RawResponse) -> Any:
        if response.status == 401:
            raise NotAuthorizedError(
                "セッションが無効です。ブラウザで DLsite に再ログインしてください。"
            )
        if not 200 <= response.status < 300:
            raise DlsiteApiError(
                f"{response.url} が想定外のステータスを返しました "
                f"({response.status}): {response.text(400)}"
            )
        try:
            return json.loads(response.text())
        except json.JSONDecodeError as error:
            raise DlsiteApiError(
                f"{response.url} の応答を JSON として解釈できませんでした: {response.text(400)}"
            ) from error

    # ------------------------------------------------------------------
    # セッション
    # ------------------------------------------------------------------

    def validate_session(self) -> bool:
        """セッションが有効かどうかを返す。"""
        response = self.request(CONTENT_COUNT_URL)
        if response.status == 401:
            return False
        if 200 <= response.status < 300:
            self._cache_limits(json.loads(response.text()))
            return True
        raise DlsiteApiError(
            f"セッション確認が想定外のステータスを返しました "
            f"({response.status}): {response.text(400)}"
        )

    def refresh_session(self) -> bool:
        """`play.dlsite.com` 側のセッションを張り直す。

        `www.dlsite.com` のログイン Cookie は長寿命だが、Play API のセッションは
        短命で先に切れる (実測で確認済み)。ブラウザでのログイン直後と同じ OAuth
        リダイレクトを辿ることで、認証情報にも 2FA にも触れずに再確立できる。
        """
        url = LOGIN_SKIP_URL

        for _ in range(MAX_DOWNLOAD_REDIRECTS):
            response = self.request(url, body_limit=1024)
            if not 300 <= response.status < 400:
                break
            location = response.location
            if not location:
                return False
            url = location
        else:
            return False

        self.request(LOGIN_FINISH_URL, body_limit=1024)

        try:
            return self.validate_session()
        except DlsiteApiError:
            return False

    def ensure_session(self) -> bool:
        """セッションが無効なら一度だけ張り直しを試みる。"""
        if self.validate_session():
            return True
        return self.refresh_session()

    def content_count(self, last: int | None = None) -> dict[str, Any]:
        """購入件数などのメタ情報を取得する。"""
        params = {"last": last} if last is not None else None
        count = self._json(self.request(CONTENT_COUNT_URL, params=params))
        self._cache_limits(count)
        return count

    def _cache_limits(self, count: Any) -> None:
        if isinstance(count, dict):
            limit = count.get("page_limit")
            if isinstance(limit, int) and limit > 0:
                self._works_batch_limit = limit

    # ------------------------------------------------------------------
    # ライブラリ
    # ------------------------------------------------------------------

    def sales(self, last: int | None = None) -> list[Purchase]:
        """購入履歴 (作品 ID と購入日) の一覧。"""
        params = {"last": last} if last is not None else None
        payload = self._json(self.request(CONTENT_SALES_URL, params=params))
        purchases = []
        for entry in payload or []:
            work_id = entry.get("workno")
            if not work_id:
                continue
            purchases.append(
                Purchase(id=str(work_id), purchased_at=_parse_datetime(entry.get("sales_date")))
            )
        return purchases

    def works(self, ids: Iterable[str]) -> list[Work]:
        """作品 ID からメタデータをまとめて取得する。

        1 リクエストあたりの件数上限はサーバー側の設定に従い、超過が判明したら
        上限を下げて全体をやり直す。
        """
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []

        while True:
            limit = max(1, self._works_batch_limit)
            collected: list[Work] = []
            retry = False

            for chunk in _chunks(ids, limit):
                try:
                    collected.extend(self._works_batch(chunk))
                except _BatchLimitExceeded as error:
                    if error.limit and 0 < error.limit < limit:
                        self._works_batch_limit = error.limit
                    else:
                        self._works_batch_limit = max(1, limit // 2)
                    if self._works_batch_limit >= limit:
                        raise DlsiteApiError(
                            "作品メタデータの取得件数上限を絞り込めませんでした。"
                        ) from error
                    retry = True
                    break

            if not retry:
                return collected

    def _works_batch(self, ids: list[str]) -> list[Work]:
        response = self.request(CONTENT_WORKS_URL, method="POST", json_body=ids)

        if response.status == 401:
            raise NotAuthorizedError(
                "セッションが無効です。ブラウザで DLsite に再ログインしてください。"
            )
        if not 200 <= response.status < 300:
            limit = _detect_works_batch_limit(response)
            if limit is not None:
                raise _BatchLimitExceeded(limit)
            raise DlsiteApiError(
                f"作品メタデータの取得に失敗しました ({response.status}): {response.text(400)}"
            )

        payload = json.loads(response.text())
        return [_parse_work(entry) for entry in (payload or {}).get("works", [])]

    def library(self) -> "Library":
        """購入済み作品のメタデータをすべて取得する。

        購入履歴にあってもメタデータが返らない作品がある (Play 非対応の商業タイトル
        や取り扱い終了分)。これらは黙って捨てず ``unavailable`` に残す。
        """
        purchases = self.sales()
        works = self.works(purchase.id for purchase in purchases)
        purchased_at = {purchase.id: purchase.purchased_at for purchase in purchases}
        for work in works:
            work.raw.setdefault("_purchased_at", _format_datetime(purchased_at.get(work.id)))

        returned = {work.id for work in works}
        unavailable = [purchase.id for purchase in purchases if purchase.id not in returned]
        return Library(works=works, unavailable=unavailable)

    # ------------------------------------------------------------------
    # ダウンロード
    # ------------------------------------------------------------------

    def download_plan(self, work_id: str) -> DownloadPlan:
        """作品 ID からダウンロード手順を解決する。

        ``/api/v3/download`` はリダイレクトで種別を伝えてくる:
        ``/home/download/...`` は直接ダウンロード、``/home/download/split/...``
        は分割、``/home/(download/)serial/...`` はシリアルコード入力ページ。
        """
        probe = self.request(DOWNLOAD_URL, params={"workno": work_id}, body_limit=2048)

        if probe.status == 401:
            raise NotAuthorizedError(
                "セッションが無効です。ブラウザで DLsite に再ログインしてください。"
            )
        if probe.status == 404:
            raise DownloadUnavailableError(f"{work_id} は見つかりませんでした。")
        if not 300 <= probe.status < 400:
            raise DownloadUnavailableError(
                f"{work_id} はダウンロードできません "
                f"(ステータス {probe.status}): {probe.text(400)}"
            )

        location = probe.location
        if not location:
            raise DownloadUnavailableError(
                f"{work_id} のリダイレクト先が取得できませんでした。"
            )

        kind = _classify_download_location(location)

        if kind == "split":
            parts = self._split_download_parts(location)
            if not parts:
                raise DownloadUnavailableError(
                    f"{work_id} の分割ダウンロードリンクが見つかりませんでした: {location}"
                )
            return DownloadPlan(work_id=work_id, files=parts)

        if kind == "serial":
            url, serial_numbers = self._serial_download_page(location)
            return DownloadPlan(
                work_id=work_id,
                files=[DownloadFile(url=url, kind="direct")],
                serial_numbers=serial_numbers,
            )

        if kind == "direct":
            # 直接ダウンロードでもシリアルコードが併記されることがあるので拾っておく
            serial_numbers: list[tuple[str, str]] = []
            url = location
            optional = self._optional_serial_page(work_id)
            if optional is not None:
                url, serial_numbers = optional
            return DownloadPlan(
                work_id=work_id,
                files=[DownloadFile(url=url, kind="direct")],
                serial_numbers=serial_numbers,
            )

        raise DownloadUnavailableError(
            f"{work_id} のリダイレクト先を解釈できませんでした: {location}"
        )

    def _split_download_parts(self, location: str) -> list[DownloadFile]:
        page = self.request(location, body_limit=DOWNLOAD_PAGE_BODY_LIMIT)
        links = _extract_download_links(page.url, page.text())

        parts: dict[tuple[int, str], DownloadFile] = {}
        for url in links:
            number = _download_part_number(url)
            if number is None:
                continue
            parts[(number, url)] = DownloadFile(url=url, kind="split", number=number)

        return [parts[key] for key in sorted(parts)]

    def _serial_download_page(self, location: str) -> tuple[str, list[tuple[str, str]]]:
        page = self.request(location, body_limit=DOWNLOAD_PAGE_BODY_LIMIT)
        if not 200 <= page.status < 300:
            raise DownloadUnavailableError(
                f"シリアルページの取得に失敗しました ({page.status}): {location}"
            )

        body = page.text()
        serial_numbers = _parse_serial_numbers(body)
        for url in _extract_download_links(page.url, body):
            if _classify_download_location(url) == "direct":
                return url, serial_numbers

        raise DownloadUnavailableError(
            f"シリアルページにダウンロードリンクがありませんでした: {location}"
        )

    def _optional_serial_page(self, work_id: str) -> tuple[str, list[tuple[str, str]]] | None:
        """直接ダウンロード作品に付随するシリアルページがあれば読む。"""
        location = f"{HOME_SERIAL_URL}{work_id}.html"
        page = self.request(location, body_limit=DOWNLOAD_PAGE_BODY_LIMIT)

        # 付随シリアルページが無い場合はこれらのステータスで返ってくる
        if page.status in (404, 500) or 300 <= page.status < 400:
            return None
        if not 200 <= page.status < 300:
            return None

        body = page.text()
        serial_numbers = _parse_serial_numbers(body)
        for url in _extract_download_links(page.url, body):
            if _classify_download_location(url) == "direct":
                return url, serial_numbers
        return None

    def open_stream(self, url: str, start: int | None = None):
        """ダウンロード用のレスポンスを開く。``start`` 指定でレジュームする。

        戻り値は ``http.client.HTTPResponse``。呼び出し側で close すること。
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            # ダウンロード本体は圧縮させない (サイズ計算とレジュームが狂うため)
            "Accept-Encoding": "identity",
        }
        if start:
            headers["Range"] = f"bytes={start}-"

        current = url
        for _ in range(MAX_DOWNLOAD_REDIRECTS):
            request = urllib.request.Request(current, headers=headers, method="GET")
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                # 自動追従を切ってあるので、3xx はここに例外として上がってくる
                if 300 <= error.code < 400:
                    with error:
                        location = error.headers.get("Location")
                    if not location:
                        raise DlsiteApiError(
                            f"リダイレクト先が示されていません: {current}"
                        ) from error
                    current = urllib.parse.urljoin(current, location)
                    continue

                with error:
                    detail = error.read(400).decode("utf-8", errors="replace")
                if error.code == 416:
                    raise AlreadyCompleteError() from error
                raise DlsiteApiError(
                    f"ダウンロードに失敗しました ({error.code}): {detail}"
                ) from error
            except urllib.error.URLError as error:
                raise DlsiteApiError(
                    f"ダウンロードに接続できませんでした: {error.reason}"
                ) from error

            if response.status not in (200, 206):
                status = response.status
                response.close()
                raise DlsiteApiError(f"ダウンロードが想定外のステータスを返しました: {status}")

            return response

        raise DlsiteApiError(f"リダイレクトが多すぎます: {url}")


class AlreadyCompleteError(Exception):
    """Range 要求がファイル末尾を超えた = 既に完了している。"""


class _BatchLimitExceeded(Exception):
    def __init__(self, limit: int | None) -> None:
        super().__init__(limit)
        self.limit = limit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx を追わずそのまま返すためのハンドラ。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _read_response(response, body_limit: int | None) -> RawResponse:
    headers = {key.lower(): value for key, value in response.headers.items()}
    body = response.read() if body_limit is None else response.read(body_limit)
    encoding = headers.get("content-encoding", "").lower()

    if encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            pass  # body_limit で途中まで読んだ場合は展開できないことがある
    elif encoding == "deflate":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            try:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
            except zlib.error:
                pass

    return RawResponse(
        url=response.geturl(),
        status=response.status,
        headers=headers,
        body=body,
    )


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def _detect_works_batch_limit(response: RawResponse) -> int | None:
    """件数上限超過のエラーかどうかを判定し、上限値を取り出す。"""
    text = response.text(2048)
    if not re.search(r"limit|too\s*many|上限|多すぎ", text, re.IGNORECASE):
        # ヘッダ側に上限が示されることもある
        header = response.headers.get("x-page-limit") or response.headers.get("x-limit")
        return int(header) if header and header.isdigit() else None

    match = re.search(r"(\d{1,4})", text)
    return int(match.group(1)) if match else None


def _parse_work(entry: dict[str, Any]) -> Work:
    return Work(
        id=str(entry.get("workno", "")),
        names=_localized(entry.get("name")),
        maker_names=_localized((entry.get("maker") or {}).get("name")),
        work_type=entry.get("work_type"),
        age_category=str(entry.get("age_category")) if entry.get("age_category") else None,
        registered_at=_parse_datetime(entry.get("regist_date")),
        published_at=_parse_datetime(entry.get("sales_date")),
        updated_at=_parse_datetime(entry.get("upgrade_date")),
        content_size=entry.get("content_size") if isinstance(entry.get("content_size"), int) else None,
        raw=entry,
    )


#: DLsite の日時は日本時間が基準。``2023-04-27T15:00:00Z`` は JST の 4/28 0 時を指す。
#: そのまま UTC の日付を出すと 1 日ずれるので、表示は JST に直す。
JST = timezone(timedelta(hours=9))


#: タイトルに書かれたバージョン表記。
#: 「～肢体- Ver.1.3.2」「withコンプデータ ver1.4逆バニー」のように、
#: 前後に区切りが無いこともあるので語の途中でも拾う。誤検出を避けるため
#: ver / version の目印を必須にし、数字だけの表記は拾わない。
_TITLE_VERSION = re.compile(
    r"(?:ver|version)\s*\.?\s*(\d+(?:[._]\d+)*[a-z]?)",
    re.IGNORECASE,
)


def extract_version(title: str) -> str | None:
    """タイトルに書かれたバージョン表記を取り出す。

    ``upgrade_date`` が取れない作品でも、タイトルに版が書かれていれば
    更新の手がかりになる。複数見つかった場合は最後のものを採る
    （「Ver1.0 の続編 Ver2.0」のような並びでは後ろが本体の版であることが多い）。
    """
    if not title:
        return None

    found = _TITLE_VERSION.findall(title)
    if not found:
        return None

    # 表記ゆれを吸収して "1.3.2" の形に揃える
    return found[-1].replace("_", ".").lower()


def _format_jst(value: datetime | str | None, pattern: str) -> str:
    """日時を日本時間で整形する。読めなければ空文字。

    DLsite は JST 0 時を UTC 15:00 として返すので、そのまま日付を出すと
    1 日ずれる。時差を戻してから整形すること。
    """
    if isinstance(value, str):
        value = _parse_datetime(value)
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST).strftime(pattern)


def format_datetime(value: datetime | str | None) -> str:
    """日時を日本時間の ``YYYY-MM-DD HH:MM`` にする。"""
    return _format_jst(value, "%Y-%m-%d %H:%M")


def format_date(value: datetime | str | None) -> str:
    """日時を日本時間の ``YYYY-MM-DD`` にする。読めなければ空文字。"""
    return _format_jst(value, "%Y-%m-%d")


def _absolute_url(url: str) -> str:
    """DLsite の API は "//img.dlsite.jp/..." のような省略形も返す。"""
    if url.startswith("//"):
        return "https:" + url
    return url


def _localized(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(text) for key, text in value.items() if text}


def _parse_datetime(value: Any) -> datetime | None:
    """DLsite が返す日時を読む。

    ``2023-04-27T15:00:00.000000Z`` の形が来る。末尾の ``Z`` は Python 3.11 より前の
    ``fromisoformat`` が扱えないので、自前で落としてから解釈する。ここを取りこぼすと
    更新検出が黙って働かなくなる。
    """
    if not value or not isinstance(value, str):
        return None

    text = value.strip().replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1]
    # 秒未満は使わないので落とす
    if "." in text:
        text = text.split(".", 1)[0]

    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _classify_download_location(location: str) -> str:
    """ダウンロード用リダイレクト先の種別を判定する。"""
    parsed = urllib.parse.urlparse(location)
    if parsed.hostname != "www.dlsite.com":
        return "unknown"

    path = parsed.path
    if path.startswith("/home/download/split") or path.startswith("/home/split"):
        return "split"
    if path.startswith("/home/download/serial") or path.startswith("/home/serial"):
        return "serial"
    if path.startswith("/home/download"):
        return "direct"
    return "unknown"


#: 属性値の取り出し。``href=`` に錨を打ち、開き引用符と同じ種類で閉じさせる。
#: ページ全体を引用符で総当たりすると、JavaScript 中の不均衡な引用符でペアが
#: ずれて href をまたいでしまい、肝心のリンクを取り逃がす。
_ATTRIBUTE = re.compile(
    r"""(?:href|src|data-url|data-href)\s*=\s*(["'])(.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)

#: 補助的に、JavaScript 中に直接書かれたリンクも拾う。URL の形は下の
#: :func:`_download_url_params` で厳密に検証するので誤検出は残らない。
_QUOTED = tuple(re.compile("{0}([^{0}]+){0}".format(quote)) for quote in ('"', "'"))

#: 作品 ID の形 (RJ/VJ/BJ... + 数字) + .html
_PRODUCT_FILE = re.compile(r"[A-Za-z]{2}[0-9]+\.html", re.IGNORECASE)


def _download_url_params(url: str) -> dict[str, str] | None:
    """ダウンロードリンクなら ``/=/`` 以降のパラメータを返す。違えば None。

    DLsite のパスは ``/home/download/=/number/1/product_id/RJ123456.html`` のように
    ``/=/`` 以降がキーと値の並びになっている。この形に厳密に一致するものだけを
    リンクとみなすことで、ページ中の JavaScript 断片を確実に弾く。
    """
    # "?" や "#" が付いた時点で素のダウンロードリンクではない
    if "?" in url or "#" in url:
        return None

    parsed = urllib.parse.urlparse(url)
    if parsed.hostname != "www.dlsite.com":
        return None

    segments = parsed.path.strip("/").split("/")
    if segments[:2] != ["home", "download"] or "=" not in segments:
        return None

    tail = segments[segments.index("=") + 1 :]
    if not tail or len(tail) % 2:
        return None

    params = dict(zip(tail[::2], tail[1::2]))
    product = params.get("product_id")
    if not product or not _PRODUCT_FILE.fullmatch(product):
        return None

    return params


def _extract_download_links(page_url: str, body: str) -> list[str]:
    """HTML 中から www.dlsite.com のダウンロードリンクを拾う。"""
    links = set()

    candidates = [match.group(2) for match in _ATTRIBUTE.finditer(body)]
    for pattern in _QUOTED:
        candidates.extend(pattern.findall(body))

    for value in candidates:
        url = urllib.parse.urljoin(page_url, value.strip())
        if _download_url_params(url) is not None:
            links.add(url)

    return sorted(links)


def _download_part_number(url: str) -> int | None:
    """``/home/download/=/number/2/product_id/RJ...`` からパート番号を取り出す。"""
    params = _download_url_params(url)
    if not params:
        return None

    number = params.get("number")
    if number is None or not number.isdigit():
        return None
    return int(number)


def _parse_serial_numbers(body: str) -> list[tuple[str, str]]:
    """シリアル番号の表を (ラベル, 値) の並びとして取り出す。"""
    start = body.find("<h2>シリアル番号</h2>")
    if start < 0:
        return []

    section = body[start:]
    table_start = section.find("<table")
    if table_start < 0:
        return []

    section = section[table_start:]
    table_end = section.find("</table>")
    table = section[:table_end] if table_end >= 0 else section

    serials = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.DOTALL):
        label = _element_text(row, "th")
        value = _element_text(row, "td")
        if not label or not value:
            continue
        if "シリアル" not in label and "serial" not in label.lower():
            continue
        serials.append((label, value))
    return serials


def _element_text(html: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.DOTALL)
    if not match:
        return None
    text = re.sub(r"<[^>]*>", "", match.group(1))
    for entity, replacement in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
        ("&#039;", "'"),
    ):
        text = text.replace(entity, replacement)
    return " ".join(text.split()) or None
