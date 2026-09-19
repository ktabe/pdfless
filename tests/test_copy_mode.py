""""C" (see Viewer.toggle_copy_mode()): clears the decorations that a
terminal select-and-copy would otherwise sweep up along with the text -
the EOL markers, the border, the scrollbar and the line-number gutter -
and puts back exactly what was on before on a second press."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless


def make_viewer(handler, border=True, eol_mark=True, scrollbar=True,
                line_numbers=False):
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 40, 320, 360))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer(
        [handler], 0, 1, tmpdir, slave, None,
        border=border, eol_mark=eol_mark, scrollbar=scrollbar,
        line_numbers=line_numbers,
    )
    viewer._load_page = lambda: None
    viewer._draw = lambda: None
    viewer._draw_text = lambda: None
    viewer.refresh()
    return viewer


def decorations(viewer):
    return (
        viewer.eol_mark, viewer.text_border, viewer.scrollbar, viewer.line_numbers,
    )


def test_copy_mode_clears_every_decoration(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), line_numbers=True)
    assert viewer.enter_text_mode() is True
    assert decorations(viewer) == (True, True, True, True)

    viewer.toggle_copy_mode()
    assert decorations(viewer) == (False, False, False, False)


def test_copy_mode_puts_back_what_was_on_before(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), line_numbers=True)
    viewer.enter_text_mode()
    viewer.toggle_copy_mode()
    viewer.toggle_copy_mode()
    assert decorations(viewer) == (True, True, True, True)


def test_copy_mode_leaves_what_was_already_off_alone(sample_text):
    """Restoring means "back to how it was", not "everything on" - a
    plain text file starts without a border, and anything switched off
    by hand beforehand stays off."""
    viewer = make_viewer(pdfless.TextDocument(sample_text))
    assert viewer.text_border is False  # a plain text file's own default
    viewer.toggle_eol_mark()
    assert decorations(viewer) == (False, False, True, False)

    viewer.toggle_copy_mode()
    assert decorations(viewer) == (False, False, False, False)
    viewer.toggle_copy_mode()
    assert decorations(viewer) == (False, False, True, False)


def test_copy_mode_does_the_recompute_its_widths_need(sample_text):
    """The scrollbar's column and the border's own space come back to
    the text when they go - the same geometry a resize redoes."""
    viewer = make_viewer(pdfless.TextDocument(sample_text))
    assert viewer._text_avail_cols() == 39  # 40 less the scrollbar's column
    viewer.toggle_copy_mode()
    assert viewer._text_avail_cols() == 40  # the column is back in play


def test_copy_mode_stays_where_you_were_reading(tmp_path):
    """Toggling it is not a resize: the file isn't re-read, so the
    scroll position survives both presses. Needs a file taller than
    the 20-row terminal make_viewer() sets up, to have somewhere to
    scroll to in the first place."""
    path = tmp_path / "long.txt"
    path.write_text("".join(f"line {i}\n" for i in range(100)))
    viewer = make_viewer(pdfless.TextDocument(str(path)))
    viewer.text_scroll = 42
    assert viewer.text_scroll_max >= 42

    viewer.toggle_copy_mode()
    assert viewer.text_scroll == 42
    viewer.toggle_copy_mode()
    assert viewer.text_scroll == 42
