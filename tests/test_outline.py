"""The table of contents (o/TAB): PdfDocument.build_outline() reading a
PDF's bookmarks, and the Viewer's box listing them - selecting the
section being read, moving the selection, and jumping to an entry.
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
    assert not viewer.outline_active


def test_opening_selects_the_section_being_read(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    viewer.go_page(3, 0)
    assert viewer.handle_global_key("\t")
    assert viewer.outline_active
    assert viewer.outline_sel == 1  # "1.1 背景" starts on page 2
    viewer.handle_outline_key("q")
    assert not viewer.outline_active
    assert viewer.page == 3


def test_enter_jumps_to_the_entry_and_back_returns(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    viewer.show_outline()
    viewer.handle_outline_key("j")
    viewer.handle_outline_key("p")  # swallowed: no page turn underneath
    assert viewer.page == 1
    viewer.handle_outline_key("\r")
    assert not viewer.outline_active
    assert viewer.page == 2
    assert viewer.scroll > 0  # halfway down page 2, not its top

    viewer.show_outline()
    viewer.handle_outline_key("G")
    assert viewer.outline_sel == 3
    viewer.handle_outline_key("k")
    viewer.handle_outline_key("\r")
    assert (viewer.page, viewer.scroll) == (4, 0)  # /Fit: the page's top

    viewer.go_back()
    assert viewer.page == 2


def test_text_mode_jumps_to_the_top_of_the_page(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    assert viewer.enter_text_mode()
    viewer.show_outline()
    viewer.handle_outline_key("G")
    viewer.handle_outline_key("\r")
    assert viewer.text_mode
    assert viewer.page == 6


def test_clicking_an_entry_jumps_and_clicking_outside_closes(outlined_pdf, monkeypatch):
    viewer = make_viewer(outlined_pdf, monkeypatch)
    viewer.show_outline()
    row0, col0, content_h, _content_w = viewer._outline_box()
    assert content_h == 4
    viewer.handle_mouse("MOUSE_CLICK", col0 + 2, row0 + 3)  # the third entry
    assert not viewer.outline_active
    assert viewer.page == 4

    viewer.show_outline()
    viewer.handle_mouse("MOUSE_WHEEL_DOWN", 1, 1)
    assert viewer.outline_sel == 3
    viewer.handle_mouse("MOUSE_CLICK", 1, 1)  # outside the box
    assert not viewer.outline_active
    assert viewer.page == 4


def test_a_long_outline_scrolls_with_the_selection(sample_pdf, tmp_path, monkeypatch):
    writer = pages_only(sample_pdf)
    for i in range(60):
        writer.add_outline_item(f"見出し {i + 1}", i % 7)
    path = tmp_path / "long.pdf"
    writer.write(str(path))
    assert len(PdfReader(str(path)).outline) == 60

    viewer = make_viewer(str(path), monkeypatch)
    viewer.show_outline()
    content_h = viewer._outline_box()[2]
    assert content_h < 60
    viewer.handle_outline_key("G")
    assert viewer.outline_sel == 59
    assert viewer.outline_scroll == 60 - content_h
    viewer.handle_outline_key("g")
    assert (viewer.outline_sel, viewer.outline_scroll) == (0, 0)


def test_the_box_blanks_its_cells_before_drawing_over_them(outlined_pdf, monkeypatch, capsys):
    """Over text mode's lines, a box cell can be the right half of a
    double-width character, and a wide character of the box's written
    there shifts the rest of the row in iTerm2 - so every row of the box
    is blanked with plain spaces first, before any of it is drawn."""
    viewer = make_viewer(outlined_pdf, monkeypatch)
    capsys.readouterr()
    viewer.show_outline()
    out = capsys.readouterr().out
    row0, col0, content_h, content_w = viewer._outline_box()
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
    viewer.show_outline()
    assert viewer.outline_active
    viewer.handle_outline_key("G")
    viewer.handle_outline_key("\r")
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
