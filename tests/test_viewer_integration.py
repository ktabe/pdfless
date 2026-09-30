"""End-to-end tests driving a real `pdfless.py` subprocess through a
pty - the only way to exercise Viewer at all (it needs a real terminal
for ioctl-based size queries). These are slower than the unit tests in
the other modules, but they're what actually catches a crash in the
Viewer/DocumentHandler wiring - e.g. the regression this module is
named for, where opening a plain text file crashed with
`TypeError: object of type 'NoneType' has no len()` because
_load_text_page() assumed a cache that's only populated by
enter_text_mode()/reload(), neither of which ever runs for a
kind=="text" file (it starts permanently in text mode)."""

import os
import signal
import re
import time

from conftest import requires_office_support, requires_macos

# Rendering an office document can take a while (LibreOffice, or Quick
# Look + Chrome) - how long to wait for its first frame.
OFFICE_TIMEOUT = 90


def test_plain_text_file_opens_without_crashing(pty_session, sample_text):
    session = pty_session([sample_text])
    session.ready()
    session.quit()


def test_rtf_file_opens_without_crashing(pty_session, sample_rtf):
    """(Through LibreOffice where it's installed, else as plain text.)"""
    session = pty_session([sample_rtf])
    session.ready(OFFICE_TIMEOUT)
    session.quit()


def test_plain_text_file_survives_a_resize(pty_session, sample_text):
    """A kind=="text" file's _load_text_page() runs again on every
    resize (refresh()'s SIGWINCH path) - make sure that doesn't crash
    either, not just the very first load."""
    session = pty_session([sample_text])
    session.ready()
    os.kill(session.pid, signal.SIGWINCH)
    session.ready()  # redrawn
    session.quit()


def test_pdf_text_mode_toggle_and_search(pty_session, sample_pdf):
    session = pty_session([sample_pdf])
    session.ready()
    session.send(b"t")
    session.ready()
    session.send(b"/Lorem\r")
    session.wait_for(b"match 1/")
    session.send(b"n")
    session.wait_for(b"match 2/")
    session.send(b"t")
    session.ready()
    session.quit(b"qq")  # (the first q clears the search)


def test_image_file_text_mode_and_search_prompt(pty_session, sample_image):
    """An image's text mode shows its information (size, EXIF, ...), and
    the search prompt opens there and is cancelled with ESC."""
    session = pty_session([sample_image])
    session.ready()
    session.send(b"t")
    assert b"PNG" in session.ready()
    session.send(b"/")
    session.wait_for(b"\x1b[?25h")  # the prompt, with the cursor shown in it
    session.send(b"\x1b")
    session.ready()
    session.quit()


@requires_office_support
def test_docx_office_text_mode_toggle(pty_session, sample_docx):
    session = pty_session([sample_docx])
    session.ready(OFFICE_TIMEOUT)
    for _ in range(2):
        session.send(b"t")
        session.ready()
    session.quit()


@requires_office_support
def test_rtf_office_text_mode_toggle(pty_session, sample_rtf):
    """An RTF file now renders as an image via RtfOfficeDocument (a
    textutil-to-docx conversion feeding the same pipeline as a native
    Word document) - make sure both the image view and 't' toggling
    into/out of text mode work end-to-end, not just the plain-text
    fallback path."""
    session = pty_session([sample_rtf])
    session.ready(OFFICE_TIMEOUT)
    for _ in range(2):
        session.send(b"t")
        session.ready()
    session.quit()


def test_f_key_toggles_follow_mode_and_its_status_indicator(pty_session, sample_pdf):
    """F is a runtime toggle for the same follow behavior -f/--follow
    turns on at startup (see run_viewer()'s "F" handling) - and the
    status line shows a " follow " segment (status_segments()) only
    while it's active, regardless of how it got turned on."""
    session = pty_session([sample_pdf])
    assert b" follow " not in session.ready()  # off by default (startup paint)

    session.send(b"F")
    assert b" follow " in session.ready()

    session.send(b"F")
    assert b" follow " not in session.ready()
    session.quit()


def test_lowercase_f_flag_still_enables_follow_at_startup(pty_session, sample_pdf):
    """-F was renamed to -f (see the new -F/--quit-if-one-screen, real
    less(1)'s own flag) - -f must still turn follow on at startup."""
    session = pty_session(["-f", sample_pdf])
    assert b" follow " in session.ready()
    session.quit()


