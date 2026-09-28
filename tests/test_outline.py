"""The table of contents (o/TAB): PdfDocument.build_outline() reading a
PDF's bookmarks, and the Viewer's box listing them - selecting the
section being read, moving the selection, and jumping to an entry.
The help box (F1) is the other overlay, taking the same keys and mouse
events (see _Overlays.handle_key()).
Also the formats that get their bookmarks from the PDF they're rendered
to: Markdown's headings (WeasyPrint), and Word's headings or
PowerPoint's slide titles (LibreOffice)."""

import fcntl
import pty
import struct
import tempfile
import termios
import unicodedata

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import Fit

import pdfless
from conftest import requires_markdown_rendering, requires_soffice


def pages_only(sample_pdf):
    """A PdfWriter holding sample_pdf's 7 pages, without the outline
    it comes with (see test_the_sample_pdfs_own_outline)."""
    writer = PdfWriter()
    writer.append(sample_pdf, import_outline=False)
    return writer


@pytest.fixture
def unoutlined_pdf(sample_pdf, tmp_path):
    path = tmp_path / "plain.pdf"
    pages_only(sample_pdf).write(str(path))
    return str(path)


@pytest.fixture
def outlined_pdf(sample_pdf, tmp_path):
    """sample_pdf's pages with a two-level outline of its own:

        第1章 はじめに      page 1 (/XYZ, near the top)
          1.1 背景          page 2 (/XYZ, halfway down)
        Chapter 2           page 4 (/Fit - no position on the page)
          2.1 Details       page 6 (/FitH, halfway down)
    """
    writer = pages_only(sample_pdf)
    ch1 = writer.add_outline_item("第1章 はじめに", 0, fit=Fit.xyz(top=800))
    writer.add_outline_item("1.1\n背景", 1, parent=ch1, fit=Fit.xyz(top=420))
    ch2 = writer.add_outline_item("Chapter 2", 3, fit=Fit.fit())
    writer.add_outline_item("2.1 Details", 5, parent=ch2, fit=Fit.fit_horizontally(top=420))
    path = tmp_path / "outlined.pdf"
    writer.write(str(path))
    return str(path)


def make_viewer(path, monkeypatch, handler=None):
    """A Viewer on a 100x30 pty showing the PDF at `path` - or, given
    `handler`, that DocumentHandler instead (`path` is then unused)."""
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 800, 480))
    doc = handler if handler is not None else pdfless.PdfDocument(path)
    viewer = pdfless.Viewer([doc], 0, 1, tempfile.mkdtemp(), slave, "width")
    viewer.refresh()
    return viewer


def test_build_outline_flattens_levels_and_resolves_destinations(outlined_pdf):
    entries = pdfless.PdfDocument(outlined_pdf).build_outline()
    assert [(e["title"], e["level"], e["page"]) for e in entries] == [
        ("第1章 はじめに", 0, 1),
        ("1.1 背景", 1, 2),  # the newline collapsed to a space
        ("Chapter 2", 0, 4),
        ("2.1 Details", 1, 6),
    ]
    assert [e["top_pt"] for e in entries] == [800, 420, None, 420]


def test_the_sample_pdfs_own_outline(sample_pdf):
    """lorem_ipsum.pdf's LaTeX-made bookmarks: the title, then one
    chapter per page."""
    entries = pdfless.PdfDocument(sample_pdf).build_outline()
    assert [(e["title"], e["level"], e["page"]) for e in entries] == (
        [("Lorem Ipsum", 0, 1)] + [(f"Chapter {i}", 1, i + 1) for i in range(1, 7)]
    )
    assert all(e["top_pt"] for e in entries)


def test_decomposed_titles_are_normalized(unoutlined_pdf, tmp_path):
    """A title made from a macOS file name is NFD ("グ" as "ク" +
    U+3099) - read back as NFC, so its width is counted right."""
    writer = PdfWriter(clone_from=unoutlined_pdf)
    writer.add_outline_item(unicodedata.normalize("NFD", "グループ.pdf"), 0)
    path = tmp_path / "nfd.pdf"
    writer.write(str(path))
    [entry] = pdfless.PdfDocument(str(path)).build_outline()
    assert entry["title"] == unicodedata.normalize("NFC", "グループ.pdf")


def test_combining_marks_take_no_column():
    nfd = unicodedata.normalize("NFD", "グループ")
    assert len(nfd) == 6
    assert pdfless.display_width(nfd) == pdfless.display_width("グループ") == 8
    assert pdfless.truncate_to_width(nfd, 2) == nfd[:2]  # the dakuten stays with its kana


def test_no_outline_is_just_a_status_message(unoutlined_pdf, monkeypatch):
    assert pdfless.PdfDocument(unoutlined_pdf).build_outline() == []
    viewer = make_viewer(unoutlined_pdf, monkeypatch)
    assert viewer.handle_global_key("o")
    assert viewer.overlays.active is None


