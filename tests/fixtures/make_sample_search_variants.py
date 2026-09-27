"""Build sample_search_variants.pdf: a PDF whose text layer carries the
Unicode variants a search should see through (see
pdfless.normalize_for_search()) - NFD kana and Latin, full-width ASCII,
half-width katakana, circled digits, squared/parenthesized ideographs and
ligatures.

Page 1 is a table of every variant with its code points; page 2 has
each one again inside a sentence. ROWS is also what
tests/test_search_normalization.py searches for.

Run from the repo root (needs WeasyPrint and the Hiragino fonts macOS
ships):

    uv run --with markdown --with weasyprint \
        python tests/fixtures/make_sample_search_variants.py
"""

import html
import os
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(HERE, "sample_search_variants.pdf")


def nfd(s: str) -> str:
    return unicodedata.normalize("NFD", s)


# (kind, text as stored in the PDF, what a user would type to find it)
ROWS: list[tuple[str, str, str]] = [
    ("NFD（濁点が結合文字）", nfd("グループ"), "グループ"),
    ("NFD（濁点が結合文字）", nfd("データベース"), "データベース"),
    ("NFD（半濁点が結合文字）", nfd("パスワード"), "パスワード"),
    ("NFD（ラテン文字）", nfd("café résumé"), "café"),
    ("全角数字", "第５回開発会議", "第5回"),
    ("全角数字", "２０２６年８月６日", "2026年8月6日"),
    ("全角英字", "ＰＤＦ　ｖｉｅｗｅｒ", "PDF viewer"),
    ("全角記号", "ＡＢＣ－１２３", "ABC-123"),
    ("半角カナ", "ﾃﾞｰﾀﾍﾞｰｽ", "データベース"),
    ("半角カナ", "ｱﾌﾟﾘｹｰｼｮﾝ", "アプリケーション"),
    ("丸数字", "手順①②③", "手順123"),
    ("組文字", "㈱テスト　㍻　㎏", "（株）テスト"),  # typed full-width, as an IME would
    ("合字（LaTeX由来のPDFに多い）", "ﬁle ﬂow ofﬁce", "file"),
    # WeasyPrint gives each glyph a single ToUnicode mapping, so a glyph
    # shared with an NFD row above would come out decomposed here too -
    # the NFC control uses glyphs of its own.
    ("NFC（比較用：正規形）", "ゲーム　ボーナス", "ゲーム"),
]


def codepoints(s: str) -> str:
    """The first few code points of `s`, as "U+XXXX" labels."""
    return " ".join(f"U+{ord(c):04X}" for c in s[:8]) + (" …" if len(s) > 8 else "")


def build_html() -> str:
    """The document, as HTML for WeasyPrint."""
    rows = "".join(
        f"<tr><td>{html.escape(kind)}</td><td class=t>{html.escape(text)}</td>"
        f"<td class=cp>{codepoints(text)}</td></tr>"
        for kind, text, _query in ROWS
    )
    prose = "".join(
        f"<p>本文中の例：{html.escape(text)} を含む文です。</p>" for _kind, text, _query in ROWS
    )
    # The cells don't wrap: a line break inside one would split a
    # variant across two lines, which is a different problem from the
    # one this file is for.
    return f"""<html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 2cm; }}
body {{ font-family: "Hiragino Sans", sans-serif; font-size: 11pt; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #aaa; padding: 4px 6px; }}
td.t {{ font-size: 13pt; white-space: nowrap; }}
td.cp {{ font-family: Menlo, monospace; font-size: 7pt; color: #555; }}
</style></head><body>
<h1>検索の表記ゆれテスト</h1>
<p>2列目が PDF のテキスト層に保存されている形です。</p>
<table><tr><th>種類</th><th>PDF内の表記</th><th>コードポイント</th></tr>{rows}</table>
<h2 style="break-before: page">本文中の例</h2>{prose}
</body></html>"""


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
    import pdfless

    # Points WeasyPrint at Homebrew's Cairo/Pango, as pdfless itself does.
    pdfless.MarkdownDocument._markdown_rendering_available()
    from weasyprint import HTML

    HTML(string=build_html()).write_pdf(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
