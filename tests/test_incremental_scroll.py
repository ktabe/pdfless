"""Differential redraw for a plain vertical scroll in image mode -
_draw()'s _scroll_shift_rows()/_draw_shifted() path.

Confirmed by hand (a throwaway spike script, not part of this repo)
that iTerm2 carries an already-placed OSC 1337 inline image along with
a terminal scroll-region shift (CSI S/T) the same as it does plain
text - see _scroll_shift_rows()'s docstring in pdfless.py for the full
reasoning. These tests cover the eligibility logic itself, and that
_draw() actually takes the shifted path (CSI S/T, no full \\x1b[2J,
only a small strip transmitted) when it applies - not what the shifted
image actually looks like on screen, which no automated test can see."""

import fcntl
import pty
import re
import struct
import termios
import tempfile

import pdfless


def make_viewer(handler, monkeypatch, fit="width", rows=30, cols=100):
    """Wide enough that a page fits horizontally but not vertically -
    i.e. there's real vertical scroll room (fit="width", unlike
    test_horizontal_pan.py's fit="height") - and forced into the
    iTerm2-like branch regardless of what terminal the test suite
    itself happens to run under, since _scroll_shift_rows() gates on
    iterm2_like()."""
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * 8, rows * 16))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer([handler], 0, 1, tmpdir, slave, fit)
    viewer.refresh()  # first real _draw() - populates _last_viewport_*/etc.
    assert viewer.scroll_max > 0  # or there's nothing to scroll for these tests
    return viewer


def test_first_draw_is_never_shifted(sample_pdf, monkeypatch):
    """Nothing to compare against yet (_last_viewport_set is only True
    after a _draw() has actually happened)."""
    viewer = pdfless.Viewer(
        [pdfless.PdfDocument(sample_pdf)], 0, 1, tempfile.mkdtemp(),
        pty.openpty()[1], "width",
    )
    assert viewer._scroll_shift_rows(viewer.crop_width, 100) is None


def test_a_one_line_scroll_is_eligible(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.scroll_down(viewer.cell_h_px)
    shift = viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px)
    assert shift == 1


def test_a_backward_scroll_is_eligible_too(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.scroll_down(viewer.cell_h_px * 3)
    viewer._draw()  # commit that position as "what's on screen"
    viewer.scroll_up(viewer.cell_h_px)
    shift = viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px)
    assert shift == -1


def test_a_sub_cell_or_zero_scroll_is_not_eligible(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None  # delta == 0

    viewer.scroll += 1  # less than a whole cell - go_page()/scroll_*() never
    # actually produce this, but _scroll_shift_rows() itself must still
    # refuse it rather than shift by a fractional/rounded row
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_a_scroll_past_the_whole_viewport_is_not_eligible(sample_pdf, monkeypatch):
    """No overlap left with what's on screen - a full redraw is just as
    cheap, and _draw_shifted()'s row math (screen_row = avail_rows -
    strip_rows + 1) assumes strip_rows < avail_rows."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    avail_rows = max(1, viewer.rows - 1)
    viewer.scroll_down(viewer.cell_h_px * avail_rows)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_a_page_turn_is_not_eligible(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.go_page(2, viewer.cell_h_px)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_a_zoom_change_is_not_eligible(sample_pdf, monkeypatch):
    """Confirm the scroll alone is eligible first, so the None below can
    only be the zoom change - not an unrelated zero scroll delta."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.scroll_down(viewer.cell_h_px)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) == 1

    viewer.set_zoom(1.5)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_a_pan_change_is_not_eligible(sample_pdf, monkeypatch):
    """fit="width" alone leaves no room to pan (the page already fills
    the width) - zooming in past 1x makes it both taller and wider than
    the viewport, the same as test_horizontal_pan.py's centered-zoom
    test does, giving both scroll and pan room at once."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.set_zoom(2.0)
    viewer._draw()  # commit the zoomed-in view as the new baseline
    assert viewer.img.width > viewer.crop_width  # confirm there's room to pan

    viewer.scroll_down(viewer.cell_h_px)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) == 1

    viewer.pan(max(1, viewer.cell_w_px))
    assert viewer.x_offset > 0
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_an_active_search_marker_is_not_eligible(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.start_search("Lorem")
    if viewer.search_pos is None:
        return  # nothing to find in this fixture at this zoom - not what
        # this test is about
    viewer._draw()  # commit the marker as "on screen"
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_tmux_disables_the_shortcut(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    viewer.scroll_down(viewer.cell_h_px)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_a_non_iterm2_terminal_disables_the_shortcut(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    for var in ("TERM_PROGRAM", "ITERM_SESSION_ID", "WEZTERM_PANE", "LC_TERMINAL"):
        monkeypatch.delenv(var, raising=False)
    viewer.scroll_down(viewer.cell_h_px)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_no_incremental_scroll_option_disables_the_shortcut(sample_pdf, monkeypatch):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    viewer.scroll_down(viewer.cell_h_px)
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) == 1  # sanity

    viewer.incremental_scroll = False  # --no-incremental-scroll
    assert viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px) is None


def test_half_page_step_is_always_a_whole_number_of_cells(sample_pdf, monkeypatch):
    """"d"/"u" used to step by avail_height_px // 2, which lands mid-row
    (and so disables the incremental-scroll shortcut) whenever rows - 1
    is odd - _half_page_step() must not have that problem regardless of
    the terminal's row count."""
    for rows in (30, 31):  # rows - 1 even and odd
        viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch, rows=rows)
        step = viewer._half_page_step()
        assert step > 0
        assert step % viewer.cell_h_px == 0


def test_d_and_u_are_eligible_for_the_shifted_path(sample_pdf, monkeypatch):
    for rows in (30, 31):
        viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch, rows=rows)
        viewer.handle_key("d")
        shift = viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px)
        assert shift is not None and shift > 0

        viewer._draw()  # commit, so "u" scrolls back from a known position
        viewer.handle_key("u")
        shift = viewer._scroll_shift_rows(viewer.crop_width, viewer.avail_height_px)
        assert shift is not None and shift < 0


def test_draw_takes_the_shifted_path_and_sends_only_a_strip(sample_pdf, monkeypatch, capsys):
    """End-to-end through the real _draw(): a one-line scroll emits a
    scroll-region CSI S, no full-screen clear, and an inline image
    payload for a one-line-tall strip rather than the whole viewport."""
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), monkeypatch)
    capsys.readouterr()  # discard the initial full draw from refresh()

    viewer.scroll_down(viewer.cell_h_px)
    viewer._draw()
    out = capsys.readouterr().out

    avail_rows = max(1, viewer.rows - 1)
    assert f"\x1b[1;{avail_rows}r" in out
    assert "\x1b[1S" in out
    assert "\x1b[2J" not in out

    m = re.search(r"height=(\d+)px", out)
    assert m is not None
    assert int(m.group(1)) == viewer.cell_h_px  # one row's worth, not the
    # whole avail_height_px-tall viewport
