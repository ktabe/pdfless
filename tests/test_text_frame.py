"""Text mode's border default: off for a plain text file regardless of
--no-frame (there's usually no real "page" boundary in one worth
framing), unchanged (--no-frame's own value) for anything else that
only reaches text mode via 't' (PDF, or a Quick Look preview file) -
see DocumentHandler.default_text_frame() and
Viewer._default_text_frame(). The f key must still toggle it either
way."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless
from conftest import requires_office_support


def make_viewer(handler, frame=True):
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 960, 720))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer(
        [handler], 0, 1, tmpdir, slave, None, frame=frame,
    )
    viewer._load_page = lambda: None
    viewer._draw = lambda: None
    viewer._draw_text = lambda: None
    viewer.refresh()
    return viewer


def test_plain_text_file_defaults_to_no_frame_even_without_no_frame_flag(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), frame=True)
    assert viewer.text_frame is False


def test_plain_text_file_f_key_still_toggles_frame(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), frame=True)
    assert viewer.text_frame is False
    viewer.toggle_text_frame()
    assert viewer.text_frame is True
    viewer.toggle_text_frame()
    assert viewer.text_frame is False


def test_pdf_text_mode_frame_default_unaffected(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), frame=True)
    assert viewer.enter_text_mode() is True
    assert viewer.text_frame is True  # unchanged - only "text" kind defaults to off

    viewer2 = make_viewer(pdfless.PdfDocument(sample_pdf), frame=False)
    assert viewer2.enter_text_mode() is True
    assert viewer2.text_frame is False  # --no-frame still respected as before


@requires_office_support
def test_office_document_text_mode_frame_default_unaffected(sample_docx):
    viewer = make_viewer(pdfless.OfficeDocument(sample_docx), frame=True)
    assert viewer.enter_text_mode() is True
    assert viewer.text_frame is True  # unchanged, matches the pre-existing PDF behavior
    viewer.toggle_text_frame()
    assert viewer.text_frame is False
