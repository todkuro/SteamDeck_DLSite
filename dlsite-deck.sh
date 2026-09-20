#!/bin/sh
# SteamDeck 用の起動スクリプト。
#
# Desktop Mode でこのファイルをダブルクリックすると Web UI が立ち上がり、
# 既定のブラウザで開く。端末を触る必要はない。
#
# 実行できるようにするには一度だけ:
#   chmod +x dlsite-deck.sh

set -eu

here=$(cd "$(dirname "$0")" && pwd)
cd "$here"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 が見つかりません。SteamOS には標準で入っているはずです。" >&2
    exit 1
fi

exec python3 -m dlsite_deck serve "$@"
