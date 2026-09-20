# SteamDeck 以外の環境

このツールは SteamDeck (SteamOS) を前提に作ってあるが、**Steam の非 Steam ゲーム登録も
Firefox の Cookie 読み取りも OS 固有の仕組みではない**ので、他の環境でも大半は動く。
ただし環境ごとに前提が変わる。

| | Linux デスクトップ | Windows | macOS |
|---|---|---|---|
| ライブラリ一覧・ダウンロード・展開 | ○ | ○ | ○ |
| Firefox の Cookie 読み取り | ○ | ○ | **×**（手動書き出しが要る） |
| Steam の `userdata` 自動検出 | ○ | ○（レジストリ） | **×**（手で指定） |
| Steam 登録・解除・表紙画像 | ○ | ○ | ○（パスを指定すれば） |
| **カバーにタイトルを埋め込む** | ○（下記が要る） | **×** | △（Homebrew で入れれば） |
| Proton の割り当て | ○ | **切ること** | × |
| exe を実行して当てるパッチ | ○ | ×（Proton 前提のため） | × |
| Steam を終了する | ○ | ○ | 未確認 |

**どれも落ちない。** 使えないものは静かに飛ばし、登録などの主目的は成立する。

## Linux デスクトップ（Ubuntu / Arch / Fedora など）

SteamDeck に一番近い。`userdata` の候補にはネイティブ版・Flatpak 版の両方を入れてある。

```
~/.steam/steam/userdata
~/.local/share/Steam/userdata
~/.var/app/com.valvesoftware.Steam/data/Steam/userdata
```

カバーにタイトルを埋め込むには **`rsvg-convert` と日本語フォント**が要る。
SteamOS には最初から入っているが、他のディストリビューションでは入れる必要がある。

まず何が足りないか確かめる:

```bash
command -v rsvg-convert; fc-list :lang=ja family | head
```

`rsvg-convert` が無い場合（パッケージ名はディストリビューションによる）:

```bash
sudo apt install librsvg2-bin fonts-noto-cjk
```

```bash
sudo pacman -S librsvg noto-fonts-cjk
```

```bash
sudo dnf install librsvg2-tools google-noto-sans-cjk-fonts
```

> **足りなければ、この設定は既定で OFF になる。** ツールが起動時に `rsvg-convert` と
> `fc-list :lang=ja` を見て決めるので、豆腐だらけのカバーが勝手にできることはない。
> 足りない状態で手動で ON にすると、設定画面に理由が出る。

> **日本語フォントが無いと豆腐になる。** `rsvg-convert` だけ入れても、フォントが
> 無ければ文字が □ で並ぶ。`fc-list :lang=ja` が空でないことを確かめること。
> 描画に使う名前は `cover.py` の `FONT_FAMILY`（既定は Noto Sans CJK JP →
> Noto Sans → sans-serif）。別のフォントを使いたい場合はここを書き換える。

PATH に置けない場合は、設定の **「展開ツールを探す場所」**（`tool_dirs`）に
`rsvg-convert` のあるディレクトリを足しても見つかる。

### Firefox のプロファイル置き場

**Firefox はプロファイルを XDG のディレクトリへ移した。** 実測した限りでは次のとおりで、
版と配布形態のどちらで決まるのかまでは確かめていない。

| | 版 | 置き場 |
|---|---|---|
| Flatpak (SteamDeck) | 155.0.1 | `~/.var/app/org.mozilla.firefox/.mozilla/firefox` |
| deb (Mozilla 公式) | 156.0 | `~/.config/mozilla/firefox` |

**両方を見るようにしてある**ので、どちらでも動く。Snap 版・Flatpak 版それぞれの
XDG の位置（Flatpak の中では `XDG_CONFIG_HOME` が `~/.var/app/<id>/config` になる）も
候補に入れてある。`XDG_CONFIG_HOME` を設定している場合はそれに従う。

見つからない場合は `DLSITE_DECK_FIREFOX_ROOT` で置き場を直接指定できる。

```bash
find ~ -name cookies.sqlite 2>/dev/null
```

## Windows

**ツールの主目的は動く。** 実際、開発はこの環境で行っている。

- Steam の場所は**レジストリ**から読む（`HKCU\Software\Valve\Steam` の `SteamPath` ほか）。
  既定以外の場所（別ドライブに入れている場合など）でも見つかる
- `steam_executable` の既定は `/usr/bin/steam` だが、そこに無ければ PATH と
  Steam のフォルダも見るので `steam.exe` が見つかる。**既定のままでよい**
