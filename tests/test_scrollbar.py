"""The scrollbar (Viewer._scrollbar_column()): one column at the
terminal's right edge, on by default (--no-scrollbar to start it off),
in both image mode (_draw(), via base_width_px - see
_recompute_geometry()) and text mode (_draw_text_wrapped()/
_draw_text_unwrapped(), via _text_avail_cols()). "r" toggles it either
way, applying to both modes uniformly since it's handled at
run_viewer()'s top level rather than per-mode. Passive/visual only -
no click-to-jump.

The thumb marks position in the WHOLE document, not just the current
page: a 10-page PDF showing the top half of page 1 puts it in the top
5% of the track (0.5 of one page out of ten) - see
_scrollbar_fractions()."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless


def make_viewer(handler, rows=10, cols=20, wrap=False, scrollbar=True):
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * 8, rows * 18))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer(
        [handler], 0, 1, tmpdir, slave, None, wrap=wrap, scrollbar=scrollbar,
    )
    viewer._load_page = lambda: None
    viewer._draw = lambda: None
    viewer.refresh()
    return viewer


def make_numbered_text(tmp_path, n_lines):
    path = tmp_path / "numbered.txt"
    path.write_text("\n".join(f"line {i}" for i in range(1, n_lines + 1)) + "\n")
    return str(path)


def thumb_rows(cells):
    """Indices of the rows the thumb covers, out of a _scrollbar_column()
    result."""
    return [i for i, cell in enumerate(cells) if pdfless.SCROLLBAR_THUMB in cell]


def test_text_avail_cols_reserves_one_column_for_the_scrollbar(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), cols=20)
    assert viewer._text_avail_cols() == 19


def test_text_avail_cols_reserves_nothing_when_scrollbar_off(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), cols=20, scrollbar=False)
    assert viewer._text_avail_cols() == 20


def test_scrollbar_is_full_thumb_when_content_fits_on_screen(tmp_path):
    path = make_numbered_text(tmp_path, n_lines=3)
    viewer = make_viewer(pdfless.TextDocument(path), rows=10, wrap=False)
    viewer._load_text_page()
    assert viewer.text_scroll_max <= viewer.text_scroll_min  # nothing to scroll
    avail_rows = viewer._text_avail_rows()
    start_frac, visible_frac = viewer._text_scrollbar_fractions(avail_rows)
    cells = viewer._scrollbar_column(avail_rows, start_frac, visible_frac)
    assert all(pdfless.SCROLLBAR_THUMB in cell for cell in cells)


def test_scrollbar_thumb_moves_from_top_to_bottom(tmp_path):
    path = make_numbered_text(tmp_path, n_lines=30)
    viewer = make_viewer(pdfless.TextDocument(path), rows=10, wrap=False)
    viewer._load_text_page()
    assert viewer.text_scroll_max > viewer.text_scroll_min  # scrolling is possible
    avail_rows = viewer._text_avail_rows()

    viewer.text_scroll = viewer.text_scroll_min
    top_cells = viewer._scrollbar_column(
        avail_rows, *viewer._text_scrollbar_fractions(avail_rows)
    )
    assert pdfless.SCROLLBAR_THUMB in top_cells[0]
    assert pdfless.SCROLLBAR_TRACK in top_cells[-1]

    viewer.text_scroll = viewer.text_scroll_max
    bottom_cells = viewer._scrollbar_column(
        avail_rows, *viewer._text_scrollbar_fractions(avail_rows)
    )
    assert pdfless.SCROLLBAR_TRACK in bottom_cells[0]
    assert pdfless.SCROLLBAR_THUMB in bottom_cells[-1]


def test_scrollbar_works_the_same_way_wrapped(tmp_path):
    path = make_numbered_text(tmp_path, n_lines=30)
    viewer = make_viewer(pdfless.TextDocument(path), rows=10, wrap=True)
    viewer._load_text_page()
    assert viewer.text_scroll_min == 0  # no border-revealing -1 while wrapped
    assert viewer.text_scroll_max > viewer.text_scroll_min

    avail_rows = viewer._text_avail_rows()
    viewer.text_scroll = viewer.text_scroll_max
    cells = viewer._scrollbar_column(
        avail_rows, *viewer._text_scrollbar_fractions(avail_rows)
    )
    assert pdfless.SCROLLBAR_THUMB in cells[-1]


def test_fractions_span_the_whole_document_not_just_the_page(sample_pdf):
    """The reported case: a 10-page document showing the top half of
    page 1 belongs in the top 5% of the track, not the top 50%."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), rows=10, cols=40)
    # start=0 (top of the page), half the page visible, page 1 of 10.
    start_frac, visible_frac = viewer._scrollbar_fractions(
        start=0, avail_extent=50, total_extent=100, page=1, npages=10
    )
    assert start_frac == 0.0
    assert abs(visible_frac - 0.05) < 1e-9

    # Halfway down page 6 of 10 - 55% + half a page's worth in.
    start_frac, visible_frac = viewer._scrollbar_fractions(
        start=50, avail_extent=50, total_extent=100, page=6, npages=10
    )
    assert abs(start_frac - 0.55) < 1e-9
    assert abs(visible_frac - 0.05) < 1e-9


