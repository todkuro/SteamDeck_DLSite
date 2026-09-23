# 他の環境で使う

このツールは SteamDeck（SteamOS）で使うことを前提に作っています。
ただし、Steam への非 Steam ゲームの登録や Firefox の Cookie の読み取りは OS に依存しない仕組みなので、他の環境でもほとんどの機能が動きます。

| 機能 | Linux デスクトップ | Windows | macOS |
|---|---|---|---|
| 作品の一覧・ダウンロード・展開 | ○ | ○ | ○ |
| Firefox の Cookie の読み取り | ○ | ○ | ×（Cookie の書き出しが必要） |
| Steam のデータの場所を自動で探す | ○ | ○（レジストリから） | ×（手で指定） |
| Steam への登録・解除、表紙の設定 | ○ | ○ | ○（場所を指定すれば） |
| 表紙へのタイトルの書き込み | ○（下記の準備が必要） | × | △（Homebrew で準備すれば） |
| Proton の割り当て | ○ | 不要（自動でオフ） | × |
| exe を実行して当てるパッチ | ○ | ×（Proton が前提のため） | × |
| Steam を終了する | ○ | ○ | 未確認 |

使えない機能は自動的に飛ばすので、エラーで止まることはありません。作品の取得や Steam への登録といった主な機能は使えます。

## Linux デスクトップ（Ubuntu、Arch、Fedora など）

SteamDeck にもっとも近い環境です。Steam のデータの場所は、通常版と Flatpak 版の両方から探します。

```
~/.steam/steam/userdata
~/.local/share/Steam/userdata
~/.var/app/com.valvesoftware.Steam/data/Steam/userdata
```

### 表紙にタイトルを入れるには

`rsvg-convert` と日本語フォントが必要です。SteamOS には最初から入っていますが、他のディストリビューションではインストールが必要です。

まず、足りないものがあるかを確認します。

```bash
command -v rsvg-convert; fc-list :lang=ja family | head
```

足りない場合は、お使いのディストリビューションに合わせてインストールしてください（パッケージ名はディストリビューションによって異なります）。

Ubuntu・Debian:

```bash
sudo apt install librsvg2-bin fonts-noto-cjk
```

Arch Linux:

```bash
sudo pacman -S librsvg noto-fonts-cjk
```

Fedora:

```bash
sudo dnf install librsvg2-tools google-noto-sans-cjk-fonts
```

> [!NOTE]
> 足りないものがある場合、この機能は最初からオフになります。ツールが起動するときに `rsvg-convert` と日本語フォントの有無を確認して決めるので、文字化けした表紙が作られることはありません。
> 足りない状態でオンにすると、設定画面に理由が表示されます。

`rsvg-convert` だけをインストールしても、日本語フォントがなければ文字は「□」になります。`fc-list :lang=ja` の結果が空でないことを確認してください。
使うフォントは `dlsite_deck/cover.py` の `FONT_FAMILY` で決めています（既定は Noto Sans CJK JP、Noto Sans、sans-serif の順）。別のフォントを使いたい場合は、ここを書き換えてください。

`rsvg-convert` を PATH の通った場所に置けない場合は、`config.json` の `tool_dirs` にそのディレクトリを追加しても使えます。

### 分割 RAR を展開するには

古い形式の分割 RAR を展開するには、`7z`・`unrar`・`bsdtar`・`unar` のいずれかが必要です。SteamOS には最初から入っていますが、他のディストリビューションでは入っていないことがあります。
購入作品のほとんどを占める ZIP には不要なので、分割 RAR の作品を取得するときだけ用意すれば足ります。

Ubuntu 24.04:

```bash
sudo apt install 7zip
```

Arch Linux:

```bash
sudo pacman -S 7zip
```

他のディストリビューションでは、パッケージ名が異なることがあります。

### Firefox のプロファイルの場所

Firefox は、環境によってプロファイルの保存場所が異なります。作者が確認した範囲では次のとおりです（バージョンと配布形態のどちらで決まるのかは確認していません）。

| 配布形態 | バージョン | 場所 |
|---|---|---|
| Flatpak（SteamDeck） | 155.0.1 | `~/.var/app/org.mozilla.firefox/.mozilla/firefox` |
| deb（Mozilla 公式） | 156.0 | `~/.config/mozilla/firefox` |

このツールはどちらの場所も探すので、どちらの Firefox でも使えます。Snap 版と Flatpak 版の新しい場所や、環境変数 `XDG_CONFIG_HOME` を設定している場合の場所も探します。

見つからない場合は、環境変数 `DLSITE_DECK_FIREFOX_ROOT` でプロファイルの場所を直接指定できます。
プロファイルの場所は、次のコマンドで探せます。

```bash
find ~ -name cookies.sqlite 2>/dev/null
```

### アプリケーションメニューの更新

