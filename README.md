# dlsite_deck

DLsite の購入済み作品を SteamDeck (SteamOS / Desktop Mode) にダウンロード・展開するツールです。
DLsite NEST が Proton 上で動かないことへの代替となります。

追加の依存はありません（Python 標準ライブラリのみ。`pykakasi` のみ任意で推奨）。

## はじめに

- **非公式 API を使用**
  DLsite が公開した仕様ではなく、ブラウザから利用されている内部 API を解析して実装したものです。
  **DLsite 側の変更で予告なく利用できなくなる可能性があり**、利用規約上どう扱われるかは保証できません。
  **利用は自己責任で、各自で DLsite の利用規約を確認してください。**
- **自分が購入した作品を、自分の端末に取得するためのツール**
  他人のライブラリに触れることはありません（できません）。不正なリクエストとみなされる可能性があるため、無用な連続アクセスは避けてください。
- **ブラウザの Cookie データベースを使用**
  ログイン済みセッションを借りるためで、**ID・パスワード・2FA コードを受け取ることも、保存することも、どこかへ送ることもしません。**
  取得した Cookie は実行中のメモリ上だけで使い、ファイルにも書き出しません（`dlsite_deck/cookies.py`）。
- **個人の環境と購入履歴**を `config.json` と `state.json` に保存
  シリアルコードが必要な作品を導入すると、そのコードが `state.json` に平文で記録されます。
  **公開リポジトリに含めないでください**（`.gitignore` で除外済み）。
- **無保証** 動作保証も、サポートが継続される約束もありません。MIT ライセンスの免責が適用されます。

## 特徴

- **2FA 有効なアカウントでも使える。** ID・パスワード・2FA コードには一切触れない
- 購入済みライブラリの一覧、ダウンロード、展開、更新検出
- 展開したゲームを Steam の非 Steam ゲームとして登録し、ストア画像を表紙・背景に設定
- **カバーにゲーム名を焼き込む**（DLsite の画像にはタイトルが無く、一覧で見分けが付かないため）
- Proton の自動割り当てと、後からの一括変更
- DLC を本編に重ねる / exe を実行して当てるパッチの適用
- 展開先は半角英数字のみのディレクトリ名で管理し、バージョン番号は名前から除去
- 通常の操作はすべてローカル Web UI で完結（端末が要るのは起動の一手だけ）

API のエンドポイントとダウンロード解決手順は非公式実装
[`AcrylicShrimp/dlsite-manager`](https://github.com/AcrylicShrimp/dlsite-manager) の `crates/dm-api` から移植しています。

## クイックスタート

SteamDeck の Desktop Mode で、本プロジェクトの `dlsite_deck/` を `~/Applications/dlsite_deck` に置き、

```bash
python3 -m dlsite_deck serve
```

ブラウザが開いたら「状態・ログイン」タブの手順に従って Firefox で DLsite にログインし、
「ライブラリ」タブから取得する。詳しい手順は [導入](docs/install.md) と [使い方](docs/usage.md) を参照。

> **`--host` で外部に開かないこと。** このツールに認証機構は無く、到達できる相手は誰でも
> 購入履歴の閲覧・ダウンロード・Steam への登録を実行できる。既定は `127.0.0.1` のみ。

## ドキュメント

| | |
|---|---|
| [導入](docs/install.md) | 必要なもの、SteamDeck 側の準備コマンド、pykakasi、Windows での開発 |
| [使い方](docs/usage.md) | Web UI の起動、ゲームモードからの利用、画面の構成、Steam 起動中の扱い、ログイン |
| [CLI](docs/cli.md) | 端末からの操作、手動展開済みゲームの取り込み、Steam 登録、Proton、起動オプション、表紙画像 |
| [DLC とパッチ](docs/dlc.md) | DLC を本編に重ねる 4 項目モデル、exe を実行して当てるパッチ |
| [設定](docs/configuration.md) | `config.json` の全項目 |
| [構成](docs/architecture.md) | モジュール構成とテストの内訳 |
| [SteamDeck 以外の環境](docs/platforms.md) | Linux デスクトップ / Windows / macOS で何が動き、何に手当てが要るか |
| [既知の制限](docs/limitations.md) | できないことと、その理由（コントローラ設定を含む） |
| [検証状況](docs/verification.md) | 実機・実環境で何をどこまで確認したか |

## 主な制限

- **Chromium 系ブラウザに未対応**（Cookie が OS のキーリングで暗号化されているため）。Firefox を使う
- **漢字タイトルのローマ字化には `pykakasi` が実質必須**
- **Steam の書き換えは Steam を終了してから行う。** UI から終了させるボタンがある
- **コントローラ設定は変更できない**（Steam の作りによる制限。ゲームモードで設定する）
- 非公式 API のため、DLsite 側の変更で予告なく壊れうる

詳細と根拠は [既知の制限](docs/limitations.md) を参照。

## ライセンス

MIT。詳細は [LICENSE](LICENSE) を参照。

DLsite Play API のエンドポイントとダウンロード種別の解決手順は、[`AcrylicShrimp/dlsite-manager`](https://github.com/AcrylicShrimp/dlsite-manager)（MIT License, Copyright (c) AcrylicShrimp）から移植。
Rust から Python へ書き直したもので、コードをそのまま複製してはいない。上流の著作権表示は LICENSE に含めている。