def test_fractions_reduce_to_the_page_itself_for_a_single_page_view(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), rows=10, cols=40)
    start_frac, visible_frac = viewer._scrollbar_fractions(
        start=25, avail_extent=50, total_extent=100, page=1, npages=1
    )
    assert abs(start_frac - 0.25) < 1e-9
    assert abs(visible_frac - 0.5) < 1e-9


def test_thumb_sits_near_the_top_for_page_one_of_many(sample_pdf):
    """The same case again, but through _scrollbar_column() - the thumb
    lands at the very top and covers only a small slice of the track."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), rows=10, cols=40)
    start_frac, visible_frac = viewer._scrollbar_fractions(
        start=0, avail_extent=50, total_extent=100, page=1, npages=10
    )
    cells = viewer._scrollbar_column(20, start_frac, visible_frac)
    rows = thumb_rows(cells)
    assert rows[0] == 0  # pinned to the top of the track
    assert len(rows) == 1  # 5% of 20 rows, rounded to a 1-row minimum


def test_thumb_reaches_the_bottom_on_the_last_page(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), rows=10, cols=40)
    # Bottom of page 10 of 10: the window's top edge is half a page in,
    # and the bottom half of that page is what's on screen.
    start_frac, visible_frac = viewer._scrollbar_fractions(
        start=50, avail_extent=50, total_extent=100, page=10, npages=10
    )
    cells = viewer._scrollbar_column(20, start_frac, visible_frac)
    assert thumb_rows(cells)[-1] == 19  # last row of the track


def test_toggle_scrollbar_off_frees_the_reserved_column(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), cols=20, scrollbar=True)
    assert viewer._text_avail_cols() == 19
    viewer.toggle_scrollbar()
    viewer.refresh()
    assert viewer.scrollbar is False
    assert viewer._text_avail_cols() == 20
    viewer.toggle_scrollbar()
    viewer.refresh()
    assert viewer.scrollbar is True
    assert viewer._text_avail_cols() == 19


def test_scrollbar_off_by_default_with_no_scrollbar(sample_text):
    """scrollbar=False here stands in for --no-scrollbar - main() passes
    scrollbar=args.scrollbar."""
    viewer = make_viewer(pdfless.TextDocument(sample_text), scrollbar=False)
    assert viewer.scrollbar is False


def test_image_mode_base_width_px_reserves_one_cell_for_scrollbar(sample_pdf):
    with_scrollbar = make_viewer(pdfless.PdfDocument(sample_pdf), cols=40, scrollbar=True)
    without_scrollbar = make_viewer(pdfless.PdfDocument(sample_pdf), cols=40, scrollbar=False)
    assert with_scrollbar.base_width_px == without_scrollbar.base_width_px - with_scrollbar.cell_w_px


def test_office_text_mode_ignores_its_image_mode_page_count(tmp_path):
    """A non-paginated text view (Office/text/RTF) shows the whole
    document in one continuous text_scroll range, so its scrollbar must
    not be divided into self.npages page-sized slices the way a PDF's
    per-page text mode is - see _text_scrollbar_fractions()."""
    path = make_numbered_text(tmp_path, n_lines=30)
    viewer = make_viewer(pdfless.TextDocument(path), rows=10, wrap=False)
    viewer._load_text_page()
    assert viewer.doc_handler.text_mode_is_paginated() is False
    viewer.npages = 10  # as if image mode had found ten pages/slides
    viewer.text_scroll = viewer.text_scroll_max
    avail_rows = viewer._text_avail_rows()
    cells = viewer._scrollbar_column(
        avail_rows, *viewer._text_scrollbar_fractions(avail_rows)
    )
    assert pdfless.SCROLLBAR_THUMB in cells[-1]  # still spans to the bottom
