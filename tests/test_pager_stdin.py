"""Reading from stdin (main()'s "reading_stdin") - no file argument at
all, or "-" in its place - lets pdfless work as $PAGER: git, man, and
friends invoke $PAGER with nothing but the piped content on stdin and
expect it to still take keyboard input from the terminal. pdfless
drains the pipe into a real file up front (every DocumentHandler.sniff()
needs a path, not a stream) and falls back to /dev/tty for the
keyboard/mouse side once its own stdin is a plain pipe rather than a
tty - the same trick less(1)/most(1) use."""

def test_no_file_argument_pages_piped_stdin(pty_session):
    content = "".join(f"piped line {i}\n" for i in range(30)).encode()
    session = pty_session([], stdin_data=content)
    out = session.ready().decode(errors="replace")
    assert "piped line 0" in out
    assert "piped line 1" in out
    session.quit()


def test_dash_argument_also_means_stdin(pty_session):
    """"-" is the explicit spelling of the same thing, e.g. for `cmd |
    pdfless.py - notes.pdf` alongside a real file."""
    session = pty_session(["-"], stdin_data=b"content via dash\n")
    assert b"content via dash" in session.ready()
    session.quit()


def test_stdin_pager_scrolls_and_quits_cleanly(pty_session):
    """Not just a static dump - the usual line/page navigation and a
    clean "q" exit work the same as paging a real file."""
    content = "".join(f"line {i}\n" for i in range(200)).encode()
    session = pty_session([], rows=20, cols=60, stdin_data=content)
    session.ready()

    session.send(b"G")  # jump to the end
    assert b"line 199" in session.ready()
    session.quit()


def test_no_file_and_no_piped_stdin_reports_an_error(pty_session):
    """With nothing piped in, pdfless's own stdin is the pty - a real
    tty - so there's nothing to read and no file to fall back to: it
    says so and exits with an error, before any raw/alt-screen mode."""
    session = pty_session([])  # no stdin_data: stdin stays the pty itself
    code, out = session.exited()
    assert b"no file given" in out
    assert code not in (None, 0)
