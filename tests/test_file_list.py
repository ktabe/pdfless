"""The file list (O): the files given on the command line in a box over
the page (_Overlays.show_files()), picked with the same keys and mouse
events as the table of contents, and opened with ENTER or a click."""

import fcntl
import os
import pty
import shutil
import struct
import tempfile
import termios

import pdfless


def make_viewer(files, monkeypatch, tmpdir=None, cols=100):
    """A Viewer on a `cols`x30 pty over `files`, showing the first."""
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, cols, cols * 8, 480))
    viewer = pdfless.Viewer(files, 0, 1, tmpdir or tempfile.mkdtemp(), slave, "width")
    viewer.refresh()
    return viewer


def three_files(sample_pdf, sample_text, tmp_path, monkeypatch):
    """A viewer on a PDF, a text file and a PDF in a subdirectory, all
    as paths relative to tmp_path, the working directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    shutil.copy(sample_pdf, tmp_path / "a.pdf")
    shutil.copy(sample_text, tmp_path / "notes.txt")
    shutil.copy(sample_pdf, tmp_path / "sub" / "a.pdf")
    paths = [str(tmp_path / p) for p in ("a.pdf", "notes.txt", "sub/a.pdf")]
    return make_viewer([pdfless.PdfDocument(paths[0])] + paths[1:], monkeypatch)


def test_one_file_has_no_list(sample_pdf, monkeypatch):
    viewer = make_viewer([pdfless.PdfDocument(sample_pdf)], monkeypatch)
    assert viewer.handle_global_key("O")
    assert viewer.overlays.active is None


def test_the_list_shows_relative_paths_and_marks_the_current_file(
    sample_pdf, sample_text, tmp_path, monkeypatch,
):
    viewer = three_files(sample_pdf, sample_text, tmp_path, monkeypatch)
    viewer.handle_global_key("O")
    assert viewer.overlays.active == "files"
    assert viewer.overlays.files_sel == 0
    assert viewer.overlays._files_lines() == [
        ("* a.pdf", "1"), ("  notes.txt", "2"), ("  " + os.path.join("sub", "a.pdf"), "3"),
    ]


def test_enter_opens_the_selected_file(sample_pdf, sample_text, tmp_path, monkeypatch):
    viewer = three_files(sample_pdf, sample_text, tmp_path, monkeypatch)
    viewer.handle_global_key("O")
    viewer.overlays.handle_key("j")
    viewer.overlays.handle_key("\r")
    assert viewer.overlays.active is None
    assert viewer.file_index == 1
    assert viewer.text_mode  # a text file

    viewer.handle_global_key("O")
    assert viewer.overlays.files_sel == 1  # opens on the file being viewed
    viewer.overlays.handle_key("G")
    viewer.overlays.handle_key("\r")
    assert viewer.file_index == 2


def test_q_esc_or_o_closes_it(sample_pdf, sample_text, tmp_path, monkeypatch):
    viewer = three_files(sample_pdf, sample_text, tmp_path, monkeypatch)
    for key in ("q", "\x1b", "O"):
        viewer.handle_global_key("O")
        viewer.overlays.handle_key("j")
        viewer.overlays.handle_key(key)
        assert viewer.overlays.active is None
        assert viewer.file_index == 0


def test_a_click_opens_a_file_and_one_outside_closes(sample_pdf, sample_text, tmp_path, monkeypatch):
    viewer = three_files(sample_pdf, sample_text, tmp_path, monkeypatch)
    viewer.handle_global_key("O")
    viewer.mouse.handle("MOUSE_CLICK", 1, 1)  # outside the box
    assert viewer.overlays.active is None

    viewer.handle_global_key("O")
    row0, col0, _content_h, _content_w = viewer.overlays.files_box()
    viewer.mouse.handle("MOUSE_CLICK", col0 + 2, row0 + 3)  # the third file
    assert viewer.overlays.active is None
    assert viewer.file_index == 2


def test_piped_input_is_listed_as_stdin(sample_text, tmp_path, monkeypatch):
    """pdfless as $PAGER keeps what came in on stdin in its scratch
    directory (see _capture_stdin()) - no path worth showing."""
    tmpdir = tempfile.mkdtemp()
    shutil.copy(sample_text, os.path.join(tmpdir, "stdin"))
    viewer = make_viewer(
        [os.path.join(tmpdir, "stdin"), sample_text], monkeypatch, tmpdir=tmpdir,
    )
    assert viewer.overlays._files_lines()[0] == ("* (stdin)", "1")


def opened_second(viewer, monkeypatch):
    """Open the second file from the list; the status line's last word."""
    said = []
    monkeypatch.setattr(viewer, "draw_status", said.append)
    viewer.handle_global_key("O")
    viewer.overlays.handle_key("j")
    viewer.overlays.handle_key("\r")
    return said[-1]


