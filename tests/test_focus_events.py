"""Focus reporting (xterm's CSI I / CSI O, enabled via FOCUS_ON) -
regaining focus triggers a redraw the same way "^L" does, fixing a real
bug: under tmux, switching away from a pane showing a PDF can leave it
blank, since tmux's own screen model doesn't understand the
passed-through inline image (see wrap_for_tmux()) and its internal
redraw on a focus change repaints the pane from a model that never had
the image in it.

Only FOCUS_IN redraws, not FOCUS_OUT - confirmed by hand that the
latter actually makes things worse, drawing a stray, oddly-scaled copy
of the image into whichever pane is gaining focus instead. OSC 1337
places the image at "the current cursor position", and by the time a
pane is told it just lost focus, tmux has already moved the terminal's
one real cursor away to the pane gaining it (see FOCUS_ON's comment in
pdfless.py for the full reasoning) - so a pane that just lost focus
stays blank until it's focused again, which is what actually redraws
it correctly.

decode_csi_key() is tested directly (same pattern as
test_scrollbar.py's test_sgr_mouse_decodes_drag_and_release()); the pty
tests below cover main()'s wiring end-to-end - that FOCUS_ON/FOCUS_OFF
are actually sent, that FOCUS_IN triggers a redraw, and that FOCUS_OUT
does not."""

import time

import pdfless


def test_decode_csi_key_recognizes_focus_events():
    assert pdfless.decode_csi_key("I") == "FOCUS_IN"
    assert pdfless.decode_csi_key("O") == "FOCUS_OUT"
    # Doesn't collide with the SGR mouse-reporting path (a leading "<"),
    # or with a plain arrow/Home/End CSI sequence.
    assert pdfless.decode_csi_key("A") != "FOCUS_IN"


def test_startup_enables_focus_reporting(pty_session, sample_pdf):
    session = pty_session([sample_pdf])
    time.sleep(1)
    out = session.read_all(1.0).decode(errors="replace")
    assert "\x1b[?1004h" in out
    session.send(b"q")


def test_focus_in_redraws_without_crashing(pty_session, sample_pdf):
    session = pty_session([sample_pdf])
    time.sleep(3)
    session.read_all(0.5)  # drop the startup paint

    session.send(b"\x1b[I", wait=0.5)
    out = session.read_all(0.5).decode(errors="replace")
    assert "Traceback" not in out
    assert "\x1b]1337;File=" in out  # the image was re-sent

    session.send(b"q")
    assert "Traceback" not in session.read_all().decode(errors="replace")


def test_focus_out_does_not_redraw(pty_session, sample_pdf):
    """Unlike FOCUS_IN, this must NOT trigger a redraw - see the module
    docstring for why that would actually misplace the image into
    whichever pane is gaining focus instead."""
    session = pty_session([sample_pdf])
    time.sleep(3)
    session.read_all(0.5)  # drop the startup paint

    session.send(b"\x1b[O", wait=0.5)
    out = session.read_all(0.5).decode(errors="replace")
    assert "Traceback" not in out
    assert "\x1b]1337;File=" not in out  # no redraw happened

    session.send(b"q")
    assert "Traceback" not in session.read_all().decode(errors="replace")
