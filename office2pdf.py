#!/usr/bin/env python3
"""office2pdf.py - convert any file this Mac's Quick Look generators
can preview (Word, Excel, PowerPoint, Keynote, Pages, RTF, and more)
to a real PDF, via the same pipeline pdfless.py's Office-document
support uses internally: `qlmanage -o -p` renders a Quick Look HTML
preview of the file, then a local headless Chrome/Chromium prints
that HTML to PDF (`--print-to-pdf`).

A separate, standalone script from pdfless.py (no shared imports, no
third-party dependencies - stdlib only) - and simpler than what
pdfless.py itself does with this HTML: this always prints at the Quick
Look preview's own page/slide size (from its plist, when it has one),
letting Chrome's print engine paginate normally if the content is
taller than that - not pdfless's FlowingText-specific "measure the
real content height and force it onto a single continuous page"
choice (see FlowingText._build_pdf_pages() there), which exists only
because *that* code needs to match pdfless's own always-continuous
Word/RTF/Pages behavior.

Usage:
    ./office2pdf.py some.docx                # writes some.pdf
    ./office2pdf.py some.pptx -o out.pdf

macOS only (qlmanage is part of Quick Look, which only exists there),
and needs a local Chrome, Chromium, or Brave install.
"""

import argparse
import concurrent.futures
import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

# Same three browsers pdfless.py's find_chrome() checks (see there for
# why Vivaldi is deliberately excluded: its headless mode just crashes)
# - kept to this short list rather than pdfless.py's own default-browser
# preference logic, which isn't worth the extra complexity here.
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)

