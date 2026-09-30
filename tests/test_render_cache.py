"""The persistent rendered-pages cache (_render_result_cached()): an
entry is served only for the file exactly as it was rendered from, and
pruning never removes one this pdfless is using. conftest.py gives every
test a cache directory of its own."""

import os

import pdfless


def renderer(tmp_path, calls, on_render=None):
    """A render_fn making a one-page "screenshot" result, counting calls."""
    def render():
        calls.append(1)
        if on_render:
            on_render()
        page = tmp_path / f"out-{len(calls)}.png"
        page.write_bytes(b"png")
        return [str(page)]
    return render


def test_an_unchanged_file_is_served_from_the_cache(tmp_path):
    doc = tmp_path / "doc.pptx"
    doc.write_bytes(b"one")
    calls = []
    assert pdfless._render_result_cached(str(doc), renderer(tmp_path, calls))[1] is False
    assert pdfless._render_result_cached(str(doc), renderer(tmp_path, calls))[1] is True
    assert len(calls) == 1


def test_a_save_during_the_render_makes_it_stale(tmp_path):
    """The entry was judged fresh by being newer than the file - so a save
    while it rendered (common under -f) left the old content cached for
    good."""
    doc = tmp_path / "doc.pptx"
    doc.write_bytes(b"one")
    calls = []

    def saved_meanwhile():
        doc.write_bytes(b"two, longer")

    pdfless._render_result_cached(str(doc), renderer(tmp_path, calls, saved_meanwhile))
    assert pdfless._render_result_cached(str(doc), renderer(tmp_path, calls))[1] is False
    assert len(calls) == 2


def test_a_copy_with_an_older_mtime_is_rendered_again(tmp_path):
    """cp -p, rsync -t or a restore put back a file with an older mtime -
    older than the entry, so it used to count as fresh."""
    doc = tmp_path / "doc.pptx"
    doc.write_bytes(b"new")
    calls = []
    pdfless._render_result_cached(str(doc), renderer(tmp_path, calls))
    doc.write_bytes(b"old")  # same size, different content...
    os.utime(doc, (1_000_000, 1_000_000))  # ...and an old mtime
    assert pdfless._render_result_cached(str(doc), renderer(tmp_path, calls))[1] is False
    assert len(calls) == 2


def test_pruning_spares_the_entries_in_use(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfless, "_CACHE_ENTRIES_IN_USE", set())
    calls = []
    docs = []
    for i in range(3):
        doc = tmp_path / f"doc{i}.pptx"
        doc.write_bytes(b"x")
        docs.append(doc)
        pdfless._render_result_cached(str(doc), renderer(tmp_path, calls))
    first = pdfless._cached_render_dir(str(docs[0]))
    cache_dir = os.path.dirname(first)
    # Make the first the least recently used, then prune down to one.
    os.utime(first, (1, os.path.getmtime(first)))
    pdfless._CACHE_ENTRIES_IN_USE.discard(pdfless._cached_render_dir(str(docs[1])))
    pdfless._CACHE_ENTRIES_IN_USE.discard(pdfless._cached_render_dir(str(docs[2])))
    pdfless._prune_office_cache(cache_dir, keep=1)
    assert os.path.isdir(first)  # in use, however old


def test_width_and_height_fits_are_cached_apart(sample_image, tmp_path):
    """The same number as a width target and as a height target is two
    different sizes - they shared a cache entry."""
    cache = pdfless.PageCache(str(tmp_path), pdfless.ImageDocument(sample_image))
    wide = cache.get(1, 120, "width")
    tall = cache.get(1, 120, "height")
    assert wide.width == 120 and tall.height == 120
    assert wide.size != tall.size or wide.width == wide.height
