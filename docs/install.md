# 導入

## 必要なもの

| もの | 要否 | 説明 |
|---|---|---|
| Python 3.8 以上 | 必須 | SteamOS には最初から入っています |
| Firefox | 必須 | DLsite へのログインに使います。SteamOS では「Discover」からインストールできます |
| `pykakasi` | 推奨 | 漢字を含むタイトルをローマ字にするのに使います |
| `rsvg-convert` | 推奨 | Steam の表紙にタイトルを描くのに使います。SteamOS には最初から入っています。無い場合は元の画像をそのまま使います |
| `7z`・`unrar`・`bsdtar`・`unar` のいずれか | 任意 | 古い形式の分割 RAR を展開するときだけ使います。SteamOS には `7z`・`unrar`・`bsdtar` が最初から入っています |

## SteamDeck での準備

デスクトップモードで端末（Konsole）を開き、以下のコマンドを実行します。
どのコマンドもホームディレクトリの中にだけ書き込み、SteamOS のシステム領域は変更しません。

### 1. Python を確認する

```bash
python3 --version
```

### 2. ツールを置く

このリポジトリ一式を `~/Applications/dlsite_deck` に置きます。
GitHub の「Code」→「Download ZIP」でダウンロードして展開するか、git を使える場合は次のコマンドで取得します。

```bash
git clone https://github.com/todkuro/SteamDeck_DLSite.git ~/Applications/dlsite_deck
```

### 3. pykakasi を入れる（推奨）

SteamOS の Python には pip が入っていません。
`ensurepip` で入れようとしても、システムの Python が保護されているため失敗します（`EXTERNALLY-MANAGED`）。
そこで、一時的に作った仮想環境（venv）の pip を借りて、自分のユーザー領域にインストールします。

```bash
python3 -m venv /tmp/bootstrap-venv && /tmp/bootstrap-venv/bin/python -m pip install --target "$(python3 -c 'import site; print(site.getusersitepackages())')" pykakasi && rm -rf /tmp/bootstrap-venv
```

インストール先は `~/.local/lib/python3.*/site-packages` です。
システム領域には触れないので、SteamOS を更新しても消えません。

次のコマンドでエラーが出なければ、インストールは成功しています。

```bash
python3 -c "import pykakasi"
```

Web UI の「状態・ログイン」タブで、「ローマ字変換」が「あり」になっていることも確認できます。

### 4. Firefox を入れる（まだ入っていない場合）

```bash
flatpak install --user flathub org.mozilla.firefox
```

### 分割 RAR を展開するには

古い形式の分割 RAR を展開するには、外部のコマンドが必要です。
**SteamOS には `7z`（7-Zip）・`unrar`・`bsdtar` が最初から入っているので、何もしなくてかまいません。**
SteamOS 3.8.16 で、どれも OS のイメージに含まれていることを確認しています（自分でインストールしたものではありません）。

購入作品のほとんどは ZIP で、ZIP の展開には外部のコマンドを使いません。分割 RAR は、作者のライブラリ（約 200 作品）では 1 作品だけでした。

ツールは次の順に探し、最初に見つかったものを使います。SteamDeck では `7z` が使われます。

```
unar → 7zz → 7z → unrar → bsdtar
```

どれが入っているかは次のコマンドで確認できます。

```bash
which unar 7zz 7z unrar bsdtar
```

PATH の通っていない場所に置いた場合は、`config.json` の `tool_dirs` でその場所を指定してください。

## pykakasi が必要な理由

pykakasi が無いと、漢字を含むタイトルの読みが分からず、ローマ字にできません。
作者のライブラリ（約 200 作品）で、ディレクトリ名がどのように決まったかを比べた結果は次のとおりです。

| ディレクトリ名の決まり方 | pykakasi なし | pykakasi あり |
|---|---|---|
| pykakasi で漢字を変換 | 0 % | 約 98 % |
| 英語のタイトルを使用 | 約 35 % | 約 2 % |
| かなだけを変換 | 約 9 % | 0 % |
| 作品 ID を含む仮の名前 | **約 56 %** | 0 % |

pykakasi が無いと、半分以上の作品が作品 ID を含む仮の名前になります。
読みやすいディレクトリ名にしたい場合は、事実上必須です。

## Windows での開発

追加の準備は必要ありません。次の環境で動作を確認しています。

- Python 3.14（`py` ランチャーから実行します。`python` や `python3` は Microsoft Store への案内用のスタブなので使えません）
- Node.js があると、テストで画面の JavaScript の構文も確認します。無い場合、その項目は飛ばします

テストは次のコマンドで実行します。

```bash
py -m unittest discover -s tests
```

### Windows で開発するときの注意

実際に動かすのは Linux なので、次の違いは Windows 上のテストでは再現できません。
展開まわりを変更したときは、Linux でもテストを実行してください。

- **ファイル名の大文字・小文字**：Linux では `README.txt` と `readme.txt` を別のファイルとして展開します。Windows では片方が消えます。
- **`:`・`?`・`*` を含むファイル名**：Linux ではそのまま展開できますが、Windows では作成できません。
- **設定ファイルを持ち込まない**：Windows で作った `config.json` と `state.json` には Windows 形式のパスが入ります。SteamDeck では作り直してください。

WSL を使える場合は、Linux 側でもテストできます。

```bash
wsl -e sh -c 'cd /mnt/c/path/to/dlsite_deck && python3 -m unittest discover -s tests'
```
