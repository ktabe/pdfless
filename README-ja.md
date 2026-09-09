# pdfless

*([English](README.md))*

[iTerm2](https://iterm2.com) のインラインイメージプロトコルに対応した端末上で動作する `less(1)` ライクなフルスクリーンPDFページャです。iTerm2 と [WezTerm](https://wezterm.org) での動作を確認しています。

`less(1)` とほぼ同じキーバインド（行単位/半ページ・1ページ単位のスクロール、ページジャンプなど）で、ターミナル上にそのままPDFを表示できます。PDFのページはテキストではなく画像なので、それに加えてズームイン/アウトもできます。

`pdfless` にはプレーンテキストモードもあり、PDFからテキストを抽出して表示します — テキストをコピーしたいときに便利です。`t` でPDFモードとテキストモードをいつでも切り替えられます。

正規表現で文書全体を検索し、マッチ箇所へ直接ジャンプすることもできます。検索はPDFモード・テキストモードのどちらでも機能します。

単なるターミナルプログラムなので、SSHでログインしている環境でも同じように使えます。X11転送は不要ですし、PDFを手元のマシンにコピーする必要もありません。

## スクリーンショット

| | |
| --- | --- |
| ![横幅に合わせる](docs/screenshots/pdfless-width-fit.png)<br>横幅に合わせる（デフォルト） | ![縦幅に合わせる](docs/screenshots/pdfless-height-fit.png)<br>縦幅に合わせる（`-h`） |
| ![ズームイン](docs/screenshots/pdfless-zoom.png)<br>ズームイン + パン | ![PDFモードでの検索](docs/screenshots/pdfless-search-pdf-mode.png)<br>検索 — PDFモード（枠でマーク） |
| ![テキストモードでの検索](docs/screenshots/pdfless-search-text-mode.png)<br>検索 — テキストモード（ハイライト表示） | |

## 必要なもの

- iTerm2のインラインイメージプロトコルに対応した端末 — [iTerm2](https://iterm2.com) と [WezTerm](https://wezterm.org) で動作確認済み。このプロトコルに非対応の端末では何も表示されません。
- [poppler](https://poppler.freedesktop.org)（`pdftoppm` / `pdfinfo`）
- Python 3.9以上
- [uv](https://docs.astral.sh/uv/)

## インストール

`pdfless`が内部で呼び出す `pdftoppm`/`pdfinfo` を提供する poppler をインストールします:

```sh
# macOS (Homebrew)
brew install poppler

# Ubuntu/Debian (apt)
sudo apt install poppler-utils
```

`pdfless.py` は [PEP 723](https://peps.python.org/pep-0723/) 形式の単体スクリプトです。Pythonの依存パッケージはスクリプト内に宣言されているので、[`uv`](https://docs.astral.sh/uv/) を使えば初回実行時に自動でインストールされます:

```sh
# macOS (Homebrew)
brew install uv

# Ubuntu/Debianなど Linux (aptにはuvがないため公式インストーラを使う)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```sh
git clone https://github.com/ktabe/pdfless.git
cd pdfless
./pdfless.py some.pdf
```

または `$PATH` の通ったところに置く:

```sh
cp pdfless.py /usr/local/bin/pdfless
chmod +x /usr/local/bin/pdfless
pdfless some.pdf
```

`uv` を使わない場合は、自分でPillowをインストールして `python3` で直接実行します:

```sh
pip install pillow
python3 pdfless.py some.pdf
```

## 使い方

```
usage: pdfless [--help] [-v] [-p PAGE] [-k] [-h] [--no-frame] [-F] pdf

positional arguments:
  pdf               PDFファイルへのパス

options:
  --help            このヘルプメッセージを表示して終了
  -v, --version     バージョン番号を表示して終了
  -p, --page PAGE   開始ページ（デフォルト: 1）
  -k, --keep        終了時 (q または ^C) に端末画面を復元せず、
                    最後に表示していたページを画面に残す
  -h, --fit-height  各ページを端末の横幅いっぱいではなく、
                    縦幅いっぱいに合わせる（デフォルト: 横幅に合わせる）
  --no-frame        テキストモード（t）でページの外枠の罫線を表示しない
                    （デフォルトは表示。fキーでいつでも切り替え可能）
  -F, --follow      PDFファイルを監視し、更新されたら自動的に読み直す
                    （3秒おきにチェック。同じページ・同じモードのまま）
```

`-F`/`--follow` は、ビルドスクリプトやLaTeXのwatchループなどでPDFを
編集・再生成している最中に便利です — 再ビルドされるたびに、表示位置を
保ったまま自動的に読み直されます。

## キー操作

ナビゲーションは `less(1)` に準拠しています:

| キー | 動作 |
| --- | --- |
| `e` `^E` `j` `^N` `Enter` `Down` | 1行進む |
| `y` `^Y` `k` `^K` `^P` `Up` | 1行戻る |
| `f` `^F` `^V` `Space` `PageDown` | 1画面進む |
| `b` `^B` `Esc-v` `PageUp` | 1画面戻る |
| `d` `^D` | 半画面進む |
| `u` `^U` | 半画面戻る |
| `g` / `Home` | 先頭ページへ |
| `G` / `End` | 最終ページへ |
| `<N> g` | ページ番号 `N` へジャンプ |
| `n` / `p` | 次/前のページへ |

ズームとパン:

| キー | 動作 |
| --- | --- |
| `+` / `-` | ズームイン / アウト |
| `0` | ズームとパンをリセット |
| `m` / `M` | ページを端末の縦幅 / 横幅いっぱいに合わせる |
| `h` / `l` / `Left` / `Right` | 左 / 右にパン（ズームイン時） |
| `H` / `L` / `Shift-Left` / `Shift-Right` | 左端 / 右端にジャンプ |
| `K` / `U` / `Shift-Up` | 現在のページの先頭へ |
| `J` / `D` / `Shift-Down` | 現在のページの末尾へ |

検索（PDFから抽出したテキストに対する大文字小文字を区別しない[Python正規表現](https://docs.python.org/ja/3/library/re.html)検索。現在のページだけでなく文書全体が対象です。パターンが正規表現として不正な場合（例: `C++`）は、リテラルな部分一致検索にフォールバックします。大文字の`N`/`P`である点に注意してください — 小文字の`n`/`p`は上記の通り次/前のページに使われています）。マッチへジャンプすると、その位置までスクロールし、周囲を枠線で囲んで表示します:

| キー | 動作 |
| --- | --- |
| `/<正規表現>` `Enter` | 文書全体から`<正規表現>`を検索 |
| `N` / `P` | 次 / 前のマッチへジャンプ |

その他:

| キー | 動作 |
| --- | --- |
| `t` | 現在のページのプレーンテキスト表示に切り替え（抽出したテキストをページ/行単位でスクロール可能。端末幅より長い行は `h`/`l`/`H`/`L` でパン可能） |
| `f` | （テキストモード）ページの外枠に罫線を表示/非表示。デフォルトは表示（`--no-frame` で最初から非表示にできる。） |
| `^L` | 画面を再描画 |
| `?` | キーバインドのヘルプを表示（`q` で閉じる） |
| `q` / `^C` | 終了 |

## 謝辞

本プログラムのコードは [Claude Code](https://claude.com/claude-code) が書きました。

## ライセンス

[MIT](LICENSE)
