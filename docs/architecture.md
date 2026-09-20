# 構成

```
dlsite_deck/
  config.py    config.json の読み書きと検証・既定値
  cookies.py   ブラウザ Cookie DB の読み取り (Firefox / 手動エクスポート)
  api.py       DLsite Play v3 API クライアント (dm-api からの移植)
  download.py  レジューム対応ダウンロードと進捗表示
  archive.py   アーカイブ判定・展開・フラット化・バージョン番号除去
  romaji.py    ヘボン式ローマ字変換とディレクトリ名生成
  category.py  作品種別のカテゴリ分類
  state.py     導入済み記録・更新検出・DLC の結び付き
  steam.py     shortcuts.vdf / config.vdf の読み書き、起動判定・セッション判別・終了
  winicon.py   exe からのアイコン取り出しと PNG 変換
  cover.py     タイトル入りカバー・ロゴの組み立てとストア画像の控え
  install.py   取得→展開→記録と Steam 登録 (CLI と UI で共有)
  session.py   Cookie 読み込みとセッション確立 (CLI と UI で共有)
  link.py      DLC を本編に重ねる (4 項目モデル)
  proton.py    ゲームのプレフィックスで exe を実行する
  webui.py     ローカル Web UI のサーバー
  page.html    画面そのもの (HTML + CSS + JavaScript)
  cli.py       コマンドライン
dlsite-deck.sh       SteamDeck 用の起動スクリプト
dlsite-deck.desktop  アプリケーションメニュー用
LICENSE              MIT。移植元の表示を含む
tests/
  test_offline.py  ネットワーク不要のテスト
  test_winicon.py  アイコン取り出しのテスト
  test_smoke.py    全モジュールの読み込み・CLI 起動・HTTP 経路
  test_link.py     DLC の重ね合わせ (実在する 3 通りの形)
  test_proton.py   Proton の引き当てとパッチ exe の列挙
  test_compat_tool.py  config.vdf の Proton 割り当て書き換え
  test_compat_fresh.py 新品の Steam (CompatToolMapping が無い) への書き込み
  test_config_validation.py 設定の検証が画面の選択肢と食い違わないこと
  test_delete.py   削除・キャッシュの片付け・セッション判別
  test_queue.py    取得の順番待ち (同時 1 本・押した順)
  test_cover.py    折り返し・SVG の組み立て・ストア画像の控え
  test_rebuild.py  カバーの作り直し (設定との突き合わせ・引き上げ)
  test_firefox_roots.py  プロファイル置き場の探索 (XDG への移行を含む)
  test_login_guidance.py ログイン前の案内と、描画で覆い隠さないこと
                                          (合計 386 件)
```