def test_a_file_that_cant_be_opened_is_reported(sample_pdf, tmp_path, monkeypatch):
    """It said "no such file" - for a file right there."""
    binary = tmp_path / "data.bin"
    binary.write_bytes(bytes(range(256)) * 4)
    monkeypatch.setattr(pdfless, "_sniff_file", lambda *args, **kwargs: None)  # (not Quick Look's either)
    viewer = make_viewer([pdfless.PdfDocument(sample_pdf), str(binary)], monkeypatch)
    said = opened_second(viewer, monkeypatch)
    assert viewer.overlays.active is None
    assert viewer.file_index == 0  # still on the PDF
    assert said == f"data.bin: {pdfless.NOT_DISPLAYABLE}"


def test_a_broken_file_says_what_is_wrong(sample_pdf, tmp_path, monkeypatch):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 and then nothing")

    def unusable(*args, **kwargs):
        raise pdfless.UnusableFile("not a valid PDF")

    monkeypatch.setattr(pdfless, "_sniff_file", unusable)
    viewer = make_viewer([pdfless.PdfDocument(sample_pdf), str(broken)], monkeypatch)
    assert opened_second(viewer, monkeypatch) == "broken.pdf: not a valid PDF"


def test_a_file_gone_since_the_start_is_no_such_file(sample_pdf, tmp_path, monkeypatch):
    gone = tmp_path / "gone.pdf"
    shutil.copy(sample_pdf, gone)
    viewer = make_viewer([pdfless.PdfDocument(sample_pdf), str(gone)], monkeypatch)
    gone.unlink()
    assert opened_second(viewer, monkeypatch) == "gone.pdf: no such file"


def test_truncating_keeps_the_end():
    assert pdfless.truncate_start_to_width("docs/a.pdf", 20) == "docs/a.pdf"
    assert pdfless.truncate_start_to_width("docs/reports/a.pdf", 10) == "…rts/a.pdf"
    assert pdfless.truncate_start_to_width("資料/会議/議事録.pdf", 12) == "…/議事録.pdf"
    grouped = "abク\u3099ループ.pdf"  # NFD: the dakuten goes with its kana
    assert pdfless.truncate_start_to_width(grouped, 13) == "…ク\u3099ループ.pdf"
    assert pdfless.truncate_start_to_width(grouped, 12) == "…ループ.pdf"
    assert pdfless.truncate_start_to_width("abc", 0) == ""


def test_a_long_path_loses_its_start_not_its_name(sample_pdf, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    deep = tmp_path / "a-rather-long-directory-name" / "and-another-one-below-it"
    deep.mkdir(parents=True)
    shutil.copy(sample_pdf, tmp_path / "top.pdf")
    shutil.copy(sample_pdf, deep / "the-file-itself.pdf")
    viewer = make_viewer(
        [pdfless.PdfDocument(str(tmp_path / "top.pdf")), str(deep / "the-file-itself.pdf")],
        monkeypatch, cols=50,
    )
    content_w = viewer.overlays.files_box()[3]
    left, number = viewer.overlays._files_lines()[1]
    assert left.startswith("  …") and left.endswith("/the-file-itself.pdf")
    assert pdfless.display_width(left) + 1 + len(number) == content_w  # fills the box, no more


def test_copy_mode_stays_with_its_own_file(sample_pdf, tmp_path, monkeypatch):
    """Regression test: T (text mode, cleared for copying) then :n left
    the next file with the scrollbar, the EOL marks and the line numbers
    stuck off - and the next C put back the first file's values."""
    other = tmp_path / "b.pdf"
    shutil.copy(sample_pdf, other)
    viewer = make_viewer([pdfless.PdfDocument(sample_pdf), str(other)], monkeypatch)
    before = (viewer.eol_mark, viewer.scrollbar, viewer.line_numbers)
    width = viewer.base_width_px
    viewer.handle_global_key("T")
    assert viewer._copy_mode_saved is not None and not viewer.scrollbar
    viewer.go_to_file(1)
    assert viewer._copy_mode_saved is None
    assert (viewer.eol_mark, viewer.scrollbar, viewer.line_numbers) == before
    assert viewer.base_width_px == width  # the scrollbar's column is back
