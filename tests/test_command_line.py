"""main()'s pieces, tested without a terminal: _build_arg_parser() (the
command line), _collect_files() (which files the viewer opens, and on
which page) and _open_key_input() (where keys are read from)."""

import os
import shutil

import pytest

import pdfless


def test_the_parser_reads_files_and_options():
    args = pdfless._build_arg_parser().parse_args(["-p", "3", "-k", "a.pdf", "b.md"])
    assert args.files == ["a.pdf", "b.md"]
    assert args.page == 3
    assert args.keep


def test_no_files_means_stdin():
    assert pdfless._build_arg_parser().parse_args([]).files == []


def test_missing_and_unviewable_files_are_skipped(sample_pdf, tmp_path, capsys):
    binary = tmp_path / "data.bin"
    binary.write_bytes(bytes(range(256)) * 4)
    missing = str(tmp_path / "missing.pdf")
    files, index, page = pdfless._collect_files(
        [missing, str(binary), sample_pdf], str(tmp_path), 1,
    )
    # A missing file is dropped; an existing one stays in the list, as a
    # path to be sniffed again if it's ever navigated to.
    assert files[0] == str(binary)
    assert index == 1
    assert isinstance(files[1], pdfless.PdfDocument)
    assert page == 1
    err = capsys.readouterr().err
    assert f"no such file, skipping: {missing}" in err
    assert f"skipping: {binary}" in err


def test_a_directory_stands_for_its_files(sample_pdf, sample_text, tmp_path, capsys):
    """It was skipped as "no such file"."""
    folder = tmp_path / "folder"
    (folder / "sub").mkdir(parents=True)
    shutil.copy(sample_text, folder / "b.txt")
    shutil.copy(sample_text, folder / "C.txt")  # after b.txt, as ls has it
    shutil.copy(sample_pdf, folder / "a.pdf")
    shutil.copy(sample_pdf, folder / "sub" / "c.pdf")  # not gone into
    (folder / ".DS_Store").write_bytes(b"\0")  # hidden
    empty = tmp_path / "empty"
    empty.mkdir()
    files, index, _page = pdfless._collect_files([str(empty), str(folder)], str(tmp_path), 1)
    assert index == 0
    assert isinstance(files[0], pdfless.PdfDocument) and files[0].path == str(folder / "a.pdf")
    assert files[1:] == [str(folder / "b.txt"), str(folder / "C.txt")]
    assert f"no files in directory, skipping: {empty}" in capsys.readouterr().err


def test_only_the_first_viewable_file_is_sniffed(sample_pdf, tmp_path):
    files, index, _page = pdfless._collect_files([sample_pdf, sample_pdf], str(tmp_path), 1)
    assert index == 0
    assert isinstance(files[0], pdfless.PdfDocument)
    assert files[1] == os.path.abspath(sample_pdf)


@pytest.mark.parametrize("asked, expected", [(3, 3), (99, 7)])
def test_the_start_page_is_kept_within_the_file(sample_pdf, tmp_path, asked, expected):
    assert pdfless._collect_files([sample_pdf], str(tmp_path), asked)[2] == expected


def test_nothing_viewable_exits(tmp_path, capsys):
    with pytest.raises(SystemExit):
        pdfless._collect_files([str(tmp_path / "missing.pdf")], str(tmp_path), 1)
    assert "no valid PDF" in capsys.readouterr().err


def test_keys_come_from_dev_tty_when_stdin_isnt_a_terminal(monkeypatch):
    """`find . -name '*.pdf' | xargs pdfless` (BSD xargs gives the command
    /dev/null for stdin) or `pdfless a.pdf < /dev/null` used to exit
    with "stdin must be a terminal"; less(1) reads /dev/tty then."""
    opened = []
    real_open = os.open

    def fake_open(path, flags, *args):
        if path == "/dev/tty":
            opened.append(path)
            return 99
        return real_open(path, flags, *args)

    r, w = os.pipe()
    try:
        monkeypatch.setattr(pdfless.sys, "stdin", os.fdopen(r))
        monkeypatch.setattr(pdfless.os, "open", fake_open)
        assert pdfless._open_key_input(reading_stdin=False) == (99, 99)
    finally:
        os.close(w)
    assert opened == ["/dev/tty"]


def test_no_terminal_at_all_exits(monkeypatch, capsys):
    def no_tty(path, flags, *args):
        raise OSError("Device not configured")

    r, w = os.pipe()
    try:
        monkeypatch.setattr(pdfless.sys, "stdin", os.fdopen(r))
        monkeypatch.setattr(pdfless.os, "open", no_tty)
        with pytest.raises(SystemExit):
            pdfless._open_key_input(reading_stdin=False)
    finally:
        os.close(w)
    assert "can't open /dev/tty" in capsys.readouterr().err
