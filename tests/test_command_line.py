"""main()'s pieces, tested without a terminal: _build_arg_parser() (the
command line), _collect_files() (which files the viewer opens, and on
which page) and _open_key_input() (where keys are read from)."""

import os

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


def test_keys_need_a_terminal_on_stdin(monkeypatch, capsys):
    """Unless the document came in on stdin, stdin is where keys come from."""
    r, w = os.pipe()
    try:
        monkeypatch.setattr(pdfless.sys, "stdin", os.fdopen(r))
        with pytest.raises(SystemExit):
            pdfless._open_key_input(reading_stdin=False)
    finally:
        os.close(w)
    assert "stdin must be a terminal" in capsys.readouterr().err
