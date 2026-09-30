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
