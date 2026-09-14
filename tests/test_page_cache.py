import pdfless


def test_pdf_page_cache_returns_sized_image_and_hits_cache(sample_pdf, tmp_path):
    handler = pdfless.PdfDocument(sample_pdf)
    cache = pdfless.PageCache(sample_pdf, str(tmp_path), handler)
    img = cache.get(1, 400, fit="width")
    assert img.width == 400

    img_again = cache.get(1, 400, fit="width")
    assert img_again is img, "a repeat request at the same size should hit the cache"


def test_image_page_cache_returns_correctly_sized_image(sample_image, tmp_path):
    handler = pdfless.ImageDocument(sample_image)
    cache = pdfless.PageCache(sample_image, str(tmp_path), handler)
    img = cache.get(1, 128, fit="width")
    # original is 64x48 (4:3) - scaled to 128 wide keeps that ratio
    assert img.width == 128
    assert img.height == 96


def test_office_page_cache_reads_from_cache_office_pages_live(tmp_path):
    """OfficeDocument._source_for_page() must read cache.office_pages
    fresh each call (not a copy captured at construction time), or a
    Viewer.reload() swapping in freshly-rendered pages after a
    -F/--follow change wouldn't be picked up."""
    from PIL import Image

    page1 = tmp_path / "page1.png"
    page2 = tmp_path / "page2.png"
    Image.new("RGB", (100, 100), "red").save(page1)
    Image.new("RGB", (100, 100), "blue").save(page2)

    handler = pdfless.OfficeDocument("/does/not/matter.pptx")
    cache = pdfless.PageCache(
        "/does/not/matter.pptx", str(tmp_path), handler,
        office_pages=[str(page1)],
    )
    first = cache.get(1, 50, fit="width")
    assert first.getpixel((0, 0))[:3] == (255, 0, 0)

    # Simulate a -F/--follow reload: new pages, cache cleared.
    cache.office_pages = [str(page2)]
    cache.clear()
    second = cache.get(1, 50, fit="width")
    assert second.getpixel((0, 0))[:3] == (0, 0, 255)


def test_page_cache_kind_dispatches_to_matching_handler_type(sample_pdf, sample_image, tmp_path):
    pdf_handler = pdfless.PdfDocument(sample_pdf)
    pdf_cache = pdfless.PageCache(sample_pdf, str(tmp_path), pdf_handler)
    assert isinstance(pdf_cache.handler, pdfless.PdfDocument)

    image_handler = pdfless.ImageDocument(sample_image)
    image_cache = pdfless.PageCache(sample_image, str(tmp_path), image_handler)
    assert isinstance(image_cache.handler, pdfless.ImageDocument)