def test_opening_selects_the_section_being_read(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    viewer.go_page(3, 0)
    assert viewer.handle_global_key("\t")
    assert viewer.overlays.active == "outline"
    assert viewer.overlays.outline_sel == 1  # "1.1 背景" starts on page 2
    viewer.overlays.handle_key("q")
    assert viewer.overlays.active is None
    assert viewer.page == 3


def test_enter_jumps_to_the_entry_and_back_returns(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    viewer.overlays.show_outline()
    viewer.overlays.handle_key("j")
    viewer.overlays.handle_key("p")  # swallowed: no page turn underneath
    assert viewer.page == 1
    viewer.overlays.handle_key("\r")
    assert viewer.overlays.active is None
    assert viewer.page == 2
    assert viewer.scroll > 0  # halfway down page 2, not its top

    viewer.overlays.show_outline()
    viewer.overlays.handle_key("G")
    assert viewer.overlays.outline_sel == 3
    viewer.overlays.handle_key("k")
    viewer.overlays.handle_key("\r")
    assert (viewer.page, viewer.scroll) == (4, 0)  # /Fit: the page's top

    viewer.links.go_back()
    assert viewer.page == 2


def test_text_mode_jumps_to_the_top_of_the_page(outlined_pdf, monkeypatch):
    """None of outlined_pdf's titles is in its (lorem ipsum) text, so
    there's no line to jump to: the top of the entry's page it is."""
    viewer = make_viewer(outlined_pdf, monkeypatch)
    assert viewer.enter_text_mode()
    viewer.overlays.show_outline()
    viewer.overlays.handle_key("G")
    viewer.overlays.handle_key("\r")
    assert viewer.text_mode
    assert viewer.page == 6


def test_clicking_an_entry_jumps_and_clicking_outside_closes(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    viewer.overlays.show_outline()
    row0, col0, content_h, _content_w = viewer.overlays.outline_box()
    assert content_h == 4
    viewer.mouse.handle("MOUSE_CLICK", col0 + 2, row0 + 3)  # the third entry
    assert viewer.overlays.active is None
    assert viewer.page == 4

    viewer.overlays.show_outline()
    viewer.mouse.handle("MOUSE_WHEEL_DOWN", 1, 1)
    assert viewer.overlays.outline_sel == 3
    viewer.mouse.handle("MOUSE_CLICK", 1, 1)  # outside the box
    assert viewer.overlays.active is None
    assert viewer.page == 4


def test_a_long_outline_scrolls_with_the_selection(sample_pdf, tmp_path, monkeypatch):
    writer = pages_only(sample_pdf)
    for i in range(60):
        writer.add_outline_item(f"見出し {i + 1}", i % 7)
    path = tmp_path / "long.pdf"
    writer.write(str(path))
    assert len(PdfReader(str(path)).outline) == 60

    viewer = make_viewer(str(path), monkeypatch)
    viewer.overlays.show_outline()
    content_h = viewer.overlays.outline_box()[2]
    assert content_h < 60
    viewer.overlays.handle_key("G")
    assert viewer.overlays.outline_sel == 59
    assert viewer.overlays.outline_scroll == 60 - content_h
    viewer.overlays.handle_key("g")
    assert (viewer.overlays.outline_sel, viewer.overlays.outline_scroll) == (0, 0)


def test_the_box_blanks_its_cells_before_drawing_over_them(outlined_pdf, monkeypatch, capsys):
    """Over text mode's lines, a box cell can be the right half of a
    double-width character, and a wide character of the box's written
    there shifts the rest of the row in iTerm2 - so every row of the box
    is blanked with plain spaces first, before any of it is drawn."""
    viewer = make_viewer(outlined_pdf, monkeypatch)
    capsys.readouterr()
    viewer.overlays.show_outline()
    out = capsys.readouterr().out
    row0, col0, content_h, content_w = viewer.overlays.outline_box()
    first_border = out.index(f"\x1b[{row0};{col0}H┌")
    for row in range(row0, row0 + content_h + 2):
        assert out.index(f"\x1b[{row};{col0}H{' ' * (content_w + 4)}") < first_border


def rendered_handler(path, tmp_path):
    """The DocumentHandler pdfless would open `path` with (as main()
    picks one), with its pages already rendered."""
    for cls in pdfless.HANDLER_CLASSES:
        handler = cls.sniff(path, str(tmp_path))
        if handler is not None:
            handler.build_pages(str(tmp_path))
            return handler
    raise AssertionError(f"no handler for {path}")


def outline_of(handler):
    """`handler`'s table of contents, as (level, title, page) triples."""
    return [(e["level"], e["title"], e["page"]) for e in handler._pdf_delegate.build_outline()]


@requires_markdown_rendering
def test_a_markdown_files_headings_are_its_outline(sample_md, tmp_path, monkeypatch):
    """WeasyPrint turns every heading into a bookmark, so a Markdown
    file's table of contents is its headings - # at level 0, ## at 1."""
    handler = rendered_handler(sample_md, tmp_path)
    assert isinstance(handler, pdfless.MarkdownDocument)
    outline = outline_of(handler)
    assert outline[:2] == [(0, "Lorem Ipsum Sample", 1), (1, "リストの例", 1)]
    assert (1, "追加セクション2", 2) in outline

    viewer = make_viewer(None, monkeypatch, handler)
    viewer.overlays.show_outline()
    assert viewer.overlays.active == "outline"
    viewer.overlays.handle_key("G")
    viewer.overlays.handle_key("\r")
    assert viewer.page == 2
    assert viewer.scroll > 0  # at the heading, not the page's top


@requires_soffice
def test_word_headings_are_its_outline(sample_headings_docx, tmp_path):
    """LibreOffice exports Word's heading styles as bookmarks."""
    handler = rendered_handler(sample_headings_docx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    assert outline_of(handler) == [
        (0, "第1章 はじめに", 1), (1, "1.1 背景", 1), (0, "Chapter 2", 2),
    ]


@requires_soffice
def test_powerpoint_slide_titles_are_its_outline(sample_twoslide_pptx, tmp_path):
    """LibreOffice exports each slide's title as a bookmark."""
    handler = rendered_handler(sample_twoslide_pptx, tmp_path)
    assert outline_of(handler) == [(0, "Lorem Ipsum", 1), (0, "サンプルスライド", 2)]


def open_overlay(viewer, which):
    """Put up the help or the table of contents, by its own key."""
    viewer.handle_global_key("F1" if which == "help" else "o")
    assert viewer.overlays.active == which


@pytest.mark.parametrize("which, key", [
    ("help", "q"), ("help", "\x1b"), ("help", "F1"),
    ("outline", "q"), ("outline", "\x1b"), ("outline", "o"), ("outline", "\t"),
])
def test_q_esc_or_its_own_key_closes_either_overlay(outlined_pdf, monkeypatch, which, key):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    open_overlay(viewer, which)
    viewer.overlays.handle_key(key)
    assert viewer.overlays.active is None


def test_the_help_scrolls_with_the_same_keys_as_the_outline(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    open_overlay(viewer, "help")
    content_h = viewer.overlays.help_box()[2]
    max_scroll = len(pdfless.KEY_TABLE.splitlines()) - content_h
    assert max_scroll > 0  # KEY_TABLE is taller than a 30-row terminal
    viewer.overlays.handle_key("G")
    assert viewer.overlays.help_scroll == max_scroll
    viewer.overlays.handle_key("u")
    assert viewer.overlays.help_scroll == max_scroll - (content_h - 1)
    viewer.overlays.handle_key("g")
    assert viewer.overlays.help_scroll == 0
    viewer.overlays.handle_key("p")  # swallowed: no page turn underneath
    assert (viewer.overlays.active, viewer.page) == ("help", 1)


def test_the_wheel_scrolls_the_help_and_a_click_outside_closes_it(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    open_overlay(viewer, "help")
    viewer.mouse.handle("MOUSE_WHEEL_DOWN", 1, 1)
    assert viewer.overlays.help_scroll == 1
    row0, col0, _content_h, _content_w = viewer.overlays.help_box()
    viewer.mouse.handle("MOUSE_CLICK", col0 + 2, row0 + 1)  # inside: nothing to click
    assert viewer.overlays.active == "help"
    viewer.mouse.handle("MOUSE_CLICK", 1, 1)
    assert viewer.overlays.active is None


@pytest.mark.parametrize("which", ["help", "outline"])
def test_ctrl_l_repaints_the_page_then_the_box(outlined_pdf, monkeypatch, which):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    open_overlay(viewer, which)
    drawn = []
    monkeypatch.setattr(viewer, "_draw", lambda: drawn.append("page"))
    monkeypatch.setattr(viewer.overlays, "draw_box", lambda *a, **k: drawn.append("box") or (0, 0))
    viewer.overlays.handle_key("\x0c")
    assert drawn == ["page", "box"]
    assert viewer.overlays.active == which


@pytest.mark.parametrize("which", ["help", "outline"])
def test_the_next_page_is_still_prefetched_under_either_overlay(outlined_pdf, monkeypatch, which):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    open_overlay(viewer, which)
    viewer._schedule_page_prefetch()
    assert viewer._page_prefetch_thread.name == "pdfless-page-prefetch-2"
    viewer._page_prefetch_thread.join(10)


def test_a_title_is_found_however_its_spacing_and_width_differ():
    lines = ["1.1   背景", "Ｃｈａｐｔｅｒ　２ Details", "chapter 2"]
    assert pdfless.find_title_line(lines, 0, 3, "1.1 背景") == 0
    assert pdfless.find_title_line(lines, 0, 3, "Chapter 2") == 1
    assert pdfless.find_title_line(lines, 2, 3, "Chapter 2") == 2  # only within the range
    assert pdfless.find_title_line(lines, 0, 3, "Chapter 3") is None


def test_the_line_nearest_the_bookmark_wins():
    lines = ["Summary"] + ["text"] * 8 + ["Summary"]
    assert pdfless.find_title_line(lines, 0, 10, "Summary") == 0
    assert pdfless.find_title_line(lines, 0, 10, "Summary", near=0.9) == 9


def test_markdown_headings_skip_code_blocks():
    source = [
        "# Title",           # 0
        "",
        "```sh",
        "# not a heading",
        "```",
        "Setext",            # 5
        "======",
        "",
        "    # indented code",
        "",
        "text",
        "",
        "---",               # a rule after a blank line, not a heading
        "~~~~",
        "## still code",
        "~~~",               # too short to close a ~~~~ fence
        "~~~~",
        "Another",           # 17
        "-------",
        "###### Six",        # 19
    ]
    assert pdfless.markdown_heading_lines(source) == [0, 5, 17, 19]


def outlined_at_a_phrase(sample_pdf, tmp_path):
    """sample_pdf's pages with an outline entry whose title is a phrase
    from the middle of page 2's text, pointing at where it is."""
    phrase = "Reprehenderit amet nostrud"
    lines = pdfless.PdfDocument(sample_pdf).extract_text(2)
    [line] = [i for i, text in enumerate(lines) if phrase in text]
    writer = pages_only(sample_pdf)
    height = float(writer.pages[1].mediabox.height)
    writer.add_outline_item("Start", 0)
    writer.add_outline_item(phrase, 1, fit=Fit.xyz(top=height * (1 - line / len(lines))))
    path = tmp_path / "phrase.pdf"
    writer.write(str(path))
    return str(path), phrase


def test_text_mode_jumps_to_the_line_the_title_is_on(sample_pdf, tmp_path, monkeypatch):
    path, phrase = outlined_at_a_phrase(sample_pdf, tmp_path)
    viewer = make_viewer(path, monkeypatch)
    assert viewer.enter_text_mode()
    viewer.overlays.show_outline()
    viewer.overlays.handle_key("G")
    viewer.overlays.handle_key("\r")
    assert viewer.page == 2
    [line] = [i for i, text in enumerate(viewer.text_lines) if phrase in text]
    assert viewer.text_scroll > 0  # not the top of the page ...
    assert viewer.text_scroll <= line < viewer.text_scroll + viewer._text_avail_rows()  # ... but its line


def test_reopening_selects_the_entry_just_jumped_to(sample_pdf, tmp_path, monkeypatch):
    """Two entries on one page: going by the page alone would select the
    later one, whichever was jumped to."""
    writer = pages_only(sample_pdf)
    writer.add_outline_item("Top", 1, fit=Fit.xyz(top=800))
    writer.add_outline_item("Middle", 1, fit=Fit.xyz(top=400))
    path = tmp_path / "two.pdf"
    writer.write(str(path))
    viewer = make_viewer(str(path), monkeypatch)
    viewer.overlays.show_outline()
    viewer.overlays.handle_key("\r")  # "Top"
    viewer.overlays.show_outline()
    assert viewer.overlays.outline_sel == 0
    viewer.overlays.hide()
    viewer.handle_key("j")  # moved on: back to going by the page
    viewer.overlays.show_outline()
    assert viewer.overlays.outline_sel == 1


@requires_markdown_rendering
def test_markdown_text_mode_jumps_to_the_heading_line(sample_md, tmp_path, monkeypatch):
    """Text mode shows the raw source, one page: the n-th heading line
    is the n-th entry's, and the one above where you're reading is the
    section selected."""
    handler = rendered_handler(sample_md, tmp_path)
    viewer = make_viewer(None, monkeypatch, handler)
    assert viewer.enter_text_mode()
    viewer.overlays.show_outline()
    for _ in range(6):
        viewer.overlays.handle_key("j")
    viewer.overlays.handle_key("\r")
    assert viewer.text_lines[viewer.overlays._reading_text_line()] == "## 追加セクション1"
    viewer.overlays.show_outline()
    assert viewer.overlays.outline_sel == 6

    viewer.overlays.hide()
    viewer.handle_key_text("k")  # a line up: still in the section before
    viewer.overlays.show_outline()
    assert viewer.overlays.outline_sel == 5
