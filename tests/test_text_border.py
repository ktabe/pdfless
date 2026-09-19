"""Text mode's border default: off for a plain text file regardless of
--no-border (there's usually no real "page" boundary in one worth
bordering), unchanged (--no-border's own value) for anything else that
only reaches text mode via 't' (PDF, or a Quick Look preview file) -
see DocumentHandler.default_text_border() and
Viewer._default_text_border(). The B key must still toggle it either
way."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless


def make_viewer(handler, border=True):
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 960, 720))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer(
        [handler], 0, 1, tmpdir, slave, None, border=border,
    )
    viewer._load_page = lambda: None
    viewer._draw = lambda: None
    viewer._draw_text = lambda: None
    viewer.refresh()
    return viewer


def test_plain_text_file_defaults_to_no_border_even_without_no_border_flag(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), border=True)
    assert viewer.text_border is False


def test_plain_text_file_b_key_still_toggles_border(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), border=True)
    assert viewer.text_border is False
    viewer.toggle_text_border()
    assert viewer.text_border is True
    viewer.toggle_text_border()
    assert viewer.text_border is False


def test_pdf_text_mode_border_default_unaffected(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), border=True)
    assert viewer.enter_text_mode() is True
    assert viewer.text_border is True  # unchanged - only "text" kind defaults to off

    viewer2 = make_viewer(pdfless.PdfDocument(sample_pdf), border=False)
    assert viewer2.enter_text_mode() is True
    assert viewer2.text_border is False  # --no-border still respected as before
