# 設定

```bash
python3 -m dlsite_deck init
```

`config.json` が作られる（Web UI の「設定」タブからも全項目を編集できるので、
通常はこのコマンドも手編集も不要）。項目は次のとおり:

| キー | 既定 | 説明 |
|---|---|---|
| `download_dir` | `<project>/downloads` | アーカイブのキャッシュ置き場 |
| `install_dirs` | `["<project>/games"]` | 選べるインストール先の一覧 |
| `install_dir` | `<project>/games` | 既定のインストール先。`install_dirs` のどれかであること |
| `state_file` | `<project>/state.json` | 導入済み記録の保存先 |
| `cookie_source` | `firefox` | Cookie の取得元。`manual` でエクスポートしたファイルを読む |
| `cookie_file` | `<project>/cookies.txt` | `cookie_source` が `manual` のときの読み込み先 |
| `directory_template` | `{romaji}` | 展開先の名前。`{romaji}` と `{id}` が使える |
| `long_vowel` | `repeat` | 長音符「ー」を母音の繰り返しにする / `drop` で捨てる |
| `delete_archives` | `true` | 展開後にアーカイブを消すか |
| `strip_versions` | `true` | 名前からバージョン番号を除去するか |
| `flatten_single_root` | `true` | 単一フォルダを展開先直下に引き上げるか |
| `include_non_games` | `false` | ゲーム以外の作品も対象にするか |
| `register_to_steam` | `false` | ダウンロード後に自動で Steam 登録するか |
| `steam_userdata_dir` | `""` | Steam の `userdata/<id>`。空なら自動検出 |
| `steam_grid_images` | `true` | 作品画像をライブラリの表紙・背景に設定するか |
| `steam_grid_slots` | `["capsule","cover","hero"]` | 画像を設定するスロット |
| `steam_cover_title` | **環境次第** | **カバーにタイトルを埋め込む**か。中心から 2:3 に切り出して下部にタイトルを描く。`rsvg-convert` と日本語フォントが揃っていれば既定 ON、欠けていれば OFF |
| `steam_logo_source` | `title` | ロゴ画像に使うもの。`title` タイトルの透過 PNG / `exe` exe のアイコン / `none` 設定しない |
| `steam_compat_tool` | `proton_experimental`（Windows では空） | 登録時に割り当てる Proton。空なら割り当てない |
| `steam_executable` | `/usr/bin/steam` | 「Steam を終了する」で使う Steam 本体。無ければ PATH と Steam のフォルダからも探す |
| `steam_launch_options` | `""` | 登録画面の起動オプションの初期値（文字化け対策。[CLI](cli.md#起動オプション日本語の文字化け対策)） |
| `acknowledged_paths` | `[]` | 書き込みを承認したプロジェクト外のパス。一度承認すると再確認しない |
| `tool_dirs` | `[]` | 展開ツールを探す追加ディレクトリ。PATH より優先する |
| `exclude` | `[]` | 対象外にする作品 ID |

既定ではすべてプロジェクトディレクトリ内に書き込む。外部パスに変更した場合、
実行時に確認を求める（`--yes` で省略可）。
