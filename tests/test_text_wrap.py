"""Text mode's line-wrap default: on for a plain text file (unless
-S/--chop-long-lines said otherwise), off for anything that only
reaches text mode via 't' (a PDF, or a Quick Look preview file) -
see DocumentHandler.default_text_wrap() and Viewer._default_text_wrap().
Typing "-S" (less(1)'s own runtime option-toggle syntax) toggles it
either way - see run_viewer()'s dash_pending handling, exercised here
directly via Viewer.toggle_text_wrap() instead (these tests construct a
Viewer directly, without a full pdfless.py subprocess)."""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless
from conftest import requires_office_support


def make_viewer(handler, wrap=True, cols=40):
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, cols, cols * 8, 40 * 18))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer(
        [handler], 0, 1, tmpdir, slave, None, wrap=wrap,
    )
    viewer._load_page = lambda: None
    viewer._draw = lambda: None
    viewer.refresh()
    return viewer


def make_long_line_text(tmp_path, n_words=30):
    path = tmp_path / "long.txt"
    path.write_text(" ".join(f"word{i}" for i in range(n_words)) + "\n" + "short line\n")
    return str(path)


def test_plain_text_file_defaults_to_wrap(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), wrap=True)
    assert viewer.text_wrap is True


def test_plain_text_file_wrap_off_with_chop_long_lines(sample_text):
    """wrap=False here stands in for -S/--chop-long-lines - main() passes
    wrap=not args.chop_long_lines."""
    viewer = make_viewer(pdfless.TextDocument(sample_text), wrap=False)
    assert viewer.text_wrap is False


def test_pdf_text_mode_wrap_default_off_regardless_of_chop_long_lines(sample_pdf):
    viewer = make_viewer(pdfless.PdfDocument(sample_pdf), wrap=True)
    assert viewer.enter_text_mode() is True
    assert viewer.text_wrap is False  # unchanged - only "text" kind defaults to wrap on

    viewer2 = make_viewer(pdfless.PdfDocument(sample_pdf), wrap=False)
    assert viewer2.enter_text_mode() is True
    assert viewer2.text_wrap is False


@requires_office_support
def test_office_document_text_mode_wrap_default_off(sample_docx):
    viewer = make_viewer(pdfless.OfficeDocument(sample_docx), wrap=True)
    assert viewer.enter_text_mode() is True
    assert viewer.text_wrap is False


def test_dash_s_toggles_wrap_either_way(sample_text):
    viewer = make_viewer(pdfless.TextDocument(sample_text), wrap=True)
    assert viewer.text_wrap is True
    viewer.toggle_text_wrap()
    assert viewer.text_wrap is False
    viewer.toggle_text_wrap()
    assert viewer.text_wrap is True


def test_wrapped_lines_never_exceed_terminal_width(tmp_path):
    path = make_long_line_text(tmp_path, n_words=30)
    viewer = make_viewer(pdfless.TextDocument(path), wrap=True, cols=40)
    viewer._load_text_page()
    viewer._ensure_display_rows()
    assert len(viewer._display_rows) > 1  # the long line had to split
    for line_idx, start, end in viewer._display_rows:
        segment = viewer.text_lines[line_idx][start:end]
        assert pdfless.display_width(segment) <= 40
    # Reassembling every segment for a given line recovers it exactly.
    by_line = {}
    for line_idx, start, end in viewer._display_rows:
        by_line.setdefault(line_idx, []).append((start, end))
    for line_idx, segments in by_line.items():
        line = viewer.text_lines[line_idx]
        rebuilt = "".join(line[s:e] for s, e in segments)
        assert rebuilt == line


def test_unwrapped_mode_keeps_one_row_per_line(tmp_path):
    """Sanity check that wrap=False leaves the pre-existing single-row-
    per-line model (_display_rows is only built/used while wrapped)."""
    path = make_long_line_text(tmp_path, n_words=30)
    viewer = make_viewer(pdfless.TextDocument(path), wrap=False, cols=40)
    viewer._load_text_page()
    assert viewer.text_max_line_width > 40  # confirms the line really is
    # wider than the terminal, i.e. this is actually exercising pan, not
    # a line that happens to already fit
    assert viewer.text_x_offset_max > 0  # panning is available


def test_toggle_wrap_resets_scroll_to_top(tmp_path):
    path = tmp_path / "many_lines.txt"
    path.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    viewer = make_viewer(pdfless.TextDocument(str(path)), wrap=False, cols=40)
    viewer._load_text_page()
    viewer.text_scroll_down(10)
    assert viewer.text_scroll != viewer.text_scroll_min

    viewer.toggle_text_wrap()
    assert viewer.text_wrap is True
    assert viewer.text_scroll == viewer.text_scroll_min


def test_go_to_text_line_lands_on_correct_display_row_when_wrapped(tmp_path):
    path = tmp_path / "mixed.txt"
    path.write_text(make_long_words_line(30) + "\nsecond\nthird\n")
    viewer = make_viewer(pdfless.TextDocument(str(path)), wrap=True, cols=40)
    viewer._load_text_page()
    viewer._ensure_display_rows()
    assert len(viewer._display_rows) > 3  # line 1 wrapped across several rows

    viewer.go_to_text_line(2)  # "second" - 1-based, raw line index 1
    line_idx, _start, _end = viewer._display_rows[viewer.text_scroll]
    assert line_idx == 1


def make_long_words_line(n_words):
    return " ".join(f"word{i}" for i in range(n_words))
