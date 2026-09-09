#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pillow",
# ]
# ///
"""pdfless - a less(1)-like full-screen PDF pager for terminals that
support iTerm2's inline image protocol (iTerm2 itself, WezTerm, ...).

Each page is rasterized once (via poppler's pdftoppm) at a resolution
matched to the terminal's actual pixel width, then scrolled by cropping
that raster in memory and redrawing the screen - the same "redraw on
each keypress" approach less(1) uses internally, since a terminal has
no way to scroll just part of an inline image.
"""

import argparse
import base64
import fcntl
import html
import io
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unicodedata
from collections import OrderedDict

from PIL import Image

__version__ = "1.0.0"

STATUS_COLOR_ON = "\x1b[44;97m"  # white on blue - used for one-off messages
STATUS_COLOR_OFF = "\x1b[0m"

# The default status line is split into differently-colored fields so
# filename/page/loc%/zoom% each stand out, with the trailing key-hints
# text in a plainer, subdued color.
STATUS_COLOR_FILENAME = "\x1b[44;97m"  # white on blue
STATUS_COLOR_PAGE = "\x1b[42;30m"  # black on green
STATUS_COLOR_LOC = "\x1b[43;30m"  # black on yellow
STATUS_COLOR_ZOOM = "\x1b[45;97m"  # white on magenta
STATUS_COLOR_HELP = "\x1b[100;37m"  # light grey on dark grey

# Text-mode search match: no image to draw a box marker over there, so
# the matched substring itself is highlighted with a background color.
TEXT_HIGHLIGHT_COLOR = "\x1b[43;30m"  # black on yellow
TEXT_HIGHLIGHT_RESET = "\x1b[0m"

# PDF (image) mode search match: same yellow, as a foreground color for
# the box-drawing border characters (there's no text to paint a
# background behind, just the underlying page image).
SEARCH_MARKER_COLOR = "\x1b[93m"  # bright yellow
SEARCH_MARKER_RESET = "\x1b[0m"
CACHE_SIZE = 6


def char_width(ch):
    """Terminal column width of one character: 2 for wide/fullwidth East
    Asian characters (e.g. most Japanese/Chinese/Korean text), 1 otherwise.
    Needed because the status line is truncated/padded to fit exactly
    self.cols columns - doing that by Python string length (len()) rather
    than actual terminal column width overshoots whenever the text
    contains such characters, since each one is 1 Python character but 2
    terminal columns; that overshoot pushes the write past the last
    column of the last row, and autowrap then scrolls the whole screen up
    a line."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s):
    return sum(char_width(c) for c in s)


def truncate_to_width(s, width):
    """Truncate `s` so its terminal column width doesn't exceed `width`."""
    out = []
    total = 0
    for ch in s:
        w = char_width(ch)
        if total + w > width:
            break
        out.append(ch)
        total += w
    return "".join(out)


def pad_to_width(s, width):
    return s + " " * max(0, width - display_width(s))


def slice_by_width(s, offset, width):
    """Extract the substring of `s` covering terminal columns
    [offset, offset+width) - the text-mode equivalent of cropping a
    pixel range out of the page image for horizontal pan. A wide
    character straddling either edge of that range can't be rendered
    half-visible, so it's dropped rather than included.

    Returns (substring, start_index): start_index is the character index
    into `s` the substring begins at, needed to re-base any offsets
    (e.g. a search highlight) that were computed against the original,
    unpanned line."""
    out = []
    col = 0
    taken = 0
    start_index = None
    for i, ch in enumerate(s):
        w = char_width(ch)
        if col < offset:
            # Starts before the pan offset - skip it, whether or not it
            # also straddles into [offset, ...): either way it can't be
            # shown intact starting exactly at `offset`.
            col += w
            continue
        if start_index is None:
            start_index = i
        if taken + w > width:
            break
        out.append(ch)
        taken += w
        col += w
    if start_index is None:
        start_index = len(s)
    return "".join(out), start_index


MIN_ZOOM = 0.5
MAX_ZOOM = 4.0
ZOOM_STEP = 1.15
PAN_STEP_CELLS = 8
FOLLOW_INTERVAL = 3.0  # seconds between checks, under -F/--follow

KEY_TABLE = """\
Keys (mirroring less(1)):
  e ^E j ^N CR DOWN forward  one line
  y ^Y k ^K ^P UP   backward one line
  f ^F ^V SPACE     forward  one window
  b ^B ESC-v        backward one window
  d ^D              forward  half window
  u ^U              backward half window
  g / G / HOME / END      jump to first / last page of the document
  <N> g                   jump straight to page N
  n / p                   next / previous page
  PAGEUP / PAGEDOWN       backward / forward one window
  + / -                   zoom in / out
  0                       reset zoom and pan
  m / M                   fit page to terminal height / width
  h / l / LEFT / RIGHT    pan left / right (when zoomed in)
  H / L / SHIFT-LEFT/RIGHT   jump to left / right edge (when zoomed in)
  K / U / SHIFT-UP        jump to top of the current page
  J / D / SHIFT-DOWN      jump to bottom of the current page
  t                       toggle plain-text view of the current page
                          (page/line navigation and h/l/H/L pan for
                          lines wider than the terminal - no zoom/fit)
  f                       (text mode) toggle a border around the page's
                          edges - on by default (--no-frame to start
                          with it off; it can get swept up when you
                          select-and-copy the text). Scroll/pan
                          (h/l/H/L, j/k, ...) to bring it into view if
                          the terminal is too small to show it already
  /<regex> ENTER          search the whole document for <regex>
                          (a Python regex; falls back to a literal
                          substring if it isn't valid regex syntax)
  N / P                   jump to next / previous search match
  ^L                      redraw the screen
  ?                       show this help (q to close it)
  q                       quit\
"""

FORWARD_LINE_KEYS = {"e", "\x05", "j", "\x0e", "\r", "DOWN"}
BACKWARD_LINE_KEYS = {"y", "\x19", "k", "\x0b", "\x10", "UP"}
FORWARD_WINDOW_KEYS = {"f", "\x06", "\x16", " ", "PAGEDOWN"}
BACKWARD_WINDOW_KEYS = {"b", "\x02", "ESC-v", "PAGEUP"}


def die(msg):
    print(f"pdfless: {msg}", file=sys.stderr)
    sys.exit(1)


def check_deps():
    for tool in ("pdftoppm", "pdfinfo"):
        if shutil.which(tool) is None:
            die(f"requires poppler's '{tool}' (brew install poppler)")


def pdf_page_count(pdf_path):
    out = subprocess.run(
        ["pdfinfo", pdf_path], capture_output=True, text=True, check=True
    ).stdout
    m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
    if not m:
        die("could not determine page count")
    return int(m.group(1))


