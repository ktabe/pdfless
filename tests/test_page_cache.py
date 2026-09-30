import subprocess

import pdfless


def test_pdf_page_cache_returns_sized_image_and_hits_cache(sample_pdf, tmp_path):
    handler = pdfless.PdfDocument(sample_pdf)
    cache = pdfless.PageCache(str(tmp_path), handler)
    img = cache.get(1, 400, fit="width")
    assert img.width == 400

    img_again = cache.get(1, 400, fit="width")
    assert img_again is img, "a repeat request at the same size should hit the cache"


def test_image_page_cache_returns_correctly_sized_image(sample_image, tmp_path):
    handler = pdfless.ImageDocument(sample_image)
    cache = pdfless.PageCache(str(tmp_path), handler)
    img = cache.get(1, 128, fit="width")
    # original is 64x48 (4:3) - scaled to 128 wide keeps that ratio
    assert img.width == 128
    assert img.height == 96


def test_office_page_cache_reads_from_handler_pages_live(tmp_path):
    """OfficeDocument._source_for_page() must read self.pages fresh
    each call (not a copy captured elsewhere), since Viewer.reload()
    (after a -f/--follow change) replaces it via a fresh build_pages()
    call on the very same handler instance."""
    from PIL import Image

    page1 = tmp_path / "page1.png"
    page2 = tmp_path / "page2.png"
    Image.new("RGB", (100, 100), "red").save(page1)
    Image.new("RGB", (100, 100), "blue").save(page2)

    handler = pdfless.OfficeDocument("/does/not/matter.pptx")
    handler.pages = [str(page1)]
    cache = pdfless.PageCache(str(tmp_path), handler)
    first = cache.get(1, 50, fit="width")
    assert first.getpixel((0, 0))[:3] == (255, 0, 0)

    # Simulate a -f/--follow reload: build_pages() (via
    # _remember_pages()) replaces handler.pages, and Viewer clears
    # the cache so the new pages actually get (re)loaded.
    handler.pages = [str(page2)]
    cache.clear()
    second = cache.get(1, 50, fit="width")
    assert second.getpixel((0, 0))[:3] == (0, 0, 255)


def test_renders_of_two_files_at_once_dont_share_a_scratch_file(sample_pdf, tmp_path):
    """Regression test: pdftoppm wrote to tmpdir/page-<page>-<dpi>.ppm
    whatever the file, so a background render of one file's page and a
    render of another file's same-numbered page (a prefetch still
    running after a file switch) could read back each other's image."""
    import threading
    from pypdf import PdfWriter
    other = tmp_path / "reversed.pdf"
    writer = PdfWriter()
    writer.append(sample_pdf, pages=(6, 7), import_outline=False)  # its page 7 as page 1
    writer.write(str(other))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    docs = [pdfless.PdfDocument(sample_pdf), pdfless.PdfDocument(str(other))]
    expected = [pdfless.PageCache(str(scratch), d).get(1, 400).tobytes() for d in docs]
    assert expected[0] != expected[1]
    results = {}

    def render(i):
        results[i] = pdfless.PageCache(str(scratch), docs[i % 2]).get(1, 400).tobytes()

    threads = [threading.Thread(target=render, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(results[i] == expected[i % 2] for i in range(8))
    assert list(scratch.iterdir()) == []  # nothing left behind


def test_pdftoppm_says_nothing_on_the_screen(sample_pdf, tmp_path, monkeypatch):
    """poppler's warnings about a malformed PDF went straight to the
    terminal, over the page - stderr is captured now."""
    calls = []
    real = pdfless.run_subprocess

    def spy(args, **kwargs):
        calls.append(kwargs)
        return real(args, **kwargs)

    monkeypatch.setattr(pdfless, "run_subprocess", spy)
    pdfless.PageCache(str(tmp_path), pdfless.PdfDocument(sample_pdf)).get(1, 300)
    assert any(kwargs.get("stderr") == subprocess.PIPE for kwargs in calls)
