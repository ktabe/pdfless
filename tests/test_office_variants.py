"""Real-world Office/iWork format coverage: multi-sheet spreadsheets,
multi-page word-processor documents, and multi-slide presentations,
across both the modern (OOXML) and legacy binary formats, plus the
native iWork formats - see conftest.py for how each fixture was built.

requires_office_support (qlmanage + a local Chrome) since these all
go through OfficeDocument._render_office_pages() for real - there is
no way to test ExcelWorkbook/SlideDeck/FlowingText selection without
actually rendering.
"""

import fcntl
import pty
import struct
import termios
import tempfile

import pdfless
from conftest import requires_office_support


def classify(path, tmp_path, debug=False):
    for cls in pdfless.HANDLER_CLASSES:
        handler = cls.sniff(path, str(tmp_path), debug=debug)
        if handler is not None:
            return handler
    return None


@requires_office_support
def test_xlsx_multisheet_renders_one_page_per_sheet(sample_multisheet_xlsx, tmp_path):
    handler = classify(sample_multisheet_xlsx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 3


@requires_office_support
def test_xls_legacy_multisheet_renders_one_page_per_sheet(sample_multisheet_xls, tmp_path):
    """The legacy binary format (soffice --headless --convert-to xls)
    must classify and paginate the same way as its .xlsx source -
    ShouldNotScale and the TabViewItem tab strip are both properties
    of qlmanage's own generated preview, not of the file format
    itself."""
    handler = classify(sample_multisheet_xls, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 3


@requires_office_support
def test_numbers_multisheet_only_renders_first_sheet(sample_multisheet_numbers, tmp_path):
    """Known gap (not a regression to fix here): Numbers' own Quick
    Look generator (iWork.qlgenerator) marks up its multi-sheet tab
    strip differently from Excel's (Office.qlgenerator) -
    OfficeDocument._parse_sheet_tabs()'s TabViewItem-based regex
    doesn't recognize it, so a multi-sheet .numbers workbook (unlike
    the equivalent .xlsx/.xls) renders only its first sheet as a
    single page rather than one page per sheet."""
    handler = classify(sample_multisheet_numbers, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 1


@requires_office_support
def test_docx_twopage_paginates_via_print_to_pdf(sample_twopage_docx, tmp_path):
    """Word has no per-page markup Quick Look exposes (no
    PageElementXPath, unlike PowerPoint), but FlowingText's PDF path
    (see _build_pdf_pages()) doesn't need any - printing at one page's
    own height and letting Chrome's print engine paginate the rest
    lands on the document's real page count on its own."""
    handler = classify(sample_twopage_docx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 2
    assert handler._pdf_delegate is not None

    text = "\n".join(handler.extract_text(1))
    assert "Page one content" in text
    assert "Page two content" in text


@requires_office_support
def test_docx_continuous_flag_forces_a_single_page(sample_twopage_docx, tmp_path):
    """-c/--continuous still collapses it back to one page - the same
    "no real page boundaries of its own" reasoning FlowingText already
    had, just no longer the default (see
    test_docx_twopage_paginates_via_print_to_pdf)."""
    handler = classify(sample_twopage_docx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path), continuous=True)
    assert len(pages) == 1


@requires_office_support
def test_docx_renders_via_a_pdf_delegate_for_crisp_zoom(sample_twopage_docx, tmp_path):
    """FlowingText tries Chrome's --print-to-pdf first (see
    FlowingText._build_pdf_pages()) - a real PDF that pdfless
    re-rasterizes at whatever DPI the current zoom needs (see
    OfficeDocument.get_page_image()), instead of resizing one
    fixed-resolution screenshot. Confirmed here by asking for the same
    page at two different target widths and checking the returned
    image's actual pixel width tracks each one - a fixed screenshot
    resized by Pillow would still report the target width after an
    upscale (Pillow's resize() always returns exactly the requested
    size), so this alone doesn't distinguish the two paths; what does
    is handler._pdf_delegate itself being set at all - only the PDF
    path ever creates one, the screenshot fallback never does."""
    handler = classify(sample_twopage_docx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    handler.build_pages(str(tmp_path))
    assert handler._pdf_delegate is not None

    cache = pdfless.PageCache(handler.path, str(tmp_path), handler)
    small = handler.get_page_image(cache, 1, 300, "width")
    big = handler.get_page_image(cache, 1, 1200, "width")
    assert small.width == 300
    assert big.width == 1200
    # Re-rasterized independently at each width, not the same bitmap
    # twice over - the aspect ratio (and so the height) should match,
    # within the couple of pixels two independently-rounded DPIs can
    # differ by.
    assert abs(big.height - round(small.height * 1200 / 300)) <= 2


@requires_office_support
def test_docx_hyperlink_survives_as_a_real_pdf_link(sample_docx_with_link, tmp_path):
    """A plain <a href> in the original Word document, run through
    Quick Look -> Chrome's --print-to-pdf, comes out as a real PDF
    /Link annotation (confirmed by hand with pypdf) - so it's clickable
    exactly like a native PDF's hyperlink, via the exact same
    PdfDocument.build_link_index() a real PDF's links go through (see
    Viewer._ensure_link_index()'s pdf_source fallback to
    doc_handler._pdf_delegate)."""
    handler = classify(sample_docx_with_link, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert handler._pdf_delegate is not None

    link_index = handler._pdf_delegate.build_link_index(len(pages))
    links = link_index[0]["links"]
    assert any(link.get("uri") == "https://example.com/hello" for link in links)


def _find_goto_links(link_index):
    """Every "kind": "page" link across a whole build_link_index()
    result, as (found_on_page, link) pairs - the internal-link fixture
    now spans several real PDF pages (see
    test_docx_twopage_paginates_via_print_to_pdf), so a fixed page
    index can't be assumed the way it could when FlowingText always
    forced a single page."""
    return [
        (page_num, link)
        for page_num, page in enumerate(link_index, start=1)
        for link in page["links"]
        if link.get("kind") == "page"
    ]


@requires_office_support
def test_docx_internal_link_survives_as_a_real_pdf_goto(sample_docx_with_internal_link, tmp_path):
    """An internal (bookmark-anchored) hyperlink survives as a real PDF
    /GoTo link, resolved the same way a real PDF's internal links are
    (build_link_index()'s "kind": "page" case). With FlowingText now
    paginating normally (see test_docx_twopage_paginates_via_print_to_pdf),
    the ~80 filler paragraphs between the link and its target bookmark
    span several real pages, so this is a genuine cross-page jump, not
    just a different scroll position on the same page."""
    handler = classify(sample_docx_with_internal_link, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) > 1
    assert handler._pdf_delegate is not None

    link_index = handler._pdf_delegate.build_link_index(len(pages))
    goto_links = _find_goto_links(link_index)
    assert len(goto_links) == 1
    found_on_page, link = goto_links[0]
    assert link["top_pt"] is not None
    # The target bookmark is ~80 filler paragraphs below the link
    # itself - a real forward jump, not a same-spot no-op.
    assert link["page"] > found_on_page


@requires_office_support
def test_docx_internal_link_click_scrolls_without_crashing(sample_docx_with_internal_link, tmp_path):
    """Regression test: Viewer.go_to_link_target() used to call
    self.doc_handler.page_size_pt() unconditionally, assuming
    doc_handler is a real PdfDocument - AttributeError for an
    OfficeDocument (even one with a _pdf_delegate, since that's the
    delegate's method, not doc_handler's own). Exercises the exact
    path a mouse click on this link takes (handle_click() ->
    _activate_link() -> go_to_link_target()) via a real Viewer,
    checking the click actually jumps to the target page and scrolls
    there instead of crashing or silently doing nothing."""
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 960, 720))
    handler = classify(sample_docx_with_internal_link, tmp_path)
    viewer = pdfless.Viewer([handler], 0, 1, str(tmp_path), slave, None)
    viewer.refresh()

    viewer._ensure_link_index()
    found_on_page, link = _find_goto_links(viewer._link_index)[0]

    viewer.page = found_on_page
    viewer._load_page()
    viewer.scroll = 0
    viewer._activate_link(link)
    assert viewer.page == link["page"]
    assert viewer.scroll > 0


@requires_office_support
def test_doc_legacy_twopage_paginates_via_print_to_pdf(sample_twopage_doc, tmp_path):
    handler = classify(sample_twopage_doc, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 2

    text = "\n".join(handler.extract_text(1))
    assert "Page one content" in text
    assert "Page two content" in text


@requires_office_support
def test_pages_twopage_renders_as_single_continuous_page(sample_twopage_pages, tmp_path):
    """Pages' Quick Look preview (iWork.qlgenerator) has the same
    shape as Word's here - no PageElementXPath - so it gets the same
    always-continuous FlowingText treatment as Word, even though the
    source document genuinely spans 2 physical pages."""
    handler = classify(sample_twopage_pages, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 1


@requires_office_support
def test_pptx_twoslide_paginates_confidently(sample_twoslide_pptx, tmp_path):
    """PowerPoint's Quick Look generator names the slide boundary via
    PageElementXPath, so (unlike Word) this is trusted to paginate by
    default with no -c/--continuous involved."""
    handler = classify(sample_twoslide_pptx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 2


@requires_office_support
def test_ppt_legacy_twoslide_paginates_confidently(sample_twoslide_ppt, tmp_path):
    handler = classify(sample_twoslide_ppt, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 2


@requires_office_support
def test_key_twoslide_renders_as_single_continuous_page(sample_twoslide_key, tmp_path):
    """Known gap (not a regression to fix here): unlike PowerPoint,
    Keynote's own Quick Look generator doesn't emit PageElementXPath
    at all, so a 2-slide .key deck isn't paginated confidently and
    falls back to a single continuously-scrollable page, the same as
    Word/Pages."""
    handler = classify(sample_twoslide_key, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 1
