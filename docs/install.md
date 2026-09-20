# 導入

## 必要なもの

| | 必須 | 備考 |
|---|---|---|
| Python 3.8 以上 | 必須 | SteamOS に標準搭載。追加インストール不要 |
| Firefox | 必須 | ログイン用。SteamOS では Discover から Flatpak で入れる |
| `pykakasi` | 任意（推奨） | 漢字タイトルのローマ字化に必要 |
| `rsvg-convert` | 任意（推奨） | Steam カバーにタイトルを載せるのに使う。**SteamOS には標準で入っている**。無い場合は元の画像をそのまま使う |
| `7z` / `unar` / `unrar` / `bsdtar` | 任意 | 旧形式の分割 RAR を展開する場合のみ。PATH に無ければ `config.json` の `tool_dirs` で場所を指定できる |

## pykakasi について

これが無いと、漢字を含むタイトルは読みを解決できない。実在のライブラリ
（200 作品ほど）での割合は次のとおり:

| | 無し | 有り |
|---|---|---|
| pykakasi で変換 | 0 % | **約 98 %** |
| 英語タイトルを流用 | 約 35 % | 約 2 % |
| 仮名のみで変換 | 約 9 % | 0 % |
| **フォールバック (作品 ID 混じりの名前)** | **約 56 %** | **0 %** |

無しでは過半数がフォールバックになる。ヘボン式ローマ字のディレクトリ名を
まともに得るには実質必須。ユーザーのホーム配下に入るだけで OS には触れない。

## SteamDeck 側の準備コマンド

Desktop Mode でコンソール (Konsole) を開いて実行する。
**いずれも `~/` 配下にしか書き込まず、SteamOS のシステム領域には触れない。**

```bash
python3 --version
```

```bash
mkdir -p ~/Applications/dlsite_deck
```

ここに本プロジェクトの `dlsite_deck/` ディレクトリを丸ごと置く (USB か `scp` で転送)。

pykakasi を入れる（推奨。実機で確認済みの手順）:

**SteamOS の Python には pip が入っておらず、`ensurepip` も
`EXTERNALLY-MANAGED` マーカーに阻まれて失敗する。** 使い捨ての venv から pip を借りて
ユーザー領域へ入れるのが確実:

```bash
python3 -m venv /tmp/bootstrap-venv && /tmp/bootstrap-venv/bin/python -m pip install --target "$(python3 -c 'import site; print(site.getusersitepackages())')" pykakasi && rm -rf /tmp/bootstrap-venv
```

インストール先は `~/.local/lib/python3.*/site-packages` で、OS の領域は一切変更しない
（SteamOS の更新でも消えない）。導入後は `python3 -c "import pykakasi"` が通ることと、
`check` の「ローマ字変換」が「あり」になることを確認する。

Firefox が未導入なら:

```bash
flatpak install --user flathub org.mozilla.firefox
```

旧形式の分割 RAR にも備えるなら（`bsdtar` は SteamOS に標準で入っているので通常は不要）:

```bash
which bsdtar 7z unar unrar
```

## Windows 側（開発用）

追加の準備は不要。確認済みの環境:

- Python 3.14.0（`py` ランチャ経由。`python` / `python3` は Microsoft Store のスタブなので `py` を使う）
- Node.js 24 / git は本ツールでは使わない

テストの実行:

```bash
py -m unittest discover -s tests
```

### Windows で開発するときの注意

本番は Linux なので、Windows 上のテストだけでは次が再現できない。
展開まわりを変更したら Linux でも走らせること。

- **ファイル名の大小**: Linux は `README.txt` と `readme.txt` を別物として展開する。Windows では片方が消える
- **`:` `?` `*` を含むファイル名**: Linux では合法なのでそのまま展開される。Windows では作成できない
- **`config.json` / `state.json` を Deck に持ち込まない**: Windows で `init` すると Windows 形式のパスが書き込まれる。Deck 側では作り直す

WSL があれば Linux 側でもテストできる:

```bash
wsl -e sh -c 'cd /mnt/c/path/to/dlsite_deck && python3 -m unittest discover -s tests'
```
