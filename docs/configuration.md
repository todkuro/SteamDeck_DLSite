# 設定

設定は `config.json` に保存します。多くの項目は Web UI の「設定」タブで編集できます。
場所やプログラムを指す項目（下の表で 🔒 の付いたもの）は、安全のため画面からは変更できないので、`config.json` を直接編集してください。

`config.json` をコマンドで作成する場合は、次のコマンドを実行します。

```bash
python3 -m dlsite_deck init
```

## 項目の一覧

表の `<project>` は、このツールを置いたディレクトリを表します。
🔒 の付いた項目は、Web UI からは変更できません。`config.json` を直接編集してください（理由は [既知の制限 — セキュリティ](limitations.md#セキュリティ)）。

| キー | 既定値 | 説明 |
|---|---|---|
| `download_dir` 🔒 | `<project>/downloads` | ダウンロードしたアーカイブを一時的に置く場所 |
| `install_dirs` | `["<project>/games"]` | 選べるインストール先の一覧。Web UI から追加できるのは承認済みの場所だけです |
| `install_dir` | `<project>/games` | 既定のインストール先。`install_dirs` のどれかを指定します |
| `runtime_dirs` | `["<project>/runtimes"]` | 共通パッチ置き場。ランタイムやコーデックのインストーラー（exe・msi）を置くと、どのゲームにも「パッチ」から実行できます（[DLC とパッチ](dlc.md#共通パッチ置き場ランタイムコーデック)）。隠しディレクトリ（`.` で始まるもの）とその中は指定できません |
| `import_dirs` | `["<project>/imports"]` | 自由登録で取り込むアーカイブやディレクトリを置く場所。画面ではこの中だけを選べます。取り込んだあとのアーカイブは画面から削除できます。隠しディレクトリ（`.` で始まるもの）とその中は指定できません |
| `state_file` 🔒 | `<project>/state.json` | 導入済みの作品の記録を保存する場所 |
| `cookie_source` | `firefox` | Cookie の読み取り元。`manual` にすると、書き出した Cookie のファイルを読みます |
| `cookie_file` 🔒 | `<project>/cookies.txt` | `cookie_source` が `manual` のときに読むファイル |
| `directory_template` | `{romaji}` | 展開先のディレクトリ名。`{romaji}`（ローマ字のタイトル）と `{id}`（作品 ID）が使えます |
| `long_vowel` | `repeat` | 長音「ー」の扱い。`repeat` で前の母音を重ね、`drop` で省きます |
| `delete_archives` | `true` | 展開後にアーカイブを削除するかどうか |
| `strip_versions` | `true` | ディレクトリ名からバージョン番号を取り除くかどうか |
| `flatten_single_root` | `true` | アーカイブの中身がディレクトリ 1 つだけの場合に、その中身を展開先の直下に移すかどうか |
| `include_non_games` | `false` | ゲーム以外の作品も対象にするかどうか |
| `register_to_steam` | `false` | ダウンロード後に自動で Steam に登録するかどうか |
| `steam_userdata_dir` 🔒 | `""` | Steam の `userdata` ディレクトリ。空の場合は自動で探します |
| `steam_grid_images` | `true` | 作品の画像をライブラリの表紙や背景に設定するかどうか |
| `steam_grid_slots` | `["capsule","cover","hero"]` | 画像を設定する場所（横長・表紙・背景） |
| `steam_cover_title` | 環境による | 表紙にタイトルを入れるかどうか。`rsvg-convert` と日本語フォントがあればオン、なければオフが既定値です |
| `steam_logo_source` | `title` | ロゴに使うもの。`title`（タイトルの透過 PNG）、`exe`（exe のアイコン）、`none`（設定しない）から選びます |
| `steam_compat_tool` | `proton_experimental`（Windows では空） | 登録時に割り当てる Proton。空の場合は割り当てません |
| `steam_executable` 🔒 | `/usr/bin/steam` | 「Steam を終了する」で使う Steam の実行ファイル。見つからない場合は PATH や Steam のディレクトリからも探します |
| `steam_launch_options` 🔒 | `""` | Web UI から新しく登録するゲームに付ける起動オプション（[文字化け対策](cli.md#起動オプション日本語の文字化け対策)） |
| `acknowledged_paths` 🔒 | `[]` | 書き込みを承認した、プロジェクト外のパス。一度承認すると、次からは確認しません |
| `tool_dirs` 🔒 | `[]` | 展開用のコマンドなどを探すディレクトリ。PATH より優先します |
| `exclude` | `[]` | 対象から外す作品 ID |
| `idle_shutdown_minutes` | `30` | Web UI がこの分数のあいだ使われなければ、自動で終了します。`0` なら終了しません。画面を開いている間や、ダウンロードの途中は終了しません（[使い方](usage.md#使っていないときは自動で終了します)） |

既定では、すべてのファイルをプロジェクトのディレクトリの中に書き込みます。
プロジェクトの外の場所を指定した場合は、端末から起動したときに確認します（`--yes` を付けると確認を省略できます）。
Steam やメニューから起動すると確認に答えられないので、`config.json` で場所を変えたあとは、一度端末から起動してください。
