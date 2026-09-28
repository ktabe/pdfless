"""`t`/`T` between image mode and text mode keeps the place being read:
the line a quarter of the way down the screen in one is put at the same
height in the other (Viewer._image_point_for_text() and
_text_line_for_image_point()), and switching straight back returns to
exactly where the view was (Viewer._switch_back_spot())."""

import fcntl
import pty
import struct
import tempfile
import termios

import pytest
from conftest import requires_markdown_rendering

import pdfless


def make_viewer(handler, monkeypatch, rows=20):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, 100, 800, rows * 16))
    viewer = pdfless.Viewer([handler], 0, 1, tempfile.mkdtemp(), slave, "width")
    viewer.refresh()
    return viewer


def printed_line_at_reading_point(viewer):
    """The text of the printed line image mode is being read at."""
    page, y_pt = viewer._image_reading_point()
    lines = pdfless.PdfDocument.screen_lines(viewer.search.ensure_index()[page - 1])
    return next(text for _top, bottom, text in lines if bottom >= y_pt).strip()


def test_interpolate():
    points = [(0, 0.0), (10, 1.0), (30, 2.0)]
    assert pdfless._interpolate(points, 5) == 0.5
    assert pdfless._interpolate(points, 20) == 1.5
    assert pdfless._interpolate(points, -1) == 0.0
    assert pdfless._interpolate(points, 99) == 2.0


def test_screen_lines_are_rows_as_they_appear():
    """A two-column row is one screen line, both columns in it."""
    found = [
        ("left", 10, 100, 40, 112), ("column", 45, 100, 90, 112),
        ("more", 10, 120, 40, 132),
        ("right", 300, 100, 340, 112),  # pdftotext gives the second column last
    ]
    page = pdfless.PdfDocument._index_page(600, 800, found)
    assert pdfless.PdfDocument.screen_lines(page) == [
        (100, 112, "left column right"), (120, 132, "more"),
    ]


def test_a_text_line_is_found_among_the_printed_ones():
    lines = [(100.0, 112.0, "Intro"), (200.0, 212.0, "Ｓｕｍｍａｒｙ   of it"), (400.0, 412.0, "summary of it")]
    assert pdfless._screen_line_for_text(lines, "Summary of it", 380) == 400.0  # nearest of two
    assert pdfless._screen_line_for_text(lines, "Summary of it", 0) == 200.0
    # pdftotext -layout can run a line into the next one's start.
    assert pdfless._screen_line_for_text(lines, "Intro      Summary", 0) == 100.0
    assert pdfless._screen_line_for_text(lines, "   ", 0) is None


def test_entering_text_mode_shows_the_line_image_mode_was_at(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.go_page(3, 0)
    viewer.scroll = viewer._clamp_image_scroll(viewer.img.height // 3)
    printed = printed_line_at_reading_point(viewer)
    assert viewer.enter_text_mode()
    assert viewer.text_lines[viewer._text_reading_line()].strip() == printed


def test_leaving_text_mode_shows_the_line_text_mode_was_at(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.go_page(2, 0)
    assert viewer.enter_text_mode()
    for _ in range(6):
        viewer.handle_key_text("j")
    reading = viewer.text_lines[viewer._text_reading_line()].strip()
    assert reading  # (a blank line would map to the next one with text)
    viewer.exit_text_mode()
    assert viewer.scroll > 0
    assert printed_line_at_reading_point(viewer) == reading


def test_straight_back_is_exactly_where_it_was(sample_pdf, monkeypatch):
    """Page 1's text fits the screen, so text mode can't scroll to match
    - without the switch remembering, t t would land somewhere else."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    for page in (1, 3):
        viewer.go_page(page, 0)
        viewer.scroll = viewer._clamp_image_scroll(viewer.img.height // 2)
        before = (viewer.page, viewer.scroll)
        assert viewer.enter_text_mode()
        viewer.exit_text_mode()
        assert (viewer.page, viewer.scroll) == before


def test_moving_in_between_maps_the_new_place_instead(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.go_page(2, 0)
    assert viewer.enter_text_mode()
    text_before = viewer.text_scroll
    viewer.exit_text_mode()
    viewer.scroll_down(viewer.cell_h_px * 10)
    printed = printed_line_at_reading_point(viewer)
    assert viewer.enter_text_mode()
    assert viewer.text_scroll != text_before
    assert viewer.text_lines[viewer._text_reading_line()].strip() == printed


@requires_markdown_rendering
def test_markdown_maps_through_its_headings(sample_md, tmp_path, monkeypatch):
    """Markdown's text mode is its source: a heading's line and where its
    bookmark points are the same place, and so on in between."""
    for cls in pdfless.HANDLER_CLASSES:
        handler = cls.sniff(sample_md, str(tmp_path))
        if handler is not None:
            break
    viewer = make_viewer(handler, monkeypatch)
    viewer.overlays.show_outline()
    viewer.overlays.handle_key("g")
    for _ in range(4):
        viewer.overlays.handle_key("j")
    viewer.overlays.handle_key("\r")  # image mode, at "## リンクの例"
    viewer.handle_key("j")  # moved on from where the jump left it
    viewer.handle_key("k")
    assert viewer.enter_text_mode()
    assert viewer.text_lines[viewer._text_reading_line()] == "## リンクの例"

    # (line, position in pages) of "## リンクの例" and of the heading after it.
    _all, *headings, _end = viewer.overlays.source_anchors()
    (line4, at4), (line5, at5) = headings[4], headings[5]
    for _ in range(2):
        viewer.handle_key_text("j")  # two lines into the section
    assert viewer._text_reading_line() == line4 + 2 < line5
    viewer.exit_text_mode()
    page, y_pt = viewer._image_reading_point()
    position = page - 1 + y_pt / viewer._page_height_pt(page)
    assert at4 < position < at5  # past the heading, and as far as between the two
    assert position == pytest.approx(at4 + (at5 - at4) * 2 / (line5 - line4), abs=0.01)
