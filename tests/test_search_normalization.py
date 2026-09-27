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
from conftest import FIXTURES_DIR, requires_soffice
from test_search import make_viewer

sys.path.insert(0, FIXTURES_DIR)
from make_sample_search_variants import ROWS, WRAPPED, WRAPPED_QUERIES  # noqa: E402

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


def test_spaces_next_to_japanese_or_chinese_are_ignored():
    """Word/LibreOffice set a gap between Japanese and Latin text that
    pdftotext reads back as a space - so a space next to a character of
    a script written without spaces between words doesn't count, in the
    text or in the query."""
    assert spans("Xプロジェクト", "X プロジェクト") == [(0, 8)]
    assert spans("X プロジェクト", "Xプロジェクト") == [(0, 7)]
    assert spans("会議", "会 議") == [(0, 3)]  # a CJK run pdftotext split in two
    assert spans("ゲーム ボーナス", "ゲーム　ボーナス") == [(0, 8)]  # full-width space
    assert spans("用PDF查看", "用 PDF 查看") == [(0, 8)]  # Chinese, spaced the usual way


def test_other_spaces_are_collapsed_but_still_count():
    """Between Latin words - or Korean ones, which are written with
    spaces between them - a space still matters; only how many there
    are doesn't (pdftotext -layout pads justified lines)."""
    assert spans("PDF viewer", "PDF    viewer") == [(0, 13)]
    assert spans("pdfviewer", "pdf viewer") == []
    assert spans("한국 어", "한국   어") == [(0, 6)]
    assert spans("한국어", "한국 어") == []
    assert spans("a b+", "a   bb") == [(0, 6)]  # regex syntax around it still works


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
    # Page 1's table too - even the full-width Latin, whose wide letters
    # pdftotext reads there as separate words, out of order (found in the
    # on-screen row order - see build_search_index()).
    assert pages == {1, 2}, f"{kind}: {query}"


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


@requires_soffice
def test_a_word_documents_mixed_script_text_is_found(tmp_path):
    """sample_twopage.docx has "これはpdflessの" with no spaces; rendered
    through LibreOffice, its PDF's text layer reads "これは pdfless の"."""
    handler = pdfless.OfficeDocument(os.path.join(FIXTURES_DIR, "sample_twopage.docx"))
    viewer = make_viewer(handler)
    assert handler._pdf_delegate is not None
    page_text = " ".join(p["text"] for p in handler.build_search_index())
    assert "これは pdfless の" in page_text  # the gap is really there
    for query in ("これはpdflessの", "日本語とEnglishが"):
        viewer.start_search(query)
        assert viewer.search_matches, query


def page_of(*words):
    """A build_search_index() page from (text, xMin, yMin, xMax, yMax)
    words, in the order pdftotext would have given them."""
    return pdfless.PdfDocument._index_page(600.0, 800.0, list(words))


def test_a_row_given_out_of_order_is_searched_as_it_appears():
    """A table row pdftotext gives cell by cell out of order - the cell
    to the right first, the one left of it after a cell from another
    row - is still found as it reads on screen, boxed from end to end."""
    page = page_of(
        ("データ", 200, 613, 283, 629),
        ("別の行", 347, 589, 489, 605),
        ("第", 169, 613, 183, 629),
    )
    assert page["rows"]["order"] == [1, 2, 0]
    [match] = pdfless.PdfDocument.find_search_matches([page], "第 データ")
    assert match == (1, 169, 613, 283, 629)
    assert pdfless.PdfDocument.find_search_matches([page], "第 +データ") == [match]


def test_a_line_break_inside_a_column_still_matches():
    """Two columns: pdftotext reads the left one top to bottom, so a
    query broken across its lines is found - the on-screen rows (which
    interleave the columns) don't lose it, and don't double it."""
    page = page_of(
        ("左の段の一行目のデータ", 50, 100, 250, 115),
        ("ベースの話", 50, 120, 150, 135),
        ("右の段の一行目", 320, 100, 480, 115),
        ("右の段の二行目", 320, 120, 480, 135),
    )
    assert page["rows"] is not None  # the rows really do interleave
    matches = pdfless.PdfDocument.find_search_matches([page], "データベース")
    assert len(matches) == 1
    assert pdfless.PdfDocument.find_search_matches([page], "一行目") == [
        (1, pytest.approx(50 + 200 * 4 / 11), 100, pytest.approx(50 + 200 * 7 / 11), 115),
        (1, pytest.approx(320 + 160 * 4 / 7), 100, 480, 115),
    ]  # once each, in pdftotext's reading order


