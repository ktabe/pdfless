import pdfless


def classify(path, tmp_path, debug=False):
    """Mirror main()'s HANDLER_CLASSES dispatch loop: the first sniff()
    that matches wins; a positively-identified-but-broken file raises
    UnusableFile instead of returning None."""
    for cls in pdfless.HANDLER_CLASSES:
        handler = cls.sniff(path, str(tmp_path), debug=debug)
        if handler is not None:
            return handler
    return None


def test_pdf_classified_as_pdf(sample_pdf, tmp_path):
    handler = classify(sample_pdf, tmp_path)
    assert isinstance(handler, pdfless.PdfDocument)
    assert handler.kind == "pdf"
    assert handler.page_count() >= 1


def test_image_classified_as_image(sample_image, tmp_path):
    handler = classify(sample_image, tmp_path)
    assert isinstance(handler, pdfless.ImageDocument)
    assert handler.kind == "image"
    assert handler.page_count() == 1


def test_plain_text_classified_as_text(sample_text, tmp_path):
    handler = classify(sample_text, tmp_path)
    assert isinstance(handler, pdfless.TextDocument)
    assert not isinstance(handler, pdfless.RtfDocument)
    assert handler.kind == "text"
    assert handler.extract_text(1) == ["line one", "line two", "line three"]


def test_rtf_classified_as_rtf_not_plain_text(sample_rtf, tmp_path):
    handler = classify(sample_rtf, tmp_path)
    assert isinstance(handler, pdfless.RtfDocument)
    assert handler.kind == "text"  # same outward "kind" as TextDocument
    lines = handler.extract_text(1)
    assert lines is not None
    text = "\n".join(lines)
    assert "Hello from a test RTF file" in text
    # The raw RTF control words must NOT leak into what's shown.
    assert r"\rtf1" not in text


def test_corrupt_pdf_raises_unusable_file_not_silently_skipped(tmp_path):
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"%PDF-1.4\nnot actually a valid pdf")
    raised = False
    try:
        classify(str(bad), tmp_path)
    except pdfless.UnusableFile as e:
        raised = True
        assert "PDF" in str(e)
    assert raised, "a corrupt PDF should raise UnusableFile, not fall through silently"


def test_invalid_utf8_non_pdf_file_raises_unusable_file(tmp_path):
    bad = tmp_path / "bad.txt"
    # No NUL byte (so is_probably_text() says yes), but not valid UTF-8.
    bad.write_bytes(b"\xff\xfe invalid utf8, no nul bytes here")
    raised = False
    try:
        classify(str(bad), tmp_path)
    except pdfless.UnusableFile as e:
        raised = True
        assert "UTF-8" in str(e)
    assert raised


def test_unrecognizable_binary_file_classifies_as_none(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"\x00\x01\x02\xff\xfe")
    assert classify(str(junk), tmp_path) is None


def test_capability_matrix_matches_expectations(
    sample_pdf, sample_image, sample_text, sample_rtf, tmp_path
):
    pdf = classify(sample_pdf, tmp_path)
    image = classify(sample_image, tmp_path)
    text = classify(sample_text, tmp_path)
    rtf = classify(sample_rtf, tmp_path)

    assert (pdf.supports_text_mode(), pdf.supports_search(), pdf.text_mode_is_paginated()) == (
        True, True, True,
    )
    assert (image.supports_text_mode(), image.supports_search(), image.text_mode_is_paginated()) == (
        False, False, False,
    )
    assert (text.supports_text_mode(), text.supports_search(), text.text_mode_is_paginated()) == (
        True, True, False,
    )
    assert (rtf.supports_text_mode(), rtf.supports_search(), rtf.text_mode_is_paginated()) == (
        True, True, False,
    )


def test_document_handler_for_kind_matches_sniffed_type(
    sample_pdf, sample_image, sample_text, sample_rtf, tmp_path
):
    """_document_handler_for_kind() reconstructs the handler Viewer
    reuses across page navigation/reload from just the "kind" string -
    it must disambiguate RtfDocument vs TextDocument (both kind=="text")
    the same way the original sniff() pass did."""
    for path, expected_cls in [
        (sample_pdf, pdfless.PdfDocument),
        (sample_image, pdfless.ImageDocument),
        (sample_text, pdfless.TextDocument),
        (sample_rtf, pdfless.RtfDocument),
    ]:
        handler = classify(path, tmp_path)
        rebuilt = pdfless._document_handler_for_kind(handler.kind, path)
        assert type(rebuilt) is expected_cls
