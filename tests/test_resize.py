"""A terminal resize (or ^Z and fg, which goes the same way - see
_suspend()) keeps text mode's place: the line at the top stays at the
top, whatever the new width does to the wrapping."""

import fcntl
import pty
import struct
import tempfile
import termios

import pdfless


def make_viewer(handler, rows=30, cols=100):
    _master, slave = pty.openpty()
    resize(slave, rows, cols)
    viewer = pdfless.Viewer([handler], 0, 1, tempfile.mkdtemp(), slave, "width")
    viewer.refresh()
    return viewer, slave


def resize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * 8, rows * 16))


def resized(viewer, slave, rows, cols):
    resize(slave, rows, cols)
    viewer.request_resize()
    viewer.refresh()


def test_a_text_file_stays_where_it_was(tmp_path):
    """It used to be read again from the top."""
    path = tmp_path / "long.txt"
    path.write_text("".join(f"line {i}\n" for i in range(1, 1001)))
    viewer, slave = make_viewer(pdfless.TextDocument(str(path)))
    viewer.go_to_text_line(500)
    top = viewer._top_text_line()
    assert top > 0
    resized(viewer, slave, 40, 120)
    assert viewer._top_text_line() == top


def test_wrapped_text_keeps_its_line_at_a_new_width(sample_pdf):
    """text_scroll counts display rows while wrapped, and a new width
    re-splits every line - the row it held pointed somewhere else."""
    viewer, slave = make_viewer(pdfless.PdfDocument(sample_pdf), cols=40)
    assert viewer.enter_text_mode()
    viewer.go_to_page_text(2, 0)  # (page 1's few lines don't scroll)
    if not viewer.text_wrap:
        viewer.toggle_text_wrap()
    viewer._set_text_scroll(30)
    top = viewer._top_text_line()
    assert top > 0
    resized(viewer, slave, 30, 60)  # (still wrapping, with room to scroll)
    assert viewer.text_scroll < viewer.text_scroll_max
    assert viewer._top_text_line() == top


def test_the_top_stays_the_top(sample_pdf):
    viewer, slave = make_viewer(pdfless.PdfDocument(sample_pdf))
    assert viewer.enter_text_mode()
    assert viewer.text_scroll == viewer.text_scroll_min  # the border's row
    resized(viewer, slave, 40, 90)
    assert viewer.text_scroll == viewer.text_scroll_min


def continuous_text_viewer(sample_pdf, rows=50):
    """The continuous text view of the 7-page sample, on a screen taller
    than its last page's text - which can never be scrolled to the top."""
    _master, slave = pty.openpty()
    resize(slave, rows, 100)
    viewer = pdfless.Viewer(
        [pdfless.PdfDocument(sample_pdf)], 0, 1, tempfile.mkdtemp(), slave, "width",
        options=pdfless.ViewerOptions(continuous=True),
    )
    viewer.refresh()
    assert viewer.enter_text_mode()
    viewer.refresh()
    return viewer


def test_a_short_last_page_can_be_the_current_one(sample_pdf):
    """It used to snap back to page 6: > and n left "page 6/7", and G at
    the end scrolled backwards, to page 6's bottom."""
    viewer = continuous_text_viewer(sample_pdf)
    viewer.handle_count_key(">", None)
    viewer.refresh()
    assert viewer.page == 7
    end = viewer.text_scroll
    viewer.handle_key_text("G")
    viewer.refresh()
    assert viewer.page == 7 and viewer.text_scroll == end  # not back up

    viewer.go_to_page_text(6, 0)
    viewer.refresh()
    assert viewer.page == 6
    viewer.handle_key_text("n")
    viewer.refresh()
    assert viewer.page == 7


def test_scrolling_back_from_the_end_follows_the_top_again(sample_pdf):
    viewer = continuous_text_viewer(sample_pdf)
    viewer.handle_count_key(">", None)
    viewer.refresh()
    for _ in range(3):
        viewer.handle_key_text("k")
    viewer.refresh()
    assert viewer.page == viewer._text_page_of_line(viewer._top_text_line())


def status_percent(viewer):
    fields = [text for text, _color in viewer.status_segments()]
    return next(int(f.strip().rstrip("%")) for f in fields if f.strip().endswith("%"))


def test_the_text_mode_percentage_runs_from_0_to_100(sample_pdf):
    """It ignored text_scroll_min (-1 with the border's row), so the top
    of a page read " -8%", and a <N>g past the last full screen "325%"."""
    viewer, _slave = make_viewer(pdfless.PdfDocument(sample_pdf))
    assert viewer.enter_text_mode()
    viewer.go_to_page_text(2, 0)
    assert status_percent(viewer) == 0
    viewer.text_scroll = viewer.text_scroll_max
    assert status_percent(viewer) == 100
    viewer.go_to_text_line(40)  # past the last full screen, on purpose
    assert status_percent(viewer) == 100
