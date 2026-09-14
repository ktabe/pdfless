"""Real-world Office/iWork format coverage: multi-sheet spreadsheets,
multi-page word-processor documents, and multi-slide presentations,
across both the modern (OOXML) and legacy binary formats, plus the
native iWork formats - see conftest.py for how each fixture was built.

requires_office_support (qlmanage + a local Chrome) since these all
go through OfficeDocument._render_office_pages() for real - there is
no way to test ExcelWorkbook/SlideDeck/FlowingText selection without
actually rendering.
"""

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
def test_docx_twopage_renders_as_single_continuous_page(sample_twopage_docx, tmp_path):
    """Word has no per-page markup Quick Look exposes (no
    PageElementXPath, unlike PowerPoint) - by design, FlowingText
    always renders as one continuously-scrollable page regardless of
    the source document's actual page count."""
    handler = classify(sample_twopage_docx, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 1

    text = "\n".join(handler.extract_text(1))
    assert "Page one content" in text
    assert "Page two content" in text


@requires_office_support
def test_doc_legacy_twopage_renders_as_single_continuous_page(sample_twopage_doc, tmp_path):
    handler = classify(sample_twopage_doc, tmp_path)
    assert isinstance(handler, pdfless.OfficeDocument)
    pages = handler.build_pages(str(tmp_path))
    assert len(pages) == 1

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