def pdf_page_size_pt(pdf_path, page):
    """Return (width_pt, height_pt) for the given page."""
    out = subprocess.run(
        ["pdfinfo", "-f", str(page), "-l", str(page), pdf_path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    m = re.search(r"^Page\s*(?:\d+\s+)?size:\s+([\d.]+) x ([\d.]+)", out, re.MULTILINE)
    if not m:
        die(f"could not determine page size for page {page}")
    return float(m.group(1)), float(m.group(2))


def extract_page_text(pdf_path, page):
    """Plain-text rendering of one page, via poppler's pdftotext -layout
    (which tries to preserve the page's visual line/column layout, unlike
    the flat word-run text used for search)."""
    out = subprocess.run(
        ["pdftotext", "-f", str(page), "-l", str(page), "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


_BBOX_PAGE_RE = re.compile(
    r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S
)
_BBOX_WORD_RE = re.compile(
    r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>'
)


def build_search_index(pdf_path):
    """Extract per-page text and word positions (via poppler's pdftotext
    -bbox) for searching. Returns a list, one entry per page, each
    {"width_pt": float, "height_pt": float, "text": str,
    "words": [(start, end, xMin, yMin, xMax, yMax), ...]} (all in points)
    where (start, end) are offsets into "text" for that word."""
    out = subprocess.run(
        ["pdftotext", "-bbox", pdf_path, "-"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    pages = []
    for width, height, body in _BBOX_PAGE_RE.findall(out):
        words = []
        parts = []
        offset = 0
        for xmin, ymin, xmax, ymax, word_html in _BBOX_WORD_RE.findall(body):
            word_text = html.unescape(word_html)
            if not word_text:
                continue
            start = offset
            parts.append(word_text)
            offset += len(word_text)
            words.append(
                (start, offset, float(xmin), float(ymin), float(xmax), float(ymax))
            )
            parts.append(" ")
            offset += 1
        pages.append(
            {
                "width_pt": float(width),
                "height_pt": float(height),
                "text": "".join(parts),
                "words": words,
            }
        )
    return pages


def compile_search_pattern(query):
    """Compile `query` as a case-insensitive regex. If it isn't valid
    regex syntax (e.g. a literal query like "C++" - "+" repeating nothing
    is a regex error), fall back to matching it literally instead of
    just failing the search."""
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(query), re.IGNORECASE)


def find_search_matches(index, query):
    """Return every match of `query` (a case-insensitive regex, or a
    literal substring if it isn't valid regex syntax) across the whole
    document, as a list of (page_number, xMin, yMin, xMax, yMax) bounding
    boxes (in points, the union of every word the match touches), in
    reading order."""
    pattern = compile_search_pattern(query)
    matches = []
    for page_num, page in enumerate(index, start=1):
        for m in pattern.finditer(page["text"]):
            pos, match_end = m.start(), m.end()
            if pos == match_end:
                continue  # skip zero-width matches (e.g. a pattern like "x*")
            box = None
            for word_start, word_end, xmin, ymin, xmax, ymax in page["words"]:
                if word_start < match_end and word_end > pos:
                    # poppler often lumps a whole run of CJK text (with no
                    # spaces to split on) into a single <word>, sometimes
                    # spanning most of a line. Highlighting that whole word
                    # would hugely overstate the match, so narrow the box
                    # to just the matched characters' share of it, assuming
                    # roughly uniform character width left-to-right.
                    word_len = word_end - word_start
                    local_start = max(pos, word_start) - word_start
                    local_end = min(match_end, word_end) - word_start
                    frac_start = local_start / word_len if word_len else 0.0
                    frac_end = local_end / word_len if word_len else 1.0
                    sub_xmin = xmin + frac_start * (xmax - xmin)
                    sub_xmax = xmin + frac_end * (xmax - xmin)
                    if box is None:
                        box = [sub_xmin, ymin, sub_xmax, ymax]
                    else:
                        box[0] = min(box[0], sub_xmin)
                        box[1] = min(box[1], ymin)
                        box[2] = max(box[2], sub_xmax)
                        box[3] = max(box[3], ymax)
            if box is not None:
                matches.append((page_num, box[0], box[1], box[2], box[3]))
    return matches


class RawTerminal:
    """Puts the tty into raw (cbreak-ish) mode for the duration of the block."""

    def __init__(self, fd):
        self.fd = fd
        self.old = None

    def __enter__(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def read_utf8_char(fd, timeout=0.1):
    """Read one full UTF-8 character (1-4 bytes) from fd, returning it
    decoded as a str, or None on EOF. A single-byte os.read() at a time
    would mangle multi-byte characters (e.g. Japanese search queries),
    since each individual byte of a multi-byte sequence is not valid
    ASCII/UTF-8 on its own."""
    first = os.read(fd, 1)
    if not first:
        return None
    lead = first[0]
    if lead & 0x80 == 0:
        length = 1
    elif lead & 0xE0 == 0xC0:
        length = 2
    elif lead & 0xF0 == 0xE0:
        length = 3
    elif lead & 0xF8 == 0xF0:
        length = 4
    else:
        length = 1  # a stray continuation byte; decode it on its own below

    buf = first
    while len(buf) < length:
        r, _, _ = select.select([fd], [], [], timeout)
        if not r:
            break
        more = os.read(fd, 1)
        if not more:
            break
        buf += more
    return buf.decode("utf-8", errors="replace")


CSI_FINAL_LETTERS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT", "H": "HOME", "F": "END"}
CSI_TILDE_CODES = {
    "1": "HOME", "7": "HOME",
    "4": "END", "8": "END",
    "5": "PAGEUP",
    "6": "PAGEDOWN",
}


def read_csi_sequence(fd, timeout=0.15):
    """Read the rest of a CSI sequence (after ESC [) up to and including its
    final byte (0x40-0x7E). Returns the sequence as a string, or None on
    timeout/EOF."""
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            return None
        b = os.read(fd, 1)
        if not b:
            return None
        buf += b
        if 0x40 <= b[0] <= 0x7E:
            return buf.decode("ascii", errors="ignore")


def decode_csi_key(seq):
    """Turn a CSI sequence body like "A", "1;2A" or "5~" into a symbolic
    key name such as "UP" or "SHIFT-LEFT", or None if unrecognized."""
    if not seq:
        return None
    final = seq[-1]
    params = seq[:-1].split(";") if seq[:-1] else [""]
    modifier = params[1] if len(params) > 1 else "1"

    if final == "~":
        name = CSI_TILDE_CODES.get(params[0])
    else:
        name = CSI_FINAL_LETTERS.get(final)

    if name is None:
        return None
    return f"SHIFT-{name}" if modifier == "2" else name


def get_term_cells(fd):
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
    return rows, cols, xpix, ypix


def query_pixel_size_osc(fd, timeout=0.5):
    """Ask the terminal for its text-area size in pixels via CSI 14t
    (an iTerm2/xterm extension). Returns (width_px, height_px) or None."""
    os.write(fd, b"\x1b[14t")
    pattern = re.compile(rb"\x1b\[4;(\d+);(\d+)t")
    buf = b""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            return None
        chunk = os.read(fd, 64)
        if not chunk:
            return None
        buf += chunk
        m = pattern.search(buf)
        if m:
            height, width = int(m.group(1)), int(m.group(2))
            return width, height


_warned_fallback = False


def get_pixel_size(fd):
    """Best-effort terminal text-area size in pixels: (width_px, height_px)."""
    global _warned_fallback
    rows, cols, xpix, ypix = get_term_cells(fd)
    if xpix and ypix:
        return xpix, ypix

    got = query_pixel_size_osc(fd)
    if got is not None:
        return got

    if not _warned_fallback:
        print(
            "pdfless: could not determine terminal pixel size; falling back "
            "to a rough estimate (image sharpness/fit may be off)",
            file=sys.stderr,
        )
        _warned_fallback = True

    # Last resort: guess a plausible cell size.
    guess_w, guess_h = 8, 17
    return cols * guess_w, rows * guess_h


def wrap_for_tmux(osc):
    if not os.environ.get("TMUX"):
        return osc
    escaped = osc.replace("\x1b", "\x1b\x1b")
    return f"\x1bPtmux;{escaped}\x1b\\"


class PageCache:
    def __init__(self, pdf_path, tmpdir, size=CACHE_SIZE):
        self.pdf_path = pdf_path
        self.tmpdir = tmpdir
        self.size = size
        self._cache = OrderedDict()  # (page, dpi_rounded) -> PIL.Image

    def clear(self):
        self._cache.clear()

    def get(self, page, target_px, fit="width"):
        """Rasterize `page` at whatever DPI makes it `target_px` wide
        (fit="width") or tall (fit="height")."""
        width_pt, height_pt = pdf_page_size_pt(self.pdf_path, page)
        page_pt = width_pt if fit == "width" else height_pt
        dpi = 72.0 * target_px / page_pt
        key = (page, round(dpi))

        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        prefix = os.path.join(self.tmpdir, f"page-{page}-{round(dpi)}")
        subprocess.run(
            [
                "pdftoppm", "-png", "-r", str(dpi),
                "-f", str(page), "-l", str(page),
                "-singlefile", self.pdf_path, prefix,
            ],
            check=True,
        )
        img = Image.open(prefix + ".png")
        img.load()
        os.unlink(prefix + ".png")

        self._cache[key] = img
        if len(self._cache) > self.size:
            self._cache.popitem(last=False)
        return img


class Viewer:
    def __init__(self, pdf_path, npages, page, tmpdir, fd, fit="width", frame=True):
        self.pdf_path = pdf_path
        self.pdf_name = os.path.basename(pdf_path)
        self.npages = npages
        self.page = page
        self.fd = fd
        self.fit = fit
        self.cache = PageCache(pdf_path, tmpdir)
        self.scroll = 0
        self.zoom = 1.0
        self.resized = True
        self.img = None
        self.avail_height_px = 0
        self.cell_h_px = 1
        self.cell_w_px = 1
        self.rows = 0
        self.cols = 0
        self.crop_width = 0
        self.x_offset = 0
        self.help_active = False
        self._search_index = None  # lazily built, via build_search_index()
        self.search_query = None
        self.search_matches = []
        self.search_pos = None
        self.text_mode = False
        self.text_lines = []
        self.text_scroll = 0
        self.text_scroll_min = 0
        self.text_scroll_max = 0
        self.text_x_offset = 0
        self.text_x_offset_min = 0
        self.text_x_offset_max = 0
        self.text_max_line_width = 0
        self.text_frame = frame  # border around the page's edges, in
        # text mode; can be swept up along with the text if you
        # select-and-copy it, so it's toggled off with --no-frame or
        # the f key

    def request_resize(self):
        self.resized = True
        # The help box's size/position and the underlying page raster are
        # both stale after a resize; simplest is to just drop back to the
        # normal view, which always does a full redraw at the new size.
        self.help_active = False

    def _recompute_geometry(self):
        rows, cols, _, _ = get_term_cells(self.fd)
        width_px, height_px = get_pixel_size(self.fd)
        cell_h = max(1, height_px // max(1, rows))
        cell_w = max(1, width_px // max(1, cols))
        self.rows = rows
        self.cols = cols
        self.base_width_px = width_px
        self.cell_h_px = cell_h
        self.cell_w_px = cell_w
        self.avail_height_px = cell_h * max(1, rows - 1)
        self.cache.clear()

    def _load_page(self):
        if self.fit == "height":
            target_height = max(1, round(self.avail_height_px * self.zoom))
            self.img = self.cache.get(self.page, target_height, fit="height")
        else:
            target_width = max(1, round(self.base_width_px * self.zoom))
            self.img = self.cache.get(self.page, target_width, fit="width")
        self.crop_width = min(self.img.width, self.base_width_px)
        self.x_offset = max(0, (self.img.width - self.crop_width) // 2)
        self.scroll_max = max(0, self.img.height - self.avail_height_px)
        self.scroll = min(self.scroll, self.scroll_max)

    def set_zoom(self, new_zoom):
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, new_zoom))
        if new_zoom == self.zoom:
            return
        self.zoom = new_zoom
        self._load_page()

    def reset_view(self):
        self.zoom = 1.0
        self._load_page()

    def set_fit(self, fit):
        self.fit = fit
        self.zoom = 1.0
        self.scroll = 0
        self._load_page()
        self.x_offset = 0

    def pan(self, dx):
        max_offset = max(0, self.img.width - self.crop_width)
        self.x_offset = max(0, min(max_offset, self.x_offset + dx))

    def refresh(self):
        if self.resized:
            self._recompute_geometry()
            self.resized = False
            if self.text_mode:
                self._clamp_text_scroll()
            else:
                self._load_page()
        if self.help_active:
            self._draw_help()
        elif self.text_mode:
            self._draw_text()
        else:
            self._draw()

    def show_help(self):
        self.help_active = True
        self._draw_help()

    def hide_help(self):
        self.help_active = False
        self.refresh()

    def enter_text_mode(self):
        self.text_mode = True
        self._load_text_page()
        # If there's a search match highlighted/boxed on this same page,
        # follow it across into text mode too, scrolled into view.
        match = self._active_search_page_match()
        if match:
            self._scroll_text_to_match(match)
        self.refresh()

    def exit_text_mode(self):
        self.text_mode = False
        # self.page may have moved while browsing in text mode (n/p, g/G,
        # <N>g all update it), but self.img was never touched during that
        # - refresh() only reloads it on a resize - so without this it'd
        # redraw whatever page/scroll was last loaded before entering text
        # mode instead of following you back to where you navigated to.
        self.scroll = 0
        self._load_page()
        # Symmetric with enter_text_mode(): carry a highlighted match back
        # into the box marker on the rendered page.
        match = self._active_search_page_match()
        if match:
            self._scroll_image_to_match(match)
        self.refresh()

    def toggle_text_mode(self):
        if self.text_mode:
            self.exit_text_mode()
        else:
            self.enter_text_mode()

    def _load_text_page(self):
        self.text_lines = extract_page_text(self.pdf_path, self.page)
        self.text_scroll = 0
        self.text_x_offset = 0
        self._clamp_text_scroll()
        if self.text_frame:
            # Default to showing the new page's top-left corner - and so
            # its border, since that's otherwise off past the default
            # (0, 0) position. go_to_page_text() below still overrides
            # this for scroll=None (continuous backward scroll wants the
            # bottom of the page instead).
            self.text_scroll = self.text_scroll_min
            self.text_x_offset = self.text_x_offset_min

    def _text_avail_rows(self):
        return max(1, self.rows - 1)  # bottom row is the status bar

    def _text_avail_cols(self):
        return self.cols

    def toggle_text_frame(self):
        self.text_frame = not self.text_frame
        self._clamp_text_scroll()

    def _clamp_text_scroll(self):
        # The border sits at the page's actual edges - one row above the
        # first line, one below the last; one column left of column 0,
        # one right of the widest line - which is usually off-screen at
        # the default scroll/pan position. It only comes into view by
        # scrolling/panning one step past the content itself, so with the
        # frame on, the scroll/pan range is widened by exactly that much;
        # with it off, the range is exactly what it was before this
        # feature existed.
        avail_rows = self._text_avail_rows()
        if self.text_frame:
            self.text_scroll_min = -1
            self.text_scroll_max = max(-1, len(self.text_lines) - avail_rows + 1)
        else:
            self.text_scroll_min = 0
            self.text_scroll_max = max(0, len(self.text_lines) - avail_rows)
        self.text_scroll = max(
            self.text_scroll_min, min(self.text_scroll, self.text_scroll_max)
        )

        avail_cols = self._text_avail_cols()
        self.text_max_line_width = max(
            (display_width(l) for l in self.text_lines), default=0
        )
        if self.text_frame:
            self.text_x_offset_min = -1
            self.text_x_offset_max = max(-1, self.text_max_line_width - avail_cols + 1)
        else:
            self.text_x_offset_min = 0
            self.text_x_offset_max = max(0, self.text_max_line_width - avail_cols)
        self.text_x_offset = max(
            self.text_x_offset_min, min(self.text_x_offset, self.text_x_offset_max)
        )

    def _active_search_page_match(self):
        """The currently-selected search match (self.search_pos), but
        only if it's on the page being displayed right now - this is
        what lets the box marker (image mode) and highlight (text mode)
        follow each other across a `t` toggle: both are derived from this
        same bit of state, recomputed fresh on every draw, rather than
        each mode tracking its own separate "is a match showing" flag."""
        if self.search_pos is None:
            return None
        match = self.search_matches[self.search_pos]
        return match if match[0] == self.page else None

    def _active_search_occurrence_index(self):
        """How many other matches with the same page precede
        self.search_matches[self.search_pos] (0-indexed) - i.e. this is
        the Nth occurrence of the query on that page, in reading order.
        Used to correlate the same occurrence between the bbox-based
        document search index and the independently pdftotext -layout
        -extracted text-mode lines: when a page has the query more than
        once, matching by *position* between the two isn't reliable
        (their coordinate systems and line-splitting differ), but
        reading order should still agree between them."""
        if self.search_pos is None:
            return None
        page = self.search_matches[self.search_pos][0]
        if page != self.page:
            return None
        return sum(1 for m in self.search_matches[: self.search_pos] if m[0] == page)

    def _match_bbox_px(self, match):
        """Pixel bounding box (in the current page image) of a
        (page, xMin, yMin, xMax, yMax) search match, in points."""
        _, xmin_pt, ymin_pt, xmax_pt, ymax_pt = match
        page_info = self._search_index[self.page - 1]
        scale_x = self.img.width / page_info["width_pt"]
        scale_y = self.img.height / page_info["height_pt"]
        return (
            xmin_pt * scale_x,
            ymin_pt * scale_y,
            xmax_pt * scale_x,
            ymax_pt * scale_y,
        )

    def _find_all_text_matches(self):
        """Every occurrence of self.search_query within self.text_lines
        (the current page's pdftotext -layout text), as a list of
        (line_idx, start, end), in reading order."""
        if not self.search_query:
            return []
        pattern = compile_search_pattern(self.search_query)
        results = []
        for i, line in enumerate(self.text_lines):
            for m in pattern.finditer(line):
                if m.start() != m.end():
                    results.append((i, m.start(), m.end()))
        return results

    def _text_highlight_for_match(self, match):
        """(line_idx, start, end) of `match` within self.text_lines, or
        None if the query doesn't appear there at all (a real
        possibility, given the two extractions can differ)."""
        if match is None:
            return None
        all_matches = self._find_all_text_matches()
        if not all_matches:
            return None

        occurrence_index = self._active_search_occurrence_index()
        if occurrence_index is not None and occurrence_index < len(all_matches):
            return all_matches[occurrence_index]

        # Fall back to a proportional-position guess, for the rare case
        # where the two extractions disagree on how many times the query
        # appears on this page.
        _, _xmin_pt, ymin_pt, _xmax_pt, _ymax_pt = match
        page_info = self._search_index[self.page - 1]
        height_pt = page_info["height_pt"]
        approx_line = (
            round((ymin_pt / height_pt) * len(self.text_lines)) if height_pt else 0
        )
        return min(all_matches, key=lambda c: abs(c[0] - approx_line))

    def _scroll_image_to_match(self, match):
        """Scroll/pan the image view so `match` is visible, landing it a
        little below the top-left rather than jammed against the edge."""
        px_left, px_top, px_right, px_bottom = self._match_bbox_px(match)
        margin = self.avail_height_px // 4
        self.scroll = max(0, min(self.scroll_max, round(px_top) - margin))
        max_x_offset = max(0, self.img.width - self.crop_width)
        if px_left < self.x_offset or px_right > self.x_offset + self.crop_width:
            self.x_offset = max(
                0,
                min(max_x_offset, round((px_left + px_right) / 2 - self.crop_width / 2)),
            )

    def _scroll_text_to_match(self, match):
        """Scroll/pan the text view so `match` is visible, landing it a
        little below the top rather than jammed against the top edge -
        panning horizontally into view too, in case the terminal is too
        narrow for the line and it's off to the side of the truncated
        view (the text-mode equivalent of _scroll_image_to_match())."""
        avail_cols = self._text_avail_cols()
        highlight = self._text_highlight_for_match(match)
        if highlight:
            line_idx, start, end = highlight
            line = self.text_lines[line_idx]
            col_start = display_width(line[:start])
            col_end = display_width(line[:end])
            if col_start < self.text_x_offset or col_end > self.text_x_offset + avail_cols:
                self.text_x_offset = max(
                    self.text_x_offset_min,
                    min(
                        self.text_x_offset_max,
                        round((col_start + col_end) / 2 - avail_cols / 2),
                    ),
                )
        else:
            _, _xmin_pt, ymin_pt, _xmax_pt, _ymax_pt = match
            height_pt = self._search_index[self.page - 1]["height_pt"]
            line_idx = (
                round((ymin_pt / height_pt) * len(self.text_lines)) if height_pt else 0
            )
        avail_rows = self._text_avail_rows()
        margin = avail_rows // 4
        self.text_scroll = max(
            self.text_scroll_min, min(self.text_scroll_max, line_idx - margin)
        )

    def go_to_page_text(self, page, scroll):
        self.page = max(1, min(self.npages, page))
        self._load_text_page()  # already leaves text_scroll at text_scroll_min
        if scroll is None:
            self.text_scroll = self.text_scroll_max  # continuous scroll-up wants the bottom
        elif scroll != 0:
            # 0 means "top of page", which _load_text_page() already set
            # up (text_scroll_min, revealing the border if there is one);
            # anything else is a specific line to land on (e.g. <N>g).
            self.text_scroll = max(
                self.text_scroll_min, min(self.text_scroll_max, scroll)
            )

    def text_scroll_down(self, n):
        if self.text_scroll < self.text_scroll_max:
            self.text_scroll = min(self.text_scroll_max, self.text_scroll + n)
        elif self.page < self.npages:
            self.go_to_page_text(self.page + 1, 0)

    def text_scroll_up(self, n):
        if self.text_scroll > self.text_scroll_min:
            self.text_scroll = max(self.text_scroll_min, self.text_scroll - n)
        elif self.page > 1:
            self.go_to_page_text(self.page - 1, None)

    def _draw_text(self):
        # The frame sits at the page's own edges in this same scrollable/
        # pannable space the text lines live in - virtual row -1 (top)
        # and row len(text_lines) (bottom), virtual column -1 (left) and
        # column text_max_line_width (right) - rather than around
        # whatever happens to be on screen. So depending on text_scroll/
        # text_x_offset, any side of it may be scrolled out of view; each
        # row below independently figures out which of border/content/
        # nothing falls at its current position.
        avail_rows = self._text_avail_rows()
        avail_cols = self._text_avail_cols()
        highlight = self._text_highlight_for_match(self._active_search_page_match())
        # Reset text attributes explicitly: \x1b[2J clears the screen's
        # contents but not a still-active SGR state (e.g. a background
        # color left on by draw_search_prompt(), which doesn't reset it
        # since it's mid-edit) - without this, that stale color bleeds
        # into everything drawn here.
        out = ["\x1b[H\x1b[2J", STATUS_COLOR_OFF]

        left_col = -1 - self.text_x_offset
        right_col = self.text_max_line_width - self.text_x_offset
        left_visible = self.text_frame and 0 <= left_col < avail_cols
        right_visible = self.text_frame and 0 <= right_col < avail_cols

        for i in range(avail_rows):
            virtual_row = self.text_scroll + i
            screen_row = i + 1

            if self.text_frame and virtual_row in (-1, len(self.text_lines)):
                line_start = max(0, left_col)
                line_end = min(avail_cols - 1, right_col)
                if line_end < line_start:
                    continue  # this border edge is panned out of view
                chars = ["─"] * (line_end - line_start + 1)
                if left_visible:
                    chars[0] = "┌" if virtual_row == -1 else "└"
                if right_visible:
                    chars[-1] = "┐" if virtual_row == -1 else "┘"
                out.append(f"\x1b[{screen_row};{line_start + 1}H{''.join(chars)}")
                continue

            if not (0 <= virtual_row < len(self.text_lines)):
                continue  # above/below the frame entirely - nothing there

            line = self.text_lines[virtual_row]
            content_start = (left_col + 1) if left_visible else 0
            content_end = right_col if right_visible else avail_cols
            content_width = max(0, content_end - content_start)
            rendered, base = slice_by_width(
                line, max(0, self.text_x_offset), content_width
            )
            if right_visible:
                # Without this, the right border would sit right after
                # each line's own (usually shorter) content instead of
                # lined up straight at the page's actual right edge -
                # padded here, ahead of splicing in highlight color
                # codes below, so display_width() isn't thrown off by
                # those.
                rendered = pad_to_width(rendered, content_width)

            if highlight and highlight[0] == virtual_row:
                # start/end are character offsets into the original,
                # unpanned `line`; `rendered` starts partway through it
                # (at character index `base`, i.e. wherever text_x_offset
                # columns in falls) once panned, so shift them into
                # rendered's own coordinates before slicing it up to
                # splice in color codes.
                _, start, end = highlight
                start, end = start - base, end - base
                start, end = max(start, 0), min(end, len(rendered))
                if start < len(rendered) and end > start:
                    rendered = (
                        rendered[:start]
                        + TEXT_HIGHLIGHT_COLOR
                        + rendered[start:end]
                        + TEXT_HIGHLIGHT_RESET
                        + rendered[end:]
                    )

            parts = []
            if left_visible:
                parts.append("│")
            parts.append(rendered)
            if right_visible:
                parts.append("│")
            start_col = (left_col if left_visible else content_start) + 1
            out.append(f"\x1b[{screen_row};{start_col}H{''.join(parts)}")

        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.draw_status()

    def _draw_help(self):
        # Overlay the help as a boxed panel centered over the page, instead
        # of clearing the screen: we only ever move the cursor and rewrite
        # the exact cells the box covers, so the PDF still showing in the
        # rest of the terminal is left untouched.
        available_rows = max(1, self.rows - 1)  # bottom row is the status bar
        lines = KEY_TABLE.splitlines()

        content_w = min(max(20, self.cols - 4), max(len(l) for l in lines))
        lines = [l[:content_w] for l in lines]
        content_h = min(max(1, available_rows - 2), len(lines))
        lines = lines[:content_h]

        box_w = content_w + 4  # border (2) + padding (2)
        box_h = content_h + 2  # top/bottom border
        row0 = max(1, (available_rows - box_h) // 2 + 1)
        col0 = max(1, (self.cols - box_w) // 2 + 1)

        out = [STATUS_COLOR_OFF, f"\x1b[{row0};{col0}H┌{'─' * (box_w - 2)}┐"]
        for i, line in enumerate(lines):
            out.append(f"\x1b[{row0 + 1 + i};{col0}H│ {line.ljust(content_w)} │")
        out.append(f"\x1b[{row0 + box_h - 1};{col0}H└{'─' * (box_w - 2)}┘")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        self.draw_status("q to close help")

    def _draw(self):
        crop_bottom = min(self.scroll + self.avail_height_px, self.img.height)
        crop = self.img.crop(
            (self.x_offset, self.scroll, self.x_offset + self.crop_width, crop_bottom)
        )

        buf = io.BytesIO()
        crop.save(buf, format="PNG", compress_level=1)
        data = buf.getvalue()
        b64 = base64.b64encode(data).decode("ascii")
        osc = (
            f"\x1b]1337;File=inline=1;size={len(data)};"
            f"width={crop.width}px;height={crop.height}px;"
            f"preserveAspectRatio=0:{b64}\x07"
        )

        out = []
        out.append("\x1b[H\x1b[2J")
        out.append(STATUS_COLOR_OFF)  # see _draw_text()'s comment on this
        out.append(wrap_for_tmux(osc))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

        match = self._active_search_page_match()
        if match:
            self._draw_match_marker(*self._match_bbox_px(match))

        self.draw_status()

    def status_segments(self):
        """The default status line, as (text, color) fields in order."""
        if self.text_mode:
            pct = (
                100
                if self.text_scroll_max == 0
                else int(100 * self.text_scroll / self.text_scroll_max)
            )
            mode_field = " text "
        else:
            pct = (
                100
                if self.scroll_max == 0
                else int(100 * self.scroll / self.scroll_max)
            )
            mode_field = f" zoom {round(self.zoom * 100)}% "
        return [
            (f" {self.pdf_name} ", STATUS_COLOR_FILENAME),
            (f" page {self.page}/{self.npages} ", STATUS_COLOR_PAGE),
            (f" {pct}% ", STATUS_COLOR_LOC),
            (mode_field, STATUS_COLOR_ZOOM),
            (" ? help ", STATUS_COLOR_HELP),
        ]

    def draw_status(self, text=None):
        if text is not None:
            status = pad_to_width(truncate_to_width(f" {text} ", self.cols), self.cols)
            sys.stdout.write(
                f"\x1b[{self.rows};1H{STATUS_COLOR_ON}\x1b[2K{status}{STATUS_COLOR_OFF}"
            )
            sys.stdout.flush()
            return

        # Truncate/pad by terminal column width, not Python string length:
        # search queries or the filename can contain wide (e.g. Japanese)
        # characters that are 1 Python character but 2 terminal columns,
        # and undercounting that would write past the last column of the
        # last row, which triggers autowrap and scrolls the whole screen
        # up a line.
        out = []
        width_used = 0
        for text_seg, color in self.status_segments():
            if width_used >= self.cols:
                break
            chunk = truncate_to_width(text_seg, self.cols - width_used)
            if not chunk:
                continue
            out.append(f"{color}{chunk}")
            width_used += display_width(chunk)
        if width_used < self.cols:
            out.append(f"{STATUS_COLOR_OFF}{' ' * (self.cols - width_used)}")

        sys.stdout.write(
            f"\x1b[{self.rows};1H\x1b[2K{''.join(out)}{STATUS_COLOR_OFF}"
        )
        sys.stdout.flush()

    def draw_search_prompt(self, buf):
        # Unlike draw_status(), this doesn't pad the line out to the full
        # terminal width: padding leaves the cursor sitting at the far
        # right edge (in autowrap's "pending wrap" state), which is past
        # where the typed text actually is. That confuses things like the
        # terminal's IME composition popup, which anchors on the cursor -
        # it ends up rendered a line below instead of right after "/query".
        # Leaving the cursor immediately after the last character keeps it
        # where it visually belongs. The line is still cleared (and thus
        # filled) with the status color first, via \x1b[2K, and the color
        # is left active (not reset) so text typed via IME composition
        # picks it up too; draw_status() resets it on the next full redraw.
        text = truncate_to_width(f"/{buf}", self.cols)
        sys.stdout.write(f"\x1b[{self.rows};1H{STATUS_COLOR_ON}\x1b[2K{text}")
        sys.stdout.flush()

    def go_page(self, page, scroll):
        self.page = max(1, min(self.npages, page))
        self._load_page()
        self.scroll = scroll

    def reload(self):
        """Re-read the PDF from disk (e.g. -F/--follow noticed it changed
        underneath us) and redraw, staying on the same page number and
        in the same mode. The rasterized-page cache and search index are
        both keyed off content that's now stale, so both get dropped;
        any in-progress search is cleared too, since its match list may
        no longer correspond to anything in the new file."""
        self.npages = pdf_page_count(self.pdf_path)
        self.page = max(1, min(self.npages, self.page))
        self.cache.clear()
        self._search_index = None
        self.clear_search()
        if self.text_mode:
            self._load_text_page()
        else:
            self._load_page()
        self.refresh()
        self.draw_status(f"reloaded (file changed) - page {self.page}/{self.npages}")

    def clear_search(self):
        self.search_query = None
        self.search_matches = []
        self.search_pos = None

    def start_search(self, query):
        if not query:
            return
        if self._search_index is None:
            self.draw_status("building search index...")
            self._search_index = build_search_index(self.pdf_path)
        self.search_query = query
        self.search_matches = find_search_matches(self._search_index, query)
        if not self.search_matches:
            self.search_pos = None
            self.draw_status(f'"{query}" not found')
            return
        # Jump to the first match at or after the current page, wrapping
        # around to the very first match if the pattern doesn't appear
        # again before the end of the document.
        idx = 0
        for i, match in enumerate(self.search_matches):
            if match[0] >= self.page:
                idx = i
                break
        self._goto_search_match(idx)

    def repeat_search(self, forward):
        if not self.search_matches:
            msg = (
                f'"{self.search_query}" not found'
                if self.search_query
                else "no previous search pattern"
            )
            self.draw_status(msg)
            return
        if self.search_pos is None:
            self.search_pos = 0
        else:
            step = 1 if forward else -1
            self.search_pos = (self.search_pos + step) % len(self.search_matches)
        self._goto_search_match(self.search_pos)

    def _goto_search_match(self, idx):
        self.search_pos = idx
        page = self.search_matches[idx][0]

        if self.text_mode:
            self.go_to_page_text(page, 0)
        else:
            self.go_page(page, 0)

        # self.page == page now, so this is the match we just landed on;
        # _draw()/_draw_text() will independently rediscover and render
        # it (marker box or text highlight) on every redraw from here on,
        # including a later `t` mode toggle - see _active_search_page_match().
        match = self._active_search_page_match()
        if self.text_mode:
            self._scroll_text_to_match(match)
        else:
            self._scroll_image_to_match(match)

        self.refresh()
        self.draw_status(
            f'"{self.search_query}" match {idx + 1}/{len(self.search_matches)} '
            f"(page {self.page})"
        )

    def _draw_match_marker(self, px_left, px_top, px_right, px_bottom):
        """Draw a box around the just-jumped-to search match, in the
        already-drawn page image, without touching any other cell (same
        non-destructive technique as _draw_help)."""
        available_rows = max(1, self.rows - 1)  # bottom row is the status bar

        col0 = (px_left - self.x_offset) // self.cell_w_px
        col1 = -(-(px_right - self.x_offset) // self.cell_w_px) - 1  # ceil - 1
        row0 = (px_top - self.scroll) // self.cell_h_px
        row1 = -(-(px_bottom - self.scroll) // self.cell_h_px) - 1

        col0, col1 = col0 - 1, col1 + 1  # border sits one cell outside the text
        row0, row1 = row0 - 1, row1 + 1

        col0, col1 = max(0, int(col0)), min(self.cols - 1, int(col1))
        row0, row1 = max(0, int(row0)), min(available_rows - 1, int(row1))
        if col0 > col1 or row0 > row1:
            return  # the match scrolled fully out of view; nothing to draw

        width = col1 - col0 + 1
        out = [SEARCH_MARKER_COLOR, f"\x1b[{row0 + 1};{col0 + 1}H┏{'━' * (width - 2)}┓"]
        for row in range(row0 + 1, row1):
            out.append(f"\x1b[{row + 1};{col0 + 1}H┃")
            out.append(f"\x1b[{row + 1};{col1 + 1}H┃")
        if row1 > row0:
            out.append(f"\x1b[{row1 + 1};{col0 + 1}H┗{'━' * (width - 2)}┛")
        out.append(SEARCH_MARKER_RESET)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def scroll_down(self, step):
        if self.scroll < self.scroll_max:
            self.scroll = min(self.scroll_max, self.scroll + step)
        elif self.page < self.npages:
            self.go_page(self.page + 1, 0)

    def scroll_up(self, step):
        if self.scroll > 0:
            self.scroll = max(0, self.scroll - step)
        elif self.page > 1:
            self.go_page(self.page - 1, None)
            self.scroll = self.scroll_max

    def handle_key_text(self, key):
        """Key handling while in text mode: page/line navigation plus
        horizontal pan (for lines too wide for the terminal) - no
        zoom/fit, since there's no image here to resize."""
        avail_rows = self._text_avail_rows()
        if key == "f":
            # Checked ahead of FORWARD_WINDOW_KEYS, which "f" is
            # otherwise also a member of: the frame is opt-in precisely
            # because its border characters would get swept up in a
            # terminal select-and-copy, so it needs its own key rather
            # than overloading one already used for something else here.
            self.toggle_text_frame()
        elif key in FORWARD_WINDOW_KEYS:
            self.text_scroll_down(avail_rows)
        elif key in BACKWARD_WINDOW_KEYS:
            self.text_scroll_up(avail_rows)
        elif key in ("d", "\x04"):
            self.text_scroll_down(avail_rows // 2)
        elif key in ("u", "\x15"):
            self.text_scroll_up(avail_rows // 2)
        elif key in FORWARD_LINE_KEYS:
            self.text_scroll_down(1)
        elif key in BACKWARD_LINE_KEYS:
            self.text_scroll_up(1)
        elif key in ("h", "LEFT"):
            self.text_x_offset = max(
                self.text_x_offset_min, self.text_x_offset - PAN_STEP_CELLS
            )
        elif key in ("l", "RIGHT"):
            self.text_x_offset = min(
                self.text_x_offset_max, self.text_x_offset + PAN_STEP_CELLS
            )
        elif key in ("H", "SHIFT-LEFT"):
            self.text_x_offset = self.text_x_offset_min
        elif key in ("L", "SHIFT-RIGHT"):
            self.text_x_offset = self.text_x_offset_max
        elif key in ("K", "U", "SHIFT-UP"):
            self.text_scroll = self.text_scroll_min
        elif key in ("J", "D", "SHIFT-DOWN"):
            self.text_scroll = self.text_scroll_max
        elif key in ("g", "HOME"):
            self.go_to_page_text(1, 0)
        elif key in ("G", "END"):
            self.go_to_page_text(self.npages, None)
        elif key == "n":
            if self.page < self.npages:
                self.go_to_page_text(self.page + 1, 0)
        elif key == "p":
            if self.page > 1:
                self.go_to_page_text(self.page - 1, 0)
        elif key == "q":
            return False
        return True

    def handle_key(self, key):
        if self.text_mode:
            return self.handle_key_text(key)
        if key in FORWARD_WINDOW_KEYS:
            self.scroll_down(self.avail_height_px)
        elif key in BACKWARD_WINDOW_KEYS:
            self.scroll_up(self.avail_height_px)
        elif key in ("d", "\x04"):
            self.scroll_down(self.avail_height_px // 2)
        elif key in ("u", "\x15"):
            self.scroll_up(self.avail_height_px // 2)
        elif key in FORWARD_LINE_KEYS:
            self.scroll_down(self.cell_h_px)
        elif key in BACKWARD_LINE_KEYS:
            self.scroll_up(self.cell_h_px)
        elif key in ("+", "="):
            self.set_zoom(self.zoom * ZOOM_STEP)
        elif key == "-":
            self.set_zoom(self.zoom / ZOOM_STEP)
        elif key == "0":
            self.reset_view()
        elif key == "m":
            self.set_fit("height")
        elif key == "M":
            self.set_fit("width")
        elif key in ("h", "LEFT"):
            self.pan(-max(1, self.cell_w_px * PAN_STEP_CELLS))
        elif key in ("l", "RIGHT"):
            self.pan(max(1, self.cell_w_px * PAN_STEP_CELLS))
        elif key in ("H", "SHIFT-LEFT"):
            self.x_offset = 0
        elif key in ("L", "SHIFT-RIGHT"):
            self.x_offset = max(0, self.img.width - self.crop_width)
        elif key in ("K", "U", "SHIFT-UP"):
            self.scroll = 0
        elif key in ("J", "D", "SHIFT-DOWN"):
            self.scroll = self.scroll_max
        elif key in ("g", "HOME"):
            self.go_page(1, 0)
        elif key in ("G", "END"):
            self.go_page(self.npages, None)
            self.scroll = self.scroll_max
        elif key == "n":
            if self.page < self.npages:
                self.go_page(self.page + 1, 0)
        elif key == "p":
            if self.page > 1:
                self.go_page(self.page - 1, 0)
        elif key == "q":
            return False
        return True


def run_viewer(pdf_path, npages, start_page, tmpdir, fd, fit="width", frame=True, follow=False):
    """Run the interactive viewer loop. Returns the Viewer instance so the
    caller can inspect its final geometry (e.g. to tidy up the screen)."""
    viewer = Viewer(pdf_path, npages, start_page, tmpdir, fd, fit=fit, frame=frame)

    def on_winch(signum, frame):
        viewer.request_resize()

    signal.signal(signal.SIGWINCH, on_winch)

    num_buf = ""
    search_buf = None  # None: not typing; otherwise the "/query" in progress

    try:
        last_mtime = os.path.getmtime(pdf_path) if follow else None
    except OSError:
        last_mtime = None
    last_follow_check = time.monotonic()

    viewer.refresh()
    while True:
        r, _, _ = select.select([fd], [], [], 0.3)

        if follow and time.monotonic() - last_follow_check >= FOLLOW_INTERVAL:
            last_follow_check = time.monotonic()
            try:
                mtime = os.path.getmtime(pdf_path)
            except OSError:
                mtime = None  # e.g. mid save-as-replace; try again next tick
            if mtime is not None and mtime != last_mtime:
                last_mtime = mtime
                try:
                    viewer.reload()
                except Exception:
                    # The file may have been mid-write when we noticed the
                    # mtime change (e.g. pdftoppm/pdfinfo saw a truncated
                    # file); keep showing the last good render and pick
                    # up the change on a later, now-complete write.
                    pass

        if viewer.resized:
            viewer.refresh()
            continue
        if not r:
            continue
        key = read_utf8_char(fd)
        if key is None:
            break
        if key == "\x1b":
            # Possibly ESC-v (Meta-v, "backward one window") or a CSI
            # sequence (arrow/Home/End/PageUp/PageDown, plain or Shift-ed).
            r2, _, _ = select.select([fd], [], [], 0.1)
            if r2:
                nxt = os.read(fd, 1)
                if nxt == b"v":
                    key = "ESC-v"
                elif nxt == b"[":
                    seq = read_csi_sequence(fd)
                    key = decode_csi_key(seq) or ""

        if search_buf is not None:
            # Typing a search pattern after "/": collect characters until
            # Enter confirms it, Esc/^C cancels, backspace edits it (or
            # also cancels, if the pattern is already empty). Every other
            # key is swallowed so it can't leak through as a page command
            # while the prompt is up.
            if key in ("\r", "\n"):
                query = search_buf
                search_buf = None
                if query:
                    viewer.start_search(query)
                else:
                    viewer.draw_status()
            elif key in ("\x1b", "\x03"):
                search_buf = None
                viewer.draw_status()
            elif key in ("\x7f", "\x08"):
                if search_buf:
                    search_buf = search_buf[:-1]
                    viewer.draw_search_prompt(search_buf)
                else:
                    search_buf = None
                    viewer.draw_status()
            elif len(key) == 1 and key.isprintable():
                search_buf += key
                viewer.draw_search_prompt(search_buf)
            continue

        if key == "\x03":
            break

        if viewer.help_active:
            # While the help screen is up, only "q" does anything: it
            # closes help and returns to the page. Everything else is
            # swallowed so page keys can't leak through underneath it.
            if key == "q":
                viewer.hide_help()
            continue

        if key == "\x0c":  # ^L: repaint the screen (e.g. after other
            # output has garbled it), without otherwise changing anything
            viewer.refresh()
            continue

        if key == "?":
            viewer.show_help()
            continue

        if key == "t":
            viewer.toggle_text_mode()
            continue

        if key == "/":
            search_buf = ""
            viewer.draw_search_prompt(search_buf)
            continue

        if key == "N":
            viewer.repeat_search(forward=True)
            continue

        if key == "P":
            viewer.repeat_search(forward=False)
            continue

        if key in ("q", "\x1b") and viewer.search_query is not None:
            # With a search active, "q"/Esc dismiss it (removing the
            # match box/highlight and its status line) rather than
            # quitting pdfless outright - quit still works normally on
            # a second press, once there's no longer a search to clear.
            viewer.clear_search()
            viewer.refresh()
            continue

        # A lone "0" (no pending page number) resets the zoom/pan instead
        # of starting a page-number entry.
        if key.isdigit() and not (key == "0" and not num_buf):
            num_buf += key
            viewer.draw_status(f"page: {num_buf}")
            continue

        if key == "g" and num_buf:
            # "<number>g" jumps straight to that page.
            if viewer.text_mode:
                viewer.go_to_page_text(int(num_buf), 0)
            else:
                viewer.go_page(int(num_buf), 0)
            num_buf = ""
            viewer.refresh()
            continue

        if num_buf:
            # Any other key cancels a pending page number.
            num_buf = ""
            viewer.draw_status()

        before = (
            viewer.page, viewer.scroll, viewer.zoom, viewer.x_offset, viewer.fit,
            viewer.text_mode, viewer.text_scroll, viewer.text_x_offset, viewer.text_frame,
        )
        if not viewer.handle_key(key):
            break
        after = (
            viewer.page, viewer.scroll, viewer.zoom, viewer.x_offset, viewer.fit,
            viewer.text_mode, viewer.text_scroll, viewer.text_x_offset, viewer.text_frame,
        )
        if after != before:
            viewer.refresh()

    return viewer


def main():
    parser = argparse.ArgumentParser(
        prog="pdfless",
        description="Display a PDF in iTerm2 or WezTerm, less(1)-style.",
        epilog=KEY_TABLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "--help", action="help", help="show this help message and exit"
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument("pdf", help="path to the PDF file")
    parser.add_argument(
        "-p", "--page", type=int, default=1, help="page to start on (default: 1)"
    )
    parser.add_argument(
        "-k", "--keep",
        action="store_true",
        help="leave the last page on screen when quitting (q or ^C) "
             "instead of restoring the terminal screen",
    )
    parser.add_argument(
        "-h", "--fit-height",
        action="store_true",
        help="fit each page to the terminal's full height instead of its "
             "full width (default: fit width)",
    )
    parser.add_argument(
        "--no-frame",
        action="store_false",
        dest="frame",
        default=True,
        help="don't draw a border around the page's edges in text mode "
             "(t); on by default, toggle any time with f",
    )
    parser.add_argument(
        "-F", "--follow",
        action="store_true",
        help="watch the PDF file and reload it if it changes on disk "
             f"(checked every {FOLLOW_INTERVAL:.0f}s), staying on the "
             "same page and in the same mode",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.pdf):
        die(f"no such file: {args.pdf}")
    check_deps()

    if not sys.stdout.isatty() or not sys.stdin.isatty():
        die("stdin/stdout must be a terminal")

    pdf_path = os.path.abspath(args.pdf)
    npages = pdf_page_count(pdf_path)
    start_page = max(1, min(npages, args.page))

    tmpdir = tempfile.mkdtemp(prefix="pdfless.")
    fd = sys.stdin.fileno()
    try:
        with RawTerminal(fd):
            sys.stdout.write("\x1b[?1049h\x1b[?25l")
            sys.stdout.flush()
            viewer = None
            try:
                fit = "height" if args.fit_height else "width"
                viewer = run_viewer(
                    pdf_path, npages, start_page, tmpdir, fd,
                    fit=fit, frame=args.frame, follow=args.follow,
                )
            finally:
                if args.keep and viewer is not None:
                    # Stay in the alternate screen buffer so the last
                    # rendered page remains visible; just clear the status
                    # line and bring the cursor back so the shell prompt
                    # lands cleanly below the image.
                    sys.stdout.write(
                        f"\x1b[{viewer.rows};1H\x1b[2K\x1b[?25h"
                    )
                else:
                    sys.stdout.write("\x1b[?25h\x1b[?1049l")
                sys.stdout.flush()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