def _drain_until_exit(session, deadline_seconds=10):
    """Wait for pdfless to exit by itself (see PtySession.exited()) -
    (whether it did, with status 0, and everything it wrote)."""
    code, out = session.exited(deadline_seconds)
    return code == 0, out


def test_quit_if_one_screen_dumps_a_single_page_image_and_exits(pty_session, sample_image):
    """-F/--quit-if-one-screen on a single-page (here: a plain image,
    always npages==1) document: no keypress needed, and - unlike a
    normal quit - it must never have entered the alternate screen at
    all (no \\x1b[?1049h), so the dump lands in real scrollback like
    less(1)'s own -F, not vanish with the rest of that buffer."""
    session = pty_session(["-F", sample_image])
    exited, out = _drain_until_exit(session)
    assert exited, "pdfless did not exit on its own under -F for a single-page file"
    assert b"\x1b[?1049h" not in out
    assert b"\x1b]1337;File=inline=1" in out  # the dumped page image itself
    # Must end \r\n, not just \n: the terminal is still in raw mode (see
    # RawTerminal) when this is written, so a bare \n leaves the cursor
    # one column short of 1 - visible as zsh's own "%" no-trailing-
    # newline marker appearing after the image.
    assert out.endswith(b"\r\n")


def test_quit_if_one_screen_dumps_a_short_text_file_and_exits(pty_session, sample_text):
    """Same as above, but for a text-mode-starting document (a plain
    text file) - "fits in one screen" there means the actual line count
    needs no scrolling, not "1 page" (every plain text file reports
    npages==1 regardless of length - see run_viewer())."""
    session = pty_session(["-F", sample_text])
    exited, out = _drain_until_exit(session)
    assert exited, "pdfless did not exit on its own under -F for a short text file"
    assert b"\x1b[?1049h" not in out
    assert b"line one" in out
    assert b"line three" in out
    # Confirms the raw-terminal-mode CR fix: without it, bare \n moves
    # down a row without returning to column 1 (tty.setraw() disables
    # the usual \n -> \r\n translation - see RawTerminal), so each
    # successive line lands one column further right instead of at the
    # start of the next line. (eol_mark's marker is on by default -
    # between "line one" and the \r\n - see the dedicated test below.)
    assert b"\r\nline two\x1b[34m" in out
    assert out.rstrip(b"\r\n").endswith(b"line three\x1b[34m\xe2\x86\xb5\x1b[0m")


def test_quit_if_one_screen_dump_respects_line_numbers_and_eol_mark(pty_session, sample_text):
    """-N/line numbers and the eol_mark (↵) marker are real content-
    display options the user asked for on the command line - not pager-
    only interactive chrome like the status line/border/scrollbar - so
    dump_and_quit() must still apply them, the same as interactive text
    mode does (see _draw_text_wrapped()'s own gutter_width/eol_mark
    handling, which dump_and_quit() mirrors for the one-shot dump)."""
    session = pty_session(["-F", "-N", sample_text])
    exited, out = _drain_until_exit(session)
    assert exited
    assert b"\x1b[?1049h" not in out
    # LINE_NUMBER_COLOR "1 " SGR_RESET "line one" - gutter and
    # content are separated by ANSI codes, not adjacent plain text.
    assert "\x1b[90m1 \x1b[0mline one".encode() in out
    assert "\x1b[90m3 \x1b[0mline three".encode() in out
    assert "↵".encode() in out  # NEWLINE_MARKER, on by default (eol_mark)


def test_quit_if_one_screen_leaves_a_margin_row_for_its_own_trailing_newline(
    pty_session, tmp_path,
):
    """Regression test: dump_and_quit() writes one trailing \\r\\n after
    its content (see the CR fix above), and _dump_margin_rows (Viewer.
    __init__) reserves a row for exactly that - without it, content
    sized to fill the terminal's usable rows (rows - 1, the same bound
    _text_avail_rows() normally uses) would make that \\r\\n force a
    one-line scroll the instant it's written, pushing the dump's own
    top line out of view right as the shell prompt appears. A file with
    that many lines must fall through to interactive mode instead of
    being dumped somewhere it would immediately scroll itself out of;
    one line shorter must still dump normally."""
    def make(nlines):
        p = tmp_path / f"{nlines}.txt"
        p.write_text("\n".join(f"line {i}" for i in range(nlines)) + "\n")
        return str(p)

    # rows=40 in pty_session's own default - _text_avail_rows() without
    # the extra margin is rows - 1 = 39; with it (this path's own -1),
    # 38 is the largest line count that still fits.
    session = pty_session(["-F", make(38)])
    exited, out = _drain_until_exit(session)
    assert exited, "38 lines should still fit and dump-and-quit"
    assert b"\x1b[?1049h" not in out

    session = pty_session(["-F", make(39)])
    out = session.ready()
    assert session.proc.poll() is None, (
        "39 lines should no longer auto-dump - it would scroll its own "
        "first line out of view the instant the trailing \\r\\n is written"
    )
    assert b"\x1b[?1049h" in out  # started up interactively instead
    session.quit()