- `shortcuts.vdf` の読み書き、表紙・背景の設置は同じように動く
- **Proton の割り当ては自動で切れる。** Windows では `steam_compat_tool` の既定が
  空になる。存在しない互換ツールを `config.vdf` に書き込まないため

### 設定を変えること

**ロゴを exe のアイコンにする。**

```json
"steam_logo_source": "exe"
```

`title`（既定）は `rsvg-convert` で描くので、Windows では作られない。
`exe` なら実行ファイルのアイコンを使うので Windows でも付く。

> 作れなかっただけの場合、**既にあるロゴは消さない**。別の環境で付けたものを
> 黙って剥がさないようにしてある。消したいときは `none` を選ぶ。

### できないこと

- **カバーにタイトルを埋め込めない。** `rsvg-convert` が標準では入らないため。
  設定は無視され、元の画像がそのまま置かれる
- **exe を実行して当てるパッチ。** Proton のプレフィックスで動かす作りなので、
  Windows では使えない（そちらでは exe を直接実行すればよい）
- loopback が切られる環境がある。[既知の制限](limitations.md#windows-で-web-ui-を使うときの注意) を参照

### 新しく入れた Steam では Proton の割り当てに注意

**Steam を入れたばかりだと `config.vdf` に `CompatToolMapping` の節がまだ無い。**
以前はこの場合に何もせず諦めていたため、登録しても Proton が割り当てられない
まま黙って進んでいた。現在は**節ごと作る**ようにしてある（素の Ubuntu に入れた
新品の Steam で確認済み）。

### アプリケーションメニューの更新コマンドは環境によって違う

[使い方](usage.md#アプリケーションメニューに載せるdesktop-mode) に書いてある
`kbuildsycoca6` は **KDE (SteamDeck の Desktop Mode) のもの**で、XFCE や GNOME には無い。
共通で使えるのはこちら:

```bash
update-desktop-database ~/.local/share/applications
```

たいていの環境では `.desktop` を置くだけで拾われるので、必要になるのは
すぐ反映されないときだけ。

### `steam -shutdown` はログイン後でないと効かない

UI の「Steam を終了する」は `steam -shutdown` に頼っている。**サインイン画面で
止まっている Steam は、この指示を受け取っても終了しない。**

Ubuntu のコンテナで実測した差:

| Steam の状態 | 結果 |
|---|---|
| サインイン画面のまま | 90 秒待っても終了しない（2 回とも） |
| ログイン済み | **4.0 秒で終了** |

どちらもログには `Steam is already running, exiting (command line was forwarded)` と
出る。**指示は届いているが、ログイン前の Steam が処理しない。**

**ツールは無理に kill しない。** 待って諦め、Steam のメニューから閉じるよう案内する
（`shortcuts.vdf` を書き戻す前に殺してしまわないため）。手で閉じたあと
「Steam を終了しました」を押せば続けられる。

### セッション種別

ゲームモードの判定は gamescope の有無で行う。XFCE や GNOME はどちらにも当たらず
`unknown` になるが、**ゲームモード以外はすべて Desktop Mode と同じ扱い**なので
問題ない（XFCE で「Steam を終了する」が出ること、案内文が Desktop Mode 向けに
なることを確認済み）。

### ロケール

SteamOS には `ja_JP.UTF-8` が無いが、普通のディストリビューションでは用意できる。
その場合、[起動オプションによる文字化け対策](cli.md#起動オプション日本語の文字化け対策)
は不要になることが多い。

## macOS

**未検証。** 次の 2 か所が macOS のパスを見ていないので、そのままでは繋がらない。

| | 実際の場所 | 対処 |
|---|---|---|
| Firefox のプロファイル | `~/Library/Application Support/Firefox` | 環境変数 `DLSITE_DECK_FIREFOX_ROOT` で指定するか、`cookie_source` を `manual` にする |
| Steam の `userdata` | `~/Library/Application Support/Steam/userdata` | 設定の `steam_userdata_dir` に書く |

`rsvg-convert` は Homebrew で入る（`brew install librsvg`）が、日本語フォントの
扱いを含めて確かめていない。

## まとめ

**カバーへのタイトル埋め込みだけが、環境をはっきり選ぶ。** `rsvg-convert` と
日本語フォントがあるかどうかで決まり、SteamOS と多くの Linux デスクトップでは
満たせるが、Windows では追加の手当てが要る。

それ以外（取得・展開・Steam 登録・表紙画像・更新検出）は、Linux と Windows の
どちらでも動くことを実際に確認している。