def test_rows_are_left_out_when_the_order_already_matches():
    page = page_of(
        ("上の行", 50, 100, 110, 115),
        ("続き", 120, 101, 160, 116),  # a slightly different baseline, same row
        ("下の行", 50, 120, 110, 135),
    )
    assert page["rows"] is None


def text_viewer(tmp_path, lines):
    path = tmp_path / "wrapped.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return make_viewer(pdfless.TextDocument(str(path)))


def test_text_mode_search_runs_across_line_breaks(tmp_path):
    """A PDF's text comes one printed line at a time, so a paragraph's
    words get split across lines ("ござ" / "います"): the lines are
    searched joined, the break dropped next to Japanese and read as a
    space between Latin words."""
    viewer = text_viewer(tmp_path, ["誠にありがとうござ", "います。Supports text mode and", "search here."])
    viewer.start_search("ございます")
    assert viewer.search_matches == [(0, 7, 9 + 1 + 3)]  # `end` counts on into line 1
    assert viewer._text_highlight_segments(viewer.search_matches[0]) == {0: (7, 9), 1: (0, 3)}

    viewer.start_search("and search")
    [(line_idx, start, end)] = viewer.search_matches
    and_at = viewer.text_lines[1].index("and")
    assert (line_idx, start, end) == (1, and_at, len(viewer.text_lines[1]) + 1 + len("search"))
    assert viewer._text_highlight_segments((line_idx, start, end)) == {
        1: (and_at, and_at + 3), 2: (0, 6),
    }
    viewer.start_search("andsearch")
    assert viewer.search_matches == []  # a line break between Latin words is still a space


def test_a_page_separator_is_never_searched_across(tmp_path):
    viewer = text_viewer(tmp_path, ["ありがとうござ", "(separator)", "います"])
    viewer._text_separator_lines = frozenset({1})
    viewer.search_query = "ございます"
    assert viewer._find_all_text_matches() == []


@pytest.mark.parametrize("wrap", [False, True])
def test_a_highlight_across_lines_is_drawn_on_each(tmp_path, capsys, wrap):
    viewer = text_viewer(tmp_path, ["誠にありがとうござ", "います。"])
    viewer.text_wrap = wrap
    viewer._display_rows = None
    viewer.start_search("ございます")
    capsys.readouterr()
    if wrap:
        pdfless.Viewer._draw_text_wrapped(viewer)
    else:
        pdfless.Viewer._draw_text_unwrapped(viewer)
    out = capsys.readouterr().out
    assert pdfless.TEXT_HIGHLIGHT_COLOR + "ござ" + pdfless.SGR_RESET in out
    assert pdfless.TEXT_HIGHLIGHT_COLOR + "います" + pdfless.SGR_RESET in out


@pytest.mark.parametrize("query", WRAPPED_QUERIES)
def test_a_word_split_across_a_pdfs_lines_is_found_in_both_modes(variants_pdf, query):
    """Page 3 of the fixture breaks WRAPPED's words mid-word."""
    handler = pdfless.PdfDocument(variants_pdf)
    assert 3 in {m[0] for m in handler.find_search_matches(handler.build_search_index(), query)}

    viewer = make_viewer(pdfless.PdfDocument(variants_pdf))
    viewer.text_mode = True
    viewer.start_search(query)
    segments = viewer._text_highlight_segments(viewer._text_search_highlight())
    assert len(segments) == 2, query  # the highlight is on both lines
    highlighted = "\n".join(viewer.text_lines[i][a:b] for i, (a, b) in sorted(segments.items()))
    assert highlighted.replace("\n", "" if query == "ございます" else " ") == query


def test_which_queries_are_literal():
    for query in ("ございます", "and search", "（案）", "ｘ＊", "f(x", "第 データ"):  # "f(x": invalid, so literal
        assert pdfless.search_query_is_literal(query), query
    for query in ("foo.*bar", "a|b", "ござ(い|り)ます", r"\d+", "^foo"):
        assert not pdfless.search_query_is_literal(query), query


