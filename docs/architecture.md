# 構成

## ファイル

| ファイル | 役割 |
|---|---|
| `dlsite_deck/cli.py` | コマンドライン |
| `dlsite_deck/server.py` | Web UI の待ち受け。HTTP の受け答えと、よそからの要求の締め出し（合言葉・接続の上限・自動終了） |
| `dlsite_deck/webui.py` | Web UI の中身。画面から頼まれた処理と、設定画面の組み立て方 |
| `dlsite_deck/page.html` | Web UI の画面（HTML・CSS・JavaScript） |
| `dlsite_deck/config.py` | `config.json` の読み書き、値の確認、既定値 |
| `dlsite_deck/cookies.py` | ブラウザの Cookie の読み取り（Firefox、または書き出したファイル） |
| `dlsite_deck/session.py` | Cookie の読み込みと DLsite へのログイン（コマンドラインと Web UI で共通） |
| `dlsite_deck/api.py` | DLsite Play API の呼び出し（dlsite-manager の `dm-api` から移植） |
| `dlsite_deck/download.py` | 途中から再開できるダウンロードと、進み具合の表示 |
| `dlsite_deck/archive.py` | アーカイブの判定と展開、ディレクトリの整理、バージョン番号の除去 |
| `dlsite_deck/romaji.py` | ヘボン式のローマ字への変換と、ディレクトリ名の作成 |
| `dlsite_deck/category.py` | 作品の種類の分類 |
| `dlsite_deck/install.py` | 取得・展開・記録と Steam への登録の流れ（コマンドラインと Web UI で共通） |
| `dlsite_deck/state.py` | 導入済みの作品の記録、更新の確認、DLC の重ね合わせの記録 |
| `dlsite_deck/steam.py` | `shortcuts.vdf` と `config.vdf` の読み書き、Steam の起動の確認・モードの判定・終了 |
| `dlsite_deck/cover.py` | タイトル入りの表紙とロゴの作成、ストア画像の保存 |
| `dlsite_deck/winicon.py` | exe からのアイコンの取り出しと PNG への変換 |
| `dlsite_deck/link.py` | DLC を本編に重ねる処理 |
| `dlsite_deck/proton.py` | ゲームの Proton 環境の中で exe を実行する処理 |
| `dlsite_deck/paths.py` | 「ある場所が別の場所の中にあるか」の判定（安全に関わる判定を 1 か所にまとめたもの） |
| `dlsite-deck.sh` | SteamDeck 用の起動スクリプト |
| `dlsite-deck.desktop` | アプリケーションメニューへの登録用 |
| `LICENSE` | MIT ライセンス（移植元の著作権表示を含む） |

## テスト

`py -m unittest discover -s tests`（Linux では `python3 -m unittest discover -s tests`）で、すべてのテストを実行できます。合計 526 件です。

`tests/support.py` は、複数のテストで使う補助（Windows で切られた接続のやり直し、待ち受けの起動、設定ファイルの差し替え）です。

| ファイル | 内容 | 件数 |
|---|---|---|
| `test_offline.py` | 通信を使わない処理全般 | 96 |
| `test_delete.py` | 作品の削除、キャッシュの片付け、モードの判定 | 72 |
| `test_cover.py` | タイトルの折り返し、表紙の組み立て、ストア画像の保存 | 59 |
| `test_compat_tool.py` | `config.vdf` への Proton の割り当て | 31 |
| `test_request_guard.py` | 他のサイトからの要求を断ること（Host・Origin・合言葉・JSON）、合言葉を URL の `#` で渡すこと | 28 |
| `test_config_lock.py` | 場所やプログラムを指す設定と起動オプションを画面から変えさせないこと、インストール先は承認済みの場所に限ること | 30 |
| `test_idle.py` | 使われなくなったら自動で終了すること、接続に時間切れと上限があること | 22 |
| `test_hardening.py` | HTTPS でしか通信しないこと、作品の外を指すシンボリックリンクを辿らないこと、「シンボリックリンクにする」で重ねた DLC を書き戻せること（リンクの試験は Linux でのみ動く） | 27 |
| `test_compat_fresh.py` | インストールしたばかりの Steam への Proton の割り当て | 18 |
| `test_smoke.py` | すべてのモジュールの読み込み、コマンドの起動、Web UI の経路 | 18 |
| `test_backup_cleanup.py` | 作品の削除に合わせた、DLC の退避ファイルの片付け | 17 |
| `test_config_validation.py` | 設定の確認と画面の選択肢の一致、Proton の既定値 | 17 |
| `test_login_guidance.py` | ログイン前の案内の表示 | 15 |
| `test_rebuild.py` | 表紙の作り直し（設定との突き合わせ、不要な画像の削除） | 15 |
| `test_link.py` | DLC の重ね合わせ（実際にあった 3 通りの形）、退避ファイルが増えないこと | 13 |
| `test_proton.py` | Proton の選択とパッチの exe の一覧 | 13 |
| `test_firefox_roots.py` | Firefox のプロファイルの場所の探索 | 11 |
| `test_winicon.py` | exe からのアイコンの取り出し | 9 |
| `test_link_ui.py` | Web UI からの DLC の書き戻しと記録の削除 | 8 |
| `test_queue.py` | ダウンロードの順番待ち（一度に 1 件、押した順） | 7 |