# A Quick Look preview can embed a whole picture (or, for Excel/Numbers,
# an entire sheet flattened into one page) as one giant PDF - hard-cap
# how many px on a side pdftocairo is asked to rasterize it at, so a
# pathological sheet can't make it (or Chrome, decoding the result)
# choke - see rasterize_pdf_img_sources() below.
EMBEDDED_IMG_MAX_PX = 6000

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")([^"]+)(")', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_SRC_ATTR_RE = re.compile(r'\bsrc="([^"]+)"', re.IGNORECASE)
_WIDTH_ATTR_RE = re.compile(r'\bwidth="([\d.]+)"', re.IGNORECASE)
_HEIGHT_ATTR_RE = re.compile(r'\bheight="([\d.]+)"', re.IGNORECASE)
_IFRAME_SRC_RE = re.compile(r'(<iframe\b[^>]*\bsrc=")([^"]+)(")', re.IGNORECASE)
_ABSOLUTE_SRC_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|/)")  # a URL scheme, or an absolute path


def _pdf_page_size_pt(pdf_path):
    try:
        out = subprocess.run(
            ["pdfinfo", pdf_path], capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"^Page\s*(?:\d+\s+)?size:\s+([\d.]+) x ([\d.]+)", out, re.MULTILINE)
    return (float(m.group(1)), float(m.group(2))) if m else None


def rasterize_pdf_img_sources(html_path, tmpdir):
    """A Quick Look Office/iWork preview can embed a picture - or, for
    Excel/Numbers, an entire sheet - as `<img src="Attachment.pdf">`:
    the generator apparently assumes a renderer that can show a PDF
    inline as an image, the way Safari/WebKit (Quick Look's own host)
    does. Plain Chrome can't, and just shows a broken-image ("sad
    face") icon instead - confirmed by hand to be exactly why Excel/
    Numbers files came out broken here.

    iWork.qlgenerator (Numbers/Pages/Keynote) additionally splits a
    multi-sheet/page document into one `<iframe src="Attachment.html">`
    per sheet rather than embedding everything directly in Preview.html
    the way Office.qlgenerator does - and it's each of *those* files
    that actually embeds the `<img src="*.pdf">`, not Preview.html
    itself - so this walks that tree recursively, not just html_path.

    Rewrites every such PDF reference (anywhere in the tree) to a PNG
    rasterized via poppler's pdftocairo, and every iframe reference to
    point at its (recursively) patched target; every patched file is
    written alongside the original it came from, so any other
    relative reference in it keeps resolving unchanged. A simplified
    version of pdfless.py's own _rasterize_pdf_img_sources() (no
    Pillow-based sanity check on the converted PNG's size, since this
    script has no third-party dependencies at all) - see there for
    more on the DPI-selection reasoning below.

    Returns html_path's own patched copy, or html_path unchanged if
    nothing needed patching, or pdftocairo isn't available."""
    file_contents = {}
    pdf_refs_by_file = {}
    to_visit = [html_path]
    seen = set()
    while to_visit:
        path = to_visit.pop()
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            continue
        file_contents[path] = content
        base_dir = os.path.dirname(path)
        pdf_refs = {}
        for tag_m in _IMG_TAG_RE.finditer(content):
            tag = tag_m.group(0)
            src_m = _SRC_ATTR_RE.search(tag)
            if not src_m or not src_m.group(1).lower().endswith(".pdf"):
                continue
            ref = src_m.group(1)
            if ref in pdf_refs:
                continue
            w_m, h_m = _WIDTH_ATTR_RE.search(tag), _HEIGHT_ATTR_RE.search(tag)
            pdf_refs[ref] = (
                float(w_m.group(1)) if w_m else None,
                float(h_m.group(1)) if h_m else None,
            )
        pdf_refs_by_file[path] = sorted((ref, w, h) for ref, (w, h) in pdf_refs.items())
        for m in _IFRAME_SRC_RE.finditer(content):
            src = m.group(2)
            if _ABSOLUTE_SRC_RE.match(src):
                continue  # http(s)/data/... - not a local sibling file
            candidate = os.path.join(base_dir, src)
            if src.lower().endswith((".html", ".htm")) and os.path.isfile(candidate):
                to_visit.append(candidate)

    if not shutil.which("pdftocairo"):
        return html_path
    all_refs = [
        (path, ref, w, h)
        for path, refs in pdf_refs_by_file.items()
        for ref, w, h in refs
    ]
    if not all_refs:
        return html_path

    def _convert(item):
        path, ref, decl_w, decl_h = item
        src_pdf = os.path.join(os.path.dirname(path), ref)
        if not os.path.isfile(src_pdf):
            return path, ref, None

        # A fixed DPI is wildly wrong for a picture that's actually an
        # entire spreadsheet flattened into one PDF, sized in the
        # thousands of points on a side - aim for roughly the size the
        # <img> tag is actually going to display it at instead (a PDF
        # point is ~1 CSS px at 96dpi), falling back to 300 if neither
        # the page size nor the declared display size is available,
        # then hard-capped regardless (see EMBEDDED_IMG_MAX_PX).
        dpi = 300.0
        page_size = _pdf_page_size_pt(src_pdf)
        if page_size:
            page_w_pt, page_h_pt = page_size
            if decl_w and page_w_pt:
                dpi = 72.0 * decl_w / page_w_pt
            elif decl_h and page_h_pt:
                dpi = 72.0 * decl_h / page_h_pt
            if page_w_pt:
                dpi = min(dpi, 72.0 * EMBEDDED_IMG_MAX_PX / page_w_pt)
            if page_h_pt:
                dpi = min(dpi, 72.0 * EMBEDDED_IMG_MAX_PX / page_h_pt)
        dpi = max(36.0, min(300.0, dpi))

        prefix = os.path.join(tmpdir, f"qlimg-{hashlib.md5(src_pdf.encode()).hexdigest()[:12]}")
        try:
            subprocess.run(
                # pdftocairo (not pdftoppm - no -transp there) so a
                # transparent-background picture keeps its transparency
                # instead of getting composited onto opaque white here.
                ["pdftocairo", "-png", "-transp", "-r", str(round(dpi)), "-singlefile", src_pdf, prefix],
                capture_output=True, check=True, timeout=20,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return path, ref, None
        png_path = prefix + ".png"
        if not os.path.isfile(png_path):
            return path, ref, None
        return path, ref, "file://" + os.path.abspath(png_path)

    png_by_file_ref = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for path, ref, uri in pool.map(_convert, all_refs):
            if uri:
                png_by_file_ref[(path, ref)] = uri

    patched_by_original = {}

    def _patch(path):
        if path in patched_by_original:
            return patched_by_original[path]
        content = file_contents.get(path)
        if content is None:
            return path
        base_dir = os.path.dirname(path)
        changed = False

        def _replace_img(m):
            nonlocal changed
            uri = png_by_file_ref.get((path, m.group(2)))
            if not uri:
                return m.group(0)
            changed = True
            return f"{m.group(1)}{uri}{m.group(3)}"

        def _replace_iframe(m):
            nonlocal changed
            src = m.group(2)
            candidate = os.path.join(base_dir, src)
            if candidate not in file_contents:
                return m.group(0)
            patched_child = _patch(candidate)
            if patched_child == candidate:
                return m.group(0)
            changed = True
            return f"{m.group(1)}{os.path.basename(patched_child)}{m.group(3)}"

        content = _IMG_SRC_RE.sub(_replace_img, content)
        content = _IFRAME_SRC_RE.sub(_replace_iframe, content)

        if not changed:
            patched_by_original[path] = path
            return path
        tag = hashlib.md5(path.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        patched_path = os.path.join(base_dir, f"office2pdf-patched-{tag}.html")
        with open(patched_path, "w", encoding="utf-8") as f:
            f.write(content)
        patched_by_original[path] = patched_path
        return patched_path

    return _patch(html_path)


def die(msg):
    print(f"office2pdf: {msg}", file=sys.stderr)
    sys.exit(1)


def find_chrome():
    for path in CHROME_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in ("google-chrome", "chromium", "chromium-browser", "brave-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def generate_ql_preview(path, tmpdir):
    """Run `qlmanage -o -p` on `path` and return (html_path, width,
    height) - width/height (logical CSS px, one page/slide's own size)
    come from the preview's own plist, or (None, None) if it didn't
    have them. Dies if qlmanage itself isn't available, or produced no
    preview at all (no Quick Look generator registered for this file
    type)."""
    if shutil.which("qlmanage") is None:
        die("qlmanage not found - this only works on macOS")

    outdir = tempfile.mkdtemp(dir=tmpdir, prefix="qlpreview-")
    try:
        subprocess.run(
            ["qlmanage", "-o", outdir, "-p", path],
            capture_output=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        die(f"qlmanage failed to run: {e}")

    bundle = os.path.join(outdir, f"{os.path.basename(path)}.qlpreview")
    html_path = os.path.join(bundle, "Preview.html")
    if not os.path.isfile(html_path):
        die(f"qlmanage produced no preview for {path!r} - no Quick Look generator for this file type?")

    width = height = None
    try:
        with open(os.path.join(bundle, "PreviewProperties.plist"), "rb") as f:
            props = plistlib.load(f)
        raw_width, raw_height = props.get("Width"), props.get("Height")
        width = round(raw_width) if raw_width else None
        height = round(raw_height) if raw_height else None
    except (OSError, ValueError):
        pass
    return html_path, width, height


def inject_page_size(html_path, width, height):
    """A scratch copy of `html_path` with an `@page` CSS rule sized to
    (width, height) - headless Chrome's CLI has no --paper-width/
    --paper-height flag of its own, so this is the only way to reach a
    custom PDF page size without going through the DevTools protocol
    (confirmed by hand; see pdfless.py's _capture_html_pdf() for the
    same technique).

    Written alongside html_path itself (same directory), not to some
    other scratch directory - html_path can still have other
    same-directory relative references (an iframe src
    rasterize_pdf_img_sources() didn't need to touch, a stylesheet,
    ...) that only keep resolving if the effective file stays put."""
    with open(html_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    style = f"<style>@page {{ size: {width}px {height}px; margin: 0; }}</style>"
    idx = content.lower().find("</head>")
    injected = content[:idx] + style + content[idx:] if idx != -1 else style + content
    out_path = os.path.join(os.path.dirname(html_path), "office2pdf-print.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(injected)
    return out_path


def print_to_pdf(chrome, html_path, output_path):
    try:
        subprocess.run(
            [
                chrome, "--headless", "--disable-gpu", "--no-sandbox",
                f"--print-to-pdf={output_path}", "--print-to-pdf-no-header",
                f"file://{os.path.abspath(html_path)}",
            ],
            capture_output=True, check=True, timeout=60,
        )
    except subprocess.CalledProcessError as e:
        die(f"chrome --print-to-pdf failed: {e.stderr.decode(errors='replace').strip()}")
    except (subprocess.TimeoutExpired, OSError) as e:
        die(f"chrome --print-to-pdf failed: {e}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="a file any of this Mac's Quick Look generators can preview")
    parser.add_argument("-o", "--output", metavar="PATH", help="output PDF path (default: <input>.pdf)")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        die(f"no such file: {args.input}")
    output_path = args.output or os.path.splitext(args.input)[0] + ".pdf"

    chrome = find_chrome()
    if chrome is None:
        die("no local Chrome/Chromium/Brave install found")

    with tempfile.TemporaryDirectory(prefix="office2pdf-") as tmpdir:
        html_path, width, height = generate_ql_preview(args.input, tmpdir)
        html_path = rasterize_pdf_img_sources(html_path, tmpdir)
        if width and height:
            html_path = inject_page_size(html_path, width, height)
        print_to_pdf(chrome, html_path, output_path)

    print(f"wrote {output_path}")


if __name__ == "__main__":
    main()