@requires_office_support
def test_quit_if_one_screen_shows_progress_then_a_clean_dump(pty_session, sample_docx):
    """A single-page Office document (Word here) still needs its usual
    Quick-Look/LibreOffice render, which can take a while - under -F it
    still shows progress (via _ensure_office_pages()'s own stderr
    _ProgressLine, not the interactive-only _ViewerProgress status
    line - the render's outcome, dump-and-quit or interactive, isn't
    known yet at this point), but must leave no trace behind once done:
    _ProgressLine self-overwrites with \\r rather than jumping to an
    absolute row (unlike _ViewerProgress, which - before this test's
    own fix - left the cursor on the bottom row right before the dump,
    scrolling its own top row out of view the moment more content
    followed it - see Viewer._dump_margin_rows for that separate,
    already-fixed bug)."""
    session = pty_session(["-F", sample_docx])
    exited, out = _drain_until_exit(session, deadline_seconds=OFFICE_TIMEOUT)
    assert exited, "pdfless did not exit on its own under -F for a single-page docx"
    assert b"\x1b[?1049h" not in out
    assert b"converting via LibreOffice" in out  # progress is now visible...
    assert b"\x1b[40;1H" not in out  # ...but never via an absolute cursor jump
    # ...and is fully gone (a bare \r, then EL/\x1b[2K to erase the whole
    # row - not just len()-based spaces, which would miss a row the
    # filename or spinner had wrapped) immediately before the dump - see
    # _ProgressLine.clear().
    osc_idx = out.index(b"\x1b]1337;File=inline=1")
    assert out[:osc_idx].endswith(b"\r\x1b[2K")


def test_quit_if_one_screen_does_not_exit_for_a_multipage_pdf(pty_session, sample_pdf):
    """-F must not affect a document that doesn't fit in one screen at
    all - lorem_ipsum.pdf is 7 pages, so this should start up exactly
    like a normal interactive session (no -F), needing an explicit quit."""
    session = pty_session(["-F", sample_pdf])
    out = session.ready()
    assert session.proc.poll() is None
    assert b"\x1b[?1049h" in out  # did enter the alternate screen normally
    session.quit()


def test_quit_if_one_screen_does_not_exit_for_a_long_text_file(pty_session, tmp_path):
    """Same as the multi-page PDF case, but exercises the text-mode
    (line-count-based) side of the "fits" check specifically, not the
    page-count one - a long file piped via $PAGER (e.g. `git log |
    pdfless -F`) must still page normally, not dump everything at once."""
    long_text = tmp_path / "long.txt"
    long_text.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
    session = pty_session(["-F", str(long_text)])
    out = session.ready()
    assert session.proc.poll() is None
    assert b"\x1b[?1049h" in out
    session.quit()


@requires_macos
def test_v_opens_the_file_externally_and_turns_on_follow_mode(
    pty_session, sample_pdf, tmp_path, monkeypatch,
):
    """"v" (see run_viewer(), pdfless._open_in_default_app()) hand
    the file off to macOS's own `open` command and switch follow mode
    on. A real `open` would launch a real GUI app, so this puts a fake
    one on PATH instead - a script that just records the path it was
    given - for the pdfless subprocess's own subprocess.Popen(["open",
    ...]) call to find via PATH lookup instead of the genuine
    /usr/bin/open. monkeypatching os.environ here (before the
    pty_session fixture starts it) is inherited by the child."""
    marker = tmp_path / "opened.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_open = fake_bin / "open"
    fake_open.write_text(f'#!/bin/sh\necho "$1" >> {marker}\n')
    fake_open.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    session = pty_session([sample_pdf])
    assert b" follow " not in session.ready()  # off by default (startup paint)

    session.send(b"O")  # no longer an alias for "v" (the file list, here one file)
    session.wait_for(b"only one file")
    assert not marker.exists()

    session.send(b"v")
    session.wait_for(b" follow ")
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)  # the fake open runs on its own
    assert marker.read_text().strip() == sample_pdf
    session.quit()


