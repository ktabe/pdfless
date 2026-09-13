import os
import struct
import fcntl
import termios
import shutil
import signal
import sys
import time
import pty

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDFLESS_PY = os.path.join(REPO_ROOT, "pdfless.py")
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

sys.path.insert(0, REPO_ROOT)
import pdfless  # noqa: E402  (import after sys.path tweak, deliberately)


# All of these are static files checked into tests/fixtures/ rather
# than generated on the fly - faster (no per-run PIL/textutil work),
# easy to inspect/open by hand, and (sample.docx in particular) works
# the same regardless of whether textutil happens to be available at
# test time; only the actual Quick Look/Chrome rendering of it is
# gated on macOS (see requires_office_support).
@pytest.fixture
def sample_pdf():
    """A real, small PDF already checked into the repo root (used for
    screenshots/manual testing too) - avoids depending on any PDF-
    writing library just to get a valid multi-page-capable sample."""
    return os.path.join(REPO_ROOT, "lorem_ipsum.pdf")


@pytest.fixture
def sample_image():
    return os.path.join(FIXTURES_DIR, "sample.png")


@pytest.fixture
def sample_text():
    return os.path.join(FIXTURES_DIR, "sample.txt")


@pytest.fixture
def sample_rtf():
    return os.path.join(FIXTURES_DIR, "sample.rtf")


@pytest.fixture
def sample_docx():
    return os.path.join(FIXTURES_DIR, "sample.docx")


def office_support_available():
    return (
        shutil.which("qlmanage") is not None
        and pdfless.find_chrome() is not None
    )


requires_office_support = pytest.mark.skipif(
    not office_support_available(),
    reason="needs macOS Quick Look (qlmanage) + a local Chrome/Chromium",
)


class PtySession:
    """Drives a real `pdfless.py <files...>` subprocess through a
    pseudo-terminal - the only way to exercise Viewer end-to-end (it
    needs a real tty for ioctl-based terminal-size queries), the same
    way this was done by hand throughout development."""

    def __init__(self, args, rows=40, cols=120):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvp(sys.executable, [sys.executable, PDFLESS_PY, *args])
        else:
            fcntl.ioctl(
                self.fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, cols * 8, rows * 16),
            )

    def send(self, data, wait=0.5):
        os.write(self.fd, data)
        time.sleep(wait)

    def read_all(self, timeout=2.0):
        """Drain whatever output is currently available, waiting up to
        `timeout` seconds for more between reads - bounded by design,
        unlike reading until EOF (which would block forever if the
        child is still alive and simply idle, e.g. stuck waiting on a
        keypress it never got)."""
        import select

        buf = b""
        while True:
            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                break
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return buf

    def close(self, timeout=5):
        """Never blocks indefinitely: SIGTERM first, then poll with
        WNOHANG so a test that leaves pdfless wedged (e.g. stuck
        waiting for a keypress it never got) can't hang the whole
        suite - SIGKILL once `timeout` elapses without it exiting."""
        try:
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                pid, _status = os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                return
            if pid != 0:
                return
            time.sleep(0.1)
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except OSError:
            pass


@pytest.fixture
def pty_session():
    sessions = []

    def _make(args, rows=40, cols=120):
        s = PtySession(args, rows=rows, cols=cols)
        sessions.append(s)
        return s

    yield _make
    for s in sessions:
        s.close()
