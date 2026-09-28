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


def test_a_file_that_cant_be_opened_is_reported(sample_pdf, tmp_path, monkeypatch):
    binary = tmp_path / "data.bin"
    binary.write_bytes(bytes(range(256)) * 4)
    viewer = make_viewer([pdfless.PdfDocument(sample_pdf), str(binary)], monkeypatch)
    viewer.handle_global_key("O")
    viewer.overlays.handle_key("j")
    viewer.overlays.handle_key("\r")
    assert viewer.overlays.active is None
    assert viewer.file_index == 0  # still on the PDF


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
