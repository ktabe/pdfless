"""Where the horizontal pan sits in image mode (Viewer.x_offset): the
left edge to start with, and wherever you last put it with h/l/H/L
afterwards - a page turn must not slide the view sideways on its own.

The terminal these use is deliberately narrow in pixels (see
make_viewer()) so that a height-fitted page comes out wider than the
window and there is something to pan across at all - the same
situation -h puts you in on a slide deck."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless


def make_viewer(handler, fit="height"):
    _master, slave = pty.openpty()
    # 40x20 cells in 200x400 pixels -> a 5px-wide cell, so the usable
    # width is ~195px while a height-fitted A4 page is ~270px wide.
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 40, 200, 400))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer([handler], 0, 1, tmpdir, slave, fit)
    viewer._draw = lambda: None
    viewer.refresh()
    return viewer


def test_a_page_wider_than_the_window_starts_at_its_left_edge(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf))
    assert viewer.img.width > viewer.crop_width  # there is room to pan
    assert viewer.x_offset == 0


def test_turning_the_page_keeps_the_left_edge(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf))
    viewer.handle_key("L")  # pan to the right edge...
    assert viewer.x_offset > 0
    viewer.handle_key("H")  # ...and back to the left one
    assert viewer.x_offset == 0

    viewer.go_page(2, 0)
    assert viewer.x_offset == 0


def test_turning_the_page_keeps_a_pan_in_the_middle_too(sample_pdf):
    """Not just the left edge: whatever you panned to stays put."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf))
    viewer.handle_key("l")
    panned = viewer.x_offset
    assert panned > 0

    viewer.go_page(2, 0)
    assert viewer.x_offset == panned


def test_scrolling_off_the_bottom_of_a_page_keeps_the_pan(sample_pdf):
    """A height-fitted page has nothing left to scroll, so "j" is a
    page turn - which is how the re-centering this guards against used
    to show up in normal use."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf))
    assert viewer.scroll_max == 0
    viewer.scroll_down(viewer.cell_h_px)
    assert viewer.page == 2
    assert viewer.x_offset == 0


def test_reset_view_goes_back_to_the_left_edge(sample_pdf):
    """"0" is documented as resetting zoom AND pan (see KEY_TABLE), and
    at zoom 1 a height-fitted page can still be wider than the terminal
    - so the pan has to be reset explicitly, not just clamped."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf))
    viewer.handle_key("L")
    assert viewer.x_offset > 0

    viewer.handle_key("0")
    assert viewer.zoom == 1.0
    assert viewer.x_offset == 0


def test_zoom_stays_centered_on_what_was_on_screen(sample_pdf):
    """Zoom is the one thing that does move the pan, deliberately: the
    point in the middle of the window stays in the middle."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), fit="width")
    assert viewer.x_offset == 0  # fits exactly, nothing to pan
    center_frac = (viewer.x_offset + viewer.crop_width / 2) / viewer.img.width

    viewer.set_zoom(3.0)
    new_frac = (viewer.x_offset + viewer.crop_width / 2) / viewer.img.width
    assert abs(new_frac - center_frac) < 0.01
    assert viewer.x_offset > 0  # i.e. not pinned to the left edge
