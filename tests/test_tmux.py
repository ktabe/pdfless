"""Under tmux: an inline image goes through as a DCS passthrough
(wrap_for_tmux()), and frames aren't wrapped in synchronized output -
tmux redraws a synchronized frame's lines whole afterwards, from its own
model of the screen, which never had the image in it: it erased the
image right after showing it (see _sync_begin())."""

import fcntl
import pty
import struct
import tempfile
import termios

import pdfless


def drawn_frame(sample_pdf, monkeypatch, capsys, tmux):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    if tmux:
        monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 800, 480))
    viewer = pdfless.Viewer([pdfless.PdfDocument(sample_pdf)], 0, 1, tempfile.mkdtemp(), slave, "width")
    capsys.readouterr()
    viewer.refresh()
    return capsys.readouterr().out


def test_no_synchronized_output_under_tmux(sample_pdf, monkeypatch, capsys):
    out = drawn_frame(sample_pdf, monkeypatch, capsys, tmux=True)
    assert "\x1bPtmux;\x1b\x1b]1337;File=" in out  # the image, passed through
    assert pdfless.SYNC_BEGIN not in out and pdfless.SYNC_END not in out


def test_synchronized_output_elsewhere(sample_pdf, monkeypatch, capsys):
    out = drawn_frame(sample_pdf, monkeypatch, capsys, tmux=False)
    assert "\x1b]1337;File=" in out and "\x1bPtmux;" not in out
    assert out.index(pdfless.SYNC_BEGIN) < out.index("\x1b]1337;File=") < out.index(pdfless.SYNC_END)
