"""設定に従ってセッションを用意する。

「Cookie をどこから読むか」「セッションが使えるか確かめる」「作品画像を取る」は
CLI と Web UI の両方が必要とする。別々に書くと片方だけ直して食い違うので、
ここにまとめてある。
"""

from __future__ import annotations

from . import api, config as config_module, cookies

#: セッションが無いときに出す案内
LOGIN_INSTRUCTIONS = cookies.login_instructions()


def load_cookies(cfg: config_module.Config) -> list[cookies.RawCookie]:
    """設定で指定された取得元から Cookie を読む。"""
    if cfg.cookie_source == "manual":
        return cookies.load_manual(cfg.cookie_path)
    return cookies.load_firefox()


def build_client(cfg: config_module.Config) -> api.DlsiteClient:
    """使える状態のクライアントを返す。駄目なら理由を付けて送出する。"""
    found = load_cookies(cfg)
    if not found:
        raise cookies.CookieError(
            "DLsite の Cookie が見つかりませんでした。\n\n" + LOGIN_INSTRUCTIONS
        )

    client = api.DlsiteClient(cookies.to_cookiejar(found))
    if not client.ensure_session():
        raise api.NotAuthorizedError(
            "セッションが無効か期限切れです。ブラウザで再ログインしてください。\n\n"
            + LOGIN_INSTRUCTIONS
        )
    return client


def fetch_image(client: api.DlsiteClient | None, url: str | None) -> bytes | None:
    """作品画像を取ってくる。取れなくても呼び出し側を止めない。

    画像はあくまで飾りなので、失敗しても登録そのものは成立させる。
    """
    if not client or not url:
        return None

    try:
        response = client.request(url)
    except api.DlsiteApiError:
        return None

    if not 200 <= response.status < 300 or not response.body:
        return None
    return response.body