def test_f1_and_colon_h_both_open_the_help(pty_session, sample_text):
    """less(1)'s "?" is a backward search, so help moved to F1 - sent
    as ESC O P by most terminals and ESC [ 1 1 ~ by the rest - with
    ":h" as a second way in."""
    session = pty_session([sample_text])
    session.ready()

    for keys in (b"\x1bOP", b"\x1b[11~", b":h"):
        session.send(keys)
        session.wait_for(b"q to close help")  # the help box's own status line
        session.send(b"q")  # close it again
        session.ready()
    session.quit()


def test_question_mark_opens_a_backward_search_prompt(pty_session, sample_text):
    session = pty_session([sample_text])
    session.ready()

    session.send(b"?")
    out = session.wait_for(b"\x1b[?25h").decode(errors="replace")  # the cursor, shown at the prompt
    assert "q to close help" not in out  # no longer the help key
    # The prompt: the line cleared, then its own "?" echoed at column 1
    # (after the ^T reminder drawn at the right end).
    assert re.search(r"\x1b\[2K.*\x1b\[\d+;1H\?", out)
    assert "^T multi-line" in out
    assert out.rstrip().endswith("\x1b[?25h")  # then the real cursor is shown at the prompt

    session.send(b"line\r")
    session.wait_for(b"match ")
    session.quit(b"qq")  # (the first q clears the search)


def test_colon_q_quits(pty_session, sample_text):
    """less(1) users reach for ":q" out of habit - it should quit just
    like the plain "q" key, not just cancel the colon-command prompt."""
    session = pty_session([sample_text])
    session.ready()
    session.quit(b":q")


def test_empty_search_pattern_repeats_the_last_one(pty_session, sample_text):
    """An empty "/"/"?" (just Enter) should re-run the previous search
    pattern, less(1)-style, rather than doing nothing."""
    session = pty_session([sample_text])
    session.ready()

    session.send(b"/line\r")
    session.wait_for(b"match 1/")

    session.send(b"/\r")
    out = session.wait_for(b"match ").decode(errors="replace")
    assert "no previous search pattern" not in out
    session.quit(b"qq")  # (the first q clears the search)


def test_encrypted_pdf_as_first_file_prompts_and_unlocks(pty_session, sample_encrypted_pdf):
    """The very first file given is classified/opened before the
    interactive viewer's raw terminal mode is ever entered (see
    main()), so a password-protected one prompts via a plain
    getpass()-style prompt over the real tty, not the status-line one -
    see test_navigating_to_encrypted_pdf_prompts_on_status_line below
    for that side."""
    session = pty_session([sample_encrypted_pdf])
    session.wait_for(b"Password")
    session.send(b"secret123\r")
    out = session.ready()
    assert b"\x1b[?1049h" in out  # entered the alternate screen - unlocked and showing normally
    session.quit()


def test_encrypted_pdf_wrong_password_then_retry_succeeds(pty_session, sample_encrypted_pdf):
    session = pty_session([sample_encrypted_pdf])
    session.wait_for(b"Password")
    session.send(b"nope\r")
    session.wait_for(b"ncorrect password")
    session.wait_for(b"Password")  # asked again - only then is echo off
    session.send(b"secret123\r")
    assert b"\x1b[?1049h" in session.ready()
    session.quit()


def test_navigating_to_encrypted_pdf_prompts_on_status_line_and_unlocks(
    pty_session, sample_pdf, sample_encrypted_pdf,
):
    """Once the viewer's raw terminal mode is already active, an
    encrypted PDF reached via :n prompts on the status line instead
    (see PdfDocument._ensure_unlocked()/_prompt_pdf_password_raw)."""
    session = pty_session([sample_pdf, sample_encrypted_pdf])
    session.ready()

    session.send(b":n")
    session.wait_for(b"Password")

    session.send(b"secret123\r")
    session.wait_for(os.path.basename(sample_encrypted_pdf).encode())
    session.quit()


def test_navigating_to_encrypted_pdf_cancel_is_skipped_by_next_file(
    pty_session, sample_pdf, sample_encrypted_pdf, sample_image,
):
    """Cancelling (Esc) the status-line password prompt is treated
    exactly like any other unusable file - :n's auto-skip keeps
    stepping past it to the next file in the list, rather than
    getting stuck or crashing."""
    session = pty_session([sample_pdf, sample_encrypted_pdf, sample_image])
    session.ready()

    session.send(b":n")
    session.wait_for(b"Password")

    session.send(b"\x1b")  # Esc cancels
    session.wait_for(os.path.basename(sample_image).encode())
    session.quit()
