"""場所どうしの関係を調べる小さな道具。

「ある場所が、別の場所の中にあるか」はこのツールのあちこちで確かめる。展開先の
外に書かない、インストール先の外を消さない、作品ディレクトリの外を指すリンクを
辿らない、など、どれも安全に関わる判定なので、式を 1 か所にまとめておく。
"""

from __future__ import annotations

from pathlib import Path


def is_within(path: Path, root: Path, strict: bool = False) -> bool:
    """``path`` が ``root`` の中にあるか。

    ``root`` そのものも「中」とみなす。``strict`` を立てると ``root`` そのものは
    含めない (「中にある別のもの」だけを認めたいとき)。

    **リンクは解かない。** 表記どおりに比べるので、リンクを辿った先で判断したい
    ときは、呼び出す側で ``resolve()`` してから渡すこと。
    """
    if path == root:
        return not strict
    return root in path.parents
