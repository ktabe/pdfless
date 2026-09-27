"""Search sees through Unicode variants (pdfless.normalize_for_search()):
a query typed one way finds text stored another - NFD kana, full-width
ASCII, half-width katakana, ligatures and so on - and every match maps
back onto the original text, so its box or highlight lands in the right
place."""

import os
import sys
import unicodedata

import pytest

import pdfless
from conftest import FIXTURES_DIR
from test_search import make_viewer

sys.path.insert(0, FIXTURES_DIR)
from make_sample_search_variants import ROWS  # noqa: E402

NFD = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731


@pytest.fixture
def variants_pdf():
    """tests/fixtures/sample_search_variants.pdf - see
    make_sample_search_variants.py, which built it."""
    return os.path.join(FIXTURES_DIR, "sample_search_variants.pdf")


def spans(query, text):
    return list(pdfless.search_spans(pdfless.compile_search_pattern(query), text))


def test_already_normalized_text_is_left_as_it_is():
    assert pdfless.normalize_for_search("plain ASCII") == ("plain ASCII", None, None)
    assert pdfless.normalize_for_search("グループ") == ("グループ", None, None)


def test_matches_map_back_onto_the_original_text():
    text = "a " + NFD("グループ") + " b"  # "グ" and "プ" each take two code points
    assert len(text) == 10
    [(start, end)] = spans("グループ", text)
    assert text[start:end] == NFD("グループ")

    # A match inside a segment widens to all of it: "ﾃﾞ" (two code
    # points) is one "デ", so "デ" alone highlights both.
    text = "xﾃﾞｰﾀy"
    assert spans("デ", text) == [(1, 3)]
    assert spans("データ", text) == [(1, 5)]
    assert spans("ｘ", text) == [(0, 1)]  # the query is normalized too


def test_compatibility_characters_and_ligatures():
    assert spans("第5回", "第５回開発会議") == [(0, 3)]
    assert spans("file", "the ﬁle") == [(4, 7)]  # "ﬁ" is one code point
    assert spans("手順123", "手順①②③") == [(0, 5)]
    assert spans("（株）", "㈱テスト") == [(0, 1)]


def test_full_width_symbols_are_literal_not_regex():
    """"（案）" typed with a Japanese input method normalizes to "(案)" -
    still the literal parentheses, not a regex group."""
    assert spans("（案）", "ＡＢＣ（案）") == [(3, 6)]
    assert spans("（案）", "ABC(案)") == [(3, 6)]
    assert spans("ｘ＊", "ｘｘ ｘ＊") == [(3, 5)]
    # ...while ASCII regex syntax next to them still works.
    assert spans("（案）|ｘ+", "ｘｘ（案）") == [(0, 2), (2, 5)]


def test_regex_and_case_folding_still_work():
    assert spans("PDF.v", "ｐｄｆ ｖｉｅｗｅｒ") == [(0, 5)]
    assert spans("x*", "ｘｘ") == [(0, 2)]  # zero-width matches are still skipped


def test_the_fixture_really_stores_the_variants(variants_pdf):
    """Guard for the tests below: the PDF's text layer (as pdftotext
    reads it) holds each variant as-is, not already normalized."""
    page2 = pdfless.PdfDocument(variants_pdf).build_search_index()[1]["text"]
    for kind, stored, query in ROWS:
        if kind.startswith("NFC"):
            continue
        assert not unicodedata.is_normalized("NFKC", stored)
        # Page 2 is running prose: every variant sits in one run of words.
        assert unicodedata.normalize("NFKC", stored).split()[0] in unicodedata.normalize("NFKC", page2)
        assert query.lower() not in page2.lower(), f"{kind}: {query} matched without normalizing"


@pytest.mark.parametrize("kind, stored, query", ROWS)
def test_every_variant_is_found_in_the_pdf(variants_pdf, kind, stored, query):
    handler = pdfless.PdfDocument(variants_pdf)
    matches = handler.find_search_matches(handler.build_search_index(), query)
    pages = {m[0] for m in matches}
    assert 2 in pages, f"{kind}: {query}"
    # On page 1's table too - except the full-width Latin, whose wide
    # letters pdftotext reads there as separate words, out of order.
    if query != "PDF viewer":
        assert 1 in pages, f"{kind}: {query}"


def test_a_match_in_decomposed_text_is_boxed_to_its_word(variants_pdf):
    """The box for an NFD match covers the word it's in, not a
    neighbor's - the offsets mapped back onto pdftotext's word list
    are right."""
    handler = pdfless.PdfDocument(variants_pdf)
    index = handler.build_search_index()
    [box] = [m for m in handler.find_search_matches(index, "パスワード") if m[0] == 2]
    page = index[1]
    [word] = [w for w in page["words"] if page["text"][w[0]:w[1]].startswith("本文中の例：" + NFD("パスワード"))]
    _start, _end, xmin, ymin, xmax, ymax = word
    assert xmin <= box[1] < box[3] <= xmax
    assert (box[2], box[4]) == (ymin, ymax)


def test_text_mode_highlights_the_decomposed_text(variants_pdf):
    viewer = make_viewer(pdfless.PdfDocument(variants_pdf))
    viewer.text_mode = True
    viewer.start_search("グループ")
    line_idx, start, end = viewer._text_search_highlight()
    assert viewer.text_lines[line_idx][start:end] == NFD("グループ")


def test_plain_text_file_search_is_normalized_too(tmp_path):
    path = tmp_path / "variants.txt"
    path.write_text("第５回\n" + NFD("グループ") + "\nﾃﾞｰﾀ\n", encoding="utf-8")
    viewer = make_viewer(pdfless.TextDocument(str(path)))
    viewer.start_search("第5回")
    assert viewer.search_matches == [(0, 0, 3)]
    viewer.start_search("グループ")
    assert viewer.search_matches == [(1, 0, 6)]
    viewer.start_search("データ")
    assert viewer.search_matches == [(2, 0, 4)]
