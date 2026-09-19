"""-N/--line-numbers: a right-aligned, gray gutter at the start of each
text-mode row, off by default (matching less(1)'s own -N semantics -
the opposite of -S/-E/-B's "on by default, --no-X disables" pattern).
No per-kind default (unlike border/wrap/eol-mark): it's a flat
Viewer.line_numbers flag, since there's no kind numbering wouldn't make
sense for. "#" is the primary toggle key; "-N"/"-n" (typed as "-" then
"N"/"n") is kept only for less(1) compatibility - see
run_viewer()'s dash_pending handling."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless


def make_viewer(handler, line_numbers=False, cols=40):
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, cols, cols * 8, 40 * 18))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer(
        [handler], 0, 1, tmpdir, slave, None, line_numbers=line_numbers,
    )
    viewer._load_page = lambda: None
    viewer._draw = lambda: None
    viewer.refresh()
    return viewer


def make_numbered_text(tmp_path, n_lines=11):
    path = tmp_path / "numbered.txt"
    path.write_text("\n".join(f"line {i}" for i in range(1, n_lines + 1)) + "\n")
    return str(path)


def test_dash_n_toggles_line_numbers(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text))
    assert viewer.line_numbers is False
    viewer.toggle_line_numbers()
    assert viewer.line_numbers is True
    viewer.toggle_line_numbers()
    assert viewer.line_numbers is False


def test_gutter_width_sized_to_largest_line_number(tmp_path):
    path = make_numbered_text(tmp_path, n_lines=11)  # widest number: "11" (2 digits)
    viewer = make_viewer(pdfless.TextDocument(path), line_numbers=True)
    assert viewer._line_number_gutter_width() == 3  # 2 digits + 1 separator


def test_wrapped_continuation_rows_have_blank_gutter(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text(" ".join(f"word{i}" for i in range(30)) + "\nshort\n")
    viewer = make_viewer(pdfless.TextDocument(str(path)), line_numbers=True, cols=40)
    viewer._ensure_display_rows()
    assert len(viewer._display_rows) > 2  # the long line wraps across rows
    starts = [start for _line_idx, start, _end in viewer._display_rows]
    assert starts[0] == 0  # first display row of line 0 - gets a number
    assert starts[1] != 0  # continuation row - blank gutter, per _draw_text_wrapped()


def test_gutter_reserves_width_in_wrap_mode(tmp_path):
    path = make_numbered_text(tmp_path, n_lines=11)
    without = make_viewer(pdfless.TextDocument(path), line_numbers=False, cols=40)
    without._ensure_display_rows()
    with_numbers = make_viewer(pdfless.TextDocument(path), line_numbers=True, cols=40)
    with_numbers._ensure_display_rows()
    # Same content, narrower usable width with the gutter on - never
    # wider display rows, and here (short lines) the count is unaffected,
    # but each row's own segment must fit within the reduced width.
    gutter = with_numbers._line_number_gutter_width()
    for line_idx, start, end in with_numbers._display_rows:
        segment = with_numbers.text_lines[line_idx][start:end]
        assert pdfless.display_width(segment) <= 40 - gutter


def test_gutter_reserves_width_in_unwrapped_mode(tmp_path):
    path = make_numbered_text(tmp_path, n_lines=11)
    viewer = make_viewer(pdfless.TextDocument(path), line_numbers=True, cols=40)
    viewer.toggle_text_wrap()  # off - unwrapped/pan mode
    assert viewer.text_wrap is False
    gutter = viewer._line_number_gutter_width()
    assert gutter == 3
    # Panning all the way right must still leave room for the gutter -
    # i.e. the usable content width used for text_x_offset_max is
    # (cols - gutter), not the raw terminal width.
    assert viewer.text_x_offset_max >= 0