def test_a_regex_stays_within_a_line_unless_asked(tmp_path):
    """Joined up, "foo.*bar" would reach from a "foo" to a "bar" any
    number of lines on - so a regex is matched line by line, as in
    less(1), unless ^T at the prompt turned multi-line matching on."""
    viewer = text_viewer(tmp_path, ["foo is here", "(far away)", "and bar there", "foo bar"])
    viewer.start_search("foo.*bar")
    assert viewer.search_matches == [(3, 0, 7)]
    viewer.start_search("foo.*bar", multiline=True)
    assert viewer.search_matches[0][:2] == (0, 0)  # from line 0 on...
    assert viewer._text_highlight_segments(viewer.search_matches[0]).keys() == {0, 1, 2, 3}
    viewer.start_search("ござ(い|り)ます")
    assert viewer.search_matches == []
    viewer = text_viewer(tmp_path, ["ありがとうござ", "います"])
    viewer.start_search("ござ(い|り)ます", multiline=True)
    assert viewer.search_matches == [(0, 5, 7 + 1 + 3)]


def test_ctrl_t_at_the_prompt_toggles_multi_line(tmp_path, capsys):
    editor = pdfless._LineEditor()
    assert editor.handle("\x14") == "changed"
    assert editor.multiline
    assert editor.handle("\x14") == "changed"
    assert not editor.multiline
    assert editor.text == ""  # ^T itself isn't typed in

    viewer = text_viewer(tmp_path, ["x"])
    capsys.readouterr()
    viewer.draw_search_prompt("foo", 3, multiline=True)
    assert "Multi-line /foo" in capsys.readouterr().out


def test_the_prompt_shows_a_ctrl_t_reminder_while_there_is_room(tmp_path, capsys):
    viewer = text_viewer(tmp_path, ["x"])
    capsys.readouterr()
    viewer.draw_search_prompt("foo", 3)
    out = capsys.readouterr().out
    assert "^T multi-line" in out
    # ...and the cursor still ends up right after "/foo", not after the hint
    assert out.endswith(f"\x1b[{viewer.rows};5H\x1b[?25h")
    viewer.draw_search_prompt("foo", 3, multiline=True)
    assert "^T single-line" in capsys.readouterr().out
    long_query = "x" * (viewer.cols - 5)
    viewer.draw_search_prompt(long_query, len(long_query))
    assert "^T" not in capsys.readouterr().out


def test_a_regex_stays_within_a_printed_line_in_a_pdf(sample_pdf):
    """In the page/bbox index a page is one run of words, so
    "labore.*labore" would box half a page, from one "labore" to
    another lines on - a regex is matched one printed line at a time,
    unless ^T turned multi-line on."""
    handler = pdfless.PdfDocument(sample_pdf)
    index = handler.build_search_index()
    line_height = max(w[5] - w[3] for w in index[1]["words"])
    per_line = handler.find_search_matches(index, "labore.*labore")
    assert per_line and all(m[4] - m[2] <= line_height for m in per_line)
    across = handler.find_search_matches(index, "labore.*labore", multiline=True)
    assert max(m[4] - m[2] for m in across) > 10 * line_height


def test_printed_lines_are_found_in_both_word_orders():
    page = page_of(
        ("first", 50, 100, 90, 112), ("line", 95, 100, 120, 112),
        ("second", 50, 120, 100, 132),
        ("right", 300, 100, 340, 112),  # a second column, read after the first
    )
    assert [page["text"][a:b] for a, b in page["lines"]] == ["first line", "second", "right"]
    rows = page["rows"]
    assert [rows["text"][a:b] for a, b in rows["lines"]] == ["first line right", "second"]
    assert pdfless.PdfDocument.find_search_matches([page], "first.*second") == []
    assert pdfless.PdfDocument.find_search_matches([page], "line.*right") != []  # the same screen row


def test_a_full_width_space_can_be_typed_at_the_prompt():
    """str.isprintable() is False for "　" (U+3000), which a Japanese
    input method types - the prompt still takes it."""
    editor = pdfless._LineEditor()
    for key in "ゲーム　ボーナス":
        editor.handle(key)
    assert editor.text == "ゲーム　ボーナス"
    assert editor.handle("\t") is None  # other non-printables are still swallowed


def test_a_space_alone_is_boxed_as_the_gap_between_two_words():
    """A PDF has no space characters - the index joins its words with
    them - so a search for " " is boxed as the gap between two words on
    the same line, and a line break isn't boxed at all."""
    page = page_of(
        ("foo", 50, 100, 80, 112), ("bar", 90, 100, 120, 112),
        ("baz", 50, 120, 80, 132),
    )
    assert pdfless.PdfDocument.find_search_matches([page], " ") == [(1, 80, 100, 90, 112)]
    assert pdfless.PdfDocument.find_search_matches([page], "　") == [(1, 80, 100, 90, 112)]
