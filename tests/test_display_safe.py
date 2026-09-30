"""display_safe(): text from a document never reaches the terminal with
control characters in it - a PDF's or an Office document's extracted
text, a bookmark's title, a file name - so the document can't send
escape sequences of its own (OSC 52 to set the clipboard, a title
change, ...)."""

import fcntl
import pty
import struct
import tempfile
import termios

from pypdf import PdfWriter

import pdfless

OSC52 = "\x1b]52;c;SGVsbG8=\x07"  # "set the clipboard to Hello"


def make_viewer(handler, monkeypatch):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 800, 480))
    viewer = pdfless.Viewer([handler], 0, 1, tempfile.mkdtemp(), slave, "width")
    viewer.refresh()
    return viewer


def test_display_safe():
    assert pdfless.display_safe("plain text 日本語") == "plain text 日本語"
    assert pdfless.display_safe(OSC52 + "x") == "^[]52;c;SGVsbG8=^Gx"
    assert pdfless.display_safe("a\x9b31mb") == "a<9B>31mb"  # C1 CSI
    assert pdfless.display_safe("a\tb") == "a       b"
    assert pdfless.display_safe(pdfless.display_safe(OSC52)) == pdfless.display_safe(OSC52)


def test_extracted_text_is_made_safe_in_either_text_view(sample_pdf, monkeypatch):
    handler = pdfless.PdfDocument(sample_pdf)
    monkeypatch.setattr(handler, "extract_text", lambda page: [OSC52 + "text"])
    monkeypatch.setattr(handler, "extract_text_pages", lambda n: [[OSC52 + "text"]] * n)
    viewer = make_viewer(handler, monkeypatch)
    assert viewer.enter_text_mode()
    assert viewer.text_lines == ["^[]52;c;SGVsbG8=^Gtext"]
    viewer.exit_text_mode()
    viewer.toggle_continuous()
    assert viewer.enter_text_mode()
    assert not any("\x1b" in line or "\x07" in line for line in viewer.text_lines)


def test_bookmark_titles_and_status_messages_are_made_safe(sample_pdf, tmp_path, monkeypatch, capsys):
    writer = PdfWriter()
    writer.append(sample_pdf, import_outline=False)
    writer.add_outline_item("Evil" + OSC52, 0)
    path = tmp_path / "evil.pdf"
    writer.write(str(path))
    viewer = make_viewer(pdfless.PdfDocument(str(path)), monkeypatch)
    viewer.overlays.show_outline()
    assert "\x1b]52" not in capsys.readouterr().out
    viewer.draw_status("found " + OSC52)
    assert "\x1b]52" not in capsys.readouterr().out
