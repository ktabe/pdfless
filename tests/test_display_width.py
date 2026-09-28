"""char_width()/display_width(): how many terminal columns text takes -
what the status line, text mode and the boxes over the page are fitted
by. Combining characters take none of their own, whether or not they
have a combining class."""

import unicodedata

import pytest

import pdfless


@pytest.mark.parametrize("text, width", [
    ("abc", 3),
    ("日本語", 6),
    ("グ", 2),                      # NFD kana: a combining dakuten
    ("é", 1),                       # NFD Latin: a combining accent
    ("葛\U000E0100", 2),                  # an ideographic variation selector
    (unicodedata.normalize("NFD", "한국"), 4),  # decomposed Hangul syllables
    ("กั", 1),                  # a Thai vowel sign (no combining class)
    ("1⃝", 1),                       # an enclosing circle
    ("a​b", 2),                      # a zero width space
    ("❤️", 2),                  # VS16: the two-column emoji form
    ("a­b", 3),                      # a soft hyphen still takes its column
])
def test_display_width(text, width):
    assert pdfless.display_width(text) == width


def test_cutting_the_start_leaves_no_mark_without_its_character():
    """Cut just past a character, the mark that goes with it goes too,
    instead of hanging off the "…"."""
    ivs = "資料葛\U000E0100.pdf"  # 葛󠄀 takes 2 columns, .pdf 4
    assert pdfless.truncate_start_to_width(ivs, 7) == "…葛\U000E0100.pdf"
    assert pdfless.truncate_start_to_width(ivs, 6) == "….pdf"
    hangul = unicodedata.normalize("NFD", "자료한.pdf")
    cut = pdfless.truncate_start_to_width(hangul, 7)
    assert cut == "…" + unicodedata.normalize("NFD", "한") + ".pdf"
    assert pdfless.display_width(cut) == 7