[使い方](usage.md#アプリケーションメニューに登録するデスクトップモード) に書いた `kbuildsycoca6` は、KDE（SteamDeck のデスクトップモード）用のコマンドです。XFCE や GNOME にはありません。
どの環境でも使えるのは次のコマンドです。

```bash
update-desktop-database ~/.local/share/applications
```

多くの環境では、`.desktop` ファイルを置くだけでメニューに表示されます。このコマンドが必要なのは、すぐに表示されないときだけです。

### 「Steam を終了する」はログインしてから

Web UI の「Steam を終了する」は、`steam -shutdown` コマンドを使っています。
Steam がサインイン画面のままだと、このコマンドを受け取っても終了しません。

| Steam の状態 | 結果 |
|---|---|
| サインイン画面のまま | 90 秒待っても終了しませんでした |
| ログイン済み | 4 秒で終了しました |

どちらの場合も、Steam のログには `Steam is already running, exiting (command line was forwarded)` と表示されます。指示は届いていますが、ログインする前の Steam はそれを処理しません。

ツールは Steam を強制終了しません。`shortcuts.vdf` を書き戻す前に止めてしまうおそれがあるためです。しばらく待っても終了しない場合は、Steam のメニューから終了するよう案内します。Steam を終了したあとに「Steam を終了しました」を押すと、操作を続けられます。

### インストールしたばかりの Steam

Steam をインストールしたばかりだと、`config.vdf` に Proton の割り当てを記録する部分（`CompatToolMapping`）がまだありません。
このツールは、その場合も必要な部分を作ってから Proton を割り当てます。

### モードの判定

SteamDeck のゲームモードかどうかは、`gamescope` が動いているかで判定します。
XFCE や GNOME はどちらにも当てはまりませんが、ゲームモード以外はすべてデスクトップモードと同じように扱うので、問題なく使えます。

### ロケール

SteamOS には日本語のロケール（`ja_JP.UTF-8`）がありませんが、通常のディストリビューションでは用意できます。
その場合、[起動オプションでの文字化け対策](cli.md#起動オプション日本語の文字化け対策) は多くの場合不要です。

## Windows

主な機能はそのまま使えます。このツールの開発も Windows で行っています。

- Steam の場所はレジストリ（`HKCU\Software\Valve\Steam` の `SteamPath` など）から読み取ります。別のドライブにインストールしている場合も見つかります。
- `steam_executable` の既定値は `/usr/bin/steam` ですが、見つからない場合は PATH と Steam のディレクトリからも探すので、`steam.exe` が見つかります。変更する必要はありません。
- `shortcuts.vdf` の読み書きや、表紙・背景の設定は Linux と同じように動きます。
- 分割 RAR の展開には、Windows に標準で入っている `C:\Windows\System32\tar.exe`（中身は `bsdtar`）を使います。見つかることは確認していますが、実際に分割 RAR を展開したことはありません。
- Proton の割り当ては、自動でオフになります（`steam_compat_tool` の既定値が空になります）。Windows のゲームを Windows で動かすのに Proton は不要で、存在しない互換ツールを `config.vdf` に書き込まないようにしています。

### ロゴを exe のアイコンにする

Windows では、設定の `steam_logo_source` を `exe` にすることをおすすめします。

```json
"steam_logo_source": "exe"
```

既定の `title` は `rsvg-convert` で描くので、Windows ではロゴが作られません。`exe` にすると exe のアイコンを使うので、Windows でもロゴが付きます。

> [!NOTE]
> ロゴを作れなかった場合でも、すでにあるロゴは削除しません。別の環境で付けたロゴを、知らないうちに消してしまわないようにするためです。
> ロゴを削除したい場合は、`none` を選んでください。

### Windows でできないこと

- **表紙にタイトルを入れられません。** `rsvg-convert` が標準では入っていないためです。設定は無視され、元の画像をそのまま使います。
- **exe を実行して当てるパッチは使えません。** Proton の環境の中で実行する仕組みのためです。Windows では、パッチの exe を直接実行してください。
- 環境によっては、Web UI への接続が切断されることがあります。[既知の制限](limitations.md#windows-で-web-ui-を使うときの注意) をご覧ください。

## macOS

動作は確認していません。次の 2 つの場所を自動では探さないので、そのままでは使えません。

| 対象 | 実際の場所 | 対処 |
|---|---|---|
| Firefox のプロファイル | `~/Library/Application Support/Firefox` | 環境変数 `DLSITE_DECK_FIREFOX_ROOT` で指定するか、`cookie_source` を `manual` にします |
| Steam の `userdata` | `~/Library/Application Support/Steam/userdata` | 設定の `steam_userdata_dir` に書きます |

`rsvg-convert` は Homebrew でインストールできます（`brew install librsvg`）が、日本語フォントの扱いも含めて確認していません。

## まとめ

環境によって使えるかどうかがはっきり分かれるのは、表紙へのタイトルの書き込みだけです。`rsvg-convert` と日本語フォントがあるかどうかで決まり、SteamOS と多くの Linux デスクトップでは使えますが、Windows では追加の準備が必要です。

それ以外の機能（作品の取得・展開・Steam への登録・表紙の設定・更新の確認）は、Linux と Windows のどちらでも動くことを確認しています。
