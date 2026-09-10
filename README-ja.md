# pdfless

*([English](README.md))*

[iTerm2](https://iterm2.com) のインラインイメージプロトコルに対応した端末上で動作する、PDFと画像ファイルのための `less(1)` ライクなフルスクリーンページャです。iTerm2 と [WezTerm](https://wezterm.org) での動作を確認しています。

`less(1)` とほぼ同じキーバインド（行単位/半ページ・1ページ単位のスクロール、ページジャンプなど）で、ターミナル上にそのままPDF（あるいはPNG・JPEGなど[Pillow](https://python-pillow.org)が対応する画像ファイル）を表示できます。ズームイン/アウト、パンもできます。

PDFの場合、`pdfless` にはプレーンテキストモードもあり、ページからテキストを抽出して表示します — テキストをコピーしたいときに便利です。`t` で画像モードとテキストモードをいつでも切り替えられます。

正規表現によるPDF全体の検索も、画像モード・テキストモードのどちらでも使えます。

PDF内のハイパーリンクは、PDF画像モードでクリックして開けます — 外部URL（システムのブラウザで開く）・文書内の他ページへのリンクのどちらにも対応しています。マウスホイールでのスクロールにも対応しています。

複数のファイルを一度に開くこともできます（`pdfless a.pdf b.png ...`）。`less(1)`と同じように`:n`/`:p`でファイル間を切り替えられます。

（おまけとして、プレーンテキストファイルもそのまま開けます。最初からテキスト表示になり、PDFと同じように検索も使えます。）

単なるターミナルプログラムなので、SSHでログインしている環境でも同じように使えます。X11転送は不要ですし、PDFを手元のマシンにコピーする必要もありません。

## スクリーンショット

<p align="center">
  <img src="docs/screenshots/pdfless-width-fit.png" alt="横幅に合わせる"><br>
  <em>横幅に合わせる（デフォルト）</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-height-fit.png" alt="縦幅に合わせる"><br>
  <em>縦幅に合わせる（<code>-h</code>）</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-zoom.png" alt="ズームイン"><br>
  <em>ズームイン + パン</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-search-pdf-mode.png" alt="PDFモードでの検索"><br>
  <em>検索 — PDFモード（枠でマーク）</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-search-text-mode.png" alt="テキストモードでの検索"><br>
  <em>検索 — テキストモード（ハイライト表示）</em>
</p>

## 必要なもの

- iTerm2のインラインイメージプロトコルに対応した端末 — [iTerm2](https://iterm2.com) と [WezTerm](https://wezterm.org) で動作確認済み。このプロトコルに非対応の端末では何も表示されません。
- [poppler](https://poppler.freedesktop.org)（`pdftoppm` / `pdfinfo`）— PDFを見るときだけ必要です。画像ファイルしか開かないなら不要です。
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

`uv` を使わない場合は、自分でPythonの依存パッケージをインストールして `python3` で直接実行します:

```sh
pip install pillow pypdf
python3 pdfless.py some.pdf
```

## 使い方

```
usage: pdfless [--help] [-v] [-p PAGE] [-k] [-h] [--no-frame] [-F]
               [--wheel-scroll-step N] file [file ...]

positional arguments:
  file                    PDF・画像・テキストファイルへのパス（複数可）

options:
  --help                  このヘルプメッセージを表示して終了
  -v, --version           バージョン番号を表示して終了
  -p, --page PAGE         最初のファイルの開始ページ（デフォルト: 1）
  -k, --keep              終了時 (q または ^C) に端末画面を復元せず、
                          最後に表示していたページを画面に残す
  -h, --fit-height        各ページを端末の横幅いっぱいではなく、
                          縦幅いっぱいに合わせる（デフォルト: 横幅に合わせる）
  --no-frame              テキストモード（t）でページの外枠の罫線を表示しない
                          （デフォルトは表示。fキーでいつでも切り替え可能）
  -F, --follow            ファイルを監視し、更新されたら自動的に読み直す
                          （3秒おきにチェック。同じページ・同じモードのまま）
  --wheel-scroll-step N   マウスホイール1ステップあたりのスクロール行数
                          （画像モードのみ。デフォルト: 1）
```

`-F`/`--follow` は、ビルドスクリプトやLaTeXのwatchループ、PNGを再生成
するスクリプトなどでPDFや画像を編集・再生成している最中に便利です —
再ビルドされるたびに、表示位置を保ったまま自動的に読み直されます。
複数ファイルを開いている場合は、今表示中のファイルだけを監視し、
`:n`/`:p`でファイルを切り替えると監視対象もそれに追従します。

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
| `g` / `G` | 現在ページの先頭 / 末尾へ（テキストモードでは先に数字を打つとその行番号へ直接ジャンプ。例: `10g` → 10行目） |
| `<` / `>` / `Home` / `End` | 文書の最初 / 最後のページへ（先に数字を打つとそのページ番号へ直接ジャンプ。例: `10<` → 10ページ目） |
| `n` / `p` | 次/前のページへ（検索中の別の役割については後述） |
| `:n` / `:p` | 次/前のファイルへ（コマンドラインで複数ファイルを指定したとき） |
| `x` / `X` | ファイルリストの先頭 / 末尾へジャンプ |
| `<N> x` | ファイル番号 `N` へジャンプ |

ズームとパン:

| キー | 動作 |
| --- | --- |
| `+` / `-` | ズームイン / アウト |
| `0` | ズームとパンをリセット |
| `m` / `M` | ページを端末の縦幅 / 横幅いっぱいに合わせる |
| `h` / `l` / `Left` / `Right` | 左 / 右にパン（ズームイン時） |
| `H` / `L` / `Shift-Left` / `Shift-Right` | 左端 / 右端にジャンプ |
| `K` / `U` / `Shift-Up` | 現在のページの先頭へ（`g`と同じ） |
| `J` / `D` / `Shift-Down` | 現在のページの末尾へ（`G`と同じ） |

検索（PDFとプレーンテキストファイルのみ、画像ファイルは不可 — 抽出したテキスト/ファイル本文に対する大文字小文字を区別しない[Python正規表現](https://docs.python.org/ja/3/library/re.html)検索。現在のページだけでなく文書全体（テキストファイルならファイル全体）が対象です。パターンが正規表現として不正な場合（例: `C++`）は、リテラルな部分一致検索にフォールバックします）。マッチへジャンプすると、その位置までスクロールし、PDFページ画像上なら枠線で、テキストならハイライトで表示します:

| キー | 動作 |
| --- | --- |
| `/<正規表現>` `Enter` | 文書全体から`<正規表現>`を検索 |
| `N` / `P` | 次 / 前のマッチへジャンプ |
| `n` / `p` | 検索がアクティブな間は上の`N`/`P`と同じ（そうでなければ次/前のページ） |

その他:

| キー | 動作 |
| --- | --- |
| クリック | （PDF画像モードのみ、テキストモードでは無効）ポインタ位置のPDFハイパーリンクを開く — URLならシステムのブラウザで開き、文書内リンクならそのジャンプ先のページ/位置へ移動する |
| マウスホイール | 上下スクロール — PDF画像モードでは`e`/`y`と同じ1行単位（`--wheel-scroll-step`で変更可能）。テキストモードでは、端末側がホイール操作を`Up`/`Down`キー入力に変換して渡してくるのでそのまま使え、クリック&ドラッグによるテキスト選択とも衝突しない |
| `[` / `]` | 文書内リンクでジャンプした位置履歴を、戻る / 進む |
| `t` | 現在のページのプレーンテキスト表示に切り替え（抽出したテキストをページ/行単位でスクロール可能。端末幅より長い行は `h`/`l`/`H`/`L` でパン可能） — PDFのみ。画像ファイルではステータスバーに使えない旨が表示されるだけ（プレーンテキストファイルは最初からこの表示なので切り替え不要） |
| `f` | （テキストモード）ページの外枠に罫線を表示/非表示。デフォルトは表示（`--no-frame` で最初から非表示にできる。） |
| `^L` | 画面を再描画 |
| `?` | キーバインドのヘルプを表示（`q` で閉じる） |
| `q` / `^C` | 終了 |

## 謝辞

本プログラムのコードは [Claude Code](https://claude.com/claude-code) が書きました。

## ライセンス

[MIT](LICENSE)
