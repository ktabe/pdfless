"""Search-while-in-text-mode: search now works whenever text_mode is
on, regardless of whether the underlying kind's image view has its own
page/bbox search index (currently PDF only, via build_search_index()).
These construct a real Viewer directly (needs a real pty for
get_term_cells()'s ioctl) rather than going through a full pdfless.py
subprocess, since asserting on search results/highlighting from a pty's
raw escape-sequence output is unreliable to parse - see
test_viewer_integration.py for the subprocess-level smoke tests."""

import fcntl
import os
import pty
import struct
import termios
import tempfile

import pdfless
from conftest import requires_office_support


def make_viewer(path, kind, npages):
    """A real Viewer, with page/match-scroll rendering stubbed out -
    this synthetic pty has no real terminal pixel size
    (get_pixel_size()), which _load_page()/the image-mode match-scroll
    helpers need but text-mode search doesn't. viewer.refresh() (which
    a real run_viewer() calls right after construction - see there) is
    what actually populates text_lines for kind=="text"/"office" via
    _load_text_page(), so tests must call this instead of
    Viewer.__init__ alone."""
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 960, 720))
    tmpdir = tempfile.mkdtemp()
    viewer = pdfless.Viewer([(os.path.abspath(path), kind, npages)], 0, 1, tmpdir, slave, None)
    viewer._load_page = lambda: None
    viewer._scroll_image_to_match = lambda match: None
    viewer._scroll_text_to_match = lambda match: None
    viewer._draw = lambda: None
    viewer._draw_text = lambda: None
    viewer.refresh()
    return viewer


@requires_office_support
def test_office_document_search_only_works_in_text_mode(sample_docx):
    viewer = make_viewer(sample_docx, "office", None)
    assert isinstance(viewer.doc_handler, pdfless.OfficeDocument)

    # Not yet in text mode: doc_handler.supports_search() alone
    # (OfficeDocument's) is False, matching the '/' key handler's
    # gating condition (`viewer.text_mode or doc_handler.supports_search()`).
    assert viewer.doc_handler.supports_search() is False

    assert viewer.enter_text_mode() is True
    assert viewer.text_mode is True

    viewer.start_search("Hello")
    assert viewer.search_matches == [(0, 0, 5)]

    viewer.start_search("ThisTextDoesNotAppearAnywhere")
    assert viewer.search_matches == []


def test_plain_text_file_search_works(sample_text):
    viewer = make_viewer(sample_text, "text", 1)
    assert viewer.text_mode is True  # permanently, for kind=="text"
    assert viewer.text_lines == ["line one", "line two", "line three"]

    viewer.start_search("line two")
    assert viewer.search_matches == [(1, 0, 8)]


def test_pdf_search_uses_bbox_index_in_both_modes(sample_pdf):
    """Regression check: PdfDocument.text_mode_is_paginated() is True,
    so PDF search must keep using the page/bbox index (5-tuple matches)
    rather than the line-based search text/rtf/office use - in image
    mode (already worked before this change) and in text mode (must
    keep working the same way after it)."""
    viewer = make_viewer(sample_pdf, "pdf", pdfless.pdf_page_count(sample_pdf))

    viewer.start_search("Lorem")
    assert viewer.search_matches
    assert len(viewer.search_matches[0]) == 5  # (page, xmin, ymin, xmax, ymax)

    viewer.text_mode = True
    viewer.start_search("Lorem")
    assert viewer.search_matches
    assert len(viewer.search_matches[0]) == 5


def test_image_document_never_supports_search(sample_image):
    viewer = make_viewer(sample_image, "image", 1)
    assert viewer.doc_handler.supports_search() is False
    assert viewer.doc_handler.supports_text_mode() is False
    assert viewer.text_mode is False
    # Mirrors the '/' key handler's gating condition directly, since an
    # ImageDocument can never reach text_mode at all.
    assert not (viewer.text_mode or viewer.doc_handler.supports_search())
