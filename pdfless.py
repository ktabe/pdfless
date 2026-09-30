#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "pillow",
#     "pypdf",
#     "markdown",
#     "weasyprint",
# ]
# ///
"""pdfless - a less(1)-like full-screen PDF pager for terminals that
support iTerm2's inline image protocol (iTerm2 itself, WezTerm, ...).

Each page is rasterized once (via poppler's pdftoppm) at a resolution
matched to the terminal's actual pixel width, then scrolled by cropping
that raster in memory and redrawing the screen - the same "redraw on
each keypress" approach less(1) uses internally, since a terminal has
no way to scroll just part of an inline image.

Plain images and text files are supported directly, and (macOS only,
given a local Chrome/Chromium install) anything else this Mac's Quick
Look generators can preview - Word, Excel, PowerPoint, Keynote, Pages,
... - via qlmanage + a headless-Chrome screenshot of its HTML preview.
"""

from __future__ import annotations

import argparse
import base64
import bisect
import concurrent.futures
import contextlib
import dataclasses
import fcntl
import functools
import getpass
import hashlib
import html
import io
import json
import logging
import os
import pathlib
import plistlib
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata
import webbrowser
from collections import OrderedDict, deque

from typing import Any, Callable, Iterator, List, Literal, NoReturn, Protocol, Sequence, Tuple, Union, cast

from PIL import ExifTags, Image, ImageChops, ImageOps

# pypdf (link/outline extraction) reports a malformed PDF's quirks - e.g.
# "Ignoring wrong pointing object" - through the logging module, whose
# last-resort handler prints them on stderr: straight over the page on
# screen. It recovers from those on its own, so they're only noise here.
logging.getLogger("pypdf").setLevel(logging.CRITICAL + 1)

# Type aliases for the shapes that travel between functions here. Spelled
# with typing's generics rather than `X | Y`, since these (unlike the
# annotations themselves - see `from __future__ import annotations`) are
# evaluated at import time, and requires-python is 3.9.
#
# What a render produces (see RenderedDocument._remember_pages()): a real
# PDF, as ("pdf", pdf_path, npages), or one image file per page.
RenderResult = Union[Tuple[str, str, int], List[str]]
# A search match: (line_idx, start, end) into a text-mode text_lines, or
# (page, xMin, yMin, xMax, yMax) in PDF points from a page/bbox index.
# A text match can run on past the end of its line into the next ones:
# `end` then counts on through them, one character for each line break (see
# _Search.find_all_text_matches() and _text_highlight_segments()).
TextMatch = Tuple[int, int, int]
BBoxMatch = Tuple[int, float, float, float, float]
SearchMatch = Union[TextMatch, BBoxMatch]  # _Search.matches holds one kind
# A page image as PageCache.get() is asked for it: (page, target_px, fit).
PageRequest = Tuple[int, int, str]

# A many-page/many-slide office document's full-height capture (see
# OfficeDocument._render_office_pages()) routinely exceeds Pillow's default "decompression
# bomb" pixel-count ceiling - that check exists for a program decoding
# untrusted images from elsewhere, which doesn't describe a CLI pager
# opening files the user themselves chose to view, so it's disabled here
# rather than raising some new, still-arbitrary limit.
Image.MAX_IMAGE_PIXELS = None

__version__ = "1.1.0"

STATUS_COLOR_ON = "\x1b[44;97m"  # white on blue - used for one-off messages
# Resets every SGR attribute (color, reverse video, ...) at once - how
# every colored piece of output here ends, whatever set the color.
SGR_RESET = "\x1b[0m"
# Synchronized output (DEC private mode 2026): a terminal that supports
# it (iTerm2 3.5+, WezTerm, ...) holds off repainting between these two,
# so a frame built from several steps - shift the image, then repaint
# the scrollbar over it - appears all at once instead of showing each
# step; one that doesn't simply ignores an unknown mode.
SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"

# The default status line is split into differently-colored fields so
# filename/page/loc%/zoom% each stand out, with the trailing key-hints
# text in a plainer, subdued color.
STATUS_COLOR_FILENAME = "\x1b[44;97m"  # white on blue
STATUS_COLOR_FILE_INDEX = "\x1b[46;97m"  # white on cyan
STATUS_COLOR_PAGE = "\x1b[42;97m"  # white on green
STATUS_COLOR_LOC = "\x1b[43;30m"  # black on yellow
STATUS_COLOR_ZOOM = "\x1b[45;97m"  # white on magenta
STATUS_COLOR_FOLLOW = "\x1b[41;97m"  # white on red - stands out, since it
# means pdfless is polling the disk behind your back (see -f/--follow)
STATUS_COLOR_HELP = "\x1b[100;37m"  # light grey on dark grey

# Text-mode search match: no image to draw a box marker over there, so
# the matched substring itself is highlighted with a background color -
# the amber of the image-mode box's line (see SEARCH_MARKER_COLOR, whose
# pale cream would hardly show on a light theme's background), in 24-bit
# color so no theme's palette changes it.
TEXT_HIGHLIGHT_COLOR = "\x1b[38;2;0;0;0;48;2;238;160;45m"  # black on amber

# PDF (image) mode search match: the box-drawing border's color. Each
# border character replaces a cell of the page image with a text cell,
# whose background would otherwise be the terminal's own - black on a
# dark theme, a heavy black frame around the match - so it's given a
# pale cream of its own, which sits well on a page (usually white),
# with the line itself in amber. 24-bit colors, which iTerm2 and
# WezTerm (the terminals image mode needs) both take, so no theme's
# palette changes them.
SEARCH_MARKER_COLOR = "\x1b[38;2;238;160;45;48;2;255;249;228m"  # amber on cream

# Wrap mode (_draw_text_wrapped()): marks a real newline (the last
# display row of a raw line) with U+21B5 (↵), distinct from a row that's
# just a soft-wrap continuation of the same line.
NEWLINE_MARKER = "↵"
NEWLINE_MARKER_COLOR = "\x1b[34m"  # blue
EOL_MARK = NEWLINE_MARKER_COLOR + NEWLINE_MARKER + SGR_RESET  # ready to append

# -N/--line-numbers: a right-aligned gutter at the start of each text-mode
# row (see Viewer._line_number_gutter_width()).
LINE_NUMBER_COLOR = "\x1b[90m"  # gray

# -c/--continuous: what fills the space between two stacked pages in
# the page image (and beside a page narrower than the widest one on
# screen) - a mid gray, like a PDF viewer's own continuous-scroll
# backdrop, so a page's white edge stays visible against it on either
# a dark or a light terminal theme. The text-mode equivalent is a
# separator row drawn in PAGE_SEPARATOR_COLOR (see
# Viewer._text_separator_rule()).
CONTINUOUS_GAP_COLOR = (128, 128, 128)
PAGE_SEPARATOR_COLOR = "\x1b[90m"  # gray
# The "N/M" page number within that separator row - green, the same hue
# as the status line's own page field, so it reads as the same thing.
PAGE_NUMBER_COLOR = "\x1b[1;32m"  # bold green

# The scrollbar's two kinds of cell, ready to write (see
# Viewer._scrollbar_column()), in 24-bit colors of pdfless's own rather
# than the theme's: the whole column filled - a mid-gray track, the
# thumb a light teal block in it - so it reads as one bar on a dark
# theme's background and a light one's alike.
SCROLLBAR_TRACK = "\x1b[48;2;122;124;125m \x1b[0m"  # #7a7c7d, mid gray
SCROLLBAR_THUMB = "\x1b[48;2;131;204;210m \x1b[0m"  # #83ccd2, light teal

# S/--sidebar: page thumbnails down the left edge in image mode (see
# _Sidebar). SIDEBAR_COLS columns wide, the last a separator line - it's
# only put up in a terminal at least SIDEBAR_MIN_TERM_COLS wide, so the
# page itself keeps room enough. The page being viewed is framed in the
# search marker's amber (see SEARCH_MARKER_COLOR), over the terminal's own
# background here since there's no image under the frame, and every
# thumbnail has its page number under it.
SIDEBAR_COLS = 20
SIDEBAR_MIN_TERM_COLS = 60
SIDEBAR_KEPT_THUMBNAILS = 64  # rendered ones kept, beyond those in view
SIDEBAR_FRAME_COLOR = "\x1b[38;2;238;160;45m"  # amber
# The thumbnail picked while choosing one with the keys (see
# _Sidebar.handle_key()) - the scrollbar thumb's teal.
SIDEBAR_PICK_COLOR = "\x1b[38;2;131;204;210m"  # #83ccd2
SIDEBAR_LABEL_COLOR = "\x1b[38;2;128;128;128m"  # mid gray
SIDEBAR_SEPARATOR = "\x1b[38;2;128;128;128m│\x1b[0m"
CACHE_SIZE = 6
# PageCache's memory budget, on top of CACHE_SIZE: a whole spreadsheet
# sheet on one page (see _SOFFICE_SPREADSHEET_PDF_FILTER) can rasterize
# to tens of thousands of pixels tall - over 200MB for one page at a
# 1600px-wide fit - so six of those would take well over a gigabyte.
# Past this many bytes of page images, the least recently used are
# dropped, down to PAGE_CACHE_MIN_KEEP (the page on screen and the two
# neighbors Viewer._schedule_page_prefetch() prepares).
PAGE_CACHE_MAX_BYTES = 512 * 1024 * 1024
PAGE_CACHE_MIN_KEEP = 3

# SGR mouse reporting (extended coordinates), only enabled while showing
# the page image - where a click can hit a PDF hyperlink or the
# scrollbar - never in text mode, where mouse tracking would swallow the
# terminal's own click-drag text selection. 1000 is buttons alone; 1002
# adds motion, but only while a button is held (unlike 1003, which
# reports every pointer move), which is all a scrollbar drag needs.
MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1000l\x1b[?1002l\x1b[?1006l"

# Alternate Scroll Mode: whenever the above button/motion reporting is
# NOT active (i.e. in text mode) - and only then, terminals ignore this
# while it's on - the terminal turns wheel scrolling into UP/DOWN key
# presses instead, which handle_key_text() already treats as one-line
# scrolling. Left on for the whole run: it's a no-op while MOUSE_ON is
# in effect, so it doesn't need to be toggled alongside it.
ALT_SCROLL_ON = "\x1b[?1007h"
ALT_SCROLL_OFF = "\x1b[?1007l"

# Focus reporting (xterm's FocusIn/FocusOut, CSI I / CSI O): lets a
# regained focus trigger a redraw (see FOCUS_IN handling in run_viewer()).
# This is what fixes a real bug, not just a nicety: under tmux, switching
# away from and back to the pane showing a PDF can leave it blank - its
# own screen model doesn't understand the passed-through inline image
# (see wrap_for_tmux()), so its internal redraw on a focus change repaints
# the pane from a model that never had the image in it.
#
# Only FOCUS_IN gets a redraw, not FOCUS_OUT, even though both panes
# involved in a focus change can go blank this way - confirmed by hand
# that redrawing on FOCUS_OUT actually makes things worse. OSC 1337 draws
# at "the current cursor position" with no coordinates of its own, and by
# the time a pane's process is told it just lost focus, tmux has already
# moved the terminal's one real cursor to the pane gaining focus - so
# that redraw's image lands in the *other* pane instead (as a stray,
# oddly-scaled fragment near its prompt). By the time a pane is told it
# gained focus, the cursor has already moved to it, which is why only
# that direction is safe to redraw on. A pane that just lost focus still
# goes blank in the meantime - nothing here can fix that under tmux -
# but switching back to it fixes it via its own FOCUS_IN.
#
# Left on for the whole run, on the same reasoning as ALT_SCROLL_ON
# above. Requires tmux's own "focus-events on" to reach an app running
# inside it at all - see the Caveats section in README.md.
FOCUS_ON = "\x1b[?1004h"
FOCUS_OFF = "\x1b[?1004l"


# Characters drawn with no column of their own, by general category
# (see char_width()): nonspacing and enclosing marks - the combining
# marks without a combining class too, such as Thai vowel signs,
# variation selectors (U+FE00-FE0F, and U+E0100- for the ideographic
# variants in Japanese names, "葛󠄀") and enclosing circles - and format
# characters, such as the zero width joiner. The soft hyphen (U+00AD,
# also a format character) is below char_width()'s Latin-1 cutoff, so
# it keeps the column terminals draw it in.
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})
# The vowels and final consonants of Hangul's conjoining jamo: in a
# decomposed (NFD, e.g. a macOS file name) syllable they follow its
# initial consonant, which takes the syllable's two columns alone.
_HANGUL_JAMO_JOINING = ((0x1160, 0x11FF), (0xD7B0, 0xD7FF))


# Cached: text mode asks for every character it lays out, and the
# checks below take several lookups apiece, while any text has only so
# many different characters in it.
@functools.lru_cache(maxsize=None)
def char_width(ch: str) -> int:
    """Terminal column width of one character: 0 for a combining mark,
    2 for wide/fullwidth East Asian characters (e.g. most Japanese/
    Chinese/Korean text), 1 otherwise.
    Needed because the status line is truncated/padded to fit exactly
    self.cols columns - doing that by Python string length (len()) rather
    than actual terminal column width overshoots whenever the text
    contains such characters, since each one is 1 Python character but 2
    terminal columns; that overshoot pushes the write past the last
    column of the last row, and autowrap then scrolls the whole screen up
    a line.

    A combining mark is drawn on top of the character before it, taking
    no column of its own - and has to be checked first: the combining
    (han)dakuten U+3099/U+309A of decomposed (NFD) kana, as in a macOS
    file name, are "W" to east_asian_width(), so "ク" + U+3099 ("グ")
    would otherwise count as four columns instead of two. Not every such
    mark has a combining class, though, so it goes by the general
    category too (see _ZERO_WIDTH_CATEGORIES), and a decomposed Hangul
    syllable's vowel and final consonant join its initial consonant's
    two columns (see _HANGUL_JAMO_JOINING).

    Args:
        ch: one character.

    Returns:
        Its width: 0, 1 or 2."""
    code = ord(ch)
    if code < 0x300:  # ASCII and Latin-1: no marks or wide characters there
        return 1
    if code == 0xFE0F:
        # VARIATION SELECTOR-16 asks for the emoji form of the character
        # before it - two columns, where that one (❤, ☺, ...) alone
        # takes one - so it's the extra column itself.
        return 1
    if (
        unicodedata.combining(ch)
        or unicodedata.category(ch) in _ZERO_WIDTH_CATEGORIES
        or any(lo <= code <= hi for lo, hi in _HANGUL_JAMO_JOINING)
    ):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(s: str) -> int:
    return sum(char_width(c) for c in s)


def truncate_to_width(s: str, width: int) -> str:
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


def truncate_start_to_width(s: str, width: int) -> str:
    """Fit `s` into `width` terminal columns by cutting its start rather
    than its end, marking the cut with "…" - for a path, whose end (the
    file's own name) is the part worth seeing.

    Args:
        s: the text.
        width: the columns it may take.

    Returns:
        `s` itself if it fits, otherwise "…" and as much of its end as
        fits after it.
    """
    if display_width(s) <= width:
        return s
    if width < 1:
        return ""
    tail: list[str] = []
    total = 1  # the "…"
    # From the end backwards: a combining mark (width 0) comes after its
    # base character, so it's taken first and stays with it.
    for ch in reversed(s):
        w = char_width(ch)
        if total + w > width:
            break
        tail.append(ch)
        total += w
    # Marks whose base character didn't fit go too, rather than hang
    # off the "…".
    while tail and char_width(tail[-1]) == 0:
        tail.pop()
    return "…" + "".join(reversed(tail))


def pad_to_width(s: str, width: int) -> str:
    return s + " " * max(0, width - display_width(s))


def slice_by_width(s: str, offset: int, width: int) -> tuple[str, int]:
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
FOLLOW_INTERVAL = 3.0  # seconds between checks, under -f/--follow
OUTLINE_MIN_W = 30  # the table of contents box is never narrower (in columns)

KEY_TABLE = """\
Keys:
                                <MOVING>
  e ^E j ^N CR DOWN       forward  one line
  y ^Y k ^K ^P UP         backward one line
  f ^F ^V SPACE PAGEDOWN  forward  one window
  b ^B ESC-v PAGEUP       backward one window
  d ^D                    forward  half window
  u ^U                    backward half window
  h l LEFT RIGHT          pan left / right (when zoomed in)
  H L SHIFT-LEFT/RIGHT    jump to left / right edge (when zoomed in)
  K U SHIFT-UP            jump to top of the current page (same as g)
  J D SHIFT-DOWN          jump to bottom of the current page (same as G)
  g G                     jump to top / bottom of the current page
                          (text mode: type a number first to jump to
                          that line instead, e.g. 10g -> line 10)
  n p                     next / previous page (match while searching)
  < > HOME END            jump to the first / last page of the document
                          (type a number first to jump to that page
                          instead, e.g. 10< -> page 10)
                              <SEARCHING>
  /<regex> ENTER          search the whole document for <regex>,
                          landing on the first match from here on
                          (a Python regex; falls back to a literal
                          substring if it isn't valid regex syntax)
  ?<regex> ENTER          the same search, landing on the last match
                          before here instead
  ^T (while typing)       let a regex match across line breaks (a plain
                          string always may)
  / ENTER  ? ENTER        repeat the last search pattern, forward / back
  N P                     jump to next / previous search match
                            <CHANGING FILES>
  :n :p  { }              next / previous file, when more than one was
                          given on the command line
  x X                     jump to the first / last file in the list
  <N> x                   jump straight to file N
  O                       list the files: pick one with j/k and open it
                          with ENTER
                               <ZOOMING>
  + -                     zoom in / out
  0                       reset zoom and pan
  m M                     fit page to terminal height / width
                           <MOUSE OPERATIONS>
  mouse click             (page image, not text mode) on the scrollbar,
                          jump to the position clicked; otherwise open
                          a PDF hyperlink under the pointer
  mouse wheel             scroll up / down
                                <LINKS>
  [ ]                     back / forward, through the positions internal
                          links (and o below) have jumped from (PDF only)
  o TAB                   table of contents (bookmarks, headings): pick
                          an entry with j/k and jump to it with ENTER
                               <TOGGLES>
  t                       toggle plain-text view
  T                       toggle plain-text view already cleared for
                          copying, same as t and C together
  B                       (text mode) toggle a border around the page
  W -S                    (text mode) toggle wrapping long lines
  E                       (text mode) toggle marking an end-of-line (↵)
  # -N                    (text mode) toggle a line-number gutter
  C                       (text mode) clear the way for a select-and-
                          copy: turn off the EOL markers, the border,
                          the scrollbar and the line numbers at once,
                          and put back whatever was on before on a
                          second press
  c                       toggle continuous view (pages one after
                          another, instead of one page at a time)
  r                       toggle the scrollbar
  S                       show / hide page thumbnails down the left
                          edge (image mode): click one to go to its page
  s                       move to the thumbnails: pick one with j/k, go
                          to its page with ENTER (t/T: in text mode), q
                          or s to go back to the page
  F                       toggle follow mode (auto-reload on file change)
                        <MISCELLANEOUS COMMANDS>
  v                       open the file in its own app (macOS only) and
                          switch follow mode on
  ^L                      redraw the screen
  F1 :h                   show this help (q to close it)
  q :q                    quit\
"""

FORWARD_LINE_KEYS = {"e", "\x05", "j", "\x0e", "\r", "DOWN"}
BACKWARD_LINE_KEYS = {"y", "\x19", "k", "\x0b", "\x10", "UP"}
FORWARD_WINDOW_KEYS = {"f", "\x06", "\x16", " ", "PAGEDOWN"}
BACKWARD_WINDOW_KEYS = {"b", "\x02", "ESC-v", "PAGEUP"}


def die(msg: str) -> NoReturn:
    print(f"pdfless: {msg}", file=sys.stderr)
    sys.exit(1)


def positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def _open_in_default_app(path: str) -> bool:
    """Hand `path` off to macOS's own default app for it, via `open` -
    the macOS-only half of "v" (see run_viewer()). Fire-and-forget:
    this only launches `open` itself and doesn't wait for or know
    anything about whatever app ends up handling the file. Returns
    True if that launch succeeded, False if this isn't macOS at all or
    `open` itself couldn't be started (there's no single equivalent
    command bundled with every Linux desktop the way `open` ships with
    every Mac, so this is deliberately macOS-only rather than guessing
    at xdg-open or similar)."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.Popen(
            ["open", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            # Detached from pdfless's own controlling terminal/session -
            # this is a real, independent program (the default app for
            # `path`), not a helper that should share pdfless's raw-
            # mode tty or die along with it.
        )
    except OSError:
        return False
    return True


POPPLER_INSTALL_HINT = (
    "install poppler (e.g. apt install poppler-utils, brew install poppler)"
)


def check_deps() -> None:
    for tool in ("pdftoppm", "pdfinfo"):
        if shutil.which(tool) is None:
            die(f"requires poppler's '{tool}' ({POPPLER_INSTALL_HINT})")

    r = run_subprocess(["pdftoppm", "-h"], capture_output=True, text=True)
    if "-png" not in r.stdout + r.stderr:
        die(f"pdftoppm is not poppler-compatible ({POPPLER_INSTALL_HINT})")


# Fallback path for anything that isn't a PDF, a plain image, or plain
# text (Word/Excel/PowerPoint/Keynote/Pages/... - whatever this Mac's
# installed Quick Look generators can handle): render it via
# `qlmanage -o DIR -p FILE`, which drops a DIR/FILE.qlpreview/ bundle
# containing Preview.html plus a PreviewProperties.plist ({Width,
# Height, ShouldNotScale, ...}), then rasterize that HTML with a local
# Chrome/Chromium (there's no Chrome-equivalent one-shot CLI screenshot
# for Safari, so this whole path is Chrome-only).
#
# Most documents (Word's CanHavePages, PowerPoint/Keynote's
# PageElementXPath - one flag per generator, and pptx sets neither)
# render as one continuously-flowing HTML block with no actual page
# breaks - "Height" from the plist is a single page/slide's height, and
# the tall render is sliced into that many page-sized PNGs, giving the
# same per-page n/p navigation as a real PDF. The one exception seen in
# practice is Excel, whose plist sets "ShouldNotScale": there "Height"
# is the exact total canvas size (just the active sheet - tab switching
# is JS-driven and invisible to a static screenshot), and it also pins
# its sheet-tab selector to the bottom of the viewport regardless of
# window height - which would defeat the "grow the capture until content
# stops reaching the bottom" trick used for the flowing case - so that
# case is instead captured at exactly the given size, no growing.
OFFICE_RENDER_SCALE = 1  # default device-pixel-ratio; --rendering-scale
# overrides this - higher gives more headroom to zoom in before it looks
# pixelated, at the cost of slower rendering (roughly linear in the
# resulting pixel count) for a large document

CHROME_CANDIDATES = (
    # (binary path, bundle id) - the bundle id lets find_chrome() move
    # whichever of these is the user's actual default browser to the
    # front, when it can tell.
    # Vivaldi is deliberately not included here: its headless mode
    # doesn't work (confirmed on 8.2.4133.52 - the UI process just
    # crashes with "--headless --screenshot", with or without
    # "--headless=old" too), so it would only make things worse for
    # anyone who has it set as their default browser.
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "com.google.chrome"),
    ("/Applications/Chromium.app/Contents/MacOS/Chromium", "org.chromium.chromium"),
    ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "com.brave.browser"),
)


def _default_browser_bundle_id() -> str | None:
    """The bundle identifier of the user's default web browser (e.g.
    "com.google.chrome"), read from Launch Services' own record of the
    "http" URL handler - or None if that can't be determined. Always
    lowercase (Launch Services itself is case-insensitive about these
    and stores them inconsistently - e.g. Chrome's own Info.plist says
    "com.google.Chrome" but the handler record says "com.google.chrome")."""
    plist_path = os.path.expanduser(
        "~/Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
    )
    try:
        r = run_subprocess(
            ["plutil", "-convert", "json", "-o", "-", plist_path],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    for handler in data.get("LSHandlers", []):
        if handler.get("LSHandlerURLScheme") == "http":
            role = handler.get("LSHandlerRoleAll")
            return role.lower() if role else None
    return None


def _debug_log(msg: str) -> None:
    """Print one -d/--debug line to stderr ("pdfless: [debug] <msg>") -
    the caller decides whether debug is on. end="\r\n", not the default
    "\n": this runs while the terminal's in raw mode (office rendering
    only ever happens from inside the interactive viewer - even a
    first/only file is rendered lazily, from Viewer.__init__), where a
    bare "\n" doesn't return the cursor to column 1 (that's OPOST's
    job, and raw mode turns it off) - every line after the first would
    print staggered one column further right than the last otherwise.

    Off the main thread (Viewer's background prefetch - see
    Viewer._schedule_prefetch()), the line is queued instead, for the
    main thread to print between its own screen writes
    (flush_background_debug()) - printed from another thread, it could
    land in the middle of an inline image's escape sequence and garble
    it."""
    line = f"pdfless: [debug] {msg}"
    if threading.current_thread() is not threading.main_thread():
        _BACKGROUND_DEBUG.append(line)
        return
    print(line, file=sys.stderr, end="\r\n")


# _debug_log() lines from a background thread, waiting for the main
# thread to print them (flush_background_debug()) - a deque, so the
# background thread's append() and the main thread's popleft() are each
# atomic without a lock.
_BACKGROUND_DEBUG: deque[str] = deque()


def flush_background_debug() -> None:
    """Print every queued background _debug_log() line - from the main
    thread only, between its own screen writes (see _debug_log())."""
    while _BACKGROUND_DEBUG:
        print(_BACKGROUND_DEBUG.popleft(), file=sys.stderr, end="\r\n")


class _DebugTimer:
    """Prints how long one stage of OfficeDocument._render_office_pages() took, when
    -d/--debug is on - e.g. "pdfless: [debug] slides.pptx: rendering:
    0.72s". A no-op (no timing overhead beyond one monotonic() call)
    when off."""

    def __init__(self, debug: bool, label: str) -> None:
        self.debug = debug
        self.label = label

    def __enter__(self) -> _DebugTimer:
        if self.debug:
            self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        if self.debug:
            _debug_log(f"{self.label}: {time.monotonic() - self.t0:.2f}s")


class _Progress(Protocol):
    """What a render reports its progress to - _ProgressLine (stderr) or
    _ViewerProgress (the viewer's status line) - and what _Spinner drives."""

    enabled: bool

    def update(self, text: str) -> None: ...

    def clear(self) -> None: ...

    def spin(self, label: str) -> _Spinner: ...


class _ProgressLine:
    """A single, self-overwriting status line on stderr - e.g.
    "pdfless: slides.pptx: rendering (1506x39796)..." - shown while
    OfficeDocument._render_office_pages() works through a Quick Look file, since that can
    take anywhere from under a second to tens of seconds and would
    otherwise look like pdfless had simply hung. Enabled only when
    stderr is a terminal (so a redirected/piped run doesn't get a stream
    of junk \\r-terminated lines) and -d/--debug isn't already printing
    its own, more detailed, per-stage timing lines."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._last_width = 0  # terminal columns last written, not len()
        self._lock = threading.Lock()

    def _stderr_cols(self) -> int:
        try:
            return os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            return shutil.get_terminal_size().columns

    def update(self, text: str) -> None:
        if not self.enabled:
            return
        with self._lock:
            text = truncate_to_width(f"pdfless: {display_safe(text)}", self._stderr_cols())
            pad = max(0, self._last_width - display_width(text))
            sys.stderr.write("\r" + text + " " * pad)
            sys.stderr.flush()
            self._last_width = display_width(text)

    def clear(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            # EL (\x1b[2K) clears the whole row - needed when a previous
            # update wrapped because the filename was wider than the
            # terminal (before truncation) or the spinner briefly pushed
            # the line over; a bare \\r plus len()-based spaces only
            # ever touched the last wrapped row.
            sys.stderr.write("\r\x1b[2K")
            sys.stderr.flush()
            self._last_width = 0

    def spin(self, label: str) -> _Spinner:
        """Context manager: animates a spinner in front of `label` in a
        background thread for the duration of the `with` block - for a
        stage (e.g. the actual Chrome screenshot) that can take a while
        with no intermediate progress to report, so at least something
        visibly moves instead of the line just sitting there."""
        return _Spinner(self, label)


_SPINNER_FRAMES = "|/-\\"


class _Spinner:
    def __init__(self, progress: _Progress, label: str) -> None:
        self.progress = progress
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Spinner:
        if self.progress.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        i = 0
        while not self._stop.wait(0.15 if i else 0):
            self.progress.update(f"{_SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]} {self.label}")
            i += 1

    def __exit__(self, *exc: object) -> None:
        if self._thread:
            self._stop.set()
            self._thread.join()


class _ViewerProgress:
    """Adapts OfficeDocument._render_office_pages()'s progress reporting to the Viewer's
    own status line, for a file rendered lazily while the interactive
    session is already running (opening a file for the first time, or
    switching to one with :n/:p, now that each office-kind file is only
    ever rendered once it's actually displayed - see
    Viewer._ensure_office_pages()) - a stderr line (what _ProgressLine
    uses for the equivalent one-off startup validation in main()) would
    either corrupt or hide behind the alternate screen buffer at that
    point, so this posts into the status line instead, the same as any
    other one-off status message. Off under -d/--debug, which already
    prints its own, more detailed, per-stage timing lines to stderr.

    Not used at all for the very first file under -F/--quit-if-one-
    screen, whose eventual dump-and-quit-or-interactive outcome isn't
    known until this render is done - _ensure_office_pages() uses a
    plain _ProgressLine (stderr, self-overwriting via \\r, no absolute
    cursor positioning) there instead, so a still-running conversion
    stays visible without leaving anything behind that would need
    cleaning up before a dump - see Viewer.dump_and_quit()."""

    def __init__(self, viewer: Viewer) -> None:
        self.viewer = viewer
        self.enabled = not viewer.debug

    def update(self, text: str) -> None:
        if self.enabled:
            self.viewer.draw_status(text)

    def clear(self) -> None:
        # Erase the status row outright rather than restoring the viewer's
        # normal status text (draw_status() with no argument) - that
        # needs geometry (self.scroll_max, etc.) this may run before the
        # current file's own first _load_page() has ever computed, if it's
        # the very first file opened. A real refresh() always follows
        # moments after this (from run_viewer() for the first file, or
        # from the end of go_to_file()/reload() otherwise), painting the
        # correct status right over this blank - so nothing is ever left
        # stuck looking wrong. Use EL directly, same as _ProgressLine:
        # a long filename in the progress message can wrap if it wasn't
        # truncated tightly enough, and padding the line back out to
        # self.cols afterward wouldn't touch any spill onto the row above.
        if self.enabled:
            sys.stdout.write(
                f"\x1b[{self.viewer.rows};1H\x1b[2K{SGR_RESET}\x1b[?25l"
            )
            sys.stdout.flush()

    def spin(self, label: str) -> _Spinner:
        return _Spinner(self, label)


def find_chrome() -> str | None:
    """A local Chrome/Chromium-family browser binary, for headless
    screenshotting - or None if none is installed. Prefers the user's
    default browser, when it's one of these and can be determined;
    otherwise falls back to CHROME_CANDIDATES' fixed order."""
    default_id = _default_browser_bundle_id()
    candidates: Sequence[tuple[str, str]] = CHROME_CANDIDATES
    if default_id is not None:
        candidates = sorted(candidates, key=lambda c: c[1] != default_id)
    for path, _bundle_id in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    for name in (
        # No Vivaldi here either, for the same reason as in
        # CHROME_CANDIDATES: its headless mode doesn't work.
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    ):
        found = shutil.which(name)
        if found:
            return found
    return None


SOFFICE_CANDIDATES = (
    "/opt/homebrew/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
)


def find_soffice() -> str | None:
    """A local LibreOffice `soffice` binary, for converting Word/RTF
    documents to a real PDF with higher fidelity than the qlmanage +
    Chrome --print-to-pdf pipeline (real page breaks, correctly
    rendered embedded pictures of any format, no reliance on Quick
    Look at all) - or None if it isn't installed. Optional: callers
    (see OfficeDocument._try_soffice_pages()) always fall back to the
    qlmanage/Chrome pipeline when this returns None."""
    for path in SOFFICE_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("soffice")




_IMG_SRC_RE = re.compile(r'(<img\b[^>]*\bsrc=")([^"]+)(")', re.IGNORECASE)
_IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.IGNORECASE)
_IFRAME_SRC_RE = re.compile(r'(<iframe\b[^>]*\bsrc=")([^"]+)(")', re.IGNORECASE)
_ABSOLUTE_SRC_RE = re.compile(r'^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|/)')  # a URL scheme, or an absolute path
_SRC_ATTR_RE = re.compile(r'\bsrc="([^"]+)"', re.IGNORECASE)
_WIDTH_ATTR_RE = re.compile(r'\bwidth="([\d.]+)"', re.IGNORECASE)
_HEIGHT_ATTR_RE = re.compile(r'\bheight="([\d.]+)"', re.IGNORECASE)


# A hard ceiling on either dimension of a broken embedded picture
# (PDF or TIFF), once rasterized/re-encoded - regardless of what DPI
# the math below otherwise settles on for a PDF one. A Numbers/Pages/
# Keynote sheet can embed its *entire* content (a whole spreadsheet,
# potentially thousands of points on a side) as one such "picture"
# (see _rasterize_broken_img_sources()), and poppler's pdftocairo (see
# there for why it's used instead of pdftoppm) has its own ceiling on
# the cairo surface size it'll produce - past that, it fails loudly (a
# non-zero exit, caught below) rather than silently writing something
# degenerate. This cap keeps the request comfortably clear of that
# ceiling either way.
OFFICE_EMBEDDED_IMG_MAX_PX = 6000


# pdfinfo's own output lines for the page count and a page's size -
# shared by PdfDocument and the failure-tolerant helpers below. The size
# line reads "Page size:" for a plain run and "Page    N size:" once
# -f/-l ask for specific pages, hence the optional page number.
_PDFINFO_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)
_PDFINFO_SIZE_RE = re.compile(r"^Page\s*(?:\d+\s+)?size:\s+([\d.]+) x ([\d.]+)", re.MULTILINE)


def _pdfinfo_safe(pdf_path: str) -> str | None:
    """pdfinfo's output for `pdf_path`, or None if it couldn't be run
    (timed out, or not there at all) - for the helpers below, which
    only ever read PDFs pdfless itself (or Quick Look) just produced."""
    try:
        return run_subprocess(
            ["pdfinfo", pdf_path], capture_output=True, text=True, timeout=10,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def _pdf_page_size_pt_safe(pdf_path: str) -> tuple[float, float] | None:
    """Like PdfDocument.page_size_pt(), but tolerant of failure (returns None
    rather than die()ing the whole program) - for sizing a picture
    embedded in a Quick Look preview, where a bad reading just means
    falling back to a default DPI rather than aborting entirely."""
    m = _PDFINFO_SIZE_RE.search(_pdfinfo_safe(pdf_path) or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def _pdf_page_count_safe(pdf_path: str) -> int | None:
    """Like PdfDocument._pdf_page_count(), but tolerant of failure
    (returns None rather than die()ing the whole program) - for reading
    back how many pages Chrome's --print-to-pdf produced (see
    FlowingText.build_pages()), where a bad reading just means falling
    back to the screenshot-based path rather than aborting entirely."""
    m = _PDFINFO_PAGES_RE.search(_pdfinfo_safe(pdf_path) or "")
    return int(m.group(1)) if m else None


def _pdf_render_result(out_pdf: str, ok: bool = True) -> tuple | None:
    """The ("pdf", out_pdf, npages) result a PDF-producing renderer
    (soffice, Chrome's --print-to-pdf, WeasyPrint) hands back, once it's
    written `out_pdf` - or None, deleting whatever it left behind, if it
    reported failure (`ok` False) or the file has no readable pages."""
    npages = _pdf_page_count_safe(out_pdf) if ok else None
    if not npages:
        if os.path.exists(out_pdf):
            os.unlink(out_pdf)
        return None
    return ("pdf", out_pdf, npages)


# Bumped whenever a change to how pdfless renders a document would make
# an already-cached rendering of it wrong (see _cached_render_dir()) -
# the cache only checks the source file itself for staleness, so
# without this an entry made before such a fix would keep being served
# until the file itself changed. Entries under an old version are simply
# never looked up again, and age out via OFFICE_CACHE_MAX_ENTRIES.
# 2: a multi-sheet Numbers workbook renders one page per sheet, with its
#    embedded sheet images converted (it used to be one page showing a
#    broken-image icon).
# 3: Excel/.ods render via soffice, one whole sheet per PDF page (see
#    _SOFFICE_SPREADSHEET_PDF_FILTER).
RENDER_CACHE_VERSION = 3

OFFICE_CACHE_MAX_ENTRIES = 50  # persistent rendered-pages cache (see
# _office_cache_dir()) - entries beyond this many, least-recently-used
# first (by atime - see _render_result_cached()), are pruned each time
# a new one is added.

_OFFICE_CACHE_ENABLED = True  # --no-cache flips this off for the whole
# process - read directly by _render_result_cached() rather than
# threaded as a parameter through every build_pages()/ensure_pages()
# layer in between (OfficeDocument/RtfOfficeDocument/
# _render_office_pages()/ExcelWorkbook/SlideDeck/FlowingText/
# SvgDocument/...) - the same ambient-global shape _CTRL_C_FD already
# uses for a similar problem (a deeply-nested function needing one bit
# of top-level CLI state).


def _office_cache_root() -> str:
    """Base directory for the persistent rendered-pages cache, without
    creating it - split out from _office_cache_dir() so --clear-cache
    can name the directory to remove without the side effect of
    creating it again right after.

    PDFLESS_OFFICE_CACHE_DIR, if set, names the cache directory itself
    directly - not a base to join "pdfless/rendered-pages" onto. Not a
    documented user-facing setting; it exists so the test suite can
    point every test (including one that launches a real `pdfless.py`
    subprocess - an env var, unlike a plain monkeypatch of this
    function, reaches a subprocess too) at a throwaway directory
    instead of ever touching the real one."""
    override = os.environ.get("PDFLESS_OFFICE_CACHE_DIR")
    if override:
        return override
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "pdfless", "rendered-pages")


def _office_cache_dir() -> str:
    """The persistent rendered-pages cache directory, created if it
    doesn't exist yet - unlike `tmpdir` (wiped on exit), this survives
    across separate pdfless invocations, so reopening the same Word/
    RTF/PowerPoint/Excel/Keynote/Pages/SVG file skips its (LibreOffice-
    or Chrome-driven) rendering entirely, as long as the file hasn't
    changed since (see _render_result_cached()). A function rather
    than a module-level constant so tests can monkeypatch it to a
    throwaway directory instead of touching the real one."""
    d = _office_cache_root()
    os.makedirs(d, exist_ok=True)
    return d


def _cached_render_dir(path: str, key_suffix: str = '') -> str:
    """Directory holding _render_result_cached()'s persistent copy of
    `path`'s rendered pages - a single "document.pdf" for a real-PDF
    result (LibreOffice, or Chrome's print-to-pdf via FlowingText/
    SvgDocument), or a "page-0.<ext>", "page-1.<ext>", ... sequence for
    a screenshot-sliced one (Excel/Keynote/Pages/PowerPoint-without-
    soffice, and Word's own screenshot fallback) - whichever's actually
    present is how _render_result_cached() tells the two shapes apart
    again on a hit, so no separate manifest file is needed.

    Keyed by `path`'s own absolute location alone, not its mtime/size,
    so repeatedly editing and reopening the same file reuses (and just
    refreshes) a single cache entry instead of accumulating a new one
    on every edit - see _render_result_cached() for how staleness
    against the current file is actually detected. `key_suffix`, if
    given, distinguishes multiple different possible renderings of the
    very same file: the internal continuous=True rendering (a real-PDF
    result differs - one oversized page vs. paginated normally) and
    -s/--rendering-scale (a
    screenshot-sliced result bakes in a fixed pixel resolution, unlike
    a real-PDF one, always re-rasterized on demand at whatever the
    current zoom needs) - caching one under a key the other could also
    match would silently serve the wrong rendering. RENDER_CACHE_VERSION
    is part of the key too, so a rendering fix can retire every entry
    made before it."""
    key = hashlib.sha256(
        f"{os.path.abspath(path)}{key_suffix}:v{RENDER_CACHE_VERSION}".encode()
    ).hexdigest()
    return os.path.join(_office_cache_dir(), key)


def _prune_office_cache(cache_dir: str, keep: int = OFFICE_CACHE_MAX_ENTRIES) -> None:
    """Delete the least-recently-used entries (oldest atime - bumped on
    every cache hit by _render_result_cached(), which is the only thing
    that ever touches atime here) beyond `keep` - never one this
    pdfless has used (_CACHE_ENTRIES_IN_USE), as what it shows is read
    straight from there. Each entry is itself a
    directory (see _cached_render_dir()), so removal is shutil.rmtree()
    rather than a plain unlink. Best-effort: an entry that vanishes or
    fails to stat/remove between listing and pruning (another process's
    own cache hit or prune, say) is just skipped rather than treated as
    an error."""
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    if len(names) <= keep:
        return
    entries = []
    for name in names:
        p = os.path.join(cache_dir, name)
        if p in _CACHE_ENTRIES_IN_USE:
            continue  # this session's, and read in place - see _render_result_cached()
        try:
            entries.append((os.stat(p).st_atime, p))
        except OSError:
            continue
    entries.sort()
    in_use = len(names) - len(entries)
    for _atime, p in entries[:max(0, len(entries) - max(0, keep - in_use))]:
        try:
            shutil.rmtree(p) if os.path.isdir(p) else os.unlink(p)
        except OSError:
            pass


def _render_result_cached(
    path: str, render_fn: Callable[[], RenderResult | None], key_suffix: str = '',
) -> tuple[RenderResult | None, bool]:
    """Run `render_fn()` (a zero-argument closure doing whatever actual
    rendering work - shelling out to LibreOffice, or driving Chrome
    through qlmanage/measuring/screenshotting/print-to-pdf - and
    returning either a ("pdf", pdf_path, npages) tuple or a plain list
    of page image paths, in reading order, or None on failure) only if
    there's no still-fresh persistent cache entry (see
    _office_cache_dir()) already covering `path` - "fresh" meaning the
    entry was rendered from `path` exactly as it is now: its mtime and
    size, recorded in the entry when that render started (see
    _source_stamp()). Deliberately NOT part of the cache key (see
    _cached_render_dir()), so this is what actually decides whether a
    given entry is still good, and writing a fresh render is exactly
    what makes it good again after an edit. Shared
    by every one of pdfless's own document-to-page-images renderings -
    the cache doesn't care which tool produced a given entry or which
    of the two result shapes it is, only that it's still a fresh
    rendering of `path`. `key_suffix` is passed straight through to
    _cached_render_dir() - see there.

    Bypassed entirely by --no-cache (_OFFICE_CACHE_ENABLED) - always
    calls render_fn() fresh, without reading or writing the cache at
    all.

    Returns (result, from_cache) with the same shape render_fn() itself
    returns - None (from_cache False) on whatever failure render_fn()
    returns None/falsy for."""
    if not _OFFICE_CACHE_ENABLED:
        return render_fn(), False

    entry_dir = _cached_render_dir(path, key_suffix)
    # Fresh means rendered from the file exactly as it is now - its
    # mtime (to the nanosecond) and size as recorded when that render
    # started (see _CACHE_SOURCE_STAMP). Not "the entry is newer than
    # the file": a save during a long render gave the old content a
    # newer entry, served ever after, and a copy put back with an older
    # mtime (cp -p, rsync -t, a restore) matched too.
    source = _source_stamp(path)
    if source is not None and source == _read_source_stamp(entry_dir):
        cached = _read_cached_render(entry_dir)
        if cached is not None:
            # A hit: bump atime (LRU recency for _prune_office_cache()).
            try:
                os.utime(entry_dir, (time.time(), os.path.getmtime(entry_dir)))
            except OSError:
                pass
            _CACHE_ENTRIES_IN_USE.add(entry_dir)
            return cached, True
        # Present but unusable (corrupt/incomplete, or left behind by
        # an older cache format) - fall through and re-render, which
        # overwrites it below.

    result = render_fn()
    if not result:
        return None, False
    if source is not None:
        # Stamped with the file as it was when the render started: if
        # it's been saved since, the entry is stale on the next look.
        _publish_cached_render(entry_dir, result, source)
        _CACHE_ENTRIES_IN_USE.add(entry_dir)
        _prune_office_cache(os.path.dirname(entry_dir))
    return result, False


# Written into every cache entry (see _publish_cached_render()): the
# source file's "<mtime in ns> <size>" when its render started.
_CACHE_SOURCE_STAMP = "source"

# Cache entries this pdfless has used - a render shown from one is read
# in place (see _read_cached_render()), and a file visited earlier can be
# gone back to - so _prune_office_cache() leaves them be.
_CACHE_ENTRIES_IN_USE: set[str] = set()


def _source_stamp(path: str) -> str | None:
    """`path`'s mtime (in ns) and size, as a cache entry records them (see
    _render_result_cached()) - None if it can't be read."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_mtime_ns} {st.st_size}"


def _read_source_stamp(entry_dir: str) -> str | None:
    """The source stamp a cache entry was written with, or None (none
    yet, or an entry from before they were recorded - stale either way)."""
    try:
        with open(os.path.join(entry_dir, _CACHE_SOURCE_STAMP), "r", encoding="ascii") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _read_cached_render(entry_dir: str) -> RenderResult | None:
    """entry_dir's cached result, re-derived from whatever's actually on
    disk there (see _cached_render_dir()) - a ("pdf", pdf_path, npages)
    tuple if it holds a document.pdf (npages read fresh via
    _pdf_page_count_safe(), rather than also stored, so a cache entry
    is just the rendered files themselves, nothing more), or a sorted
    list of its "page-N.<ext>" paths otherwise. None if neither is
    present/usable at all."""
    pdf_path = os.path.join(entry_dir, "document.pdf")
    if os.path.isfile(pdf_path):
        npages = _pdf_page_count_safe(pdf_path)
        return ("pdf", pdf_path, npages) if npages else None
    try:
        names = [n for n in os.listdir(entry_dir) if n.startswith("page-")]
        names.sort(key=lambda n: int(n.split("-", 1)[1].split(".", 1)[0]))
    except OSError:
        return None
    return [os.path.join(entry_dir, n) for n in names] if names else None


def _publish_cached_render(entry_dir: str, result: RenderResult, source: str) -> None:
    """Copy `result` (render_fn()'s return value - see
    _render_result_cached()) into entry_dir, atomically: built up in a
    temp directory alongside it (same filesystem as entry_dir, unlike
    wherever the actual render output landed - typically `tmpdir` - so
    the os.replace() below can't hit a cross-device error) and only
    then moved into place with one directory rename, so no reader ever
    sees a partially-written cache entry. Best-effort: any OSError here
    just leaves the cache without this entry, since the render itself
    already succeeded and that's what actually matters to the caller.
    `source` is the source file's stamp (see _source_stamp()), written
    alongside."""
    tmp_dir = entry_dir + f".tmp{os.getpid()}"
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        with open(os.path.join(tmp_dir, _CACHE_SOURCE_STAMP), "w", encoding="ascii") as f:
            f.write(source)
        if isinstance(result, tuple):  # ("pdf", pdf_path, npages) - see RenderResult
            shutil.copyfile(result[1], os.path.join(tmp_dir, "document.pdf"))
        else:
            for i, p in enumerate(result):
                shutil.copyfile(p, os.path.join(tmp_dir, f"page-{i}{os.path.splitext(p)[1]}"))
        if os.path.isdir(entry_dir):
            shutil.rmtree(entry_dir, ignore_errors=True)
        elif os.path.exists(entry_dir):
            os.unlink(entry_dir)
        os.replace(tmp_dir, entry_dir)
    except OSError:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# Spreadsheets soffice converts with Calc's SinglePageSheets PDF export
# option (see _convert_via_soffice()): each sheet becomes exactly one PDF
# page, sized to fit the whole sheet, instead of being paginated by its
# print area/page setup - which, for a workbook never tuned for
# printing, cuts one sheet into many oddly-split pages (confirmed by
# hand: a real 13-sheet workbook came out as 224 pages without it, 13
# with it). See https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html
_SOFFICE_SPREADSHEET_EXTENSIONS = (".xls", ".xlsx", ".xlsm", ".ods")
_SOFFICE_SPREADSHEET_PDF_FILTER = (
    'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
)
# A whole sheet on one page can take much longer to lay out than a
# document's pages do (confirmed by hand: a real 18-sheet workbook took
# 43s), so spreadsheets get more time before giving up.
_SOFFICE_SPREADSHEET_TIMEOUT = 180


# Held while soffice runs - see _convert_via_soffice(): one profile,
# one conversion at a time (a background prefetch and a jump to another
# document can otherwise start two).
_SOFFICE_LOCK = threading.Lock()


def _convert_via_soffice(
    soffice: str, path: str, tmpdir: str, timeout: int = 60, debug: bool = False,
) -> str | None:
    """Convert `path` (a Word/RTF/PowerPoint/Excel document - see
    OfficeDocument._SOFFICE_EXTENSIONS - or a SofficeOnlyDocument) to a
    real PDF via LibreOffice's
    `soffice --convert-to pdf`, natively - no Quick Look/Chrome
    involved at all - returning the output PDF's path, or None on any
    failure (timeout, non-zero exit, or no output file), so callers
    (see OfficeDocument._try_soffice_pages()) can always fall back to
    the qlmanage/Chrome pipeline.

    Deliberately does NOT pass --headless: confirmed by hand that
    --headless breaks CJK (Japanese) font rendering entirely (text
    comes out blank, though the PDF's own text layer is fine - a pure
    glyph-resolution bug), while running without it renders correctly,
    including over SSH.

    -env:UserInstallation points at a profile directory of pdfless's own
    in `tmpdir` - kept for the session, as making one takes soffice a
    few seconds - and conversions take turns with it (see _SOFFICE_LOCK):
    a second soffice on a profile in use just hands its request to the
    first and produces nothing. Each conversion writes into a directory
    of its own, so two documents with the same name (report.docx and
    report.pptx, or a/x.docx and b/x.docx) can't overwrite, or be
    mistaken for, each other's PDF.

    A spreadsheet (_SOFFICE_SPREADSHEET_EXTENSIONS) is exported one page
    per sheet (see _SOFFICE_SPREADSHEET_PDF_FILTER), with at least
    _SOFFICE_SPREADSHEET_TIMEOUT seconds to finish.

    With debug=True (-d/--debug), a failure to actually produce a PDF
    is reported to stderr - see below for why that's not rare (soffice
    routinely exits 0 without one)."""
    profile_dir = os.path.join(tmpdir, "soffice-profile")
    outdir = tempfile.mkdtemp(prefix="soffice-out-", dir=tmpdir)
    name = os.path.basename(path)
    convert_to = "pdf"
    if path.lower().endswith(_SOFFICE_SPREADSHEET_EXTENSIONS):
        convert_to = _SOFFICE_SPREADSHEET_PDF_FILTER
        timeout = max(timeout, _SOFFICE_SPREADSHEET_TIMEOUT)
    try:
        with _SOFFICE_LOCK:
            result = run_subprocess(
                [
                    soffice,
                    f"-env:UserInstallation={pathlib.Path(profile_dir).as_uri()}",
                    "--convert-to", convert_to,
                    "--outdir", outdir,
                    path,
                ],
                capture_output=True, timeout=timeout,
            )
    except (subprocess.TimeoutExpired, OSError) as e:
        if debug:
            _debug_log(f"{name}: soffice failed to run: {e}")
        return None
    base = os.path.splitext(os.path.basename(path))[0]
    out_pdf = os.path.join(outdir, f"{base}.pdf")
    if os.path.isfile(out_pdf):
        return out_pdf
    if debug:
        # soffice --convert-to typically exits 0 even when it couldn't
        # actually load the file (e.g. a password-protected document,
        # or one flagged by its macro-security settings) - the only
        # sign is stderr/stdout text and no output file, so both are
        # worth showing here rather than just silently returning None.
        detail = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
        _debug_log(
            f"{name}: soffice produced no output"
            + (f" - {detail.splitlines()[0]}" if detail else "")
        )
    return None



def _rasterize_broken_img_sources(
    html_path: str, tmpdir: str, on_progress: Callable[[int, int], None] | None = None,
) -> str:
    """A Quick Look Office/iWork preview can embed a picture as
    `<img src="AttachmentN.pdf">` or `<img src="AttachmentN.tiff">` -
    formats the relevant generator apparently assumes a renderer that
    can show inline as an image, the way Safari/WebKit (Quick Look's
    own host) does; plain Chrome can't decode either one, and just
    shows a broken-image icon at whatever size the <img> tag's style
    gives it (confirmed by hand: a real Word document with a mix of
    PNG and TIFF pictures showed the PNGs fine and the TIFFs broken,
    scattered throughout - TIFF has no web-standard decode support at
    all, unlike PDF, which at least fails the same way in every
    browser other than Safari/WebKit). Across every picture in a slide
    deck, that's often enough bogus extra height to throw off later
    slides' measured positions entirely (see OfficeDocument._measure_slide_offsets()).

    iWork.qlgenerator (Numbers/Pages/Keynote) additionally splits a
    multi-sheet/page document into one `<iframe src="AttachmentN.html">`
    per sheet rather than embedding everything directly in Preview.html
    the way Office.qlgenerator does - and it's each of *those* files
    that actually embeds the broken `<img>`, not Preview.html itself.
    So this looks for that pattern recursively, through every local
    (not http/data/...) iframe target, not just in `html_path` itself.

    Rewrites each such reference found anywhere in that tree to a PNG -
    rasterized from a PDF via poppler's pdftocairo (not pdftoppm - see
    _convert() below for why), or straightforwardly re-encoded via
    Pillow for a TIFF (already a raster image; no DPI to choose, unlike
    a PDF) - and rewrites each iframe reference to point at its
    (recursively) patched target. Every patched file is written
    alongside the original it came from (same directory, different
    name) rather than elsewhere, so any *other* (unrelated) relative
    reference in it keeps resolving exactly as before, untouched.

    Returns the path to `html_path`'s own patched copy - or `html_path`
    unchanged if nothing anywhere in the tree needed patching.
    `on_progress`, if given, is called as `on_progress(done, total)`
    after each conversion finishes, `total` counting every one found
    across the whole tree."""
    # Phase 1: walk html_path plus every local HTML file it (recursively)
    # embeds via <iframe src>, collecting each file's own content and
    # its own <img src="*.pdf"/"*.tiff"/"*.tif"> references.
    file_contents = {}  # path -> content
    broken_refs_by_file = {}  # path -> [ref, ...]
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
        # (ref, declared width, declared height) for every broken <img
        # src=...> - the declared size (in CSS px, i.e. ~1:1 with
        # points) is what lets _convert() below pick a sane DPI for a
        # PDF ref instead of a fixed one that can be wildly wrong for a
        # huge embedded page (moot for a TIFF ref, already a fixed-size
        # raster). Deduplicated by ref, first occurrence winning, same
        # as a plain set() would for the src-only info this replaced.
        broken_refs = {}
        for tag_m in _IMG_TAG_RE.finditer(content):
            tag = tag_m.group(0)
            src_m = _SRC_ATTR_RE.search(tag)
            if not src_m or not src_m.group(1).lower().endswith((".pdf", ".tiff", ".tif")):
                continue
            ref = src_m.group(1)
            if ref in broken_refs:
                continue
            w_m, h_m = _WIDTH_ATTR_RE.search(tag), _HEIGHT_ATTR_RE.search(tag)
            broken_refs[ref] = (
                float(w_m.group(1)) if w_m else None,
                float(h_m.group(1)) if h_m else None,
            )
        broken_refs_by_file[path] = sorted(
            (ref, w, h) for ref, (w, h) in broken_refs.items()
        )
        for m in _IFRAME_SRC_RE.finditer(content):
            src = m.group(2)
            if _ABSOLUTE_SRC_RE.match(src):
                continue  # http(s)/data/... - not a local sibling file
            candidate = os.path.join(base_dir, src)
            if src.lower().endswith((".html", ".htm")) and os.path.isfile(candidate):
                to_visit.append(candidate)

    all_refs = [
        (path, ref, w, h)
        for path, refs in broken_refs_by_file.items()
        for ref, w, h in refs
    ]
    if not all_refs:
        return html_path
    have_pdftocairo = shutil.which("pdftocairo") is not None

    def _convert(item: tuple[str, str, float | None, float | None]) -> tuple[str, str, str | None]:
        path, ref, decl_w, decl_h = item
        src_path = os.path.join(os.path.dirname(path), ref)
        if not os.path.isfile(src_path):
            return path, ref, None
        # surrogateescape, like every other path tag here: a filename
        # that isn't valid UTF-8 would make a plain .encode() raise.
        tag = hashlib.md5(src_path.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        prefix = os.path.join(tmpdir, f"qlimg-{tag}")
        png_path = prefix + ".png"

        if ref.lower().endswith(".pdf"):
            if not have_pdftocairo:
                return path, ref, None
            # A fixed DPI (this used to always be 300) is wildly wrong for
            # a picture that's actually an entire spreadsheet/page flattened
            # into one PDF, sized in the thousands of points on a side - Numbers
            # in particular does this for a sheet too big to fit its normal
            # preview. Aim instead for roughly the size the <img> tag is
            # actually going to display it at (that's in CSS px, and a PDF
            # point is already ~1 CSS px at 96dpi, so this is close to
            # 1:1 - not literally 1:1 only because a page's declared point
            # size can differ slightly from its <img> tag's declared pixel
            # size), falling back to the old 300 if either the page size or
            # the declared display size isn't available - then hard-capped
            # regardless (see OFFICE_EMBEDDED_IMG_MAX_PX).
            dpi = 300.0
            page_size = _pdf_page_size_pt_safe(src_path)
            if page_size:
                page_w_pt, page_h_pt = page_size
                if decl_w and page_w_pt:
                    dpi = 72.0 * decl_w / page_w_pt
                elif decl_h and page_h_pt:
                    dpi = 72.0 * decl_h / page_h_pt
                if page_w_pt:
                    dpi = min(dpi, 72.0 * OFFICE_EMBEDDED_IMG_MAX_PX / page_w_pt)
                if page_h_pt:
                    dpi = min(dpi, 72.0 * OFFICE_EMBEDDED_IMG_MAX_PX / page_h_pt)
            dpi = max(36.0, min(300.0, dpi))

            try:
                run_subprocess(
                    # pdftocairo (not pdftoppm - it has no -transp option) so a
                    # picture with a transparent background (e.g. a PNG/GIF with
                    # alpha, flattened into this PDF by the Quick Look
                    # generator) keeps its transparency instead of getting
                    # composited onto an opaque white background here, before
                    # Chrome ever gets to draw it over the slide's real
                    # background.
                    ["pdftocairo", "-png", "-transp", "-r", str(round(dpi)), "-singlefile", src_path, prefix],
                    capture_output=True, check=True, timeout=20,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                return path, ref, None
        else:  # .tiff / .tif - already a raster image, just re-encoded
            # to a format Chrome can actually decode inline, downscaled
            # if it's absurdly large (see OFFICE_EMBEDDED_IMG_MAX_PX) -
            # there's no DPI to choose the way a PDF page needs.
            try:
                with Image.open(src_path) as img:
                    img.load()
                    if img.width > OFFICE_EMBEDDED_IMG_MAX_PX or img.height > OFFICE_EMBEDDED_IMG_MAX_PX:
                        img.thumbnail((OFFICE_EMBEDDED_IMG_MAX_PX, OFFICE_EMBEDDED_IMG_MAX_PX), Image.Resampling.LANCZOS)
                    img.convert("RGBA" if "A" in img.getbands() else "RGB").save(png_path, "PNG")
            except Exception:
                return path, ref, None

        if not os.path.isfile(png_path):
            return path, ref, None
        # Defensive: pdftocairo failing outright over OFFICE_EMBEDDED_IMG_MAX_PX
        # is already caught above (a non-zero exit raises
        # CalledProcessError) - this instead guards a degenerate ~1px
        # output from some other cause (e.g. a wildly wrong declared
        # width/height), treating it as a failure too (leaving the
        # original broken <img src=...> in place rather than silently
        # serving a blank picture).
        try:
            with Image.open(png_path) as probe:
                if probe.width <= 2 or probe.height <= 2:
                    return path, ref, None
        except Exception:
            return path, ref, None
        return path, ref, pathlib.Path(png_path).as_uri()

    # A slide deck can embed dozens to a couple hundred of these (one
    # pdftocairo/Pillow conversion each) - running them one at a time was
    # most of this whole function's cost, and each is independent, so a
    # thread pool (subprocess.run releases the GIL while the child runs,
    # and Pillow's C decoders release it too) cuts that down by roughly
    # the number of workers.
    png_by_file_ref = {}  # (path, ref) -> uri
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_convert, item) for item in all_refs]
        for future in concurrent.futures.as_completed(futures):
            path, ref, uri = future.result()
            if uri:
                png_by_file_ref[(path, ref)] = uri
            done += 1
            if on_progress:
                on_progress(done, len(all_refs))

    # Phase 2: write a patched copy of every file that needs one - its
    # own img srcs rasterized, and/or an iframe src repointed at its
    # child's patched copy. _patch() recurses into iframe targets first,
    # so by the time a parent rewrites its own iframe src, it already
    # knows whether (and where) that child got patched.
    patched_by_original: dict[str, str] = {}

    def _patch(path: str) -> str:
        if path in patched_by_original:
            return patched_by_original[path]
        content = file_contents.get(path)
        if content is None:
            return path
        base_dir = os.path.dirname(path)
        changed = False

        def _replace_img(m: re.Match[str]) -> str:
            nonlocal changed
            uri = png_by_file_ref.get((path, m.group(2)))
            if not uri:
                return m.group(0)
            changed = True
            return f"{m.group(1)}{uri}{m.group(3)}"

        def _replace_iframe(m: re.Match[str]) -> str:
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
        patched_path = os.path.join(base_dir, f"pdfless-patched-{tag}.html")
        with open(patched_path, "w", encoding="utf-8") as f:
            f.write(content)
        patched_by_original[path] = patched_path
        return patched_path

    return _patch(html_path)


def _open_screenshot(path: str) -> Image.Image | None:
    """A screenshot Chrome was to write, opened and loaded - None if it
    didn't (it can exit 0 without one) or wrote something that isn't
    an image, for the caller to report as a failed render rather than
    crash on.

    Args:
        path: the PNG's path.

    Returns:
        The image, or None.
    """
    try:
        img = Image.open(path)
        img.load()
        return img
    except OSError:  # FileNotFoundError, PIL's UnidentifiedImageError, a truncated file
        return None


def _capture_html_screenshot(
    chrome: str, html_path: str, width: int, height: int, out_png: str, render_scale: float,
) -> None:
    physical_w = width * render_scale
    physical_h = height * render_scale
    base_args = [
        chrome, "--headless", "--hide-scrollbars", "--no-sandbox",
        f"--window-size={width},{height}",
        f"--force-device-scale-factor={render_scale}",
        f"--screenshot={out_png}",
        _file_url(html_path),
    ]
    if max(physical_w, physical_h) < OfficeVariant.OFFICE_GPU_SAFE_PHYSICAL_PX:
        try:
            run_subprocess(base_args, capture_output=True, check=True, timeout=8)
            if os.path.isfile(out_png):
                return
            # Exited cleanly without a screenshot: try again without the GPU.
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            # Bigger than expected for this content, or the GPU path
            # itself failed: fall through to the software renderer.
            pass
    run_subprocess(
        [base_args[0], "--disable-gpu", *base_args[1:]],
        capture_output=True, check=True, timeout=30,
    )


def _capture_html_pdf(
    chrome: str, html_path: str, width: int, height: int, out_pdf: str, timeout: int = 20,
) -> bool:
    """Print `html_path` to a real (vector) PDF at an exact `width` x
    `height` CSS-pixel page size, via Chrome's headless --print-to-pdf -
    unlike _capture_html_screenshot()'s fixed-resolution PNG, this keeps
    text as real PDF text (not a bitmap of it), so pdfless can
    re-rasterize it at whatever DPI the current zoom needs (see
    PdfDocument.get_page_image()) instead of upscaling one fixed
    screenshot. Confirmed by hand that Chrome's print engine happily
    paginates content taller than one `height`-tall page into further
    real PDF pages, and that a plain `<a href>` survives as a real PDF
    link annotation (see _Links.ensure_index()).

    The page size is set via an injected `@page` CSS rule, in a scratch
    copy of html_path that's never written back (same idea as
    OfficeDocument._measure_slide_offsets()'s instrumented copy) - headless Chrome's
    CLI has no --paper-width/--paper-height flag of its own; this is
    the only way to reach a custom page size without going through the
    DevTools protocol (which is what a browser-automation library like
    Playwright would use instead - not worth the extra dependency just
    for this one knob).

    Returns True on success, False for anything that went wrong
    (a too-old Chrome without --print-to-pdf, a bad html_path, ...) -
    the caller falls back to the screenshot-based path either way, so
    this never raises."""
    try:
        with open(html_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return False

    style = f"<style>@page {{ size: {width}px {height}px; margin: 0; }}</style>"
    idx = content.lower().find("</head>")
    injected = content[:idx] + style + content[idx:] if idx != -1 else style + content
    print_path = os.path.join(os.path.dirname(html_path), "pdfless-print.html")
    try:
        with open(print_path, "w", encoding="utf-8") as f:
            f.write(injected)
    except OSError:
        return False

    try:
        run_subprocess(
            [
                chrome, "--headless", "--disable-gpu", "--no-sandbox",
                f"--print-to-pdf={out_pdf}", "--print-to-pdf-no-header",
                _file_url(print_path),
            ],
            capture_output=True, check=True, timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    finally:
        if os.path.exists(print_path):
            os.unlink(print_path)
    return os.path.exists(out_pdf)


def _sample_background_color(img: Image.Image) -> tuple[int, ...]:
    """A representative "blank" background color for `img`, sampled from
    its very top-left corner - outside the actual document content for
    every Quick Look generator seen so far (page margin; or, for a slide
    deck, the grey area around the first slide). Some generators' body
    background isn't plain white (e.g. PowerPoint's is a mid-grey) and
    stretches to fill the whole browser viewport regardless of window
    height, so _trim_trailing_blank_rows() needs the actual color to
    compare against rather than assuming white."""
    return cast("tuple[int, ...]", img.convert("RGB").getpixel((0, 0)))  # RGB: always a 3-tuple


def _trim_trailing_blank_rows(
    img: Image.Image, bg_color: tuple[int, ...],
) -> tuple[Image.Image, bool]:
    """Crop `img` to drop blank (uniformly `bg_color`) rows at the
    bottom - left over from over-provisioning the capture height in
    OfficeDocument._render_office_pages(), since the real content height isn't known
    ahead of a render. Returns (trimmed_img, might_be_cut_off) - the
    second value is True when content reaches all the way to the bottom
    of `img`, meaning the capture may have been too short to fit
    everything."""
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, bg_color)
    bbox = ImageChops.difference(rgb, bg).getbbox()
    if bbox is None:
        return img, False  # a blank page
    bottom = min(img.height, bbox[3] + 4)
    return img.crop((0, 0, img.width, bottom)), bottom >= img.height - 2


def _build_slide_measure_script(page_element_xpath: str) -> str:
    """The plist's own "PageElementXPath" (see OfficeDocument._generate_ql_preview()) is
    exactly what selects each page/slide's top-level element for this
    particular generator - Office.qlgenerator (Word/PowerPoint) says
    "/html/body/div", iWork.qlgenerator (Keynote) says
    "/html/body/div[starts-with(@class, 'slideStyle')]" - so use it via
    document.evaluate() rather than guessing a CSS selector (a single
    class name that happens to work for one generator, e.g. Office's
    "div.slide", silently selects nothing at all for another - which is
    indistinguishable from "no pages to measure" and falls back to
    slicing by the plist's Height instead, drifting out of alignment
    with the actual page/slide boundaries after enough of them - this
    was previously confirmed with PowerPoint, and resurfaces the same
    way with Keynote using a hardcoded selector that's specific to
    Office's own output).

    Waits for every <img> to finish loading before measuring, not just
    the page's own load event: one iWork.qlgenerator variant gives each
    page div no explicit size at all, relying entirely on its one
    full-bleed <img>'s natural (post-decode) height, so measuring before
    every image is actually decoded reads every offsetTop as 0 (each
    div still being height-0 at that point, none of them having pushed
    the next one down yet) - confirmed on a 36-slide deck where that's
    exactly what happened."""
    measure_fn = (
        "function(){"
        "document.title = JSON.stringify({"
        "total: document.body.scrollHeight,"
        "tops: (function(){"
        f"var r = document.evaluate({json.dumps(page_element_xpath)}, document, null, "
        "XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);"
        "var out = [];"
        "for (var i = 0; i < r.snapshotLength; i++) { out.push(r.snapshotItem(i).offsetTop); }"
        "return out;"
        "})()"
        "});"
        "}"
    )
    return (
        "<script>"
        f"var _pdflessMeasure = {measure_fn};"
        "var _pdflessImgs = Array.from(document.images);"
        "var _pdflessPending = _pdflessImgs.filter(function(i){return !i.complete;}).length;"
        "if (_pdflessPending === 0) {"
        "_pdflessMeasure();"
        "} else {"
        "_pdflessImgs.forEach(function(img){"
        "if (img.complete) return;"
        "var done = function(){ _pdflessPending--; if (_pdflessPending <= 0) _pdflessMeasure(); };"
        "img.addEventListener('load', done);"
        "img.addEventListener('error', done);"
        "});"
        "}"
        "</script>"
    )


_TOP_DIV_RE = re.compile(r'<div\b.*?</div>', re.IGNORECASE | re.DOTALL)
_TOP_DIV_IMG_ONLY_RE = re.compile(r'<div\b[^>]*>\s*<img\b[^>]*>\s*</div>', re.IGNORECASE)


def _detect_fallback_page_xpath(content: str) -> str | None:
    """Some Quick Look generator output (seen from an older Keynote/
    iWork.qlgenerator variant) has no "PageElementXPath" in its plist at
    all - unlike every other case seen so far - despite still having
    one clearly-delimited element per slide: each is simply a `<div>`
    directly under `<body>` wrapping one full-bleed
    `<img src="*.pdf">`, with no distinguishing class to select by.

    Detect that specific shape - rather than assuming any document
    missing "PageElementXPath" has it, which a continuously-flowing
    Word document very much doesn't - and if (almost) every direct
    `<div>` child of `<body>` matches it, return the same
    "/html/body/div" xpath a generator that DID report one would have
    (e.g. PowerPoint's). Returns None if this doesn't look like that
    shape."""
    body_idx = content.find("<body")
    if body_idx == -1:
        return None
    top_divs = _TOP_DIV_RE.findall(content[body_idx:])
    if len(top_divs) < 2:
        return None
    matching = sum(1 for d in top_divs if _TOP_DIV_IMG_ONLY_RE.fullmatch(d.strip()))
    return "/html/body/div" if matching >= len(top_divs) * 0.9 else None


def _read_html(path: str) -> str | None:
    """`path`'s contents for the measure helpers below - decoding errors
    replaced rather than raised - or None if it can't be read at all."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _chrome_dump_title(
    chrome: str, html_path: str, content: str, script: str, scratch_name: str,
    width: int | None = None, timeout: int = 30,
) -> str | None:
    """The measuring technique every _measure_*() helper shares: inject
    `script` (a <script> that leaves its answer in document.title) just
    before `content`'s </body> - `content` being html_path's own HTML,
    already read by the caller - write that to a scratch copy named
    `scratch_name` next to html_path (so relative references still
    resolve), load it in headless Chrome with --dump-dom, and return the
    text of the dumped <title> (still HTML-escaped), or None on any
    failure. Always cleans up the scratch copy.

    `width`, if given, sets the viewport width (layout can depend on
    it - see OfficeDocument._measure_slide_offsets()); the height is an
    arbitrary 1080, since dump-dom doesn't render anything, just loads
    and serializes the DOM at that viewport size. The 8s virtual time
    budget is an upper bound on how long a script may wait (e.g. for
    every <img> to finish decoding) before Chrome gives up and dumps
    whatever it's got - it only matters as a cap, since a script that
    finishes sooner ends it sooner."""
    idx = content.rfind("</body>")
    instrumented = content[:idx] + script + content[idx:] if idx != -1 else content + script
    measure_path = os.path.join(os.path.dirname(html_path), scratch_name)
    try:
        with open(measure_path, "w", encoding="utf-8") as f:
            f.write(instrumented)
    except OSError:
        return None
    args = [chrome, "--headless", "--no-sandbox"]
    if width is not None:
        args.append(f"--window-size={width},1080")
    args += ["--dump-dom", "--virtual-time-budget=8000", _file_url(measure_path)]
    try:
        r = run_subprocess(args, capture_output=True, text=True, timeout=timeout, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    finally:
        if os.path.exists(measure_path):
            os.unlink(measure_path)
    m = re.search(r"<title>(.*?)</title>", r.stdout, re.S)
    return m.group(1) if m else None


def _measure_content_height(
    chrome: str, html_path: str, width: int, timeout: int = 30,
) -> int | None:
    """document.body.scrollHeight for `html_path` at `width` (logical
    CSS px) - the same script-injected-title / --dump-dom technique as
    OfficeDocument._measure_slide_offsets() (see there for why dump-dom rather than a
    screenshot), but for a document's total flowing height rather than
    per-slide boundaries.

    Used to size FlowingText's single continuous @page height to the
    document's real content instead of one size-fits-all oversized
    page regardless of how short the document actually is - CSS's
    `size: <width> auto` looks like the obvious way to ask a browser's
    print engine to size a page's height to its content, but confirmed
    by hand that Chromium doesn't support "auto" here at all (silently
    falls back to a default Letter page instead), so measuring first
    and passing an explicit height is the only way. Returns None on
    any failure - the caller falls back to a fixed cap
    (OfficeVariant.OFFICE_MAX_CAPTURE_HEIGHT) instead."""
    content = _read_html(html_path)
    if content is None:
        return None
    title = _chrome_dump_title(
        chrome, html_path, content,
        "<script>document.title = String(Math.ceil(document.body.scrollHeight));</script>",
        "pdfless-height-measure.html", width=width, timeout=timeout,
    )
    m = re.fullmatch(r"\d+", title.strip()) if title else None
    return int(m.group(0)) if m else None


def _measure_svg_natural_size(
    chrome: str, wrapper_path: str, timeout: int = 20,
) -> tuple[int, int] | None:
    """(width, height) in CSS px that `wrapper_path`'s <img id="svg">
    (see SvgDocument._write_svg_wrapper()) naturally renders at - the
    same script-injected-title / --dump-dom technique
    _measure_content_height() uses, but reading the image's own
    naturalWidth/naturalHeight once it finishes loading (via onload,
    since decoding a local file happens asynchronously) rather than
    the document's scroll height.

    Needed because an SVG's intrinsic size varies in both dimensions
    at once - unlike FlowingText/SlideDeck's HTML, where a fixed
    Quick-Look-reported width is already known up front and only the
    height needs measuring, an SVG has no such external plist to read
    a width from; Chrome's print engine still can't size a page to its
    content on its own either way (see _measure_content_height()).

    Returns None on any failure, including an SVG Chrome couldn't
    determine a natural size for at all - the caller falls back to a
    fixed default size."""
    content = _read_html(wrapper_path)
    if content is None:
        return None
    script = (
        '<script>document.getElementById("svg").onload = function() {'
        'document.title = this.naturalWidth + "x" + this.naturalHeight;'
        "};</script>"
    )
    title = _chrome_dump_title(
        chrome, wrapper_path, content, script, "pdfless-svg-measure.html", timeout=timeout,
    )
    m = re.fullmatch(r"(\d+)x(\d+)", title.strip()) if title else None
    if not m:
        return None
    width, height = int(m.group(1)), int(m.group(2))
    return (width, height) if width > 0 and height > 0 else None


def _slice_and_save_pages(
    trimmed: Image.Image, bounds: list[int], tmpdir: str, tag: str,
) -> list[str]:
    """Crop `trimmed` at each consecutive pair in `bounds` (a list of Y
    pixel offsets, first always 0, last always trimmed.height) and save
    each slice as its own page PNG - shared by every OfficeVariant that
    captures one big image and then splits it, as opposed to
    ExcelWorkbook's multi-sheet case, which renders each page directly
    and never needs slicing at all."""
    page_paths = []
    for i in range(len(bounds) - 1):
        top, bottom = bounds[i], bounds[i + 1]
        page_path = os.path.join(tmpdir, f"office-page-{tag}-{i + 1}.png")
        trimmed.crop((0, top, trimmed.width, bottom)).save(page_path)
        page_paths.append(page_path)
    return page_paths


class OfficeVariant:
    """Base class for the different "how to turn this Quick Look
    preview into page images" strategies OfficeDocument._render_office_pages() can use -
    one of these is chosen once qlmanage's plist/HTML are known (see
    OfficeDocument._render_office_pages()), and it owns the capture strategy and the
    bounds/pagination decision for its own case. The shared preamble
    (finding Chrome, running qlmanage, deciding which variant to use)
    lives in OfficeDocument._render_office_pages() itself, since it has to run before
    any variant can even be chosen."""

    # GPU compositing has a texture-size ceiling that a multi-page/multi-
    # slide document's full-height capture (thousands of px, sometimes tens
    # of thousands) routinely exceeds, and Chrome then just hangs rather
    # than erroring out - confirmed hanging indefinitely with a physical
    # (post device-scale-factor) height anywhere from 24000px up on this
    # machine, well under Chrome's largest documented max-texture-size
    # (16384px on some GPUs) that a naive "keep the safety margin below
    # that" guess would have assumed safe. Software rasterization
    # (--disable-gpu) has no such limit but is noticeably slower, so a
    # capture kept safely under any plausible ceiling still uses the GPU;
    # anything at or beyond it skips straight to software rather than
    # wasting a mostly-guaranteed-to-time-out attempt first.
    OFFICE_GPU_SAFE_PHYSICAL_PX = 12000

    OFFICE_MAX_CAPTURE_HEIGHT = 40000  # logical px cap on how tall a single
    # document's content is allowed to be, to keep a pathological document
    # from trying to rasterize an unbounded amount of image

    def __init__(
        self, chrome: str, html_path: str, width: int, height: int, tag: str, name: str,
    ) -> None:
        self.chrome = chrome
        self.html_path = html_path
        self.width = width
        self.height = height
        self.tag = tag
        self.name = name

    def build_pages(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress, continuous: bool,
    ) -> RenderResult | None:
        """Returns a list of page PNG paths, or None on failure (a
        Chrome screenshot subprocess failing) - or, for FlowingText
        specifically, the 3-tuple ("pdf", pdf_path, npages) when a real
        PDF was captured instead (see FlowingText._build_pdf_pages()
        and OfficeDocument._render_office_pages(), which turns that
        into a PdfDocument delegate)."""
        raise NotImplementedError

    def _save_pages(
        self, trimmed: Image.Image, bounds: list[int], tmpdir: str, debug: bool,
        progress: _Progress,
    ) -> list[str]:
        """Wraps _slice_and_save_pages() with the same progress/debug
        reporting every variant that slices one big capture wants -
        not used by ExcelWorkbook's multi-sheet case, which has no
        single `trimmed` capture to slice at all (each sheet's capture
        already *is* its own page)."""
        npages = len(bounds) - 1
        progress.update(f"{self.name}: splitting into {npages} page(s)...")
        with _DebugTimer(debug, f"{self.name}: splitting into {npages} page(s)"):
            return _slice_and_save_pages(trimmed, bounds, tmpdir, self.tag)


class ExcelWorkbook(OfficeVariant):
    """should_not_scale (Excel's tell - see OfficeDocument._render_office_pages()).
    self.sheet_tabs, if non-empty (see _parse_sheet_tabs()), means a
    multi-sheet workbook: Office.qlgenerator renders every sheet up
    front as its own AttachmentN.html, with Preview.html itself being
    just a JS tab strip - each sheet is rendered as its own page here,
    concurrently, and never needs slicing (each capture already *is* a
    whole page). Otherwise (a single-sheet workbook, where Preview.html
    *is* the sheet) it's one capture at exactly the plist's own Width/
    Height - already sized to fit this generator's content exactly,
    unlike FlowingText's grow-and-trim approach (Excel's sheet-tab
    selector is pinned to the bottom of the viewport regardless of
    window height, which would defeat that trick)."""

    # Office.qlgenerator's tab strip for a multi-sheet spreadsheet - see
    # _parse_sheet_tabs(). One <div class="TabViewItem ..."> per sheet, each
    # with a <div class="TabHeader"> (the sheet's own name) and an <a
    # href="..."> pointing at that sheet's already-rendered AttachmentN.html.
    _TAB_VIEW_ITEM_RE = re.compile(
        r'<div\s+class="TabViewItem[^"]*">\s*'
        r'<div\s+class="TabHeader">(.*?)</div>\s*'
        r'<a\s+href="([^"]+)">',
        re.IGNORECASE | re.DOTALL,
    )
    # iWork.qlgenerator's own tab strip for a Numbers spreadsheet - a
    # different shape for the same thing: one <div class="navpane-sheet
    # ..."> per sheet, whose onclick="...SelectSheet(N, 'AttachmentN.html')"
    # names that sheet's HTML and whose title="..." is its name.
    _NAVPANE_SHEET_RE = re.compile(
        r"<div\s+onclick=\"javascript:SelectSheet\(\d+,\s*'([^']+)'\);\"[^>]*?"
        r'\stitle="([^"]*)"[^>]*\sclass="navpane-sheet[^"]*"',
        re.IGNORECASE | re.DOTALL,
    )
    _TAG_RE = re.compile(r'<[^>]+>')

    def __init__(
        self, chrome: str, html_path: str, width: int, height: int, tag: str, name: str,
    ) -> None:
        super().__init__(chrome, html_path, width, height, tag, name)
        self.sheet_tabs = self._parse_sheet_tabs(html_path)

    def _parse_sheet_tabs(self, html_path: str) -> list[tuple[str, str]]:
        """For a multi-sheet Excel-like Quick Look preview, return an
        ordered [(sheet_name, absolute_html_path), ...] - one per sheet
        - by reading the tab strip out of `html_path`'s own content
        (see _TAB_VIEW_ITEM_RE, or _NAVPANE_SHEET_RE for Numbers). A
        single-sheet workbook's Preview.html *is* the sheet itself (no
        tab strip, no <iframe>) and this returns []."""
        try:
            with open(html_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except OSError:
            return []
        base_dir = os.path.dirname(html_path)
        # (raw sheet-name markup, href) per tab, in order - from Office's
        # tab strip, or failing that Numbers' own.
        found = [(m.group(1), m.group(2)) for m in self._TAB_VIEW_ITEM_RE.finditer(content)]
        if not found:
            found = [(m.group(2), m.group(1)) for m in self._NAVPANE_SHEET_RE.finditer(content)]
        tabs: list[tuple[str, str]] = []
        for raw_name, href in found:
            if _ABSOLUTE_SRC_RE.match(href):
                continue  # http(s)/data/... - not a local sibling file
            candidate = os.path.join(base_dir, href)
            if not os.path.isfile(candidate):
                continue
            name = html.unescape(self._TAG_RE.sub("", raw_name)).strip()
            tabs.append((name or f"Sheet {len(tabs) + 1}", candidate))
        return tabs

    def build_pages(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress, continuous: bool,
    ) -> RenderResult | None:
        if self.sheet_tabs:
            return self._build_multi_sheet(tmpdir, debug, render_scale, progress)
        return self._build_single_sheet(tmpdir, debug, render_scale, progress)

    def _build_multi_sheet(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> list[str] | None:
        # Each sheet is an independent render (its own already-
        # rasterized HTML, its own Chrome screenshot subprocess), so -
        # like _rasterize_broken_img_sources()'s embedded-image
        # conversion - run them concurrently rather than one at a time;
        # a workbook can have dozens of sheets, and each is mostly
        # subprocess wait (releases the GIL), not CPU time here.
        sheet_tabs = self.sheet_tabs
        name = self.name

        def _render_sheet(i: int, sheet_name: str, sheet_html_path: str) -> str:
            with _DebugTimer(
                debug, f"{name}: sheet {i + 1} ({sheet_name}): converting embedded images"
            ):
                sheet_html_path = _rasterize_broken_img_sources(sheet_html_path, tmpdir)
            page_path = os.path.join(tmpdir, f"office-page-{self.tag}-{i + 1}.png")
            label = f"{name}: sheet {i + 1}/{len(sheet_tabs)} ({sheet_name}): rendering"
            with _DebugTimer(debug, label):
                _capture_html_screenshot(
                    self.chrome, sheet_html_path, self.width, self.height, page_path, render_scale
                )
            return page_path

        page_paths: list[str | None] = [None] * len(sheet_tabs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(sheet_tabs))) as pool:
            futures = {
                pool.submit(_render_sheet, i, sheet_name, sheet_html_path): i
                for i, (sheet_name, sheet_html_path) in enumerate(sheet_tabs)
            }
            done = 0
            for future in concurrent.futures.as_completed(futures):
                try:
                    page_paths[futures[future]] = future.result()
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                    return None
                done += 1
                progress.update(f"{name}: rendered {done}/{len(sheet_tabs)} sheet(s)...")
        return [p for p in page_paths if p is not None]  # every slot filled by now

    def _build_single_sheet(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> list[str] | None:
        out_png = os.path.join(tmpdir, f"office-capture-{self.tag}.png")
        # The same embedded-image fix every sheet of a multi-sheet
        # workbook gets - see _build_multi_sheet(). A no-op (the path
        # comes back unchanged) when nothing needs converting.
        with _DebugTimer(debug, f"{self.name}: converting embedded images"):
            html_path = _rasterize_broken_img_sources(self.html_path, tmpdir)
        label = f"{self.name}: rendering ({self.width * render_scale:.0f}x{self.height * render_scale:.0f})"
        try:
            with _DebugTimer(debug, label), progress.spin(label + "..."):
                _capture_html_screenshot(
                    self.chrome, html_path, self.width, self.height, out_png, render_scale
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        try:
            trimmed = _open_screenshot(out_png)
            if trimmed is None:
                return None
            return self._save_pages(trimmed, [0, trimmed.height], tmpdir, debug, progress)
        finally:
            if os.path.exists(out_png):
                os.unlink(out_png)


class SlideDeck(OfficeVariant):
    """A slide deck (PowerPoint/Keynote) whose page/slide boundaries
    were measured (see OfficeDocument._measure_slide_offsets()) - the exact total
    height is already known, so this is captured in one shot rather
    than guessed. `confident` (see OfficeDocument._render_office_pages()) is whether the
    boundary came from the plist's own PageElementXPath, as opposed to
    a shape-based guess (_detect_fallback_page_xpath()) - only then is
    it trusted to paginate; a guessed boundary has been observed to
    drift/overflow on some real decks, so it's rendered as a single
    continuous page instead."""

    def __init__(
        self, chrome: str, html_path: str, width: int, height: int, tag: str, name: str,
        slide_offsets: tuple[float, list[float]], confident: bool,
    ) -> None:
        super().__init__(chrome, html_path, width, height, tag, name)
        self.slide_offsets = slide_offsets
        self.confident = confident

    def build_pages(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress, continuous: bool,
    ) -> RenderResult | None:
        total_height, tops = self.slide_offsets
        capture_height = min(self.OFFICE_MAX_CAPTURE_HEIGHT, max(1, round(total_height)))
        out_png = os.path.join(tmpdir, f"office-capture-{self.tag}.png")
        label = (
            f"{self.name}: rendering "
            f"({self.width * render_scale:.0f}x{capture_height * render_scale:.0f})"
        )
        try:
            with _DebugTimer(debug, label), progress.spin(label + "..."):
                _capture_html_screenshot(
                    self.chrome, self.html_path, self.width, capture_height, out_png, render_scale
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        try:
            trimmed = _open_screenshot(out_png)
            if trimmed is None:
                return None
            if continuous or not self.confident:
                bounds = [0, trimmed.height]
            else:
                # Slice at each slide's own measured start, in physical
                # (scaled) pixels - exact, unlike guessing from the
                # screenshot itself (see OfficeDocument._measure_slide_offsets() for
                # why that doesn't work here).
                bounds = [
                    min(trimmed.height, round(t * render_scale)) for t in tops
                ] + [trimmed.height]
            return self._save_pages(trimmed, bounds, tmpdir, debug, progress)
        finally:
            if os.path.exists(out_png):
                os.unlink(out_png)


class FlowingText(OfficeVariant):
    """A continuously-flowing document with no slide markers (e.g.
    Word). Tries Chrome's headless --print-to-pdf first (see
    _build_pdf_pages()) - a real PDF pdfless can re-rasterize crisply at
    any zoom - and only falls back to the older screenshot-and-slice
    approach (_build_pages_via_screenshot(), see there for the
    "grow the capture height and retry" doubling loop, up to a hard
    cap) if that Chrome build doesn't support it. Renders the fallback
    at OFFICE_RENDER_SCALE_FLOWING rather than the caller's default -
    these documents are usually short and text-heavy enough that
    sharpness matters more than the render-time tradeoff the default
    otherwise makes for a large slide deck - unless the caller
    (-s/--rendering-scale) asked for a specific scale explicitly (moot
    for the PDF path, which is always re-rasterized at whatever DPI the
    current zoom needs, regardless of any render_scale)."""

    OFFICE_RENDER_SCALE_FLOWING = 2  # default device-pixel-ratio for
    # continuously-flowing text (Word and the like - no slide/page markers
    # at all, so no slide_offsets - see OfficeDocument._render_office_pages()): these are
    # text-heavy and usually short, so favor sharpness over the render-time
    # tradeoff OFFICE_RENDER_SCALE otherwise makes for a many-slide deck -
    # unless --rendering-scale was passed explicitly.

    def build_pages(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress, continuous: bool,
    ) -> RenderResult | None:
        pdf_pages = self._build_pdf_pages(tmpdir, debug, progress, continuous)
        if pdf_pages is not None:
            return pdf_pages
        return self._build_pages_via_screenshot(tmpdir, debug, render_scale, progress, continuous)

    def _build_pdf_pages(
        self, tmpdir: str, debug: bool, progress: _Progress, continuous: bool,
    ) -> RenderResult | None:
        """Real PDF pages via _capture_html_pdf(). Returns ("pdf",
        path, npages) for OfficeDocument._render_office_pages() to
        wire up as a PdfDocument delegate, or None to fall back to the
        screenshot path (an older Chrome without --print-to-pdf, or
        anything else going wrong).

        Not continuous (the common case): one page's own height
        (self.height, from the Quick Look preview's own plist) becomes
        the @page height, and Chrome's print engine paginates the rest
        on its own - real page breaks driven by the actual content
        flow, unlike the screenshot path's own bounds logic below,
        which can only guess at a fixed pixel height after the fact.
        That's also why this doesn't need SlideDeck's
        OfficeDocument._measure_slide_offsets() dance in the first place: a
        print-mode page break follows the content wherever it actually
        flows, so a slightly-off page height just spills part of one
        page onto the next rather than throwing off every later page's
        position too (fixed pixel-slicing's boundaries are cumulative
        - one page's error shifts every one after it; print-mode's
        aren't, each page break is independent).

        continuous=True (only RtfOfficeDocument's own qlmanage/Chrome
        fallback asks for it - see _render_office_pages()) instead
        measures the
        document's real total height first (_measure_content_height())
        and requests one oversized page sized to fit it - a
        continuously-flowing document has no real page boundaries of
        its own to paginate at in the first place (see the class
        docstring), so there's nothing for the print engine to do here
        that measuring wouldn't do more precisely.

        Persistent caching (see _render_result_cached()) happens one
        level up, in OfficeDocument._render_office_pages() - wrapping
        its *entire* pipeline (qlmanage, measuring, this) in one cache
        check, not just this one step, so a hit skips all of it, not
        only the final render."""
        out_pdf = os.path.join(tmpdir, f"office-capture-{self.tag}.pdf")
        if continuous:
            measure_label = f"{self.name}: measuring content height"
            with _DebugTimer(debug, measure_label), progress.spin(measure_label + "..."):
                content_height = _measure_content_height(self.chrome, self.html_path, self.width)
            if not content_height:
                # Couldn't measure - rather than guess a one-size-fits-
                # all OfficeVariant.OFFICE_MAX_CAPTURE_HEIGHT (a real, if rare, source
                # of a giant near-blank page under load, where Chrome's
                # dump-dom measurement is more likely to time out), fall
                # back to the screenshot path, which doesn't depend on
                # this measurement at all.
                return None
            # A little headroom: print-mode layout can measure a few px
            # taller than screen-mode's scrollHeight for the same
            # content (a marginal font-metric difference between the
            # two rendering paths) - this only has to avoid spilling a
            # couple of leftover px onto a needless second page, not be
            # exact.
            page_height = min(self.OFFICE_MAX_CAPTURE_HEIGHT, content_height + 40)
        else:
            page_height = self.height
        label = f"{self.name}: rendering to PDF"
        with _DebugTimer(debug, label), progress.spin(label + "..."):
            ok = _capture_html_pdf(self.chrome, self.html_path, self.width, page_height, out_pdf)
        return _pdf_render_result(out_pdf, ok)

    def _build_pages_via_screenshot(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress, continuous: bool,
    ) -> list[str] | None:
        if render_scale == OFFICE_RENDER_SCALE:
            render_scale = self.OFFICE_RENDER_SCALE_FLOWING
        out_png = os.path.join(tmpdir, f"office-capture-{self.tag}.png")
        # Starting small (rather than generously large) matters for
        # speed, not just to avoid over-capturing a short document: it
        # also keeps a short document's capture(s) under
        # _capture_html_screenshot's GPU-safe size threshold, where
        # every doubling from here on is much faster than the single
        # oversized one this used to start with.
        capture_height = min(self.OFFICE_MAX_CAPTURE_HEIGHT, max(self.height * 3, 1500))
        try:
            attempt = 0
            while True:
                attempt += 1
                label = (
                    f"{self.name}: rendering attempt {attempt} "
                    f"({self.width * render_scale:.0f}x{capture_height * render_scale:.0f})"
                )
                try:
                    with _DebugTimer(debug, label), progress.spin(label + "..."):
                        _capture_html_screenshot(
                            self.chrome, self.html_path, self.width, capture_height, out_png, render_scale
                        )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                    return None
                img = _open_screenshot(out_png)
                if img is None:
                    return None
                trimmed, cut_off = _trim_trailing_blank_rows(img, _sample_background_color(img))
                if not cut_off or capture_height >= self.OFFICE_MAX_CAPTURE_HEIGHT:
                    break
                capture_height = min(self.OFFICE_MAX_CAPTURE_HEIGHT, capture_height * 2)
            if continuous:
                bounds = [0, trimmed.height]
            else:
                page_px = max(1, round(self.height * render_scale))
                npages = max(1, -(-trimmed.height // page_px))  # ceil division
                bounds = [min(trimmed.height, i * page_px) for i in range(npages + 1)]
            return self._save_pages(trimmed, bounds, tmpdir, debug, progress)
        finally:
            if os.path.exists(out_png):
                os.unlink(out_png)


def extract_office_text(path: str) -> list[str] | None:
    """Plain-text extraction of a whole Word-family document (.doc,
    .docx, .rtf, .odt, ...), via macOS's own `textutil -convert txt
    -stdout` - unlike pdftotext for a PDF, this has no notion of pages,
    so it's always the entire document at once (see how kind=="office"
    is handled in Viewer._load_text_page() and around it). Returns None
    if textutil isn't available, doesn't understand this file at all
    (a spreadsheet or a slide deck: no single flowing "text" to extract,
    and textutil silently produces nothing), or the conversion otherwise
    failed."""
    if shutil.which("textutil") is None:
        return None
    try:
        out = run_subprocess(
            ["textutil", "-convert", "txt", "-stdout", path],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.splitlines()


def is_probably_text(path: str, sniff_bytes: int = 8000) -> bool:
    """The same binary/text heuristic git and file(1) use: if the first
    few KB contain a NUL byte, treat it as binary. (NUL is technically
    valid UTF-8, but genuine text essentially never contains it.)"""
    try:
        with open(path, "rb") as f:
            return b"\x00" not in f.read(sniff_bytes)
    except OSError:
        return False


_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # OLE/Compound File Binary


def is_password_protected_ooxml_or_visio(path: str) -> bool:
    """A modern Word/PowerPoint/Excel/Visio file (.docx/.pptx/.xlsx/.vsdx/...) is
    ordinarily a ZIP archive - MS-OFFCRYPTO password protection instead
    wraps the whole encrypted package in an OLE/CFB container (the same
    on-disk shape a *legacy* .doc/.ppt/.vsd already has natively,
    encrypted or not - so this magic-number check only means anything
    for an extension that's normally plain ZIP; callers only use it for
    those). Cheap enough to run during sniff()/_probe_preview(), before
    ever committing to soffice/qlmanage - both fail on a file like this
    anyway (soffice exits 0 having printed "Error: source file could
    not be loaded" to stderr; qlmanage exits 0 having simply produced
    no preview), so without this check the failure only surfaces much
    later, as an uninformative "Quick Look rendering failed" placeholder
    - see OfficeDocument._probe_preview()."""
    try:
        with open(path, "rb") as f:
            return f.read(len(_CFB_MAGIC)) == _CFB_MAGIC
    except OSError:
        return False


# Control characters that mustn't reach the terminal as they are: C0
# (but tab, newline and carriage return), DEL, and the C1 range - a
# terminal may take U+009B as CSI, say.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _caret_notation(match: re.Match[str]) -> str:
    """"^X" caret notation for one C0 control character or DEL (e.g.
    "\\x0c" (^L) or "\\x1b" (^[)) - XORing the byte with 0x40 maps the
    whole range (0x00-0x1f, plus 0x7f) to the right letter/symbol in one
    step, the same trick a terminal's own ^-echoing uses. A C1 one
    (U+0080-U+009F) has no caret form, so it's shown as its code, "<9B>",
    as less(1) does."""
    code = ord(match.group())
    return f"<{code:02X}>" if code >= 0x80 else "^" + chr(code ^ 0x40)


def display_safe(text: str, tab_width: int = 8) -> str:
    """`text` made safe to write to the terminal: control characters in
    caret notation (see _caret_notation()) - so a document can't send
    escape sequences of its own (setting the clipboard with OSC 52,
    say) - and tabs expanded, as the renderer's column math knows
    nothing of tab stops.

    Args:
        text: text from a document (its extracted text, a title, a file
            name, ...).
        tab_width: the tab stops' spacing.

    Returns:
        The text, as it's to be shown.
    """
    if "\t" in text:
        text = text.expandtabs(tab_width)
    if _CONTROL_CHAR_RE.search(text):
        text = _CONTROL_CHAR_RE.sub(_caret_notation, text)
    return text


# What a text file that isn't UTF-8 is tried as, in turn (see
# read_text_file()): the encodings Japanese text still often comes in.
_LEGACY_TEXT_ENCODINGS = ("cp932", "euc_jp")
_PRIVATE_USE_RE = re.compile("[\ue000-\uf8ff]")


def read_text_file(path: str, legacy: bool = True) -> str:
    """A text file's content: as UTF-8 (a BOM dropped), or failing that
    as each of _LEGACY_TEXT_ENCODINGS (Shift_JIS as Windows writes it,
    EUC-JP), or failing those as UTF-8 again with whatever doesn't decode
    shown as U+FFFD - so a file in some other encoding still opens,
    rather than crashing text mode or being refused.

    Args:
        path: the file.
        legacy: whether to try _LEGACY_TEXT_ENCODINGS - not for a
            Markdown file, which in practice is always UTF-8.

    Returns:
        Its text.

    Raises:
        OSError: if it can't be read.
    """
    with open(path, "rb") as f:
        data = f.read()
    for encoding in ("utf-8-sig",) + (_LEGACY_TEXT_ENCODINGS if legacy else ()):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        # Python's cp932 "decodes" bytes Shift_JIS leaves unused (0xFD-
        # 0xFF, the user-defined rows) into the Private Use Area - taken
        # as a sign it isn't Shift_JIS at all, which next to never has them.
        if encoding != "utf-8-sig" and _PRIVATE_USE_RE.search(text):
            continue
        return text
    return data.decode("utf-8", errors="replace")


def read_plain_text_lines(path: str, tab_width: int = 8, legacy: bool = True) -> list[str]:
    """A plain text file's lines, as pdftotext -layout's output is for a
    PDF page: ready to hand straight to the existing text-mode renderer.
    Tabs are expanded (there's no terminal-native tab stop handling in
    that renderer's column math) and stray control characters (e.g. a
    raw ESC, or a form feed/^L) are shown in caret notation (^[, ^L, ...)
    - see _caret_notation() - rather than passed through raw, so odd
    file content can't corrupt the terminal display the way that would.

    Always reads `path` verbatim - an RTF file's raw markup (control
    words, font/color tables, ...) rather than its document text, which
    is exactly what a plain-text sniff/decode check wants. RtfDocument
    is the one that knows to prefer extract_office_text() over this raw
    markup for actual display - see RtfDocument.extract_text(). Decoded
    as read_text_file() decodes it (`legacy` being passed on to it)."""
    content = read_text_file(path, legacy)
    content = content.expandtabs(tab_width)
    content = _CONTROL_CHAR_RE.sub(_caret_notation, content)
    return content.splitlines()


def compile_search_pattern(query: str) -> re.Pattern[str]:
    """Compile `query` as a case-insensitive regex, normalized the way
    normalize_for_search() normalizes the text it's run against (NFKC,
    and the same whitespace folding). If it isn't valid regex syntax
    (e.g. "f(x" - an unclosed group is a regex error), fall back to
    matching it literally instead of just failing the search. ("C++"
    doesn't qualify: since Python 3.11 it's a valid possessive "C+",
    matching only the "C".)

    Only what the user typed as regex syntax counts as such: a part that
    normalizing changes (full-width "（案）", "＊" from a Japanese input
    method) is matched literally, not as the "(", "*" it becomes.

    Args:
        query: the search as typed.

    Returns:
        The pattern, for search_spans()."""
    # Each piece of the query as (its NFKC form, the regex text for it).
    pieces = []
    for seg_start, seg_end in _search_segments(query):
        segment = query[seg_start:seg_end]
        normalized = unicodedata.normalize("NFKC", segment)
        pieces.append((normalized, segment if normalized == segment else re.escape(normalized)))
    # Fold whitespace by the same rule as the text (see
    # _fold_search_spaces()): a run of whitespace pieces is dropped or
    # becomes a single space, judged by the pieces either side of it.
    parts = []
    i = 0
    while i < len(pieces):
        if not pieces[i][0].isspace():
            parts.append(pieces[i][1])
            i += 1
            continue
        run_end = i
        while run_end < len(pieces) and pieces[run_end][0].isspace():
            run_end += 1
        before = pieces[i - 1][0][-1] if i > 0 else ""
        after = pieces[run_end][0][0] if run_end < len(pieces) else ""
        if not (_is_unspaced_script(before) or _is_unspaced_script(after)):
            parts.append(" ")
        i = run_end
    try:
        return re.compile("".join(parts), re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(normalize_for_search(query)[0]), re.IGNORECASE)


# Scripts written without spaces between words - Han (Chinese and
# Japanese alike), hiragana, katakana, bopomofo, and CJK punctuation -
# as ranges of code points (after NFKC, so no half-width katakana or
# full-width ASCII). Hangul is deliberately not here: Korean puts spaces
# between words, so they mean something.
_UNSPACED_SCRIPT_RANGES = (
    (0x3001, 0x303F),    # CJK symbols and punctuation (not U+3000, a space)
    (0x3040, 0x30FF),    # hiragana, katakana
    (0x3100, 0x312F),    # bopomofo
    (0x31A0, 0x31FF),    # bopomofo extended, CJK strokes, katakana extensions
    (0x3400, 0x4DBF),    # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),    # CJK unified ideographs
    (0xF900, 0xFAFF),    # CJK compatibility ideographs
    (0xFE30, 0xFE4F),    # CJK compatibility forms
    (0x20000, 0x3FFFF),  # CJK unified ideographs extensions B and on
)
_UNSPACED_SCRIPT_CLASS = "".join(
    f"\\U{lo:08x}-\\U{hi:08x}" for lo, hi in _UNSPACED_SCRIPT_RANGES
)
# Whitespace _fold_search_spaces() changes: a run next to an
# unspaced-script character (the "drop" group - dropped), else a run of
# two or more or anything but a plain space (becomes one space). A single
# plain space between two other characters doesn't match at all.
_FOLDABLE_SPACE_RE = re.compile(
    rf"(?P<drop>(?<=[{_UNSPACED_SCRIPT_CLASS}])\s+|\s+(?=[{_UNSPACED_SCRIPT_CLASS}]))"
    r"|\s{2,}|[^\S ]"
)


def _is_unspaced_script(ch: str) -> bool:
    """Whether `ch` (one character, or "" for none) belongs to a script
    written without spaces between words - see _UNSPACED_SCRIPT_RANGES."""
    if not ch:
        return False
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _UNSPACED_SCRIPT_RANGES)


# ASCII characters with a meaning in Python's regex syntax.
_REGEX_METACHARS = frozenset(".^$*+?{}[]\\|()")


def search_query_is_literal(query: str) -> bool:
    """Whether `query` matches only its own characters - no regex syntax
    in it (full-width "（" or "＊" from an input method doesn't count:
    see compile_search_pattern()), or none that's valid, since an invalid
    regex is searched for literally. Such a query can't reach further
    than itself, so text mode lets it match across line breaks by
    default (see _Search.find_all_text_matches()).

    Args:
        query: the search as typed.

    Returns:
        True for a literal query.
    """
    try:
        re.compile(query)
    except re.error:
        return True
    return not any(
        ch in _REGEX_METACHARS
        for seg_start, seg_end in _search_segments(query)
        if unicodedata.normalize("NFKC", query[seg_start:seg_end]) == query[seg_start:seg_end]
        for ch in query[seg_start:seg_end]
    )


def _search_segments(text: str) -> Iterator[tuple[int, int]]:
    """Cut `text` into the pieces normalize_for_search() normalizes one
    at a time: a character plus any combining marks after it, since NFKC
    may fuse those into one character.

    Args:
        text: the text (or query) to cut.

    Returns:
        (start, end) offsets of each piece, in order, covering all of `text`.
    """
    seg_start = 0
    for i in range(1, len(text)):
        # A combining mark (or one in disguise: half-width "ﾞ" is U+3099
        # once normalized) stays with the character before it.
        if not unicodedata.combining(unicodedata.normalize("NFKC", text[i])[0]):
            yield seg_start, i
            seg_start = i
    if text:
        yield seg_start, len(text)


# One normalize_for_search() result: the normalized text, plus - unless
# it's the text itself, unchanged - where each of its characters came
# from (see there).
NormalizedText = Tuple[str, Union[List[int], None], Union[List[int], None]]


def normalize_for_search(text: str) -> NormalizedText:
    """NFKC-normalize `text` for searching, so that a query typed one way
    finds text stored another: decomposed kana (NFD "ク" + U+3099, common
    in PDFs made from macOS file names) or Latin, full-width ASCII
    ("第５回"), half-width katakana ("ﾃﾞｰﾀ"), circled digits, ligatures
    ("ﬁle", common in LaTeX PDFs) and the like. Whitespace is folded
    too - see _fold_search_spaces().

    Normalizing changes the text's length, so it also records where each
    normalized character came from, for mapping a match back onto the
    original text (see search_spans() and _nfkc_joined()).

    Args:
        text: the text to search (or a query).

    Returns:
        (normalized, starts, ends): starts[i]/ends[i] are the original
        offsets of the segment normalized[i] came from. Both are None when
        normalizing changes nothing - the common case, checked at C speed -
        and offsets carry over unchanged.
    """
    # Line by line (see _nfkc_joined()), keeping each line's own ending.
    return _fold_search_spaces(_nfkc_joined(text.splitlines(keepends=True), ""))


def normalize_lines_for_search(lines: list[str]) -> NormalizedText:
    """normalize_for_search() of `lines` joined by single spaces - how
    text mode searches its lines as one, across line breaks (see
    _Search._find_text_matches_in_block()) - without paying for the
    joined text as a whole: each line already in NFKC (all of a long
    document's plain-English ones, say) is copied over as it is.

    Args:
        lines: the lines, without their line breaks.

    Returns:
        As normalize_for_search(" ".join(lines)) returns it.
    """
    return _fold_search_spaces(_nfkc_joined(lines, " "))


def _nfkc_joined(chunks: list[str], sep: str) -> NormalizedText:
    """normalize_for_search()'s first step, NFKC, for sep.join(chunks):
    each of _search_segments()'s pieces normalized on its own, so every
    normalized character belongs to exactly one piece of the original.

    Chunk by chunk - so that one already in NFKC, the common case
    (checked at C speed), is copied over as it is rather than a
    character at a time. A chunk boundary is never inside a segment, as
    long as a chunk doesn't start with a combining mark - true of lines.

    Args:
        chunks: the text, in pieces.
        sep: what goes between them (itself already NFKC).

    Returns:
        (normalized, starts, ends), as normalize_for_search() describes
        them - starts/ends None if nothing changed.
    """
    text = sep.join(chunks)
    if text.isascii() or unicodedata.is_normalized("NFKC", text):
        return text, None, None
    pieces: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    chunk_start = 0
    for k, chunk in enumerate(chunks):
        if k:
            chunk = sep + chunk  # the separator rides along with the next chunk
        n = len(chunk)
        if chunk.isascii() or unicodedata.is_normalized("NFKC", chunk):
            pieces.append(chunk)
            starts.extend(range(chunk_start, chunk_start + n))
            ends.extend(range(chunk_start + 1, chunk_start + n + 1))
        else:
            for seg_start, seg_end in _search_segments(chunk):
                piece = unicodedata.normalize("NFKC", chunk[seg_start:seg_end])
                pieces.append(piece)
                starts.extend([chunk_start + seg_start] * len(piece))
                ends.extend([chunk_start + seg_end] * len(piece))
        chunk_start += n
    return "".join(pieces), starts, ends


def _fold_search_spaces(normalized: NormalizedText) -> NormalizedText:
    """normalize_for_search()'s second step: fold the whitespace in its
    NFKC-normalized text, so that spacing a PDF's text layer adds (or
    leaves out) doesn't stop a match. Word and LibreOffice set a gap
    between Japanese and Latin text ("Xプロジェクト"), which pdftotext
    reads back as a space ("X プロジェクト"); it splits CJK runs at
    wide letter spacing too ("会 議"); and pdftotext -layout pads
    justified lines with runs of spaces.

    A run of whitespace next to a character of a script written without
    spaces between words (Chinese, Japanese - see _is_unspaced_script())
    is dropped; any other run becomes a single space.

    Args:
        normalized: (text, starts, ends), as normalize_for_search()
            describes them.

    Returns:
        The same, with the whitespace folded and starts/ends following
        along - unchanged (and as cheap as one regex search) when there's
        nothing to fold, the common case.
    """
    text, starts, ends = normalized
    if not _FOLDABLE_SPACE_RE.search(text):
        return normalized
    out: list[str] = []
    new_starts: list[int] = []
    new_ends: list[int] = []

    def keep(i: int, j: int, replacement: str | None = None) -> None:
        # Carry text[i:j] over (or `replacement`, one character standing
        # for all of it), with where it came from in the original.
        if replacement is None:
            out.append(text[i:j])
            new_starts.extend(starts[i:j] if starts is not None else range(i, j))
            new_ends.extend(ends[i:j] if ends is not None else range(i + 1, j + 1))
        else:
            out.append(replacement)
            new_starts.append(starts[i] if starts is not None else i)
            new_ends.append(ends[j - 1] if ends is not None else j)

    pos = 0
    for m in _FOLDABLE_SPACE_RE.finditer(text):
        keep(pos, m.start())
        if m.group("drop") is None:
            keep(m.start(), m.end(), " ")
        pos = m.end()
    keep(pos, len(text))
    return "".join(out), new_starts, new_ends


def search_spans(
    pattern: re.Pattern[str], text: str, normalized: NormalizedText | None = None,
) -> Iterator[tuple[int, int]]:
    """Every match of `pattern` (from compile_search_pattern()) in `text`,
    matched against its normalize_for_search() form.

    Args:
        pattern: the compiled query.
        text: the text to search.
        normalized: normalize_for_search(text), if the caller has it
            cached; computed here otherwise.

    Returns:
        (start, end) offsets into the original `text`, widened to whole
        segments - so a highlight never splits a character from its
        combining marks. Zero-width matches (e.g. a pattern like "x*")
        are skipped.
    """
    norm_text, starts, ends = normalized if normalized is not None else normalize_for_search(text)
    for m in pattern.finditer(norm_text):
        if m.start() == m.end():
            continue
        if starts is None or ends is None:
            yield m.start(), m.end()
        else:
            yield starts[m.start()], ends[m.end() - 1]



class UnusableFile(Exception):
    """Raised by DocumentHandler.sniff() when a file's format was
    positively identified (e.g. its PDF magic bytes matched) but it
    turned out not to be actually usable (failed to decode/parse) -
    distinct from sniff() returning None, which means "not this kind,
    try the next one instead". The caller (main()) reports this
    exception's own message and skips the file, without trying any
    further handler classes - once a format is positively identified,
    a failure to actually use it is that format's problem, not a sign
    the file might be some other kind instead."""


def find_title_line(
    lines: Sequence[str], start: int, end: int, title: str, near: float | None = None,
) -> int | None:
    """Where a section `title` (a bookmark's) appears among lines[start:end]
    - the text mode lines of the page it points to - so the table of
    contents can jump to it in text mode too. Compared the way search
    compares (see normalize_for_search()), case-insensitively, so the
    extra spaces pdftotext -layout puts in a heading ("1.1   背景") or a
    full-width digit in it don't matter.

    Args:
        lines: the text mode lines.
        start, end: the range to look in.
        title: the section's title.
        near: where on the page the bookmark points, as a fraction of the
            way down (0 = top) - picks among several lines with the title
            on them. None takes the first.

    Returns:
        The index into `lines` of the line with the title on it, or None
        if no line in the range has it (e.g. a title too long for one
        line, or worded differently from the text).
    """
    wanted = normalize_for_search(title)[0].strip().casefold()
    if not wanted:
        return None
    found = [
        i for i in range(start, min(end, len(lines)))
        if wanted in normalize_for_search(lines[i])[0].casefold()
    ]
    if not found:
        return None
    if near is None:
        return found[0]
    target = start + near * (end - start)
    return min(found, key=lambda i: abs(i - target))


def _interpolate(points: Sequence[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation: the y at `x` on the line through
    `points`, (x, y) pairs in increasing x - held at the ends outside it.

    Args:
        points: the (x, y) pairs, at least one.
        x: where to read it.

    Returns:
        y at x.
    """
    i = bisect.bisect_right([p[0] for p in points], x)
    if i == 0:
        return points[0][1]
    if i == len(points):
        return points[-1][1]
    (x0, y0), (x1, y1) = points[i - 1], points[i]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _screen_line_for_text(
    lines: list[tuple[float, float, str]], text: str, near_pt: float,
) -> float | None:
    """Where on the page a text mode line is: the top of the printed line
    (from PdfDocument.screen_lines()) with the same text - compared as
    search compares - or, as pdftotext -layout can put a line and the
    next one's start together, one wholly inside it. The nearest to
    `near_pt` wins among several.

    Args:
        lines: the page's printed lines, as (yMin, yMax, text).
        text: the text mode line.
        near_pt: roughly where it should be, in points from the top.

    Returns:
        The printed line's yMin, or None if none matches.
    """
    wanted = normalize_for_search(text)[0].strip().casefold()
    if not wanted:
        return None
    same, inside = [], []
    for top, _bottom, line_text in lines:
        printed = normalize_for_search(line_text)[0].strip().casefold()
        if printed == wanted:
            same.append(top)
        elif printed and (printed in wanted or wanted in printed):
            inside.append(top)
    found = same or inside
    return min(found, key=lambda top: abs(top - near_pt)) if found else None


class DocumentHandler:
    """Base class for a file's format-specific behavior. Each subclass
    corresponds to one of main()'s "kind" strings ("pdf"/"image"/
    "text"/"office") and is tried, in a fixed priority order, via
    sniff() - see HANDLER_CLASSES."""

    kind: str | None = None  # overridden per subclass

    def __init__(self, path: str) -> None:
        self.path = path

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> DocumentHandler | None:
        """Return an instance of this class if `path` looks like this
        kind, else None if it doesn't (try the next class). Raises
        UnusableFile if it does look like this kind but isn't actually
        usable. `tmpdir`/`debug` are only used by OfficeDocument
        (qlmanage needs a scratch dir; -d wants to know about a crashed
        qlmanage) - every other subclass ignores them; they're part of
        the common signature so a dispatcher can try each class
        uniformly without knowing which."""
        raise NotImplementedError

    def page_count(self) -> int | None:
        """Number of pages, or None if unknown until the file is
        actually rendered (only a RenderedDocument - its page count
        isn't known until soffice/Quick Look/Chrome/WeasyPrint have run;
        see Viewer._ensure_office_pages())."""
        raise NotImplementedError

    def extract_text(self, page: int) -> list[str] | None:
        """Text-mode content for `page` (1-based) - or the whole
        document, for a handler whose text isn't paginated (see
        text_mode_is_paginated()), which ignores `page` entirely. None
        means there's nothing to show (the 't' key reports that)."""
        return None

    def extract_text_pages(self, npages: int) -> list[list[str]] | None:
        """Every page's text-mode content at once, as a list of `npages`
        line lists (page 1 first) - what the continuous text view (see
        Viewer._text_continuous()) stitches together. Only meaningful
        for a handler whose text is paginated (text_mode_is_paginated());
        by default just extract_text() once per page - PdfDocument
        overrides it to do the whole document in one pdftotext run.
        None if any page has nothing to show."""
        pages = []
        for page in range(1, npages + 1):
            lines = self.extract_text(page)
            if lines is None:
                return None
            pages.append(lines)
        return pages

    def supports_text_mode(self) -> bool:
        """Whether 't' should even try entering text mode at all - the
        actual content still comes from extract_text(), which can
        return None for a specific file even when this is True (e.g.
        OfficeDocument: textutil produces nothing for a PowerPoint/
        Excel file even though it works for Word)."""
        return False

    def supports_search(self) -> bool:
        return False

    def build_search_index(self) -> list[dict[str, Any]] | None:
        """Per-page text and word positions for image-mode search - only
        a handler whose supports_search() is True has one (see
        PdfDocument.build_search_index() for its shape)."""
        return None

    def find_search_matches(
        self, index: list[dict[str, Any]], query: str, multiline: bool = False,
    ) -> list[BBoxMatch]:
        """Every match for `query` in a build_search_index() index - see
        PdfDocument.find_search_matches() for `multiline`."""
        return []

    def text_mode_is_paginated(self) -> bool:
        """Whether text mode's content is naturally split into pages
        the same way image mode is, so n/p/g/G page navigation should
        apply to it too. False means text mode shows one flowing blob
        regardless of the current image-mode page (e.g. TextDocument/
        RtfDocument's whole file, OfficeDocument's whole document via
        textutil)."""
        return False

    def heading_lines(self) -> list[int] | None:
        """For a text mode that shows the document's own source (not
        paginated - see text_mode_is_paginated()), which of its lines are
        the headings the table of contents lists, in order - or None if
        this handler can't tell. Lets the table of contents jump to a
        section's line in text mode (see _Overlays.jump_to_outline_entry())."""
        return None

    def search_resets_on_text_mode_toggle(self) -> bool:
        """Whether an active search should be cleared when `t` crosses
        between image mode and text mode. False for handlers where both
        views search the same extracted text (e.g. PDF); True when the
        two modes search different things (MarkdownDocument)."""
        return False

    def starts_in_text_mode(self) -> bool:
        """Whether this handler has no image view at all - permanently
        "in text mode" from the moment the file opens (a plain text
        file - see TextDocument, and so by inheritance RtfDocument) -
        as opposed to starting in image mode and only switching to text
        mode via 't' (a PDF, or a Quick Look preview file)."""
        return False

    def default_text_border(self, border_default: bool) -> bool:
        """Whether text mode's border should be on by default for this
        handler - see Viewer._default_text_border(). `border_default` is
        whatever --no-border requested; overridden by TextDocument (and
        so, by inheritance, RtfDocument), which have no real "page"
        boundary worth bordering at all, regardless of --no-border."""
        return border_default

    def default_text_wrap(self, wrap_default: bool) -> bool:
        """Whether text mode should default to soft-wrapping long lines
        (off means panning across them instead, with h/l/H/L) - see
        Viewer._default_text_wrap(). Off here regardless of
        `wrap_default` (whatever -S/--chop-long-lines requested): a
        PDF's per-page text or an Office document's whole-document text
        (via textutil) is derived from something else, not the actual
        file being paged through, so panning (the pre-existing behavior)
        stays the default - overridden by TextDocument (and so, by
        inheritance, RtfDocument), which default to `wrap_default`
        itself, i.e. wrapped unless -S said otherwise."""
        return False

    def get_page_image(self, cache: PageCache, page: int, target_px: int, fit: str) -> Image.Image:
        """Return `page`'s image (a PIL.Image), scaled so it's
        `target_px` wide (fit="width") or tall (fit="height") - used by
        PageCache.get(), which only ever calls this for the kinds it's
        used for at all (pdf/image/office - never text). `cache` (a
        PageCache) owns the shared LRU eviction bookkeeping (see
        cache._cached()/cache._store()) while each handler owns the
        format-specific way to actually produce/scale the page.

        This default implementation - used by ImageDocument and
        OfficeDocument via _source_for_page() - treats the page as a
        single native image, loaded once (see _native_page_image()) and
        resized by pixel ratio; PdfDocument overrides this entirely,
        rasterizing on demand at whatever DPI the target size implies
        instead, since a PDF has no fixed native pixel size at all."""
        native = self._native_page_image(cache, page)
        native_dim = native.width if fit == "width" else native.height
        # The fit is part of it: a width target at one zoom can equal a
        # height target at another, and they're different sizes.
        key = (page, round(target_px), fit)
        cached = cache._cached(key)
        if cached is not None:
            return cached
        scale = target_px / native_dim
        new_size = (
            max(1, round(native.width * scale)),
            max(1, round(native.height * scale)),
        )
        img = native.resize(new_size, Image.Resampling.LANCZOS)
        cache._store(key, img)
        return img

    def _native_page_image(self, cache: PageCache, page: int) -> Image.Image:
        """Load (once per page, cached in `cache`) and normalize
        _source_for_page()'s file - shared by every get_page_image()
        that treats a page as a single native image (see there)."""
        native = cache._native_images.get(page)
        if native is not None:
            return native
        source = self._source_for_page(cache, page)
        img: Image.Image
        try:
            img = Image.open(source)
            img = ImageOps.exif_transpose(img)  # also .load()s it
            if img.mode in ("RGBA", "LA") or (
                img.mode == "P" and "transparency" in img.info
            ):
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")
        except Exception as e:
            # Reached from -f/--follow noticing the file changed into
            # something that fails to decode - main() already checks
            # this once up front, but the file can always go bad again
            # later. Re-raised as a plain RuntimeError so callers
            # (reload()'s caller in run_viewer) don't need to know
            # anything PIL-specific to catch it.
            raise RuntimeError(f"cannot load {source}: {e}") from e
        cache._store_native(page, img)
        return img

    def _source_for_page(self, cache: PageCache, page: int) -> str:
        """The file to load for `page`, for the default get_page_image()
        - overridden by ImageDocument (always its own path) and
        OfficeDocument (one pre-rendered PNG per page, from `cache`)."""
        raise NotImplementedError


class PdfDocument(DocumentHandler):
    kind = "pdf"

    _BBOX_PAGE_RE = re.compile(
        r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', re.S
    )
    _BBOX_WORD_RE = re.compile(
        r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</word>'
    )

    def __init__(self, path: str, encrypted: bool = False) -> None:
        super().__init__(path)
        self.encrypted = encrypted  # sniff() found this PDF needs a
        # password - still True until _ensure_unlocked() is given a
        # correct one (see there); an ordinary, never-encrypted PDF is
        # simply never True in the first place.
        self.password: str | None = None  # the password that actually unlocked it,
        # once _ensure_unlocked() succeeds - passed to every later
        # pdfinfo/pdftoppm/pdftotext call against self.path (harmless,
        # as an extra -upw, even for a PDF that was never encrypted at
        # all, since it's None then and _password_args() omits it)
        self._page_sizes: dict[int, tuple[float, float]] = {}  # page -> (width_pt, height_pt), filled in
        # by page_size_pt() one page at a time as pages are first needed,
        # and cleared by forget_page_sizes() when the file changes

    @staticmethod
    def is_pdf_file(path: str) -> bool:
        """Sniff the file's own content rather than trusting its
        extension - a PDF always starts with "%PDF-", regardless of
        what it's named. A staticmethod (not reading self.path) since
        main() also calls this directly, on a candidate path, before
        any classification (and so any PdfDocument instance) exists -
        to decide up front whether poppler is required at all."""
        try:
            with open(path, "rb") as f:
                return f.read(5) == b"%PDF-"
        except OSError:
            return False

    @staticmethod
    def _password_args(password: str | None) -> list[str]:
        """The "-upw <password>" poppler CLI tools (pdfinfo/pdftoppm/
        pdftotext/...) all share, needed on every call against an
        encrypted PDF once _ensure_unlocked() has one - [] (no-op) for
        an unencrypted PDF, where `password` is always None."""
        return ["-upw", password] if password else []

    @staticmethod
    def _poppler_stdout(
        tool: str, path: str, password: str | None, *options: str, to_stdout: bool = False,
    ) -> str:
        """Run poppler's `tool` (pdfinfo/pdftotext) on `path` and return
        what it printed: `options` go before the path, the -upw password
        (see _password_args()) first of all, and `to_stdout` adds the "-"
        output-file argument pdftotext needs to print instead of writing
        a .txt file. Raises CalledProcessError on failure, with stderr
        captured - sniff()/_ensure_unlocked() read it to tell a wrong
        password apart from a broken file."""
        args = [tool, *PdfDocument._password_args(password), *options, path]
        if to_stdout:
            args.append("-")
        return run_subprocess(args, capture_output=True, text=True, check=True).stdout

    @staticmethod
    def _pdf_page_count(path: str, password: str | None = None) -> int:
        out = PdfDocument._poppler_stdout("pdfinfo", path, password)
        m = _PDFINFO_PAGES_RE.search(out)
        if not m:
            die("could not determine page count")
        return int(m.group(1))

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> PdfDocument | None:
        if not cls.is_pdf_file(path):
            return None
        try:
            cls._pdf_page_count(path)
        except subprocess.CalledProcessError as e:
            if "Incorrect password" not in (e.stderr or ""):
                raise UnusableFile(f"not a usable PDF ({e})") from e
            # Encrypted - not actually unusable, just not readable yet;
            # _ensure_unlocked() prompts for a password (and retries
            # this same probe with it) the moment anything actually
            # needs to read the file (page_count(), the first of
            # those - see Viewer._set_current_file()), not here, so a
            # file that's merely being classified (as opposed to the
            # one about to be shown) is never prompted for.
            return cls(path, encrypted=True)
        except Exception as e:
            raise UnusableFile(f"not a usable PDF ({e})") from e
        return cls(path)

    def _ensure_unlocked(self) -> None:
        """Prompt for this PDF's password, retrying on a wrong one,
        the first time anything needs to actually read it - a no-op
        every time after that (whether or not a password was ever
        needed at all), the same "resolve once, remember on the
        handler" shape OfficeDocument's own lazy rendering uses.
        page_count() is always the first such call (see
        Viewer._set_current_file()), so this is the only place that
        needs to call it.

        Raises UnusableFile if the user cancels the prompt (Esc/^C/^D)
        - go_to_file() already treats that exception exactly like any
        other unusable file: skipped over by :n/:p's auto-skip, or
        reported in place for a direct jump (x/X)."""
        if not self.encrypted:
            return
        message = None
        while True:
            password = _prompt_pdf_password(os.path.basename(self.path), message)
            if password is None:
                raise UnusableFile("password required")
            try:
                self._pdf_page_count(self.path, password)
            except subprocess.CalledProcessError as e:
                if "Incorrect password" not in (e.stderr or ""):
                    raise UnusableFile(f"not a usable PDF ({e})") from e
                message = "incorrect password, try again"
                continue
            self.password = password
            self.encrypted = False
            return

    def page_count(self) -> int:
        self._ensure_unlocked()
        return self._pdf_page_count(self.path, self.password)

    def page_size_pt(self, page: int) -> tuple[float, float]:
        """(width_pt, height_pt) for `page` - remembered after the first
        ask, since get_page_image() needs it on every call (even a page-
        cache hit, to know which cache key to look up) and -c/--continuous
        asks for every page on screen on every single draw: without
        this, each scroll step would run one pdfinfo per visible page,
        twice over."""
        size = self._page_sizes.get(page)
        if size is None:
            size = self._page_sizes[page] = self._read_page_size_pt(page)
        return size

    def forget_page_sizes(self) -> None:
        """Drop page_size_pt()'s remembered sizes - for Viewer.reload(),
        when the file changed on disk and its pages may have too."""
        self._page_sizes.clear()

    def _read_page_size_pt(self, page: int) -> tuple[float, float]:
        """page_size_pt()'s actual pdfinfo run, for one page."""
        out = self._poppler_stdout(
            "pdfinfo", self.path, self.password, "-f", str(page), "-l", str(page),
        )
        m = _PDFINFO_SIZE_RE.search(out)
        if not m:
            die(f"could not determine page size for page {page}")
        return float(m.group(1)), float(m.group(2))

    def extract_text(self, page: int) -> list[str]:
        """Plain-text rendering of one page, via poppler's pdftotext
        -layout (which tries to preserve the page's visual line/column
        layout, unlike the flat word-run text used for search)."""
        out = self._poppler_stdout(
            "pdftotext", self.path, self.password,
            "-f", str(page), "-l", str(page), "-layout", to_stdout=True,
        )
        return out.splitlines()

    def extract_text_pages(self, npages: int) -> list[list[str]]:
        """The whole document's extract_text() at once: one pdftotext
        -layout run instead of `npages` of them, split back into pages
        at the form feed pdftotext ends every page with. Each piece
        keeps its own trailing form feed so splitlines() treats it
        exactly as extract_text() does for that page on its own - the
        two must agree line for line, since search highlighting maps a
        match's page-relative line position between them."""
        out = self._poppler_stdout(
            "pdftotext", self.path, self.password, "-layout", to_stdout=True,
        )
        pieces = out.split("\f")
        page_texts = [piece + "\f" for piece in pieces[:-1]]
        if pieces[-1]:
            page_texts.append(pieces[-1])  # no trailing form feed after the last page
        pages = [text.splitlines() for text in page_texts]
        # pdftotext and page_count() (pdfinfo) should always agree, but
        # never let a disagreement leave a page with no entry at all.
        pages += [[] for _ in range(npages - len(pages))]
        return pages[:npages]

    def build_search_index(self) -> list[dict[str, Any]]:
        """Extract per-page text and word positions (via poppler's pdftotext
        -bbox) for searching. Returns a list, one entry per page, each
        {"width_pt": float, "height_pt": float, "text": str,
        "words": [(start, end, xMin, yMin, xMax, yMax), ...], "rows": ...}
        (all in points) where (start, end) are offsets into "text" for that
        word.

        "text"/"words" follow the order pdftotext -bbox gives the words
        in, which is usually reading order - including across a line
        break inside one column of a two-column page. But it isn't always
        the order they appear in on screen: a table row laid out cell by
        cell can come out with its cells out of order (a cell ahead of
        the one left of it). So "rows" is the same words again, sorted
        into rows as they appear on the page (see _screen_rows()):
        {"text": str, "words": [...like "words"...], "order": [the
        index in "words" of each of them]} - or None when that's the order
        they're already in. find_search_matches() searches both.

        "lines" (in both) holds each printed line's (start, end) offsets
        in its "text", for a regex matched one line at a time.
        find_search_matches() also adds normalize_for_search() of each
        "text" (or of its lines) the first time it searches it."""
        out = self._poppler_stdout(
            "pdftotext", self.path, self.password, "-bbox", to_stdout=True,
        )

        pages = []
        for width, height, body in self._BBOX_PAGE_RE.findall(out):
            found = []
            for xmin, ymin, xmax, ymax, word_html in self._BBOX_WORD_RE.findall(body):
                word_text = html.unescape(word_html)
                if word_text:
                    found.append((word_text, float(xmin), float(ymin), float(xmax), float(ymax)))
            pages.append(self._index_page(float(width), float(height), found))
        return pages

    @staticmethod
    def _index_page(
        width_pt: float, height_pt: float, found: list[tuple[str, float, float, float, float]],
    ) -> dict[str, Any]:
        """One page's entry in build_search_index() - see there.

        Args:
            width_pt, height_pt: the page's size.
            found: (text, xMin, yMin, xMax, yMax) of each word on it, in
                pdftotext's order.

        Returns:
            The entry, with "rows" filled in.
        """
        text, words, lines = PdfDocument._join_words(found, PdfDocument._stream_line_starts(found))
        screen_rows = PdfDocument._screen_rows(found)
        order = [i for row in screen_rows for i in row]
        rows = None
        if order != list(range(len(found))):
            row_starts: list[int] = []
            for row in screen_rows[:-1]:
                row_starts.append((row_starts[-1] if row_starts else 0) + len(row))
            row_text, row_words, row_lines = PdfDocument._join_words(
                [found[i] for i in order], [0] + row_starts,
            )
            rows = {"text": row_text, "words": row_words, "lines": row_lines, "order": order}
        return {
            "width_pt": width_pt, "height_pt": height_pt,
            "text": text, "words": words, "lines": lines, "rows": rows,
        }

    @staticmethod
    def screen_lines(page_info: dict[str, Any]) -> list[tuple[float, float, str]]:
        """The printed lines of one build_search_index() page, top to
        bottom, with where each is on the page - for lining text mode's
        lines up with the page image (see Viewer._image_point_for_text()).

        Args:
            page_info: the page's entry in build_search_index().

        Returns:
            (yMin, yMax, text) for each line, in points from the page's
            top - a line being a row as it appears on screen (see
            _screen_rows()), so a two-column page's row has both columns.
        """
        view = page_info["rows"] or page_info
        words, text = view["words"], view["text"]
        out = []
        w = 0  # words are in "text" order, and so are the lines
        for a, b in view["lines"]:
            while w < len(words) and words[w][0] < a:
                w += 1
            ys = []
            while w < len(words) and words[w][0] < b:
                ys.append((words[w][3], words[w][5]))
                w += 1
            if ys:
                out.append((min(y[0] for y in ys), max(y[1] for y in ys), text[a:b]))
        return sorted(out, key=lambda line: line[0])

    @staticmethod
    def _stream_line_starts(found: list[tuple[str, float, float, float, float]]) -> list[int]:
        """Where each printed line starts among words in pdftotext's own
        order: a word begins a new line unless it's on the same line as
        the one before - overlapping it vertically by at least half the
        shorter one's height - and further right.

        Args:
            found: (text, xMin, yMin, xMax, yMax) of each word, in order.

        Returns:
            The index in `found` of each line's first word.
        """
        starts = []
        for i, (_text, xmin, ymin, _xmax, ymax) in enumerate(found):
            if i:
                _p_text, p_xmin, p_ymin, _p_xmax, p_ymax = found[i - 1]
                overlap = min(p_ymax, ymax) - max(p_ymin, ymin)
                if overlap >= 0.5 * min(p_ymax - p_ymin, ymax - ymin) and xmin >= p_xmin:
                    continue
            starts.append(i)
        return starts

    @staticmethod
    def _join_words(
        found: list[tuple[str, float, float, float, float]], line_starts: list[int],
    ) -> tuple[str, list[tuple[int, int, float, float, float, float]], list[tuple[int, int]]]:
        """Join words into one searchable string, a space between each.

        Args:
            found: (text, xMin, yMin, xMax, yMax) of each word, in order.
            line_starts: the index in `found` of each line's first word.

        Returns:
            (text, words, lines): the joined string; each word's
            (start, end, xMin, yMin, xMax, yMax) with (start, end) its
            offsets in it; and each line's (start, end) offsets in it.
        """
        parts = []
        words = []
        offset = 0
        for word_text, xmin, ymin, xmax, ymax in found:
            parts.append(word_text)
            words.append((offset, offset + len(word_text), xmin, ymin, xmax, ymax))
            offset += len(word_text) + 1
        bounds = list(line_starts) + [len(found)]
        lines = [
            (words[a][0], words[b - 1][1]) for a, b in zip(bounds, bounds[1:]) if a < b
        ]
        return " ".join(parts) + (" " if parts else ""), words, lines

    @staticmethod
    def _screen_rows(found: list[tuple[str, float, float, float, float]]) -> list[list[int]]:
        """The rows words appear in on screen: words whose vertical extents
        overlap by at least half the shorter one's height share a row;
        rows go top to bottom, words in a row left to right.

        Args:
            found: (text, xMin, yMin, xMax, yMax) of each word.

        Returns:
            Each row's indices into `found`, in that order.
        """
        by_middle = sorted(range(len(found)), key=lambda i: found[i][2] + found[i][4])
        rows: list[list[int]] = []
        row_top = row_bottom = 0.0
        for i in by_middle:
            _text, _xmin, ymin, _xmax, ymax = found[i]
            if rows:
                overlap = min(row_bottom, ymax) - max(row_top, ymin)
                if overlap >= 0.5 * min(row_bottom - row_top, ymax - ymin):
                    rows[-1].append(i)
                    row_top, row_bottom = min(row_top, ymin), max(row_bottom, ymax)
                    continue
            rows.append([i])
            row_top, row_bottom = ymin, ymax
        return [sorted(row, key=lambda i: found[i][1]) for row in rows]

    @staticmethod
    def _resolve_link_dest(
        reader: Any, page_num_by_ref: dict[Any, int], dest: Any,
    ) -> tuple[int, float | None] | None:
        """A /Dest or action /D value - either an explicit destination array,
        or a name/bytestring to look up among the document's named
        destinations. Returns (page_num, top_pt) - top_pt is the target's y
        position in PDF points (bottom-up, i.e. still in the PDF's own
        coordinate system - the caller converts it), or None if it isn't
        specified by this destination type. Returns None outright if the
        destination can't be resolved at all (e.g. it points outside this
        document, or the PDF is malformed)."""
        from pypdf.generic import IndirectObject

        if isinstance(dest, (str, bytes)):
            name = dest if isinstance(dest, str) else dest.decode("utf-8", "replace")
            named = reader.named_destinations.get(name)
            if named is None:
                return None
            dest = getattr(named, "dest_array", named)
        if not dest:
            return None

        target = dest[0]
        ref = target if isinstance(target, IndirectObject) else getattr(
            target, "indirect_reference", None
        )
        if ref is None:
            return None
        page_num = page_num_by_ref.get((ref.idnum, ref.generation))
        if page_num is None:
            return None

        top_pt = None
        fit_type = str(dest[1]) if len(dest) > 1 else None
        try:
            if fit_type == "/XYZ" and len(dest) > 3 and dest[3] is not None:
                top_pt = float(dest[3])
            elif fit_type in ("/FitH", "/FitBH") and len(dest) > 2 and dest[2] is not None:
                top_pt = float(dest[2])
        except (TypeError, ValueError):
            top_pt = None
        return page_num, top_pt

    def build_link_index(self, npages: int) -> list[dict[str, Any]]:
        """Extract clickable link annotations for every page, via pypdf.
        Returns a list of `npages` dicts, one per page in order, each
        {"width_pt": float, "height_pt": float, "links": [...]}, where each
        link is {"xmin", "ymin", "xmax", "ymax"} (points, top-down - flipped
        from the PDF's own bottom-up rects to match this file's coordinate
        system elsewhere, e.g. build_search_index()) plus either
        {"kind": "uri", "uri": str} for an external link or
        {"kind": "page", "page": int, "top_pt": float | None} for a jump to
        another page in the same document."""
        empty = [{"width_pt": 0.0, "height_pt": 0.0, "links": []} for _ in range(npages)]
        try:
            from pypdf import PdfReader
        except ImportError:
            return empty

        try:
            reader = PdfReader(self.path)
            if reader.is_encrypted:
                reader.decrypt(self.password or "")
        except Exception:
            return empty

        page_num_by_ref = {}
        for i, p in enumerate(reader.pages):
            ref = p.indirect_reference
            if ref is not None:
                page_num_by_ref[(ref.idnum, ref.generation)] = i + 1

        result = []
        for i in range(npages):
            links = []
            width_pt = height_pt = 0.0
            try:
                page = reader.pages[i]
                box = page.mediabox
                width_pt, height_pt = float(box.width), float(box.height)
                for annot_ref in page.get("/Annots") or []:
                    try:
                        annot = annot_ref.get_object()
                        if annot.get("/Subtype") != "/Link":
                            continue
                        rect = annot.get("/Rect")
                        if rect is None or len(rect) != 4:
                            continue
                        x0, y0, x1, y1 = (float(v) for v in rect)
                        xmin, xmax = min(x0, x1), max(x0, x1)
                        ymin_bu, ymax_bu = min(y0, y1), max(y0, y1)
                        # Flip the PDF's bottom-up rect into the top-down
                        # system used everywhere else here.
                        ymin, ymax = height_pt - ymax_bu, height_pt - ymin_bu

                        action = annot.get("/A")
                        if action is not None and action.get("/S") == "/URI":
                            uri = action.get("/URI")
                            if uri:
                                links.append({
                                    "xmin": xmin, "ymin": ymin,
                                    "xmax": xmax, "ymax": ymax,
                                    "kind": "uri", "uri": str(uri),
                                })
                            continue

                        dest = annot.get("/Dest")
                        if dest is None and action is not None and action.get("/S") == "/GoTo":
                            dest = action.get("/D")
                        if dest is None:
                            continue
                        resolved = self._resolve_link_dest(reader, page_num_by_ref, dest)
                        if resolved is None:
                            continue
                        page_num, top_pt = resolved
                        links.append({
                            "xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                            "kind": "page", "page": page_num, "top_pt": top_pt,
                        })
                    except Exception:
                        continue  # one malformed annotation shouldn't lose the rest
            except Exception:
                pass
            result.append({"width_pt": width_pt, "height_pt": height_pt, "links": links})
        return result

    def build_outline(self) -> list[dict[str, Any]]:
        """Extract the document's outline (its bookmarks, a.k.a. table
        of contents), via pypdf. Returns a flat list in document order,
        one dict per entry: {"title": str, "level": int (0 = top level),
        "page": int | None, "top_pt": float | None} - page/top_pt as
        _resolve_link_dest() gives them (top_pt bottom-up, in PDF
        points), or both None for an entry whose destination can't be
        resolved (kept anyway, so the entries below it keep their
        place in the hierarchy). An empty list if there's no outline,
        or pypdf is missing or can't read the file."""
        try:
            from pypdf import PdfReader
        except ImportError:
            return []

        try:
            reader = PdfReader(self.path)
            if reader.is_encrypted:
                reader.decrypt(self.password or "")
            outline = reader.outline
        except Exception:
            return []

        page_num_by_ref = {}
        for i, p in enumerate(reader.pages):
            ref = p.indirect_reference
            if ref is not None:
                page_num_by_ref[(ref.idnum, ref.generation)] = i + 1

        entries: list[dict[str, Any]] = []

        # pypdf's outline is a list of Destinations, where a nested list
        # right after an entry holds that entry's children - so a list
        # item means "one level deeper", not an entry of its own.
        def walk(items: list[Any], level: int) -> None:
            for item in items:
                if isinstance(item, list):
                    walk(item, level + 1)
                    continue
                try:
                    # NFC: a title made from a macOS file name is often
                    # decomposed (NFD - "グ" as "ク" + a combining
                    # dakuten), which truncating by width could split.
                    title = unicodedata.normalize("NFC", str(item.title or ""))
                    title = re.sub(r"\s+", " ", title).strip()
                    resolved = self._resolve_link_dest(reader, page_num_by_ref, item.dest_array)
                except Exception:
                    continue  # one malformed entry shouldn't lose the rest
                page_num, top_pt = resolved if resolved is not None else (None, None)
                entries.append({
                    "title": title or "(untitled)", "level": level,
                    "page": page_num, "top_pt": top_pt,
                })

        try:
            walk(outline, 0)
        except Exception:
            pass  # keep whatever was read before the malformed part
        return entries

    @staticmethod
    def find_search_matches(
        index: list[dict[str, Any]], query: str, multiline: bool = False,
    ) -> list[BBoxMatch]:
        """Return every match of `query` (a case-insensitive regex, or a
        literal substring if it isn't valid regex syntax, matched through
        normalize_for_search() - so "第5回" also finds "第５回") across the whole
        document, as a list of (page_number, xMin, yMin, xMax, yMax) bounding
        boxes (in points, the union of every word the match touches), in
        reading order.

        Each page is searched in both of build_search_index()'s word
        orders - pdftotext's and the on-screen rows - and a match found in
        both counts once. A literal query (see search_query_is_literal())
        or any query with `multiline` (^T at the prompt) is matched against
        a whole page at once, so it can run across line breaks; any other
        regex, one printed line at a time - "labore.*labore" would
        otherwise reach from one to another half a page on."""
        pattern = compile_search_pattern(query)
        across_lines = multiline or search_query_is_literal(query)
        matches: list[BBoxMatch] = []
        for page_num, page in enumerate(index, start=1):
            views = [page]
            if page.get("rows") is not None:
                views.append(page["rows"])
            # (index in page["words"] of the first word it touches, where
            # in that word it starts) -> box: the key both orders agree on
            # for the same match, and what puts them in reading order.
            found: dict[tuple[int, int], tuple[float, float, float, float]] = {}
            for view in views:
                order = view.get("order")
                for pos, match_end in PdfDocument._view_spans(pattern, view, across_lines):
                    hit = PdfDocument._match_box(view["words"], pos, match_end)
                    if hit is None:
                        continue  # a line break (see _gap_box())
                    word_idx, local_start, box = hit
                    key = (order[word_idx] if order is not None else word_idx, local_start)
                    found.setdefault(key, box)
            for key in sorted(found):
                matches.append((page_num, *found[key]))
        return matches

    @staticmethod
    def _view_spans(
        pattern: re.Pattern[str], view: dict[str, Any], across_lines: bool,
    ) -> Iterator[tuple[int, int]]:
        """search_spans() of `pattern` in one of a page's word orders (see
        build_search_index()), as a whole or line by line.

        Args:
            pattern: from compile_search_pattern().
            view: the page entry, or its "rows".
            across_lines: search the whole text at once, rather than each
                of view["lines"] on its own.

        Returns:
            (start, end) offsets of each match in view["text"].
        """
        # Normalized once and kept in the index, which the Viewer holds
        # on to across searches.
        if across_lines:
            if "normalized" not in view:
                view["normalized"] = normalize_for_search(view["text"])
            yield from search_spans(pattern, view["text"], view["normalized"])
            return
        if "normalized_lines" not in view:
            view["normalized_lines"] = [
                normalize_for_search(view["text"][a:b]) for a, b in view["lines"]
            ]
        for (a, b), normalized in zip(view["lines"], view["normalized_lines"]):
            for start, end in search_spans(pattern, view["text"][a:b], normalized):
                yield a + start, a + end

    @staticmethod
    def _match_box(
        words: list[tuple[int, int, float, float, float, float]], pos: int, match_end: int,
    ) -> tuple[int, int, tuple[float, float, float, float]] | None:
        """Where a match at text[pos:match_end] is on the page.

        Args:
            words: the (start, end, xMin, yMin, xMax, yMax) of each word
                in the text searched.
            pos, match_end: the match's offsets in that text.

        Returns:
            (index of the first word it touches, its offset in that word,
            (xMin, yMin, xMax, yMax) - the union of every word it
            touches), or - touching no word at all - _gap_box().
        """
        box = None
        first = None
        for word_idx, (word_start, word_end, xmin, ymin, xmax, ymax) in enumerate(words):
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
                    first = (word_idx, local_start)
                    box = [sub_xmin, ymin, sub_xmax, ymax]
                else:
                    box[0] = min(box[0], sub_xmin)
                    box[1] = min(box[1], ymin)
                    box[2] = max(box[2], sub_xmax)
                    box[3] = max(box[3], ymax)
        if box is None or first is None:
            return PdfDocument._gap_box(words, pos, match_end)
        return first[0], first[1], (box[0], box[1], box[2], box[3])

    @staticmethod
    def _gap_box(
        words: list[tuple[int, int, float, float, float, float]], pos: int, match_end: int,
    ) -> tuple[int, int, tuple[float, float, float, float]] | None:
        """_match_box() for a match that touches no word: only the space
        joining two words (a search for " "), which isn't in the PDF
        itself - boxed as the gap between those two words, if they're on
        the same printed line.

        Args:
            words, pos, match_end: as for _match_box().

        Returns:
            (index of the word after the gap, -1 - so it sorts ahead of
            any match inside that word, (xMin, yMin, xMax, yMax) of the
            gap), or None if the gap is a line break.
        """
        k = bisect.bisect_right([w[0] for w in words], pos)  # the first word after it
        if not 0 < k < len(words) or not (words[k - 1][1] <= pos and match_end <= words[k][0]):
            return None
        _s, _e, _xmin, p_ymin, p_xmax, p_ymax = words[k - 1]
        _s, _e, n_xmin, n_ymin, _xmax, n_ymax = words[k]
        overlap = min(p_ymax, n_ymax) - max(p_ymin, n_ymin)
        if overlap < 0.5 * min(p_ymax - p_ymin, n_ymax - n_ymin) or n_xmin < p_xmax:
            return None
        return k, -1, (p_xmax, min(p_ymin, n_ymin), n_xmin, max(p_ymax, n_ymax))

    def supports_text_mode(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return True

    def text_mode_is_paginated(self) -> bool:
        return True

    def get_page_image(self, cache: PageCache, page: int, target_px: int, fit: str) -> Image.Image:
        """Rasterize `page` at whatever DPI makes it `target_px` wide
        (fit="width") or tall (fit="height") - a PDF page has no fixed
        native pixel size, unlike a plain image or an office-preview
        PNG (see DocumentHandler.get_page_image()'s default)."""
        width_pt, height_pt = self.page_size_pt(page)
        page_pt = width_pt if fit == "width" else height_pt
        dpi = 72.0 * target_px / page_pt
        key = (page, round(dpi))

        cached = cache._cached(key)
        if cached is not None:
            return cached

        # Uncompressed PPM (pdftoppm's default), not -png: the file is
        # only read back into memory and deleted, never kept, so PNG's
        # zlib compression would be pure overhead - and it's most of
        # pdftoppm's run time (e.g. ~700ms of ~770ms for a 1600px-wide
        # page), far more than writing/reading the bigger raw file.
        #
        # Into a directory of its own: renders run on more than one thread
        # (the page prefetch, the thumbnails) and for more than one file
        # (a prefetch for the one just left can still be running), and
        # one named for the page and DPI alone would have two of them
        # writing - and reading back - the same file.
        workdir = tempfile.mkdtemp(prefix="page-", dir=cache.tmpdir)
        prefix = os.path.join(workdir, "page")
        try:
            # stderr captured: poppler's warnings about a slightly
            # malformed PDF ("Syntax Error: ...") would otherwise land
            # over the page on screen - and it's what a failure says.
            run_subprocess(
                [
                    "pdftoppm", *self._password_args(self.password), "-r", str(dpi),
                    "-f", str(page), "-l", str(page),
                    "-singlefile", self.path, prefix,
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            img = Image.open(prefix + ".ppm")
            img.load()
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        cache._store(key, img)
        return img


class ImageDocument(DocumentHandler):
    kind = "image"

    # UserComment/GPSProcessingMethod/GPSAreaInformation are all the EXIF
    # spec's "UNDEFINED"-type comment layout (EXIF 2.3 section 4.6.5): an
    # 8-byte character-code prefix, not part of the text itself, saying how
    # to decode whatever bytes follow it.
    _EXIF_COMMENT_TAG_NAMES = {"UserComment", "GPSProcessingMethod", "GPSAreaInformation"}
    _EXIF_COMMENT_CODES = {
        b"ASCII\x00\x00\x00": "ascii",
        b"UNICODE\x00": "utf-16",
        b"JIS\x00\x00\x00\x00\x00": "shift_jis",
    }

    # Standard EXIF GPS IFD tag ids (EXIF 2.3 section 4.6.6) for the four
    # tags _gps_decimal_degrees() combines - fixed by the spec, unlike
    # every other tag here, which is instead looked up by name through
    # ExifTags.GPSTAGS.
    _GPS_LATITUDE_REF, _GPS_LATITUDE = 1, 2
    _GPS_LONGITUDE_REF, _GPS_LONGITUDE = 3, 4

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> ImageDocument | None:
        try:
            # .load() rather than the lighter .verify(): some formats
            # (e.g. WMF without a system loader available) pass
            # verify() - which only sanity-checks the file structure -
            # but still fail to actually decode.
            with Image.open(path) as probe:
                probe.load()
        except Exception:
            return None
        return cls(path)

    def page_count(self) -> int:
        return 1

    def _source_for_page(self, cache: PageCache, page: int) -> str:
        return self.path  # always page 1 - see page_count()

    def extract_text(self, page: int) -> list[str] | None:
        # No document text to speak of - this is facts about the image
        # itself (format/size/EXIF/...) instead. See _image_info_lines().
        return self._image_info_lines()

    def supports_text_mode(self) -> bool:
        return True

    def _human_size(self, num_bytes: int) -> str:
        """`num_bytes` as a short "12.3 MB"-style string - _image_info_lines()'s
        own file-size line, not a general-purpose formatter (no need for one
        elsewhere in this file)."""
        if num_bytes < 1024:
            return f"{num_bytes} bytes"
        size = num_bytes / 1024
        for unit in ("KB", "MB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    def _decode_exif_comment(self, value: bytes | str) -> str | None:
        """The text half of a UserComment/GPSProcessingMethod/
        GPSAreaInformation value, per its own 8-byte code prefix (see
        _EXIF_COMMENT_CODES) - or None if that prefix isn't one the spec
        defines, for _format_exif_value()'s generic bytes handling to fall
        back to instead.

        Spec-compliant EXIF declares this tag's type as UNDEFINED, which
        Pillow hands back as bytes - but plenty of real cameras/phones
        (confirmed by hand: a Motorola Moto G6 Plus's own
        GPSProcessingMethod, "ASCII\\0\\0\\0network") mislabel it as plain
        ASCII instead, which Pillow then decodes into a str itself - one
        still carrying this same 8-byte prefix as literal characters,
        Pillow having no way to know to strip it. latin-1 round-trips
        every byte 0-255 back losslessly (the only encoding Python
        guarantees that for), undoing exactly that str decode so the same
        prefix-stripping logic below applies regardless of which shape
        Pillow gave this in."""
        if isinstance(value, str):
            value = value.encode("latin-1", errors="ignore")
        prefix, rest = value[:8], value[8:]
        encoding = self._EXIF_COMMENT_CODES.get(prefix)
        if encoding is None:
            return None
        try:
            return rest.decode(encoding).rstrip("\x00").strip()
        except UnicodeDecodeError:
            return None

    def _format_exif_value(self, name: str, value: Any) -> str:
        """A single EXIF tag's value as display text - most are already
        plain numbers/strings/Pillow IFDRationals (str()'s fine for all of
        those), but a few need special handling by `name` (the tag's own
        display name, from ExifTags.TAGS/GPSTAGS - see _exif_lines()):
        GPSVersionID is 4 raw version-number bytes (e.g. 2.3.0.0), not
        text, and the UNDEFINED-type comment fields (_EXIF_COMMENT_TAG_NAMES)
        carry a non-text prefix before their actual value - both would
        otherwise come out as garbled control characters through the
        generic bytes handling below, which everything else (MakerNote,
        thumbnail data, ...) still falls back to."""
        if name == "GPSVersionID" and isinstance(value, (bytes, tuple, list)):
            return ".".join(str(b) for b in value)
        if name in self._EXIF_COMMENT_TAG_NAMES and isinstance(value, (bytes, str)):
            decoded = self._decode_exif_comment(value)
            if decoded is not None:
                return decoded
        if isinstance(value, bytes):
            if len(value) > 64:
                return f"<binary, {len(value)} bytes>"
            try:
                return value.decode("ascii").strip("\x00")
            except UnicodeDecodeError:
                return f"<binary, {len(value)} bytes>"
        return str(value)

    def _gps_decimal_degrees(self, dms: Any, ref: str) -> float:
        """A GPSLatitude/GPSLongitude (degrees, minutes, seconds) tuple plus
        its Ref ("N"/"S"/"E"/"W") as one signed decimal-degrees float -
        negative for S/W. The raw DMS form EXIF stores this in is standard
        but not practically usable (nothing takes DMS input directly),
        unlike decimal degrees, which is what maps/GPS tools (Google Maps'
        own search box included) expect."""
        degrees, minutes, seconds = (float(v) for v in dms)
        value = degrees + minutes / 60 + seconds / 3600
        return -value if ref in ("S", "W") else value

    def _exif_lines(self, img: Image.Image) -> list[str]:
        """"Tag: value" lines for `img`'s EXIF metadata (most JPEGs from a
        camera/phone carry some; most PNGs/screenshots don't), or [] if it
        has none. GPSInfo is a nested sub-IFD of its own (see
        ExifTags.IFD.GPSInfo) rather than a plain tag - expanded into its
        own "GPS <tag>" lines instead of the raw dict getexif() otherwise
        leaves it as. Where both GPSLatitude/GPSLongitude and their Ref are
        present, those four raw DMS tags are replaced with two decimal-
        degrees lines and a single "lat,lon" line ready to paste into
        Google Maps' search box - see _gps_decimal_degrees()."""
        try:
            exif = img.getexif()
        except Exception:
            return []
        if not exif:
            return []
        lines = []
        for tag_id in sorted(exif.keys()):
            if tag_id == ExifTags.IFD.GPSInfo:
                continue
            name = ExifTags.TAGS.get(tag_id, str(tag_id))
            lines.append(f"{name}: {self._format_exif_value(name, exif[tag_id])}")
        try:
            gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        except Exception:
            gps = None
        gps = gps or {}

        lat_dd = lon_dd = None
        try:
            if self._GPS_LATITUDE in gps and self._GPS_LATITUDE_REF in gps:
                lat_dd = self._gps_decimal_degrees(gps[self._GPS_LATITUDE], gps[self._GPS_LATITUDE_REF])
            if self._GPS_LONGITUDE in gps and self._GPS_LONGITUDE_REF in gps:
                lon_dd = self._gps_decimal_degrees(gps[self._GPS_LONGITUDE], gps[self._GPS_LONGITUDE_REF])
        except (TypeError, ValueError, ZeroDivisionError):
            lat_dd = lon_dd = None

        skip_ids = set()
        if lat_dd is not None:
            lines.append(f"GPS Latitude: {abs(lat_dd):.6f}° {gps[self._GPS_LATITUDE_REF]}")
            skip_ids |= {self._GPS_LATITUDE, self._GPS_LATITUDE_REF}
        if lon_dd is not None:
            lines.append(f"GPS Longitude: {abs(lon_dd):.6f}° {gps[self._GPS_LONGITUDE_REF]}")
            skip_ids |= {self._GPS_LONGITUDE, self._GPS_LONGITUDE_REF}
        if lat_dd is not None and lon_dd is not None:
            lines.append(f"GPS Coordinates (paste into Google Maps): {lat_dd:.6f},{lon_dd:.6f}")

        for tag_id in sorted(gps):
            if tag_id in skip_ids:
                continue
            name = ExifTags.GPSTAGS.get(tag_id, str(tag_id))
            lines.append(f"GPS {name}: {self._format_exif_value(name, gps[tag_id])}")
        return lines

    def _image_info_lines(self) -> list[str] | None:
        """Text-mode content for an image, shown via the same "t" key every
        other kind uses (see ImageDocument.extract_text()) - there's no
        document text to extract from a raw image, so this shows facts
        about the image itself instead: basic format/size info every image
        has, then EXIF metadata and any embedded text chunks (a screenshot
        tool's own tag, or an AI image generator's embedded prompt - PNG
        stores both the same way, as a tEXt/iTXt chunk) where the file
        happens to carry them. None if the file can no longer be opened
        (e.g. deleted since being classified)."""
        try:
            img = Image.open(self.path)
        except Exception:
            return None
        with img:
            lines = ["Image Info", "==========", ""]
            lines.append(f"File:        {os.path.basename(self.path)}")
            lines.append(f"Format:      {img.format or 'unknown'}")
            lines.append(f"Size:        {img.width} x {img.height} px")
            lines.append(f"Color mode:  {img.mode}")
            try:
                lines.append(f"File size:   {self._human_size(os.path.getsize(self.path))}")
            except OSError:
                pass
            dpi = img.info.get("dpi")
            if dpi:
                # dpi's own values can be a plain float or Pillow's
                # IFDRational (when read from EXIF) - the latter has no
                # :.0f support of its own, hence the explicit float() first.
                lines.append(f"DPI:         {float(dpi[0]):.0f} x {float(dpi[1]):.0f}")
            n_frames = getattr(img, "n_frames", 1)
            if n_frames > 1:
                lines.append(f"Frames:      {n_frames} (animated)")
            if "transparency" in img.info or "A" in img.mode:
                lines.append("Transparency: yes")
            if img.info.get("icc_profile"):
                lines.append("ICC profile: yes")

            # PNG (and some other formats') text chunks - Pillow decodes
            # these straight to str in .info, unlike the binary metadata
            # (icc_profile, exif, ...) also stored there, so a plain
            # isinstance check is enough to single them out.
            text_chunks = {k: v for k, v in img.info.items() if isinstance(v, str)}
            if text_chunks:
                lines += ["", "Embedded text", "=============", ""]
                for key, value in sorted(text_chunks.items()):
                    value_lines = value.splitlines() or [""]
                    for i, line in enumerate(value_lines):
                        label = f"{key}:" if i == 0 else " " * (len(key) + 1)
                        lines.append(f"{label} {line}")

            exif_lines = self._exif_lines(img)
            if exif_lines:
                lines += ["", "EXIF", "====", ""]
                lines += exif_lines

            # EXIF fields (Artist/Copyright/UserComment/...) and PNG text
            # chunks both come straight from the file's own bytes - same
            # untrusted-content caret-notation treatment as a plain text
            # file (see read_plain_text_lines()), so a raw ESC or other
            # control byte tucked into one can't inject terminal escape
            # sequences or otherwise corrupt the display.
            return [_CONTROL_CHAR_RE.sub(_caret_notation, line) for line in lines]


class _RawTextView:
    """Text mode as the file's own raw text, one flowing blob - shared
    by TextDocument (that *is* the whole document) and MarkdownDocument
    (its raw Markdown source, beside the rendered image view). Mixed in
    ahead of the DocumentHandler base, so these win over its defaults
    (and over RenderedDocument's PDF-delegating ones)."""

    path: str  # set by the DocumentHandler it's mixed into
    # Whether the file may be in a legacy encoding (see read_text_file())
    # - not a Markdown file, which in practice is always UTF-8.
    _legacy_encodings = True

    def extract_text(self, page: int) -> list[str]:
        return read_plain_text_lines(self.path, legacy=self._legacy_encodings)

    def text_mode_is_paginated(self) -> bool:
        return False

    def default_text_border(self, border_default: bool) -> bool:
        # No real "page" boundary in a plain text file worth bordering,
        # regardless of --no-border.
        return False

    def default_text_wrap(self, wrap_default: bool) -> bool:
        # This *is* the actual file content being paged through (unlike
        # a PDF's extracted text or an Office document's textutil
        # dump), so it defaults to wrapping like less(1) itself does -
        # unless -S/--chop-long-lines said otherwise.
        return wrap_default


class TextDocument(_RawTextView, DocumentHandler):
    kind = "text"

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> TextDocument | None:
        # (No need to read it all to see that it decodes: read_text_file()
        # takes any encoding, one way or another.)
        if not is_probably_text(path):
            return None
        return cls(path)

    def page_count(self) -> int:
        return 1

    def supports_text_mode(self) -> bool:
        return True

    def supports_search(self) -> bool:
        return True

    def starts_in_text_mode(self) -> bool:
        return True


class RtfDocument(TextDocument):
    """An RTF file is - deliberately - plain ASCII text, so it also
    matches TextDocument's own signature; tried first (see
    HANDLER_CLASSES) so its more specific one wins. Everything else is
    inherited from TextDocument - only extract_text() differs: showing
    an RTF file's "plain text" verbatim would mean showing its raw
    markup (control words, font/color tables, ...), not the document's
    actual content - see is_rtf_file()."""

    @staticmethod
    def is_rtf_file(path: str) -> bool:
        """Sniff for RTF's own signature ("{\\rtf1" right at the start),
        the same way PdfDocument.is_pdf_file() sniffs "%PDF-" rather
        than trusting the extension. RTF is - deliberately - plain
        ASCII text, so is_probably_text()'s NUL-byte heuristic happily
        calls it plain text too; sniff() uses this to tell the two
        apart. A staticmethod (not reading self.path) since
        RtfOfficeDocument.sniff() also calls this directly, on a
        candidate path, before any RtfDocument instance exists."""
        try:
            with open(path, "rb") as f:
                return f.read(6) == b"{\\rtf1"
        except OSError:
            return False

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> TextDocument | None:
        if not cls.is_rtf_file(path):
            return None
        # The rest is TextDocument's own check - vanishingly unlikely to
        # fail for genuine RTF (it's pure ASCII by spec), but kept as the
        # same NUL-byte/decoding safety net rather than trusting the RTF
        # signature alone.
        return super().sniff(path, tmpdir, debug)

    def extract_text(self, page: int) -> list[str]:
        return extract_office_text(self.path) or super().extract_text(page)


class RenderedDocument(DocumentHandler):
    """A file that has to be rendered - into a real PDF, or a list of
    page images - before it can be shown at all: OfficeDocument (Quick
    Look/soffice/Chrome), SofficeOnlyDocument (soffice), SvgDocument
    (Chrome) and MarkdownDocument (WeasyPrint). What they share is the
    lazy rendering itself - page_count() is None, since the page count
    isn't known until build_pages() has actually run (see
    Viewer._ensure_office_pages()) - and, once a render produced a real
    PDF, a PdfDocument delegate that page images, text and search all go
    through. Each subclass supplies only how to render (_renderer()) and
    how to cache it (_cache_key_suffix()) - see build_pages()."""

    kind = "office"

    OFFICE_DEFAULT_WIDTH = 816  # 8.5in at 96dpi, if the plist has no Width
    OFFICE_DEFAULT_HEIGHT = 1056  # 11in at 96dpi, if the plist has no Height

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.pages: list[str] | None = None  # [png_path, ...] once rendered - see
        # build_pages()/ensure_pages(); None until the first render.
        self._pdf_delegate: PdfDocument | None = None  # a PdfDocument wrapping a real,
        # print-to-pdf-rendered PDF, when FlowingText managed one (see
        # _render_office_pages()) - get_page_image() forwards to it
        # instead of treating self.pages as a list of PNGs, so these
        # pages stay crisp at any zoom the same way a real PDF does.
        # self.pages is still set (to a same-length placeholder list)
        # in that case, purely so len(self.pages) keeps working for
        # Viewer._ensure_office_pages()/reload().

    def _render_error_placeholder(self, tmpdir: str, message: str) -> list[str]:
        """A single-page fallback for when _render_office_pages()
        succeeds at sniff()/_probe_preview() time (qlmanage has a
        generator for this file) but then fails for real later (e.g.
        Chrome crashes or times out on this particular render) -
        rendering happening lazily, on first display rather than
        upfront, means a failure here can't just fall back to skipping
        the file the way main() does for one that never looked
        previewable in the first place. Returns a one-item list of PNG
        paths, the same shape _render_office_pages() itself returns on
        success, so callers don't need to special-case this."""
        from PIL import ImageDraw

        img = Image.new("RGB", (900, 200), "white")
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), f"could not render {os.path.basename(self.path)}", fill="black")
        draw.text((20, 50), message, fill="black")
        out_path = os.path.join(
            tmpdir,
            f"office-error-{hashlib.md5(self.path.encode('utf-8', 'surrogateescape')).hexdigest()[:12]}.png",
        )
        img.save(out_path)
        return [out_path]

    def page_count(self) -> int | None:
        return None

    # What -d/--debug reports when build_pages() is served from the
    # persistent cache - each subclass names what that let it skip.
    _CACHE_HIT_NOTE = "reusing cached render, skipped rendering"

    def _renderer(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> Callable[[], RenderResult | None] | None:
        """build_pages()'s actual render, as a zero-argument callable
        returning _remember_pages()'s input - a ("pdf", pdf_path, npages)
        tuple or a list of per-page image paths, or None on failure - or
        None if there's no point even trying. Every subclass has its own."""
        raise NotImplementedError

    def _cache_key_suffix(self, render_scale: float) -> str | None:
        """build_pages()'s persistent-cache key suffix (see
        _cached_render_dir()), or None for no persistent caching. By
        default "": a real PDF is re-rasterized at whatever scale the
        view needs, so one rendering serves every -s/--rendering-scale."""
        return ""

    def build_pages(
        self, tmpdir: str, debug: bool = False, render_scale: float = OFFICE_RENDER_SCALE,
        progress: _Progress | None = None,
    ) -> list[str] | None:
        """Render fresh - always, regardless of self.pages - remembering
        the result for _source_for_page() and any later ensure_pages()
        call. Used directly by Viewer.reload() (which always wants a
        fresh render, since the file changed on disk); see
        ensure_pages() for the memoized entry point everything else
        wants instead.

        The same steps for every subclass, which only supply what
        differs: _renderer() (the actual render, as a zero-argument
        callable, or None if it can't even be attempted) and
        _cache_key_suffix() (how the persistent cache - see
        _render_result_cached() - tells apart renderings of the same
        file, or None to skip that cache altogether). The whole render
        is wrapped in one cache check, so a hit skips all of it -
        qlmanage, soffice and Chrome alike."""
        if progress is None:
            progress = _ProgressLine(enabled=False)
        render = self._renderer(tmpdir, debug, render_scale, progress)
        if render is None:
            return None
        key_suffix = self._cache_key_suffix(render_scale)
        if key_suffix is None:
            result = render()
        else:
            result, from_cache = _render_result_cached(self.path, render, key_suffix=key_suffix)
            if debug and result is not None and from_cache:
                _debug_log(f"{os.path.basename(self.path)}: {self._CACHE_HIT_NOTE}")
        return self._remember_pages(result)

    def _remember_pages(self, pages: RenderResult | None) -> list[str] | None:
        """Normalize and remember whatever build_pages()'s underlying
        rendering produced - either a ("pdf", pdf_path, npages) tuple
        (FlowingText's own PDF path, or soffice - see
        _try_soffice_pages()) or a plain list of per-page PNG paths -
        into self.pages/self._pdf_delegate, and return the same
        same-length list of paths every caller of build_pages()/
        ensure_pages() (which only ever does len(pages)) already
        expects."""
        if not pages:
            return None
        if isinstance(pages, tuple):  # ("pdf", pdf_path, npages) - see RenderResult
            # A real PDF (Chrome's --print-to-pdf or soffice) - wrap it
            # as a PdfDocument delegate (see get_page_image()) and
            # normalize back to a same-length list of paths.
            _, pdf_path, npages = pages
            self._pdf_delegate = PdfDocument(pdf_path)
            paths = [pdf_path] * npages
        else:
            self._pdf_delegate = None
            paths = list(pages)
        self.pages = paths
        return paths

    def _try_soffice_pages(
        self, path: str, tmpdir: str, debug: bool, progress: _Progress,
    ) -> RenderResult | None:
        """Render `path` to a real PDF via LibreOffice's `soffice
        --convert-to pdf` (see _convert_via_soffice() for why
        --headless is never used) - the mechanism _soffice_pages_if_eligible()
        decides whether to even attempt. Preferred over the qlmanage/
        Chrome pipeline when available - soffice paginates natively
        (real page breaks/slide boundaries matching the original
        document) and needs no embedded-picture workaround (see
        _rasterize_broken_img_sources()) at all.

        A pure, uncached render - persistent caching (see
        _render_result_cached()) is handled by this method's own
        callers (_render_office_pages(), and RtfOfficeDocument.
        build_pages()'s own direct attempt against the original .rtf),
        each wrapping their *entire* qlmanage-preview-generation-and-
        all pipeline in one cache check, not just this one step -
        caching only this step would still pay for qlmanage/measuring
        on every soffice-eligible file's cache hit, for no reason (this
        step, when eligible, always pre-empts qlmanage entirely anyway).

        Returns the same ("pdf", pdf_path, npages) tuple shape
        FlowingText._build_pdf_pages() already produces, or None if
        soffice isn't installed or the conversion/page-count reading
        failed for any reason - callers always fall back to the
        existing qlmanage/Chrome pipeline in that case."""
        name = os.path.basename(path)
        progress.update(f"{name}: looking for LibreOffice...")
        soffice = find_soffice()
        if soffice is None:
            return None
        if debug:
            _debug_log(f"{name}: using soffice: {soffice}")
        label = f"{name}: converting via LibreOffice"
        with _DebugTimer(debug, label), progress.spin(label + "..."):
            out_pdf = _convert_via_soffice(soffice, path, tmpdir, debug=debug)
        if out_pdf is None:
            return None
        return _pdf_render_result(out_pdf)

    def ensure_pages(self, tmpdir: str, **kwargs: Any) -> list[str] | None:
        """Render once and reuse afterward - a no-op on every call after
        the first (see Viewer._ensure_office_pages(), which only needs
        this once per file no matter how many times it's revisited)."""
        if self.pages is None:
            self.build_pages(tmpdir, **kwargs)
        return self.pages

    def extract_text(self, page: int) -> list[str] | None:
        if self._pdf_delegate is not None:
            # A real PDF page (soffice or Chrome's --print-to-pdf) -
            # use its own per-page text (pdftotext -layout), so page
            # breaks - lost once extract_office_text()'s textutil
            # flattens the whole document into one blob - come through
            # correctly (see also text_mode_is_paginated()).
            return self._pdf_delegate.extract_text(page)
        # textutil (see extract_office_text()) has no notion of pages -
        # the whole document, or None for a format it can't handle at
        # all (a spreadsheet or slide deck).
        return extract_office_text(self.path)

    def extract_text_pages(self, npages: int) -> list[list[str]] | None:
        if self._pdf_delegate is not None:
            # One pdftotext run over the rendered PDF, not one per page.
            return self._pdf_delegate.extract_text_pages(npages)
        return super().extract_text_pages(npages)

    def supports_text_mode(self) -> bool:
        return True

    def supports_search(self) -> bool:
        # Real per-page/bbox search (build_search_index()/
        # find_search_matches() below) only works against a real PDF -
        # a screenshot-based OfficeVariant (always true for
        # Keynote/Pages/Numbers; for Excel, only without soffice; for
        # Word/RTF/PowerPoint, only when neither soffice nor Chrome's
        # --print-to-pdf could be used)
        # has no such index to search.
        return self._pdf_delegate is not None

    def text_mode_is_paginated(self) -> bool:
        return self._pdf_delegate is not None

    def build_search_index(self) -> list[dict[str, Any]] | None:
        if self._pdf_delegate is not None:
            return self._pdf_delegate.build_search_index()
        return None

    def find_search_matches(
        self, index: list[dict[str, Any]], query: str, multiline: bool = False,
    ) -> list[BBoxMatch]:
        if self._pdf_delegate is not None:
            return self._pdf_delegate.find_search_matches(index, query, multiline)
        return []

    def _source_for_page(self, cache: PageCache, page: int) -> str:
        # One pre-rendered PNG per page (see OfficeDocument._render_office_pages()) -
        # unlike ImageDocument, there's no single fixed path, so this
        # reads self.pages (kept in sync by build_pages()/
        # ensure_pages()) rather than a path fixed at construction time.
        # Only reached when self._pdf_delegate is None - get_page_image()
        # (below) forwards to it directly otherwise, without ever
        # calling this (self.pages holds a same-length placeholder list
        # in that case, not real per-page paths - see
        # _remember_pages()).
        assert self.pages is not None  # rendered before any page is shown
        return self.pages[page - 1]

    def get_page_image(self, cache: PageCache, page: int, target_px: int, fit: str) -> Image.Image:
        # A FlowingText document rendered to a real PDF instead of PNGs
        # (see _remember_pages()) - re-rasterize it the same way a
        # real PdfDocument would, at whatever DPI the current zoom
        # needs, instead of resizing one fixed-resolution screenshot
        # (the DocumentHandler default this falls back to otherwise).
        if self._pdf_delegate is not None:
            return self._pdf_delegate.get_page_image(cache, page, target_px, fit)
        return super().get_page_image(cache, page, target_px, fit)


class OfficeDocument(RenderedDocument):
    """Anything this Mac's Quick Look generators can preview (Word,
    Excel, PowerPoint, Keynote, Pages, ...) via qlmanage + a local
    Chrome - or, for Word/RTF/PowerPoint/Excel, soffice when it's installed
    (see _soffice_pages_if_eligible()). The actual rendering
    (_render_office_pages()) picks one of the ExcelWorkbook/SlideDeck/
    FlowingText OfficeVariant strategies and delegates to it; the lazy
    rendering and PDF delegation around it come from RenderedDocument."""

    # Extensions where soffice's own --convert-to pdf pagination lands
    # on the same "page" boundary the qlmanage/Chrome pipeline already
    # uses - a real Word/RTF page break, or one slide per PowerPoint
    # page (confirmed by hand: soffice's page count matches the
    # existing qlmanage/Chrome one exactly, both for a small fixture
    # and for a real 55-slide deck with hidden slides). .docm/.pptm
    # (macro-enabled Word/PowerPoint) already classify as
    # OfficeDocument via the same Office.qlgenerator that handles
    # .docx/.pptx (confirmed by hand), so they get the same treatment.
    # Excel (.xls/.xlsx/.xlsm) is exported one page per sheet (see
    # _SOFFICE_SPREADSHEET_PDF_FILTER) - the same "one full sheet per
    # page" as qlmanage's, except that the page holds the whole sheet
    # (a Quick Look preview only shows its top-left part), and it's a
    # real PDF, so it's searchable and stays sharp when zoomed. It
    # also opens workbooks qlmanage times out on. Without that option,
    # soffice paginates by print area/page setup, fragmenting one
    # sheet across several oddly-cut pages.
    _SOFFICE_EXTENSIONS = (
        ".doc", ".docx", ".docm", ".ppt", ".pptx", ".pptm", ".rtf", ".xls", ".xlsx", ".xlsm",
    )

    # The subset of _SOFFICE_EXTENSIONS that's ordinarily a ZIP archive
    # (OOXML) rather than always-OLE/CFB (.doc/.ppt) or plain text
    # (.rtf) - see is_password_protected_ooxml_or_visio(), which this
    # is paired with in sniff() to reject a password-protected one
    # upfront instead of committing to a soffice/qlmanage render that's
    # bound to fail uninformatively.
    _OOXML_ZIP_EXTENSIONS = (".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm")

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> OfficeDocument | None:
        if path.lower().endswith(cls._OOXML_ZIP_EXTENSIONS) and is_password_protected_ooxml_or_visio(path):
            raise UnusableFile("password-protected Office document, unsupported")
        return cls(path) if cls._probe_preview(path, tmpdir, debug=debug) else None

    @staticmethod
    def _generate_ql_preview(
        path: str, tmpdir: str, debug: bool = False,
    ) -> tuple[str, int | None, int | None, bool, str | None] | None:
        """Ask Quick Look for an HTML preview of `path` via
        `qlmanage -p`. Returns (html_path, width, height,
        should_not_scale, page_element_xpath) - width/height are None
        if the plist didn't have them, page_element_xpath is None if it
        has no "PageElementXPath" (meaning this document has no
        distinct page/slide elements to speak of - e.g. Word's
        continuously-flowing text) - or None if qlmanage isn't
        available, has no generator for this file, or produced
        nothing. `path` isn't necessarily self.path (see
        RtfOfficeDocument, which previews a converted .docx instead),
        so this is a staticmethod rather than reading self.path.

        With debug=True (-d/--debug), a `qlmanage` that crashed or
        exited non-zero is reported to stderr - this isn't rare: buggy
        third-party (or even Apple's own) Quick Look generators can
        crash qlmanage outright (e.g. an uncaught NSException, seen in
        the wild for some .odt files) rather than just fail to produce
        a preview, and without this it's indistinguishable from "no
        generator for this file at all", which looks identical from
        here (no Preview.html either way)."""
        if shutil.which("qlmanage") is None:
            return None
        outdir = tempfile.mkdtemp(dir=tmpdir, prefix="qlpreview-")
        name = os.path.basename(path)
        try:
            result = run_subprocess(
                ["qlmanage", "-o", outdir, "-p", path],
                capture_output=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            if debug:
                _debug_log(f"{name}: qlmanage failed to run: {e}")
            return None
        bundle = os.path.join(outdir, f"{os.path.basename(path)}.qlpreview")
        html_path = os.path.join(bundle, "Preview.html")
        if not os.path.isfile(html_path):
            if debug and result.returncode != 0:
                # A negative code means killed by that signal (e.g.
                # -6/SIGABRT for an uncaught Objective-C exception) -
                # qlmanage itself crashed, not just "no generator for
                # this file".
                how = (
                    f"crashed (signal {-result.returncode})"
                    if result.returncode < 0
                    else f"exited with status {result.returncode}"
                )
                stderr_lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
                detail = f" - {stderr_lines[0]}" if stderr_lines else ""
                _debug_log(f"{name}: qlmanage {how}{detail}")
            return None

        width = height = page_element_xpath = None
        should_not_scale = False
        plist_path = os.path.join(bundle, "PreviewProperties.plist")
        try:
            with open(plist_path, "rb") as f:
                props = plistlib.load(f)
            raw_width, raw_height = props.get("Width"), props.get("Height")
            # Chrome's --window-size silently falls back to a default
            # size if given a float (e.g. "637.0,792.0") rather than
            # plain integers - the plist's numbers are often floats, so
            # round them here.
            width = round(raw_width) if raw_width else None
            height = round(raw_height) if raw_height else None
            should_not_scale = bool(props.get("ShouldNotScale"))
            page_element_xpath = props.get("PageElementXPath") or None
        except (OSError, ValueError):
            pass
        return html_path, width, height, should_not_scale, page_element_xpath

    @staticmethod
    def _probe_preview(path: str, tmpdir: str, debug: bool = False) -> bool:
        """Cheaply check whether `path` is something this Mac's Quick
        Look generators can preview at all (Word, Excel, PowerPoint,
        Keynote, Pages, ...) - i.e. whether _render_office_pages() has
        any chance of working - without doing that method's expensive
        part (the actual Chrome rendering). Used by sniff() (main()'s
        classification, so a file that will never be looked at doesn't
        pay for a render - see Viewer._ensure_office_pages()) - `path`
        isn't necessarily what self.path will end up being (see
        RtfOfficeDocument.sniff(), which probes a converted .docx), so
        this can't be a normal instance method.

        debug=True (-d/--debug) reports a crashed/failing qlmanage -
        see _generate_ql_preview()."""
        # soffice alone is enough, without ever touching qlmanage/
        # Chrome - both macOS-only, so on Linux (no Quick Look at all)
        # this is the only way Word/PowerPoint/RTF get previewed. Safe
        # even on macOS: _render_office_pages()/build_pages() already
        # try soffice before qlmanage for these extensions (see
        # _soffice_pages_if_eligible()), so this just matches what
        # rendering would do anyway instead of paying for a redundant
        # qlmanage dry run.
        if path.lower().endswith(OfficeDocument._SOFFICE_EXTENSIONS) and find_soffice() is not None:
            return True
        if find_chrome() is None:
            return False
        return OfficeDocument._generate_ql_preview(path, tmpdir, debug=debug) is not None

    # What -d/--debug reports when build_pages() is served from the
    # persistent cache - each subclass names what that let it skip.
    _CACHE_HIT_NOTE = "reusing cached render, skipped qlmanage/soffice/Chrome"

    def _renderer(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> Callable[[], RenderResult | None]:
        """build_pages()'s actual render, as a zero-argument callable
        returning _remember_pages()'s input (or None on failure) - or
        None if there's no point even trying. Here: the qlmanage/
        soffice/Chrome pipeline, _render_office_pages()."""
        return lambda: self._render_office_pages(
            self.path, tmpdir, debug=debug, render_scale=render_scale, progress=progress,
        )

    def _cache_key_suffix(self, render_scale: float) -> str:
        """build_pages()'s persistent-cache key suffix (see
        _cached_render_dir()), or None for no persistent caching. A
        screenshot-sliced render bakes in -s/--rendering-scale's pixel
        resolution, so it's part of the key here."""
        return f":scale={render_scale}"

    def _soffice_pages_if_eligible(
        self, path: str, tmpdir: str, debug: bool, progress: _Progress, continuous: bool,
    ) -> RenderResult | None:
        """The one place that decides whether _try_soffice_pages() is
        even worth attempting for `path` - both call sites
        (_render_office_pages(), for Word/PowerPoint/Excel, and
        RtfOfficeDocument.build_pages(), for RTF) delegate here instead
        of repeating the same two checks:

        - continuous=True is never eligible: soffice always paginates
          for real (one PDF page per real page/slide), and unlike the
          qlmanage/Chrome screenshot path, there's no way to collapse
          that back into a single continuously-scrollable page.
        - the extension must be one of _SOFFICE_EXTENSIONS - see its
          comment for how Excel is exported (one page per sheet).

        Returns the ("pdf", pdf_path, npages) tuple _try_soffice_pages()
        produces, or None - either because it wasn't eligible to try at
        all, or because the attempt itself failed - so callers always
        fall through to their own qlmanage/Chrome-based rendering."""
        if continuous or not path.lower().endswith(self._SOFFICE_EXTENSIONS):
            return None
        return self._try_soffice_pages(path, tmpdir, debug, progress)

    def _measure_slide_offsets(
        self, chrome: str, html_path: str, page_element_xpath: str | None, width: int,
    ) -> tuple[float, list[float]] | None:
        """For a document whose Quick Look preview has distinct page/slide
        elements (see _generate_ql_preview()'s page_element_xpath - Word's
        continuously-flowing text has none, so this is never called for
        that), ask Chrome for the exact pixel boundary between each one, via
        a small script injected into a scratch copy of the HTML and read
        back with `--dump-dom` - real computed layout (offsetTop) rather
        than a guess. This matters because they can butt right up against
        each other with no clean gap to detect in a screenshot (e.g. a
        PowerPoint slide's drop shadow bleeding into the margin before the
        next one), so slicing by the plist's Height (one page/slide's own
        height, not counting that margin) drifts out of alignment with the
        actual boundaries after enough of them.

        Returns (total_height, [offset, ...]) in logical (unscaled) CSS
        pixels - one offset per page/slide, in document order - or None if
        page_element_xpath matched nothing, or Chrome/parsing failed.

        Note this still can't guarantee a slide's content never bleeds onto
        the next one: PowerPoint/Keynote auto-shrink text at display time so
        it fits its placeholder, but this static HTML preview doesn't
        reproduce that, so a slide relying on it can render past its box's
        bottom edge despite the box's own overflow:hidden - that's a
        limitation of the generator's HTML output itself (also visible in
        Quick Look proper), not something fixable from here.

        `width` must match the logical width the real screenshot is later
        taken at (_render_office_pages()'s own `width`): layout - and so each
        element's offsetTop - can depend on the viewport's width (e.g. a
        slide whose content is one `<img width="100%">`, scaling with the
        container), so measuring at any other width (Chrome's own headless
        default, if not given explicitly) can silently disagree with the
        boundaries the real capture ends up with, throwing off every slice
        from that point on."""
        content = _read_html(html_path)
        if content is None:
            return None

        if not page_element_xpath:
            page_element_xpath = _detect_fallback_page_xpath(content)
            if not page_element_xpath:
                return None

        # Must match the real capture's width - see the docstring. The
        # 8s budget (see _chrome_dump_title()) is a cap on how long the
        # injected script waits for every <img> to finish decoding, which
        # only matters for a deck with many/large embedded images.
        title = _chrome_dump_title(
            chrome, html_path, content, _build_slide_measure_script(page_element_xpath),
            "pdfless-slide-measure.html", width=width, timeout=30,
        )
        if title is None:
            return None
        try:
            data = json.loads(html.unescape(title))
            tops = [float(t) for t in data["tops"]]
            if not tops:
                return None
            # Sanity check: a real stack of pages/slides lays out top to
            # bottom, so each offsetTop should be strictly greater than the
            # last. Seen in the wild for one Keynote/iWork.qlgenerator
            # variant: the same shape _detect_fallback_page_xpath() looks
            # for (a <div> wrapping one full-bleed <img> per slide), but
            # with every single one reporting offsetTop 0 and a tiny total
            # scrollHeight - i.e. this generator overlaps them (probably
            # meant to be shown one at a time via some JS this static
            # capture doesn't run), not stacked in document flow at all. If
            # so, slicing by these numbers would compute zero- or negative-
            # height "pages" - return None and let the caller fall back to
            # the plain grow-and-trim path instead of acting on bogus data.
            if any(tops[i + 1] <= tops[i] for i in range(len(tops) - 1)):
                return None
            return float(data["total"]), tops
        except (ValueError, KeyError, TypeError):
            return None

    def _render_office_pages(
        self, path: str, tmpdir: str, debug: bool = False,
        render_scale: float = OFFICE_RENDER_SCALE, progress: _Progress | None = None,
        continuous: bool = False,
    ) -> RenderResult | None:
        """Try to render `path` - any file this Mac's Quick Look
        generators can preview (Word, Excel, PowerPoint, Keynote,
        Pages, ...) - into one or more page PNGs, via qlmanage + a
        local Chrome/Chromium. `path` isn't necessarily self.path -
        RtfOfficeDocument renders a converted .docx instead (see its
        build_pages()). Returns a list of PNG file paths (one per page,
        in reading order), or None if qlmanage has no generator for
        this file or no Chrome is installed. With debug=True
        (-d/--debug), prints each stage's wall-clock time (and which
        browser got used) to stderr, plus a total at the end.
        `render_scale` is the device-pixel-ratio to rasterize at
        (--rendering-scale) - higher is sharper when zoomed in but
        slower for a large document; left at its default
        (OFFICE_RENDER_SCALE), continuously-flowing text (Word and the
        like) renders at FlowingText.OFFICE_RENDER_SCALE_FLOWING instead, favoring
        sharpness since these documents are usually short. `progress`,
        if given, is a _ProgressLine to post a one-line "what's
        happening right now" status to, since this whole method can
        take anywhere from under a second to tens of seconds and would
        otherwise look like pdfless had simply hung.

        Pagination for a screenshot-sliced variant (splitting into
        distinct page/slide images, so `n`/`p`/`g`/`G` and jumping
        straight to page N work) is only used when the slide
        boundaries are known with confidence - i.e. the Quick Look
        generator's own plist named a PageElementXPath (currently true
        for PowerPoint and some Keynote decks), and measuring it
        produced sane (strictly increasing) offsets. Anything else -
        spreadsheets, and Keynote/Pages variants where the boundary can
        only be guessed via a content-shape heuristic (see
        _detect_fallback_page_xpath()) - is rendered as a single,
        continuously-scrollable "page" instead (the same as a plain
        image file), since a guessed boundary has been observed to
        drift/overflow on some real decks. FlowingText is the one
        exception to all of this: its own PDF path
        (_build_pdf_pages()) paginates for real when not continuous,
        driven by Chrome's print engine rather than any measured/
        guessed boundary - though RtfOfficeDocument's own qlmanage/
        Chrome fallback always asks for continuous=True regardless
        (see its build_pages()), since a converted RTF's page-height
        metadata doesn't correspond to anything in the original file
        either way. Word, PowerPoint and Excel (see _SOFFICE_EXTENSIONS) try
        LibreOffice's soffice before any of this (see
        _soffice_pages_if_eligible(), called at the very top of this
        method) when it's installed, since it paginates natively and
        renders with higher fidelity than either of the above; RTF
        does the same, but from its own build_pages(), against the
        original .rtf rather than a converted .docx - so it isn't
        limited to always-continuous the way the qlmanage/Chrome
        fallback is.

        continuous=True forces the single-continuous-page behavior even
        for a document that would otherwise paginate confidently - only
        RtfOfficeDocument.build_pages()'s fallback asks for it. (-c/
        --continuous is unrelated: it's the viewer's own continuous page
        view - see Viewer.continuous - which stacks the real, paginated
        pages instead.)

        Not cached itself: build_pages() wraps it (and RTF's own
        fallback around it) in one persistent-cache check, keyed on the
        original file - RTF's call here is against a throwaway converted
        .docx that nothing would ever ask for again."""
        if progress is None:
            progress = _ProgressLine(enabled=False)
        name = os.path.basename(path)

        def render() -> RenderResult | None:
            t_start = time.monotonic()
            try:
                soffice_pages = self._soffice_pages_if_eligible(path, tmpdir, debug, progress, continuous)
                if soffice_pages is not None:
                    return soffice_pages

                progress.update(f"{name}: looking for a local Chrome...")
                chrome = find_chrome()
                if chrome is None:
                    return None
                if debug:
                    _debug_log(f"{name}: using browser: {chrome}")

                progress.update(f"{name}: reading Quick Look preview...")
                with _DebugTimer(debug, f"{name}: qlmanage preview"):
                    preview = self._generate_ql_preview(path, tmpdir, debug=debug)
                if preview is None:
                    return None
                html_path, width, height, should_not_scale, page_element_xpath = preview
                width = width or self.OFFICE_DEFAULT_WIDTH
                height = height or self.OFFICE_DEFAULT_HEIGHT

                # A short, stable-per-path tag so this file's capture/page
                # PNGs don't collide with another file's (or its own
                # previous ones, e.g. across a -f/--follow reload) - unlike
                # id(path), collision odds are negligible even if a path
                # string gets reused after being freed.
                tag = hashlib.md5(path.encode("utf-8", "surrogateescape")).hexdigest()[:12]

                # Pick which OfficeVariant strategy applies - see their own
                # docstrings for what distinguishes each. should_not_scale
                # (a plist flag) is Excel's tell and is cheap to check up
                # front; the rest can only be told apart by actually
                # attempting to measure the slide/page layout.
                if should_not_scale:
                    # A spreadsheet with more than one sheet:
                    # Office.qlgenerator renders every sheet up front as its
                    # own AttachmentN.html, with Preview.html itself being
                    # just a JS tab strip that swaps an <iframe> between
                    # them - ExcelWorkbook._parse_sheet_tabs() reads that
                    # strip back out; empty for a single-sheet workbook,
                    # where Preview.html *is* the sheet.
                    variant: OfficeVariant = ExcelWorkbook(chrome, html_path, width, height, tag, name)
                else:
                    progress.update(f"{name}: converting embedded images...")
                    on_img_progress = lambda done, total: progress.update(
                        f"{name}: converting embedded images ({done}/{total})..."
                    )
                    with _DebugTimer(debug, f"{name}: converting embedded images"):
                        html_path = _rasterize_broken_img_sources(html_path, tmpdir, on_progress=on_img_progress)

                    # Confident means the Quick Look generator itself named
                    # the page/slide element (page_element_xpath came from
                    # the plist, not guessed by _detect_fallback_page_xpath
                    # inside _measure_slide_offsets) - only then is
                    # pagination trusted; see SlideDeck's docstring for why
                    # a guessed boundary defaults to continuous instead.
                    confident = bool(page_element_xpath)
                    slide_offsets = None
                    if not continuous:
                        # _measure_slide_offsets() still tries a
                        # content-shape-based fallback before giving up even
                        # when page_element_xpath is None, purely so
                        # FlowingText's "attempt 1/2/3..." growth loop can be
                        # skipped when it happens to work out - but a result
                        # obtained that way isn't "confident" (see above)
                        # and won't be used to paginate. Skipped entirely in
                        # continuous mode - nothing needs a per-page/slide
                        # boundary if there's only ever going to be one
                        # "page".
                        progress.update(f"{name}: measuring page/slide layout...")
                        with _DebugTimer(debug, f"{name}: measure slide/page offsets"):
                            slide_offsets = self._measure_slide_offsets(chrome, html_path, page_element_xpath, width)

                    if slide_offsets is not None:
                        variant = SlideDeck(chrome, html_path, width, height, tag, name, slide_offsets, confident)
                    else:
                        variant = FlowingText(chrome, html_path, width, height, tag, name)

                page_paths = variant.build_pages(tmpdir, debug, render_scale, progress, continuous)
                if page_paths is None:
                    return None
                if debug:
                    _debug_log(f"{name}: total: {time.monotonic() - t_start:.2f}s")
                return page_paths
            finally:
                progress.clear()

        return render()


class RtfOfficeDocument(OfficeDocument):
    """An RTF file, rendered as an image. Preferably via soffice
    reading the original .rtf natively (see build_pages()'s
    _soffice_pages_if_eligible() call against self.path, not a
    converted .docx) - the only path available on Linux, where
    textutil doesn't exist. Falls back to converting it to .docx via
    macOS's own textutil (_rtf_to_docx()) when soffice isn't
    installed, since RTF's own Quick Look preview is just a redirect
    back to the original file (a Preview.url), not an HTML bundle
    qlmanage/Chrome could render directly (see
    RtfDocument.is_rtf_file()). Once converted, it's handled through
    the exact same OfficeDocument._render_office_pages() pipeline as
    any other Word document (most often as a FlowingText
    OfficeVariant).

    Tried before the plain-text-only RtfDocument fallback (see
    HANDLER_CLASSES) - falls through to it if neither soffice nor
    textutil is available, or qlmanage can't preview the converted
    .docx for some reason."""

    @staticmethod
    def _rtf_to_docx(path: str, tmpdir: str) -> str | None:
        """Convert an RTF file to .docx via macOS's own `textutil`, so
        it can be handled through the same Office/Quick Look + Chrome
        pipeline as a native Word document (formatting - bold/italic/
        color/fonts, verified by hand - survives the round trip
        reasonably well; a table degrades to plain concatenated text, a
        known limitation left as-is). `path` isn't necessarily
        self.path (sniff() converts before an instance even exists), so
        this is a staticmethod.

        Returns the converted file's path, or None if textutil isn't
        available or the conversion failed. Always reconverts (rather
        than reusing a previous run's output) so a changed source file
        (e.g. -f/--follow) can't leave a stale docx behind."""
        if shutil.which("textutil") is None:
            return None
        tag = hashlib.md5(path.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        out_path = os.path.join(tmpdir, f"rtf-as-docx-{tag}.docx")
        try:
            run_subprocess(
                ["textutil", "-convert", "docx", "-output", out_path, path],
                capture_output=True, check=True, timeout=20,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
        return out_path if os.path.isfile(out_path) else None

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> RtfOfficeDocument | None:
        if not RtfDocument.is_rtf_file(path):
            return None
        if find_soffice() is not None:
            # build_pages() will try soffice against the original
            # .rtf first anyway (see _soffice_pages_if_eligible()) -
            # no need to pay for a textutil conversion just to probe
            # that here, and textutil doesn't even exist on Linux.
            return cls(path)
        docx_path = cls._rtf_to_docx(path, tmpdir)
        if docx_path is None:
            return None
        if not cls._probe_preview(docx_path, tmpdir, debug=debug):
            return None
        return cls(path)

    def _renderer(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> Callable[[], RenderResult | None]:
        """soffice against the original .rtf, or else the textutil-to-
        docx-then-qlmanage/Chrome fallback - cached by build_pages() on
        self.path (the original .rtf), never the throwaway .docx."""
        def render() -> RenderResult | None:
            # Unlike the textutil-converted-docx path below, soffice reads
            # the original .rtf natively, so its page breaks correspond to
            # the real document - no need to force continuous=True just to
            # dodge untrustworthy converted-page-height metadata (see the
            # comment below).
            soffice_pages = self._soffice_pages_if_eligible(self.path, tmpdir, debug, progress, False)
            if soffice_pages is not None:
                return soffice_pages

            docx_path = self._rtf_to_docx(self.path, tmpdir)
            if docx_path is None:
                return None
            # Always continuous - a converted RTF's page-height pagination (the plist
            # Width/Height textutil's own docx conversion reports) doesn't
            # correspond to anything in the original RTF, so it's not worth
            # trusting as a page boundary the way a native Word document's
            # is.
            return self._render_office_pages(
                docx_path, tmpdir, debug=debug, render_scale=render_scale,
                progress=progress, continuous=True,
            )

        return render


class SofficeOnlyDocument(RenderedDocument):
    """Formats macOS Quick Look has no generator for at all - an ODF
    document, a Visio drawing, or a WMF vector metafile (confirmed by
    hand: qlmanage crashes outright on a real .odt, and produces no
    preview whatsoever - not even a Preview.url - for a real .ods/
    .odp/.odg/.vsd). Previewable only when soffice is installed, with
    no qlmanage/Chrome fallback to speak of - unlike Word/RTF/
    PowerPoint (see OfficeDocument._SOFFICE_EXTENSIONS), there's
    nothing to fall back TO here; these formats were entirely
    unsupported before this class existed, so requiring soffice isn't
    a regression for anyone.

    sniff() deliberately never touches qlmanage (unlike
    OfficeDocument._probe_preview()) - both because it's known not to
    work for any of these extensions, and because it would risk
    reproducing the .odt crash above just to classify a file.

    .ods (Calc) is exported one page per sheet, the same as Excel (see
    _SOFFICE_SPREADSHEET_PDF_FILTER) - without that, a real 4-sheet
    workbook came out as 8 soffice pages, with a chart split across
    two of them.

    .vsdx hasn't been verified by hand (no local sample was
    available) - LibreOffice's Visio import filter (libvisio) handles
    both .vsd and .vsdx through the same code, so the same behavior is
    expected, but only .vsd has actually been confirmed to render
    correctly."""

    _SOFFICE_ONLY_EXTENSIONS = (".odt", ".odp", ".odg", ".ods", ".vsd", ".vsdx", ".wmf")

    # .vsdx is the one _SOFFICE_ONLY_EXTENSIONS format that's ordinarily
    # a ZIP archive (OOXML) rather than always-OLE/CFB (.vsd, .wmf) or
    # still a ZIP even when password-protected (.odt/.odp/.odg/.ods -
    # ODF encryption keeps the outer container as ZIP, encrypting each
    # entry inside it instead) - see is_password_protected_ooxml_or_visio().
    _OOXML_ZIP_EXTENSIONS = (".vsdx",)

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> SofficeOnlyDocument | None:
        if not path.lower().endswith(cls._SOFFICE_ONLY_EXTENSIONS):
            return None
        if path.lower().endswith(cls._OOXML_ZIP_EXTENSIONS) and is_password_protected_ooxml_or_visio(path):
            raise UnusableFile("password-protected Office document, unsupported")
        if find_soffice() is None:
            return None
        return cls(path)

    _CACHE_HIT_NOTE = "reusing cached PDF, skipped LibreOffice"

    def _renderer(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> Callable[[], RenderResult | None]:
        return lambda: self._try_soffice_pages(self.path, tmpdir, debug, progress)

    def _cache_key_suffix(self, render_scale: float) -> str:
        return ""  # a real PDF, re-rasterized at any scale on demand


class SvgDocument(RenderedDocument):
    """A standalone SVG file, rendered to a real PDF via headless
    Chrome directly - no Quick Look or LibreOffice involved at all.
    Quick Look's own preview for an SVG is just a Preview.url redirect
    back to the file (confirmed by hand - the same dead end WMF hits;
    see SofficeOnlyDocument), not a proper HTML bundle to build on, so
    there's nothing to reuse from the qlmanage/Chrome pipeline here.
    Chrome was chosen over soffice's Draw import (both were compared
    by hand on a complex real SVG - embedded raster photos plus vector
    line art - and came out visually equivalent) since Chrome is
    already a hard requirement for every other Office kind, so this
    doesn't add a new soffice dependency just to view an SVG.

    The SVG is embedded via a plain <img>, which is what lets
    _capture_html_pdf()'s existing @page-injection machinery work
    unmodified (an SVG file has no <head>/<body> of its own for that
    to target safely) - confirmed by hand that the vector parts of a
    complex SVG survive print-to-pdf as real vector PDF content, not
    a flattened bitmap (checked via pdfimages -list: only the source
    SVG's own embedded raster images showed up there, nothing for the
    vector line art). The one real cost of this approach: <img> always
    strips interactivity, so a hyperlink inside the SVG itself can't
    be clickable here the way one in a Word document is."""

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> SvgDocument | None:
        if not path.lower().endswith(".svg"):
            return None
        if find_chrome() is None:
            return None
        return cls(path)

    _CACHE_HIT_NOTE = "reusing cached PDF, skipped rendering"

    def _cache_key_suffix(self, render_scale: float) -> str:
        return ""  # a real PDF, re-rasterized at any scale on demand

    def _renderer(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> Callable[[], RenderResult | None] | None:
        name = os.path.basename(self.path)
        chrome = find_chrome()
        if chrome is None:
            return None
        if debug:
            _debug_log(f"{name}: using browser: {chrome}")
        tag = hashlib.md5(self.path.encode("utf-8", "surrogateescape")).hexdigest()[:12]
        wrapper_path = os.path.join(tmpdir, f"svg-wrap-{tag}.html")

        def render() -> RenderResult | None:
            # There's no plist (unlike a Quick Look preview) to read a
            # width from up front, and an SVG's own intrinsic size varies
            # in both dimensions - so the natural size has to be measured
            # first, the same "auto" page sizing isn't supported reasoning
            # as _measure_content_height() (see _measure_svg_natural_size()).
            progress.update(f"{name}: measuring size...")
            self._write_svg_wrapper(wrapper_path, self.path, None, None)
            with _DebugTimer(debug, f"{name}: measure SVG size"):
                size = _measure_svg_natural_size(chrome, wrapper_path)
            width, height = size if size else (self.OFFICE_DEFAULT_WIDTH, self.OFFICE_DEFAULT_HEIGHT)
            self._write_svg_wrapper(wrapper_path, self.path, width, height)

            out_pdf = os.path.join(tmpdir, f"svg-capture-{tag}.pdf")
            label = f"{name}: rendering to PDF"
            with _DebugTimer(debug, label), progress.spin(label + "..."):
                ok = _capture_html_pdf(chrome, wrapper_path, width, height, out_pdf)
            return _pdf_render_result(out_pdf, ok)

        return render

    @staticmethod
    def _write_svg_wrapper(
        wrapper_path: str, svg_path: str, width: int | None, height: int | None,
    ) -> None:
        """A minimal HTML page embedding `svg_path` as a plain <img> -
        see this class's docstring for why <img> rather than
        navigating to the SVG directly. `width`/`height` (CSS px), if
        given, are set explicitly so the rendered page size matches
        exactly what _capture_html_pdf()'s own @page injection expects
        - left unset (None) for the first, size-finding pass (see
        build_pages()), which needs the image at its own natural size
        instead."""
        style = f"width:{width}px;height:{height}px;" if width and height else ""
        page = (
            "<!DOCTYPE html><html><head></head><body style=\"margin:0\">"
            f'<img id="svg" style="display:block;{style}" '
            f'src="{html.escape(_file_url(svg_path))}"></body></html>'
        )
        with open(wrapper_path, "w", encoding="utf-8") as f:
            f.write(page)


# A fenced code block's opening/closing line (``` or ~~~), an ATX
# heading ("## Title"), and a setext heading's underline ("===" or
# "---" under a line of text) - see markdown_heading_lines().
_MD_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_MD_ATX_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
_MD_SETEXT_UNDERLINE_RE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")


def markdown_heading_lines(lines: Sequence[str]) -> list[int]:
    """The lines of a Markdown source that are headings, in order - the
    ones WeasyPrint turns into bookmarks, one per heading, so the n-th of
    these is the n-th entry of a rendered Markdown file's table of
    contents.

    Args:
        lines: the source, one line per item.

    Returns:
        Indices into `lines`: an ATX heading's own line, or a setext
        heading's text line (the one above its underline). Lines inside a
        fenced code block don't count (a "# comment" in a shell snippet
        is no heading), and nor does one indented four spaces or more
        (an indented code block's).
    """
    headings: list[int] = []
    fence: str | None = None  # the open fence's ``` or ~~~, while inside one
    paragraph = False  # whether the previous line is paragraph text
    for i, line in enumerate(lines):
        m = _MD_FENCE_RE.match(line)
        if fence is not None:
            # Only a fence of the same character, at least as long,
            # closes it.
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence, paragraph = m.group(1), False
        elif _MD_ATX_HEADING_RE.match(line):
            headings.append(i)
            paragraph = False
        elif paragraph and _MD_SETEXT_UNDERLINE_RE.match(line):
            headings.append(i - 1)
            paragraph = False
        elif not line.strip():
            paragraph = False  # a blank line ends a paragraph
        elif not paragraph and line.startswith("    "):
            # An indented code block's line (indenting only continues a
            # paragraph already under way) - no setext underline after it.
            pass
        else:
            paragraph = True
    return headings


class MarkdownDocument(_RawTextView, RenderedDocument):
    """A Markdown file, rendered to a real PDF via the `markdown` +
    `weasyprint` Python libraries (see _render_markdown_pdf() below) - no
    Quick Look, Chrome, or LibreOffice involved at all, and
    dramatically faster than either (confirmed by hand: well under a
    second, against Chrome's own ~1-2s process startup alone), since
    WeasyPrint paginates for real on its own rather than needing a
    measured/injected @page size the way FlowingText/SvgDocument do.

    Falls back to plain text (TextDocument, showing the raw Markdown
    source) if `markdown`/`weasyprint` - or the system libraries
    WeasyPrint itself needs (Cairo/Pango/GLib, not something pip can
    install on its own) - aren't available; see sniff().

    Text mode (`t`) always shows the raw Markdown source from disk -
    not text extracted from the rendered PDF - so you can read or
    search the `#`/`*` markup while keeping the WeasyPrint preview in
    image mode. text_mode_is_paginated() is False because that source
    is one continuous blob in text mode; image-mode / search still
    uses the PDF delegate's own per-page bbox index (see
    _Search.uses_text_lines())."""

    _MARKDOWN_EXTENSIONS = (".md", ".markdown")
    _legacy_encodings = False  # read as UTF-8 only - see _RawTextView

    # Minimal styling for the rendered pages - just enough that
    # headings/code/quotes are visually distinct, deliberately not
    # trying to imitate any particular Markdown renderer's house style.
    # No @font-face/font-family override: left to whatever WeasyPrint
    # picks as the system default, so CJK text (which needs a real CJK
    # font) renders using whatever's actually installed rather than a
    # Latin-only font silently dropping every Japanese glyph.
    # `code` in a table cell is an inline-block: WeasyPrint sizes a
    # column as if a cell like "`.doc`, `.docx`, `.docm`" could wrap
    # between the code spans, but then can't break a line there, so the
    # cell ran on into the next column (confirmed with WeasyPrint 70 and
    # this README's format table). As atomic boxes, the spans wrap
    # between one another as the column width expects.
    MARKDOWN_CSS = """
body { line-height: 1.5; padding: 2em; }
h1, h2, h3, h4, h5, h6 { line-height: 1.2; margin-top: 1em; }
pre, code { font-family: monospace; background: #f0f0f0; }
pre { padding: 0.6em; white-space: pre-wrap; }
code { padding: 0.1em 0.3em; }
pre code { padding: 0; background: none; }
blockquote { border-left: 4px solid #ccc; margin-left: 0; padding-left: 1em; color: #555; }
table { border-collapse: collapse; max-width: 100%; }
th, td { border: 1px solid #ccc; padding: 0.3em 0.6em; }
th code, td code { display: inline-block; }
img { max-width: 100%; height: auto; }
"""

    @staticmethod
    def _ensure_homebrew_lib_path_for_weasyprint() -> None:
        """WeasyPrint (via cffi) dlopen()s Cairo/Pango/GLib by their bare
        library names, relying on the dynamic linker's own default search
        to find them. Confirmed by hand: that default search does NOT
        include Homebrew's own lib directory when running under a
        uv-managed standalone Python build (e.g. a free-threaded 3.14
        install) - even with those exact libraries installed via Homebrew
        - while the same import succeeds unmodified under a
        Homebrew-installed Python on the same machine. Rather than
        requiring every user hitting this to discover and set
        DYLD_LIBRARY_PATH by hand (WeasyPrint's own macOS troubleshooting
        docs suggest exactly that), add Homebrew's lib directory to
        DYLD_FALLBACK_LIBRARY_PATH before the first import attempt -
        confirmed by hand this alone is enough to fix the failing case.
        A *fallback* path (rather than DYLD_LIBRARY_PATH, which is
        consulted first) can't ever shadow a library some other search
        step already finds correctly, so this is safe to always do.
        macOS-only; a no-op everywhere else."""
        if sys.platform != "darwin":
            return
        existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
        parts = existing.split(":") if existing else []
        for lib_dir in ("/opt/homebrew/lib", "/usr/local/lib"):
            if os.path.isdir(lib_dir) and lib_dir not in parts:
                parts.append(lib_dir)
        if parts:
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(parts)

    @staticmethod
    def _markdown_rendering_available() -> bool:
        """Whether MarkdownDocument can even attempt to render at all -
        both the `markdown` and `weasyprint` libraries import cleanly.
        Cheap to call more than once: a successful import is cached by
        Python itself (sys.modules), so only the first call actually pays
        for it. See _render_markdown_pdf() for why this has to be a
        runtime check rather than something assumed from the PEP723
        dependency list.

        Deliberately catches more than just ImportError: weasyprint's own
        import chain reaches into cffi to dlopen() the actual Cairo/Pango/
        GLib shared libraries, and a missing one there raises a plain
        OSError, not an ImportError (confirmed by hand: "cannot load
        library 'libgobject-2.0-0'" on a machine without those system
        libraries installed) - anything going wrong at import time means
        the same thing here (rendering isn't available), so it's all
        caught the same way rather than letting a fairly common
        installation gap crash the whole program on startup.

        Also deliberately swallows whatever weasyprint prints along the
        way: on that same missing-libraries path, it print()s its own
        multi-line "could not import some external libraries" notice
        before the OSError above even reaches here (confirmed by hand).
        Since this whole function's job is to fail silently and let the
        caller fall back to plain text, letting that notice through would
        defeat the point - and pdfless spends most of its life with the
        terminal in raw mode showing an alternate screen, where a stray
        print from a library is a corrupted-looking screen, not just
        unwanted noise."""
        MarkdownDocument._ensure_homebrew_lib_path_for_weasyprint()
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                import markdown  # noqa: F401
                import weasyprint  # noqa: F401
        except Exception:
            return False
        return True

    def _render_markdown_pdf(self, out_pdf: str) -> bool:
        """Convert self.path (a Markdown file) to a real PDF via the
        `markdown` (Markdown -> HTML) and `weasyprint` (HTML+CSS -> PDF)
        libraries - no headless Chrome or LibreOffice involved at all, and
        dramatically faster than either (confirmed by hand: a whole
        conversion takes well under a second, against Chrome's own ~1-2s
        process startup alone), since WeasyPrint has a real CSS
        pagination engine of its own - no need for FlowingText/
        SvgDocument's own "measure the content first, then set an exact
        @page size" dance; a long document just comes out as however many
        pages it naturally takes.

        Both libraries are declared PEP723 dependencies (so `uv run`
        always installs the pip packages), but weasyprint also needs
        system-level Cairo/Pango/GLib libraries pip can't install by
        itself - so the import happens lazily, here (via
        _markdown_rendering_available()), rather than at module load time,
        and any failure is treated the same as "not installed": returns
        False, and the caller falls back to plain-text rendering, the
        same graceful degradation soffice/Chrome already get when they're
        missing.

        Returns False on any failure (including a missing library) - never
        raises, so this is always safe for a caller to attempt speculatively."""
        if not self._markdown_rendering_available():
            return False
        import markdown
        from weasyprint import HTML

        try:
            source = read_text_file(self.path, legacy=False)  # as text mode reads it
        except OSError:
            return False
        body = markdown.markdown(source, extensions=["extra", "sane_lists"])
        html = f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{self.MARKDOWN_CSS}</style></head><body>{body}</body></html>'
        # base_url lets a relative-path image reference in the source
        # (e.g. "![alt](./diagram.png)") resolve against the Markdown
        # file's own directory, the same as a browser would for a page
        # loaded from there.
        base_url = os.path.dirname(os.path.abspath(self.path)) + "/"
        try:
            HTML(string=html, base_url=base_url).write_pdf(out_pdf)
        except Exception:
            # WeasyPrint can raise a variety of its own exception types for
            # a malformed document/CSS - none of them worth the whole
            # program aborting over; the caller's error-placeholder
            # fallback handles it the same as any other rendering failure.
            return False
        return os.path.exists(out_pdf)

    @classmethod
    def sniff(cls, path: str, tmpdir: str, debug: bool = False) -> MarkdownDocument | None:
        if not path.lower().endswith(cls._MARKDOWN_EXTENSIONS):
            return None
        if not cls._markdown_rendering_available():
            return None
        return cls(path)

    def _cache_key_suffix(self, render_scale: float) -> str | None:
        # No persistent cache: WeasyPrint renders a Markdown file in
        # well under a second (see the class docstring), fast enough
        # that caching it isn't worth the disk space.
        return None

    def _renderer(
        self, tmpdir: str, debug: bool, render_scale: float, progress: _Progress,
    ) -> Callable[[], RenderResult | None]:
        def render() -> RenderResult | None:
            name = os.path.basename(self.path)
            tag = hashlib.md5(self.path.encode("utf-8", "surrogateescape")).hexdigest()[:12]
            out_pdf = os.path.join(tmpdir, f"markdown-capture-{tag}.pdf")
            label = f"{name}: rendering to PDF"
            with _DebugTimer(debug, label), progress.spin(label + "..."):
                ok = self._render_markdown_pdf(out_pdf)
            return _pdf_render_result(out_pdf, ok)

        return render

    def search_resets_on_text_mode_toggle(self) -> bool:
        return True

    def heading_lines(self) -> list[int] | None:
        """Text mode shows the raw source, whose headings are the
        bookmarks WeasyPrint made - see markdown_heading_lines()."""
        return markdown_heading_lines(self.extract_text(1))


# The order main()'s classification loop tries these in - RtfOfficeDocument
# and RtfDocument (both RTF - the former tried first, see their own
# docstrings), SvgDocument, and MarkdownDocument before the generic
# TextDocument, since all would otherwise match the same file (RTF,
# SVG, and Markdown are all plain text - SvgDocument/MarkdownDocument
# fall through to TextDocument's raw-source rendering when their own
# dependency isn't installed, the same way RtfOfficeDocument falls
# through to RtfDocument without textutil); SofficeOnlyDocument before
# OfficeDocument since it covers formats OfficeDocument's own qlmanage
# probe can't handle at all; OfficeDocument last since it's the most
# expensive check (shells out to qlmanage).
HANDLER_CLASSES: list[type[DocumentHandler]] = [
    PdfDocument, ImageDocument, RtfOfficeDocument, RtfDocument, SvgDocument,
    MarkdownDocument, TextDocument, SofficeOnlyDocument, OfficeDocument,
]


def _sniff_file(path: str, tmpdir: str, debug: bool = False) -> DocumentHandler | None:
    """The DocumentHandler for `path` - the first one (in HANDLER_CLASSES
    order) whose sniff() claims it - or None if none do. Raises
    UnusableFile if one positively identified the format but couldn't
    actually use it (e.g. a corrupt PDF).

    Shared by main()'s own upfront search for the first file it can
    actually display, and Viewer._classify()'s later, lazy sniff of
    every other file (only attempted the moment you actually navigate to
    it - see there) - callers differ in how they report a failure (a
    detailed reason to stderr up front vs. a short status-line message
    once already interactive), not in how the sniffing itself works."""
    for handler_cls in HANDLER_CLASSES:
        handler = handler_cls.sniff(path, tmpdir, debug=debug)
        if handler is not None:
            return handler
    return None


_CTRL_C_FD = None  # the interactive session's tty fd while RawTerminal is
# active, otherwise None - see run_subprocess(), which uses this to
# temporarily restore ISIG (which raw mode turns off - it would
# otherwise deliver ^C as a literal, unread byte sitting in the tty's
# input buffer until whatever blocking external-tool call is
# in progress returns on its own) for the duration of that call.


class RawTerminal:
    """Puts the tty into raw (cbreak-ish) mode for the duration of the block."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.old: list[Any] = []  # the tty's own settings, from __enter__()

    def __enter__(self) -> RawTerminal:
        global _CTRL_C_FD
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        _CTRL_C_FD = self.fd
        return self

    def __exit__(self, *exc: object) -> None:
        global _CTRL_C_FD
        _CTRL_C_FD = None
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


# termios.tcgetattr()'s return value is a fixed-shape list with no named
# accessors in this Python version - [iflag, oflag, cflag, lflag,
# ispeed, ospeed, cc] - tty.LFLAG/tty.CC spell these out, but only since
# Python 3.12 (pdfless supports 3.9+, see its shebang), hence these.
_TC_LFLAG, _TC_CC = 3, 6


def _file_url(path: str) -> str:
    """A file:// URL for `path`, percent-encoded - a file name with "#",
    "?" or "%" in it (they go into the Quick Look preview's own path, see
    OfficeDocument._generate_ql_preview()) otherwise reads to Chrome as
    a fragment or a query, and the page doesn't load at all.

    Args:
        path: the file, relative or absolute.

    Returns:
        The URL.
    """
    return pathlib.Path(os.path.abspath(path)).as_uri()


def run_subprocess(
    args: list[str], *, timeout: float | None = None, **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Drop-in replacement for subprocess.run(), used for every external
    tool pdfless shells out to (qlmanage, soffice, Chrome, textutil,
    poppler's pdftoppm/pdfinfo/pdftocairo, ...) so a long one can be
    interrupted with ^C instead of running to completion no matter what
    - see _CTRL_C_FD's comment for why raw mode otherwise defeats that.

    While the interactive viewer's RawTerminal is active (_CTRL_C_FD
    set), this restores ISIG on that tty for the call's duration, so
    the terminal driver itself turns ^C into a real SIGINT - delivered
    to pdfless *and* the child (they share a process group; none of
    these calls detach into their own - see main()'s one exception,
    the Popen() that opens a file in an external app), interrupting
    whichever of them was still blocked in a syscall. Python's default
    SIGINT handler then raises KeyboardInterrupt here, which every
    caller up the stack (OfficeDocument.build_pages() and friends) lets
    propagate rather than swallowing alongside subprocess.TimeoutExpired/
    OSError - it isn't a subclass of Exception, so their existing
    `except (subprocess.TimeoutExpired, OSError):` clauses already
    don't catch it - all the way up to run_viewer()'s main loop, which
    quits the same way a plain "\x03" typed between renders already
    does.

    Before RawTerminal is ever entered (e.g. the -h capability probe at
    import time, or classifying files in main()) or after it exits,
    _CTRL_C_FD is None and this behaves exactly like subprocess.run() -
    the tty is still in its normal cooked mode there anyway, where ^C
    already generates SIGINT on its own.

    ISIG governs ^C/^\\/^Z's signal generation as one bundle - it can't
    enable just ^C - so VQUIT and VSUSP are pinned to VDISABLE for the
    same duration, leaving them literal, unread bytes exactly like
    today (^Z already has its own hand-rolled suspend - see
    run_viewer()'s "\\x1a" handling - keyed off actually reading that
    byte back in the main loop; a real SIGTSTP firing here instead
    would suspend the process without it ever running, skipping the
    terminal cleanup that handling does first)."""
    fd = _CTRL_C_FD
    if fd is None or threading.current_thread() is not threading.main_thread():
        # Off the main thread (Viewer's background prefetch - see
        # _schedule_prefetch()) the tty's settings are the main thread's
        # to juggle, not this one's: two threads saving/restoring them
        # around overlapping calls could leave either with the other's.
        # ^C still stops a background child - it's in the same process
        # group as the main thread's own, whenever that one has ISIG on.
        return subprocess.run(args, timeout=timeout, **kwargs)
    vdisable = os.fpathconf(fd, "PC_VDISABLE")
    attrs = termios.tcgetattr(fd)
    old_cc = attrs[_TC_CC][termios.VQUIT], attrs[_TC_CC][termios.VSUSP]
    attrs[_TC_LFLAG] |= termios.ISIG
    attrs[_TC_CC][termios.VQUIT] = attrs[_TC_CC][termios.VSUSP] = vdisable
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    try:
        return subprocess.run(args, timeout=timeout, **kwargs)
    finally:
        attrs[_TC_LFLAG] &= ~termios.ISIG
        attrs[_TC_CC][termios.VQUIT], attrs[_TC_CC][termios.VSUSP] = old_cc
        termios.tcsetattr(fd, termios.TCSANOW, attrs)


def read_utf8_char(fd: int, timeout: float = 0.1) -> str | None:
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
    "3": "DEL",
    "5": "PAGEUP",
    "6": "PAGEDOWN",
    "11": "F1",  # terminals that send F1 as a CSI; see SS3_FINAL_LETTERS
    # for the ESC O P form most of them use instead
}
SS3_FINAL_LETTERS = {"P": "F1"}


def read_csi_sequence(fd: int, timeout: float = 0.15) -> str | None:
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


def read_ss3_key(fd: int, timeout: float = 0.15) -> str | None:
    """Read the single byte following ESC O (SS3) and turn it into a
    symbolic key name - F1 is the only one pdfless has any use for, and
    most terminals send it as ESC O P. Returns None on timeout/EOF, or
    for anything not in SS3_FINAL_LETTERS (the other function keys, and
    the arrows as a terminal in application-cursor mode sends them -
    pdfless never turns that mode on, so its arrows arrive as CSI)."""
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return None
    b = os.read(fd, 1)
    if not b:
        return None
    return SS3_FINAL_LETTERS.get(b.decode("ascii", errors="ignore"))


def decode_csi_key(seq: str | None) -> str | None:
    """Turn a CSI sequence body like "A", "1;2A" or "5~" into a symbolic
    key name such as "UP" or "SHIFT-LEFT", or None if unrecognized."""
    if not seq:
        return None
    if seq == "I":
        return "FOCUS_IN"
    if seq == "O":
        return "FOCUS_OUT"
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


def decode_sgr_mouse(seq: str) -> tuple[str, int, int] | None:
    """Parse an SGR mouse-reporting sequence body (after ESC [), e.g.
    "<0;42;17M" - a left-button press at column 42, row 17 (both
    1-based, in terminal cells). Returns (kind, col, row) where kind is
    "MOUSE_CLICK", "MOUSE_DRAG"/"MOUSE_RELEASE" (left button held and
    moved, then let go - see MOUSE_ON's 1002),
    "MOUSE_WHEEL_UP"/"MOUSE_WHEEL_DOWN", or "MOUSE_BACK"/"MOUSE_FORWARD"
    (a mouse's side buttons, where it has them - button 8/9 in the xterm
    protocol). Returns None for anything else (a button other than the
    left one, a malformed sequence) - not a recognized action here, so
    ignored."""
    if not seq.startswith("<") or seq[-1] not in "Mm":
        return None
    body, final = seq[1:-1], seq[-1]
    parts = body.split(";")
    if len(parts) != 3:
        return None
    try:
        cb, cx, cy = (int(p) for p in parts)
    except ValueError:
        return None
    if cb & 0x80:
        # Side buttons 8-11: bit 7 (0x80) marks the group, low 2 bits
        # pick which one - button 8 (back) and 9 (forward) are the
        # common "browser navigation" buttons; 10/11 aren't mapped to
        # anything here. Reported as a press/release pair like the
        # ordinary buttons, so only act on the press.
        if final != "M":
            return None
        offset = cb & 0x03
        if offset == 0:
            return "MOUSE_BACK", cx, cy
        if offset == 1:
            return "MOUSE_FORWARD", cx, cy
        return None
    if cb & 0x40:
        # The scroll wheel: bit 6 (0x40) marks it, and bit 0 then tells
        # up from down. Always reported as a "press" (final "M"), never
        # a release.
        if final != "M":
            return None
        return ("MOUSE_WHEEL_DOWN" if cb & 1 else "MOUSE_WHEEL_UP"), cx, cy
    if (cb & 3) != 0:
        return None  # a button other than the left one
    if final == "m":
        # A release ends a drag (see _Mouse.end_drag()); the
        # motion bit may or may not still be set on it, so don't look.
        return "MOUSE_RELEASE", cx, cy
    if cb & 0x20:
        return "MOUSE_DRAG", cx, cy  # bit 5 (0x20) is drag/motion
    return "MOUSE_CLICK", cx, cy


def get_term_cells(fd: int) -> tuple[int, int, int, int]:
    packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, xpix, ypix = struct.unpack("HHHH", packed)
    return rows, cols, xpix, ypix


def _prompt_pdf_password(filename: str, message: str | None = None) -> str | None:
    """Ask for `filename`'s PDF password, interactively - via a plain
    getpass() prompt if the interactive viewer's raw terminal mode
    hasn't been entered yet (_CTRL_C_FD unset - see RawTerminal), or,
    once it has (encountering a still-locked encrypted PDF while
    already paging - see PdfDocument._ensure_unlocked()), via a masked
    prompt drawn directly on the status line instead, since getpass()
    would otherwise fight over the tty's raw/echo settings. `message`
    (e.g. "incorrect password, try again") is shown alongside a retry.
    Returns the entered password, or None if the user cancelled
    (Esc/^C/^D in raw mode; ^C/^D or a blank line under getpass())."""
    if _CTRL_C_FD is None:
        return _prompt_pdf_password_cooked(filename, message)
    return _prompt_pdf_password_raw(_CTRL_C_FD, filename, message)


def _prompt_pdf_password_cooked(filename: str, message: str | None = None) -> str | None:
    if message:
        print(f"pdfless: {message}", file=sys.stderr)
    try:
        password = getpass.getpass(f"Password for {filename}: ")
    except (EOFError, KeyboardInterrupt):
        return None
    return password or None


def _prompt_pdf_password_raw(fd: int, filename: str, message: str | None = None) -> str | None:
    """The raw-mode half of _prompt_pdf_password() - reads its own
    small, self-contained loop of keypresses directly from `fd`
    (select()+read_utf8_char(), the same pattern run_viewer()'s main
    loop uses) rather than going through that loop's own key
    dispatch, since this is only ever needed the moment a
    just-navigated-to file (see Viewer.go_to_file()) turns out to be
    an encrypted PDF - a one-off, modal prompt, not a new steady-state
    mode that loop itself would need to know about."""
    rows, cols, _, _ = get_term_cells(fd)
    buf = ""

    def redraw() -> None:
        prefix = f"{message} - " if message else ""
        text = truncate_to_width(f"{prefix}Password for {filename}: " + "*" * len(buf), cols)
        col = min(cols, 1 + display_width(text))
        sys.stdout.write(
            f"\x1b[{rows};1H{STATUS_COLOR_ON}\x1b[2K{text}"
            f"\x1b[{rows};{col}H\x1b[?25h"
        )
        sys.stdout.flush()

    redraw()
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.3)
            if not r:
                continue
            ch = read_utf8_char(fd)
            if ch is None:
                return None
            if ch in ("\r", "\n"):
                return buf or None
            if ch in ("\x1b", "\x03", "\x04"):  # Esc, ^C, ^D
                return None
            if ch in ("\x7f", "\x08"):  # Backspace
                buf = buf[:-1]
            elif ch.isprintable():
                buf += ch
            else:
                continue
            redraw()
    finally:
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()


def query_pixel_size_osc(fd: int, timeout: float = 0.5) -> tuple[int, int] | None:
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


def get_pixel_size(fd: int) -> tuple[int, int]:
    """Best-effort terminal text-area size in pixels: (width_px, height_px)."""
    global _warned_fallback
    rows, cols, xpix, ypix = get_term_cells(fd)
    if xpix and ypix:
        return xpix, ypix

    got = query_pixel_size_osc(fd)
    if got is not None:
        return got

    if not _warned_fallback:
        # end="\r\n": this can print from inside the viewer's raw-mode
        # terminal (get_pixel_size() is called on every resize), where a
        # bare "\n" wouldn't return the cursor to column 1.
        print(
            "pdfless: could not determine terminal pixel size; falling back "
            "to a rough estimate (image sharpness/fit may be off)",
            file=sys.stderr, end="\r\n",
        )
        _warned_fallback = True

    # Last resort: guess a plausible cell size.
    guess_w, guess_h = 8, 17
    return cols * guess_w, rows * guess_h


def wrap_for_tmux(osc: str) -> str:
    if not os.environ.get("TMUX"):
        return osc
    escaped = osc.replace("\x1b", "\x1b\x1b")
    return f"\x1bPtmux;{escaped}\x1b\\"


def iterm2_like() -> bool:
    """True when running under iTerm2, WezTerm, or a close OSC-1337
    clone - terminals that support the OSC 1337 inline image protocol
    well enough to accept a JPEG-encoded payload, not just PNG."""
    if os.environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm"):
        return True
    if os.environ.get("ITERM_SESSION_ID") or os.environ.get("WEZTERM_PANE"):
        return True
    return "iterm" in os.environ.get("LC_TERMINAL", "").lower()


def _inline_image(data: bytes, width: int, height: int) -> str:
    """The escape sequence that draws an encoded image (JPEG/PNG) at the
    cursor, `width` x `height` pixels, leaving the cursor where it is -
    iTerm2's inline-image protocol (see iterm2_like()), wrapped for tmux.

    Args:
        data: the encoded image.
        width, height: its size in pixels.

    Returns:
        The escape sequence.
    """
    b64 = base64.b64encode(data).decode("ascii")
    return wrap_for_tmux(
        f"\x1b]1337;File=inline=1;doNotMoveCursor=1;size={len(data)};"
        f"width={width}px;height={height}px;preserveAspectRatio=0:{b64}\x07"
    )


def _format_ech_clear(char_w: int, char_h: int) -> str:
    """Erase `char_w` x `char_h` cells at the home position (ECH/CUU)."""
    out = ["\x1b[H"]
    for i in range(char_h):
        out.append(f"\x1b[{char_w}X")
        if i < char_h - 1:
            out.append("\x1b[1B")
    if char_h > 0:
        out.append(f"\x1b[{char_h}A")
    return "".join(out)


def _strip_leading_home(s: str) -> str:
    home = "\x1b[H"
    if s.startswith(home):
        return s[len(home):]
    return s


def _image_bytes(img: Image.Image) -> int:
    """Roughly how much memory `img`'s pixels take - one byte per band
    per pixel, true of the RGB/RGBA/L images PageCache holds."""
    return img.width * img.height * len(img.getbands())


class PageCache:
    """Caches rasterized/resized page images, keyed by (page, size) -
    the actual per-kind work (rasterize a PDF page at some DPI, resize
    a pre-rendered image/office PNG) is delegated to `handler` (the
    same DocumentHandler instance Viewer itself uses - see
    Viewer._set_current_file() - which for an OfficeDocument owns its
    own rendered page list, self.pages), which reaches back into this
    cache's own bookkeeping (_cached()/_store(), and tmpdir) since that
    bookkeeping - LRU eviction, the "loaded once natively" table - is
    shared machinery rather than any one kind's own concern.

    Safe to use from two threads at once - the main thread drawing, and
    Viewer's background render of a neighboring page (see
    Viewer._schedule_page_prefetch()): the same request (page, target_px,
    fit) is only ever rendered once at a time, a second asker waiting
    for the first instead (see get())."""

    def __init__(self, tmpdir: str, handler: DocumentHandler, size: int = CACHE_SIZE) -> None:
        self.tmpdir = tmpdir
        self.size = size
        self._cache: OrderedDict[tuple[Any, ...], Image.Image] = OrderedDict()  # (page, dpi) or (page, px, fit) -> PIL.Image
        self._native_images: dict[int, Image.Image] = {}  # page -> PIL.Image, loaded once each - see
        # DocumentHandler._native_page_image(): for kind == "image"
        # there's only ever page 1, but kind == "office" has one source
        # file per pre-rendered page (handler.pages).
        self.handler = handler
        # Guards every structure below and above - held only for
        # bookkeeping, never across an actual render.
        self._lock = threading.RLock()
        # Requests being rendered right now, each with an Event set when
        # that render ends (see get()).
        self._in_flight: dict[PageRequest, threading.Event] = {}
        # Which image each request last produced - the key a request maps
        # to is the handler's own business (a PDF's depends on the page's
        # size in points, say), so has() looks the image itself up among
        # the cached ones instead.
        self._served: dict[PageRequest, Image.Image] = {}
        # Bumped by clear(): a render that started before it (a
        # background one, finishing after a reload) mustn't put its
        # now-stale image into the emptied cache - see _store().
        self._generation = 0
        self._render_generation = threading.local()

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._cache.clear()
            self._served.clear()
            self._native_images.clear()  # re-read the file(s), e.g. for -f/--follow

    def get(self, page: int, target_px: int, fit: str = 'width') -> Image.Image:
        """The PDF page, or a pre-rendered page (a plain image file for
        kind=="image", or one of the pre-sliced Quick Look preview PNGs
        for kind=="office"), scaled so it's `target_px` wide (fit="width")
        or tall (fit="height").

        If another thread is rendering this very request right now, this
        waits for it and then (normally) finds its result in the cache,
        rather than running a second pdftoppm alongside for the same
        page."""
        request: PageRequest = (page, target_px, fit)
        while True:
            with self._lock:
                pending = self._in_flight.get(request)
                if pending is None:
                    done = self._in_flight[request] = threading.Event()
                    generation = self._generation
                    break
            # Someone else's render of it: wait, then go round again - it
            # is normally cached by then, but if that render failed or
            # was for a since-cleared generation, this becomes the owner.
            pending.wait()
        self._render_generation.value = generation
        try:
            img = self.handler.get_page_image(self, page, target_px, fit)
            with self._lock:
                if generation == self._generation:
                    self._served[request] = img
            return img
        finally:
            del self._render_generation.value
            with self._lock:
                del self._in_flight[request]
            done.set()

    def has(self, page: int, target_px: int, fit: str = 'width') -> bool:
        """Whether get() with these arguments would return at once (or
        wait on a render already under way) rather than start one."""
        request: PageRequest = (page, target_px, fit)
        with self._lock:
            if request in self._in_flight:
                return True
            img = self._served.get(request)
            return img is not None and any(v is img for v in self._cache.values())

    def _cached(self, key: tuple[Any, ...]) -> Image.Image | None:
        """A previously-computed page image for `key`, or None - shared
        LRU bookkeeping used by every DocumentHandler.get_page_image()."""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
            return None

    def _current_generation(self) -> bool:
        """Whether the render under way on this thread (if any - see
        get()) started after the latest clear(). Call with _lock held."""
        return getattr(self._render_generation, "value", self._generation) == self._generation

    def forget_native(self, page: int) -> None:
        """Drop `page`'s natively-loaded image (see _store_native()), for
        a cache that only needs what was made from it - the sidebar's
        thumbnails (see _Sidebar._render()) - so it isn't kept at full
        size for every page scrolled past."""
        with self._lock:
            self._native_images.pop(page, None)

    def _store_native(self, page: int, img: Image.Image) -> None:
        """Remember `page`'s natively-loaded image (see
        DocumentHandler._native_page_image()) - unless, like _store(),
        it was loaded for a generation clear() has since thrown away."""
        with self._lock:
            if self._current_generation():
                self._native_images[page] = img

    def _store(self, key: tuple[Any, ...], img: Image.Image) -> None:
        with self._lock:
            # Rendered for a generation clear() has since thrown away
            # (see __init__): hand it back to its caller, but don't keep it.
            if not self._current_generation():
                return
            self._cache[key] = img
            self._cache.move_to_end(key)
            # Least recently used first: past the entry count, or past
            # the memory budget while more than the few pages actually
            # in use are held (see PAGE_CACHE_MAX_BYTES).
            while len(self._cache) > self.size or (
                len(self._cache) > PAGE_CACHE_MIN_KEEP
                and sum(_image_bytes(v) for v in self._cache.values()) > PAGE_CACHE_MAX_BYTES
            ):
                _key, evicted = self._cache.popitem(last=False)
                for request in [r for r, v in self._served.items() if v is evicted]:
                    del self._served[request]


class EncodeCache:
    """Cache JPEG/PNG payloads keyed by viewport position."""

    def __init__(self, size: int = CACHE_SIZE * 4) -> None:
        self.size = size
        self._cache: OrderedDict[tuple[Any, ...], bytes] = OrderedDict()  # encode_key -> bytes

    def clear(self) -> None:
        self._cache.clear()

    def get(self, key: tuple[Any, ...]) -> bytes | None:
        if key not in self._cache:
            return None
        self._cache.move_to_end(key)
        return self._cache[key]

    def put(self, key: tuple[Any, ...], data: bytes) -> None:
        self._cache[key] = data
        if len(self._cache) > self.size:
            self._cache.popitem(last=False)


@dataclasses.dataclass(frozen=True)
class ViewerOptions:
    """How the viewer starts up, as the command line asked for it - built
    once by main() (from_args()) and handed through run_viewer() to the
    Viewer as one value, rather than as a dozen-odd keyword arguments
    repeated at every step. Frozen: the ones that can change at runtime
    (scrollbar, continuous, follow, ...) are copied onto the Viewer's own
    attributes, and those are what change - this stays what was asked for
    at startup (which some toggles, e.g. copy mode's restore, rely on)."""

    fit: str | None = "width"  # -h/--fit-height: "height", else "width"
    border: bool = True  # --no-border
    wrap: bool = True  # -S/--chop-long-lines, inverted
    eol_mark: bool = True  # --no-eol-mark
    line_numbers: bool = False  # -N/--line-numbers
    scrollbar: bool = True  # --no-scrollbar
    sidebar: bool = False  # --sidebar
    wheel_scroll_step: int = 2  # --wheel-scroll-step
    incremental_scroll: bool = True  # --no-incremental-scroll
    debug: bool = False  # -d/--debug
    office_render_scale: float = OFFICE_RENDER_SCALE  # -s/--rendering-scale
    continuous: bool = False  # -c/--continuous
    follow: bool = False  # -f/--follow
    quit_if_one_screen: bool = False  # -F/--quit-if-one-screen
    keep: bool = False  # -k/--keep (run_viewer() only)

    @classmethod
    def from_args(cls, args: argparse.Namespace, nfiles: int) -> ViewerOptions:
        """The options main()'s parsed command line asks for, `nfiles`
        being how many files it's about to show."""
        return cls(
            fit="height" if args.fit_height else "width",
            border=args.border,
            wrap=not args.chop_long_lines,
            eol_mark=args.eol_mark,
            line_numbers=args.line_numbers,
            scrollbar=args.scrollbar,
            sidebar=args.sidebar,
            wheel_scroll_step=args.wheel_scroll_step,
            incremental_scroll=args.incremental_scroll,
            debug=args.debug,
            office_render_scale=args.rendering_scale,
            continuous=args.continuous,
            follow=args.follow,
            # Only meaningful for a single file - dumping the first of
            # several and quitting would silently drop the rest.
            quit_if_one_screen=args.quit_if_one_screen and nfiles == 1,
            keep=args.keep,
        )


class _Overlays:
    """The boxes drawn over the page: the help (F1 - see show_help()),
    the table of contents (o/TAB - see show_outline()) and the file list
    (O - see show_files()). While one is up it takes every key and mouse
    event (see handle_key() and handle_mouse()); all take the same keys,
    and only ENTER and a click on an entry are the table of contents'
    and the file list's own.

    Attributes:
        viewer: the Viewer the boxes are drawn over.
        active: which box is up - "help", "outline", "files" or None.
        help_scroll: the first help line in view.
        outline_sel: the table of contents' selected entry.
        outline_scroll: its first entry in view.
        files_sel: the file list's selected file.
        files_scroll: its first file in view.
    """

    def __init__(self, viewer: Viewer) -> None:
        """Args:
            viewer: the Viewer the boxes are drawn over.
        """
        self.viewer = viewer
        self.active: Literal["help", "outline", "files"] | None = None
        self.help_scroll = 0
        self.outline_sel = 0
        self.outline_scroll = 0
        self.files_sel = 0
        self.files_scroll = 0
        self._outline: list[dict[str, Any]] | None = None  # lazily built, via PdfDocument.build_outline()
        # The entry last jumped to, and where that left the view (see
        # _view_position()) - so reopening before moving on selects it.
        self._jumped: tuple[int, tuple[bool, int, int]] | None = None

    def forget_outline(self) -> None:
        """Drop the table of contents read from the file (it's read again
        on next use) - and close its box if it's up, as its entries
        may be gone. For a reload or a switch to another file."""
        self._outline = None
        self._jumped = None
        if self.active == "outline":
            self.active = None

    def _box(
        self, lines: list[tuple[str, str]], min_w: int = 0
    ) -> tuple[int, int, int, int]:
        """Where a boxed overlay (the help, the table of contents, the
        file list) goes:
        centered over the page, above the status bar, wide enough for its
        longest line but shrunk to fit the terminal - for draw_box() and
        for the callers' own scrolling and click hit-testing.

        Args:
            lines: every line of the box's content, as draw_box() takes them.
            min_w: the narrowest the content area may be.

        Returns:
            (row0, col0, content_h, content_w): the box's top-left corner
            (1-based terminal cells) and the size of the content area
            inside its border - content_h < len(lines) means it scrolls.
        """
        available_rows = max(1, self.viewer.rows - 1)  # bottom row is the status bar
        # A line with a right part needs a space between its two parts.
        longest = max(
            display_width(left) + (1 + display_width(right) if right else 0)
            for left, right in lines
        )
        content_w = min(max(20, self.viewer.cols - 4), max(min_w, longest))
        content_h = min(max(1, available_rows - 2), len(lines))
        box_w = content_w + 4  # border (2) + padding (2)
        box_h = content_h + 2  # top/bottom border
        row0 = max(1, (available_rows - box_h) // 2 + 1)
        col0 = max(1, (self.viewer.cols - box_w) // 2 + 1)
        return row0, col0, content_h, content_w

    def draw_box(
        self, lines: list[tuple[str, str]], scroll: int,
        selected: int | None = None, min_w: int = 0,
    ) -> tuple[int, int]:
        """Draw a boxed overlay (see _box()) over the page, showing
        the window of `lines` that starts at `scroll`. We only ever move
        the cursor and rewrite the exact cells the box covers, instead of
        clearing the screen, so the page still showing in the rest of the
        terminal is left untouched.

        Args:
            lines: every line of the content, each a (left, right) pair:
                `left` is truncated to fit (by terminal columns - it may be
                Japanese) and `right` (e.g. a page number, or "") is shown
                in full, flush right.
            scroll: the index of the first line in view.
            selected: the index of the line to show in reverse video -
                also scrolled into view - or None for no selection.
            min_w: as for _box().

        Returns:
            (scroll, max_scroll): `scroll` clamped to what the box can
            actually show (e.g. after a resize shrank it), for the caller
            to store back, and the largest scroll there is (0 = no scrolling).
        """
        row0, col0, content_h, content_w = self._box(lines, min_w)
        max_scroll = max(0, len(lines) - content_h)
        # Scroll just enough to keep the selection in view.
        if selected is not None:
            if selected < scroll:
                scroll = selected
            elif selected >= scroll + content_h:
                scroll = selected - content_h + 1
        scroll = max(0, min(max_scroll, scroll))

        box_w = content_w + 4
        out = [SGR_RESET]
        # Blank the box's cells first. Over text mode's lines, a cell can
        # be the right half of a double-width character; a wide character
        # of the box's written there makes the terminal (iTerm2, at least)
        # drop the old one and move over a column, shifting the rest of
        # the row (redrawing the box put it right, as its own cells were
        # all that was left underneath by then). Plain spaces split such a
        # character cleanly, leaving nothing for the box to trip on.
        for row in range(row0, row0 + content_h + 2):
            out.append(f"\x1b[{row};{col0}H{' ' * box_w}")
        out.append(f"\x1b[{row0};{col0}H┌{'─' * (box_w - 2)}┐")
        for i in range(content_h):
            left, right = lines[scroll + i]
            # `left` gets whatever `right` (and a space before it) leaves.
            left_w = content_w - display_width(right)
            left = truncate_to_width(left, max(0, left_w - 1) if right else left_w)
            line = pad_to_width(left, left_w) + right
            if scroll + i == selected:
                line = f"\x1b[7m{line}{SGR_RESET}"
            out.append(f"\x1b[{row0 + 1 + i};{col0}H│ {line} │")
        out.append(f"\x1b[{row0 + content_h + 1};{col0}H└{'─' * (box_w - 2)}┘")
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        return scroll, max_scroll

    def _help_lines(self) -> list[tuple[str, str]]:
        """The help box's content (KEY_TABLE), as draw_box() takes it."""
        return [(line, "") for line in KEY_TABLE.splitlines()]

    def help_box(self) -> tuple[int, int, int, int]:
        """_box() for the help - see there."""
        return self._box(self._help_lines())

    def show_help(self) -> None:
        """F1/:h: put up the help (KEY_TABLE) as a box over the page."""
        self.active = "help"
        self.help_scroll = 0
        self._draw_help()

    def scroll_help(self, delta: int) -> None:
        """Scroll the help box by `delta` lines (negative = up), for when
        KEY_TABLE has grown taller than the box can show at once."""
        lines = KEY_TABLE.splitlines()
        content_h = self.help_box()[2]
        max_scroll = max(0, len(lines) - content_h)
        new_scroll = max(0, min(max_scroll, self.help_scroll + delta))
        if new_scroll != self.help_scroll:
            self.help_scroll = new_scroll
            self._draw_help()

    def _draw_help(self) -> None:
        """Overlay the help (KEY_TABLE) as a box over the page, scrolled
        to self.help_scroll."""
        self.help_scroll, max_scroll = self.draw_box(self._help_lines(), self.help_scroll)
        if max_scroll:
            pct = round(100 * self.help_scroll / max_scroll)
            self.viewer.draw_status(f"q to close help - j/k or wheel to scroll ({pct}%)")
        else:
            self.viewer.draw_status("q to close help")

    def _ensure_outline(self) -> list[dict[str, Any]]:
        """self._outline (see PdfDocument.build_outline()), built on
        first use - an empty list for anything without a real PDF behind
        it (see Viewer._pdf_source()). A rendered document gets the bookmarks
        of the PDF it was rendered to: WeasyPrint makes one per Markdown
        heading, LibreOffice one per Word/OpenDocument heading or
        PowerPoint slide title; Chrome's --print-to-pdf fallback makes
        none, and neither does LibreOffice for RTF."""
        if self._outline is None:
            pdf_source = self.viewer._pdf_source()
            self._outline = pdf_source.build_outline() if pdf_source is not None else []
        return self._outline

    def show_outline(self) -> None:
        """o/TAB: put up the table of contents (the PDF's bookmarks - see
        _ensure_outline() for the other formats that have one) as a
        box over the page, with the entry for the page on screen already
        selected - or just say so on the status line if there isn't one."""
        outline = self._ensure_outline()
        if not outline:
            self.viewer.draw_status("no table of contents in this file")
            return
        # The last entry starting at or before the current page is the
        # section being read - the same one a PDF viewer's sidebar
        # highlights. Entries without a page don't count. A text mode
        # showing the document's source has no pages to go by, but its
        # headings are right there: the last one above where reading is.
        headings = self._source_heading_lines()
        self.outline_sel = 0
        if self._jumped is not None and self._jumped[1] == self._view_position():
            # Still where the last jump left it: that entry, even when
            # another one starts on the same page, or (near the end of
            # the text) on the same screen.
            self.outline_sel = self._jumped[0]
        elif headings is not None:
            reading = self._reading_text_line()
            for i, line in enumerate(headings):
                if line <= reading:
                    self.outline_sel = i
        else:
            for i, entry in enumerate(outline):
                if entry["page"] is not None and entry["page"] <= self.viewer.page:
                    self.outline_sel = i
        self.active = "outline"
        self.outline_scroll = 0
        self._draw_outline()

    def _source_heading_lines(self) -> list[int] | None:
        """In a text mode showing the document's own source (Markdown's -
        see DocumentHandler.heading_lines()), the line each entry's
        heading is on, entry by entry - or None if text mode isn't such
        a view, or its headings don't pair up one-to-one with the
        entries (then _text_line_of_entry() looks for the titles
        instead)."""
        viewer = self.viewer
        if not viewer.text_mode or viewer.doc_handler.text_mode_is_paginated():
            return None
        headings = viewer.doc_handler.heading_lines()
        if headings is None or len(headings) != len(self._ensure_outline()):
            return None
        return headings

    def _reading_text_line(self) -> int:
        """The text mode line being read: the one a quarter of the way
        down the screen, where a jump to a heading puts it (see
        Viewer._scroll_text_to_row()) - or, scrolled all the way to the
        end, the last line on screen, as a heading near the end can't be
        scrolled up that far."""
        viewer = self.viewer
        if viewer.text_scroll < viewer.text_scroll_max:
            return viewer._text_reading_line()
        return viewer._text_line_at_row(viewer.text_scroll + viewer._text_avail_rows() - 1)

    def source_anchors(self) -> list[tuple[int, float]] | None:
        """In a text mode showing the document's source (see
        _source_heading_lines()), the points where the source and the
        rendered pages are known to line up: each heading's line and
        where its bookmark points, plus the two ends - for mapping a
        place in one onto the other in between (see
        Viewer._image_point_for_text()).

        Returns:
            (line, position) pairs in order, position in pages from the
            document's start (2.5 is halfway down page 3) - or None if
            text mode isn't such a view.
        """
        headings = self._source_heading_lines()
        if headings is None:
            return None
        viewer = self.viewer
        anchors = [(0, 0.0)]
        for line, entry in zip(headings, self._ensure_outline()):
            page = entry["page"] or 1
            height_pt = viewer._page_height_pt(page) if entry["top_pt"] is not None else None
            down = (height_pt - entry["top_pt"]) / height_pt if height_pt else 0.0
            # Never backwards, whatever a bookmark says.
            anchors.append((max(line, anchors[-1][0]), max(page - 1 + down, anchors[-1][1])))
        anchors.append((max(len(viewer.text_lines), anchors[-1][0]), max(float(viewer.npages), anchors[-1][1])))
        return anchors

    def _text_line_of_entry(self, index: int) -> int | None:
        """The text mode line entry `index`'s heading is on, for a jump
        to it: the matching heading of the source (see
        _source_heading_lines()), or else a line with its title on it
        (see find_title_line()) - on its own page, or anywhere in a text
        mode that isn't split into pages, nearest to where the bookmark
        points. None if there's no such line."""
        viewer = self.viewer
        headings = self._source_heading_lines()
        if headings is not None:
            return headings[index]
        entry = self._ensure_outline()[index]
        down = self._fraction_down_page(entry)
        if viewer.doc_handler.text_mode_is_paginated():
            start, end = viewer._text_page_range(entry["page"])
            near = down
        else:
            # The whole document at once: its pages' share of it.
            start, end = 0, len(viewer.text_lines)
            near = (entry["page"] - 1 + (down or 0.0)) / max(1, viewer.npages)
        return find_title_line(viewer.text_lines, start, end, entry["title"], near)

    def _fraction_down_page(self, entry: dict[str, Any]) -> float | None:
        """How far down its page entry's destination is (0 = the top),
        or None if the bookmark gives no position."""
        pdf_source = self.viewer._pdf_source()
        if entry["top_pt"] is None or pdf_source is None:
            return None
        height_pt = pdf_source.page_size_pt(entry["page"])[1]
        if not height_pt:
            return None
        # top_pt is PDF-style, measured up from the bottom.
        return max(0.0, min(1.0, (height_pt - entry["top_pt"]) / height_pt))

    def _outline_lines(self) -> list[tuple[str, str]]:
        """The table-of-contents box's content, as draw_box() takes it:
        each entry's title indented by its level, with its page number
        (if it has one) flush right."""
        return [
            ("  " * e["level"] + display_safe(e["title"]), str(e["page"]) if e["page"] is not None else "")
            for e in self._ensure_outline()
        ]

    def outline_box(self) -> tuple[int, int, int, int]:
        """_box() for the table of contents - see there."""
        return self._box(self._outline_lines(), OUTLINE_MIN_W)

    def _draw_outline(self) -> None:
        """Draw the table-of-contents box over the page, with the selected
        entry in reverse video and scrolled into view."""
        outline = self._ensure_outline()
        self.outline_sel = max(0, min(len(outline) - 1, self.outline_sel))
        self.outline_scroll, _max_scroll = self.draw_box(
            self._outline_lines(), self.outline_scroll, self.outline_sel, OUTLINE_MIN_W
        )
        self.viewer.draw_status(
            f"ENTER to jump, q to close - {self.outline_sel + 1}/{len(outline)}"
        )

    def _move_outline_selection(self, delta: int) -> None:
        """Move the selection `delta` entries down (negative = up),
        stopping at either end."""
        n = len(self._ensure_outline())
        new_sel = max(0, min(n - 1, self.outline_sel + delta))
        if new_sel != self.outline_sel:
            self.outline_sel = new_sel
            self._draw_outline()

    def jump_to_outline_entry(self, index: int) -> None:
        """Close the table of contents and go to entry `index`'s place:
        in image mode, exactly where its destination points (like an
        internal link, and recorded in the same [/] history); in text
        mode, the line its heading is on (see _text_line_of_entry()),
        placed where a search match would be - or the top of its page
        if that line can't be found."""
        entry = self._ensure_outline()[index]
        if entry["page"] is None:
            self.viewer.draw_status("this entry doesn't point to a page in this file")
            return
        self.active = None
        self.viewer._invalidate_screen()
        if self.viewer.text_mode:
            self.viewer.go_to_page_text(entry["page"], 0)
            line = self._text_line_of_entry(index)
            if line is not None:
                self.viewer._scroll_text_to_row(self.viewer._text_row_for_line(line))
        else:
            self.viewer.links.push_history()
            self.viewer.go_to_link_target(entry["page"], entry["top_pt"])
        self._jumped = (index, self._view_position())
        self.viewer.refresh()

    def _view_position(self) -> tuple[bool, int, int]:
        """Where the view is: the mode, the page, and how far it's
        scrolled - for telling whether it has moved since a jump."""
        viewer = self.viewer
        return viewer.text_mode, viewer.page, viewer.text_scroll if viewer.text_mode else viewer.scroll

    def show_files(self) -> None:
        """O: put up the files given on the command line as a box over
        the page, the one being viewed selected - or just say so on the
        status line if there's only the one."""
        viewer = self.viewer
        if len(viewer.files) < 2:
            viewer.draw_status("only one file")
            return
        self.files_sel = viewer.file_index
        self.active = "files"
        self.files_scroll = 0
        self._draw_files()

    def _files_lines(self) -> list[tuple[str, str]]:
        """The file list's content, as draw_box() takes it: each file's
        path relative to where pdfless was started ("(stdin)" for piped
        input), the one being viewed marked, with its number flush
        right (the N of <N>x). A path too long for the box loses its
        start rather than its end (see truncate_start_to_width()), so
        the file's own name stays in view - draw_box() would cut the
        end."""
        viewer = self.viewer
        rows = []
        for i, entry in enumerate(viewer.files):
            path = entry if isinstance(entry, str) else entry.path
            if os.path.dirname(path) == viewer.tmpdir:
                name = "(stdin)"  # see _capture_stdin()
            else:
                name = os.path.relpath(path)
            rows.append(("* " if i == viewer.file_index else "  ", display_safe(name), str(i + 1)))
        # How wide the box gets for the whole paths (capped by the
        # terminal's width) - then each path cut to fit in it beside
        # its mark and number, and a space before the number.
        content_w = self._box([(mark + name, number) for mark, name, number in rows], OUTLINE_MIN_W)[3]
        return [
            (mark + truncate_start_to_width(name, content_w - len(mark) - 1 - len(number)), number)
            for mark, name, number in rows
        ]

    def files_box(self) -> tuple[int, int, int, int]:
        """_box() for the file list - see there."""
        return self._box(self._files_lines(), OUTLINE_MIN_W)

    def _draw_files(self) -> None:
        """Draw the file list over the page, with the selected file in
        reverse video and scrolled into view."""
        n = len(self.viewer.files)
        self.files_sel = max(0, min(n - 1, self.files_sel))
        self.files_scroll, _max_scroll = self.draw_box(
            self._files_lines(), self.files_scroll, self.files_sel, OUTLINE_MIN_W
        )
        self.viewer.draw_status(f"ENTER to open, q to close - {self.files_sel + 1}/{n}")

    def _move_files_selection(self, delta: int) -> None:
        """Move the file list's selection `delta` files down (negative =
        up), stopping at either end."""
        new_sel = max(0, min(len(self.viewer.files) - 1, self.files_sel + delta))
        if new_sel != self.files_sel:
            self.files_sel = new_sel
            self._draw_files()

    def open_file(self, index: int) -> None:
        """Close the file list and switch to file `index` - see
        Viewer.go_to_file(), which says so on the status line if it
        can't be opened."""
        self.active = None
        self.viewer._invalidate_screen()
        # Take the box away first: go_to_file() only redraws once it has
        # switched, not when it just reports that it couldn't.
        self.viewer.refresh()
        if index != self.viewer.file_index:
            self.viewer.go_to_file(index)

    # What each overlay (see self.active) is, as the calls the shared
    # code below makes on it - everything else about the boxes is
    # handled the same way.
    def _box_now(self) -> tuple[int, int, int, int]:
        """_box() for the overlay that's up."""
        if self.active == "help":
            return self.help_box()
        return self.outline_box() if self.active == "outline" else self.files_box()

    def draw(self) -> None:
        """Draw the overlay that's up over the page, and its status line."""
        if self.active == "help":
            self._draw_help()
        elif self.active == "outline":
            self._draw_outline()
        else:
            self._draw_files()

    def _move(self, delta: int) -> None:
        """j/k and the like on the overlay that's up: scroll the help by
        `delta` lines, or move the table of contents' or the file list's
        selection by `delta` entries (negative = up) - either way
        stopping at the ends."""
        if self.active == "help":
            self.scroll_help(delta)
        elif self.active == "outline":
            self._move_outline_selection(delta)
        else:
            self._move_files_selection(delta)

    def _choose(self, index: int) -> None:
        """ENTER (or a click) on entry `index` of the table of contents
        or the file list: jump to it, or open it."""
        if self.active == "outline":
            self.jump_to_outline_entry(index)
        elif self.active == "files":
            self.open_file(index)

    def hide(self) -> None:
        """Close the box that's up, redrawing what it covered."""
        self.active = None
        self.viewer._invalidate_screen()  # the box overlays the page
        self.viewer.refresh()

    def handle_key(self, key: str) -> None:
        """A key pressed while the help, the table of contents or the
        file list is up. All take the same keys:

        - q, ESC, or the key that opened it (F1, o/TAB, or O) closes it;
        - ^L (or a focus change - see Viewer.handle_global_key()) repaints the
          page underneath, then the box back over it;
        - the usual line/window keys, d/u and g/G/</>/HOME/END scroll the
          help or move the selection;
        - ENTER (not the help) jumps to the selected entry, or opens the
          selected file.

        Everything else is swallowed, so page keys can't act on the page
        hidden underneath."""
        window = max(1, self._box_now()[2] - 1)
        # Past either end, whichever box it is.
        far = len(KEY_TABLE.splitlines()) + len(self._ensure_outline()) + len(self.viewer.files)
        closing = {"help": ("F1",), "outline": ("o", "\t"), "files": ("O",)}[self.active or "help"]
        # ENTER before the line keys: "\r" is one of FORWARD_LINE_KEYS.
        if key in ("\r", "\n") and self.active != "help":
            self._choose(self.outline_sel if self.active == "outline" else self.files_sel)
        elif key in ("q", "\x1b") or key in closing:
            self.hide()
        elif key in ("\x0c", "FOCUS_IN"):
            self.viewer._invalidate_screen()
            if self.viewer.text_mode:
                self.viewer._draw_text()
            else:
                self.viewer._draw()
            self.draw()
        elif key in FORWARD_LINE_KEYS:
            self._move(1)
        elif key in BACKWARD_LINE_KEYS:
            self._move(-1)
        elif key in FORWARD_WINDOW_KEYS or key in ("d", "\x04"):
            self._move(window)
        elif key in BACKWARD_WINDOW_KEYS or key in ("u", "\x15"):
            self._move(-window)
        elif key in ("g", "<", "HOME"):
            self._move(-far)
        elif key in ("G", ">", "END"):
            self._move(far)

    def handle_mouse(self, kind: str, col: int, row: int) -> None:
        """A mouse event while a box is up: the wheel scrolls it (moves
        the selection, but for the help), a click anywhere outside the
        box closes it, and a click on an entry of the table of contents
        or the file list jumps to it or opens it."""
        if kind == "MOUSE_WHEEL_UP":
            self._move(-1)
        elif kind == "MOUSE_WHEEL_DOWN":
            self._move(1)
        elif kind == "MOUSE_CLICK":
            row0, col0, content_h, content_w = self._box_now()
            inside_cols = col0 <= col < col0 + content_w + 4
            if inside_cols and row0 < row <= row0 + content_h:
                scroll = self.outline_scroll if self.active == "outline" else self.files_scroll
                self._choose(scroll + row - row0 - 1)
            elif not (inside_cols and row0 <= row <= row0 + content_h + 1):
                self.hide()  # outside the box (its border doesn't count)


class _Search:
    """"/" and "?" search: the query, every match it found, which one is
    selected, and the PDF search index they came from - and moving the
    view to a match. The view draws the selected match itself (a box in
    image mode, a highlight in text mode) from visible_match() and
    text_highlight(), recomputed on every draw.

    A match is (page, xMin, yMin, xMax, yMax) from a paginated
    document's bbox index, or (line_idx, start, end) into the viewer's
    text_lines when the text is searched directly (see
    uses_text_lines()).

    Attributes:
        viewer: the Viewer searched in.
        query: the query, or None when no search is active.
        multiline: whether a regex may match across line breaks (^T at
            the search prompt) - see find_all_text_matches() and
            PdfDocument.find_search_matches().
        matches: every match of the query, in reading order.
        pos: the index of the selected match in `matches`, or None.
        index: the PDF search index (see PdfDocument.build_search_index()),
            built on the first search - or None.
    """

    def __init__(self, viewer: Viewer) -> None:
        """Args:
            viewer: the Viewer searched in.
        """
        self.viewer = viewer
        self.query: str | None = None
        self.multiline = False
        self.matches: Sequence[SearchMatch] = []
        self.pos: int | None = None
        self.index: list[dict[str, Any]] | None = None

    def reset(self) -> None:
        """Clear the search and drop the index, for a reload or a switch
        to another file - the index is built again on the next search."""
        self.index = None
        self.clear()

    def ensure_index(self) -> list[dict[str, Any]] | None:
        """self.index, built on first use (saying so on the status line,
        as it takes a moment on a long document) - None if the document
        has none to build (see DocumentHandler.build_search_index())."""
        if self.index is None:
            self.viewer.draw_status("building search index...")
            self.index = self.viewer.doc_handler.build_search_index()
        return self.index

    def start(self, query: str, backward: bool = False, multiline: bool = False) -> None:
        """Search the whole document for `query` and jump to one match -
        which one depends on where you are now and on `backward`, i.e.
        on whether the prompt was opened with "?" rather than "/" (see
        _match_index_from()). N/P walk every match from there on,
        regardless of the direction this started in. `multiline` (^T at
        the prompt) lets a regex match across line breaks, as a literal
        query always may - see find_all_text_matches() and
        PdfDocument.find_search_matches()."""
        if not query:
            return
        self.query = query
        self.multiline = multiline
        if self.uses_text_lines():
            # No page/bbox structure for a non-paginated document
            # (plain text/RTF, or Markdown currently in text mode) -
            # matches are just (line_idx, start, end) straight out of
            # text_lines, the whole document's text.
            self.matches = self.find_all_text_matches()
            if not self.matches:
                self.pos = None
                self.viewer.draw_status(f'"{query}" not found')
                return
            # Positions are raw line indices, so "here" has to be one
            # too - text_scroll itself counts display rows while the
            # text is wrapped (see Viewer._top_text_line()).
            self.go_to_match(self._match_index_from(
                [m[0] for m in self.matches], self.viewer._top_text_line(), backward,
            ))
            return

        self.ensure_index()
        self.matches = (
            self.viewer.doc_handler.find_search_matches(self.index, query, multiline)
            if self.index is not None else []  # nothing to search
        )
        if not self.matches:
            self.pos = None
            self.viewer.draw_status(f'"{query}" not found')
            return
        # A paginated document's matches are only ordered down to the
        # page they're on, so that's the unit the starting point is
        # measured in too.
        self.go_to_match(self._match_index_from(
            [m[0] for m in self.matches], self.viewer.page, backward,
        ))

    def repeat(self, forward: bool) -> None:
        if not self.matches:
            msg = (
                f'"{self.query}" not found'
                if self.query
                else "no previous search pattern"
            )
            self.viewer.draw_status(msg)
            return
        if self.pos is None:
            self.pos = 0
        else:
            step = 1 if forward else -1
            next_pos = self.pos + step
            if not (0 <= next_pos < len(self.matches)):
                # No wraparound - N past the last match / P before the
                # first one just reports there's nowhere further to go,
                # the way less(1)'s own (non-wrapping) search does.
                self.viewer.draw_status(
                    f'"{self.query}" no more matches '
                    + ("forward" if forward else "backward")
                )
                return
            self.pos = next_pos
        self.go_to_match(self.pos)

    def clear(self) -> None:
        self.query = None
        self.matches = []
        self.pos = None

    def go_to_match(self, idx: int) -> None:
        self.pos = idx

        if self.uses_text_lines():
            self.viewer._scroll_text_to_highlight(*self.matches[idx])
            self.viewer.refresh()
            self.viewer.draw_status(
                f'"{self.query}" match {idx + 1}/{len(self.matches)}'
            )
            return

        page = self.matches[idx][0]

        if self.viewer.text_mode:
            self.viewer.go_to_page_text(page, 0)
        else:
            self.viewer.go_page(page, 0)

        # The viewer's page == page now, so this is the match we just
        # landed on; Viewer._draw()/_draw_text() will independently
        # rediscover and render it (marker box or text highlight) on every
        # redraw from here on, including a later `t` mode toggle - see
        # active_page_match().
        match = self.active_page_match()
        if match is None:
            pass  # (the page couldn't be shown - nothing to scroll to)
        elif self.viewer.text_mode:
            self.scroll_text_to_match(match)
        else:
            self.scroll_image_to_match(match)

        self.viewer.refresh()
        self.viewer.draw_status(
            f'"{self.query}" match {idx + 1}/{len(self.matches)} '
            # The match's own page, not self.viewer.page: under -c/--continuous
            # the view can settle with an earlier page at the top.
            f"(page {page})"
        )

    @staticmethod
    def _match_index_from(positions: list[int], here: int, backward: bool) -> int:
        """Which of the matches a search should land on, given where it
        started from. `positions` is every match's position in ascending
        order, in whatever unit `here` is in (a page number, or a raw
        text_lines index); the answer is an index into it.

        Forward ("/"), that's the first match at or after `here`;
        backward ("?"), the last one strictly before it - so "?" can
        never move you forward, while "/" can leave you where you are if
        a match is already on screen. Either way it wraps around the
        ends of the document less(1)-style: a backward search from
        before the first match lands on the last one, a forward search
        from past the last one lands on the first."""
        if backward:
            for i in range(len(positions) - 1, -1, -1):
                if positions[i] < here:
                    return i
            return len(positions) - 1
        for i, pos in enumerate(positions):
            if pos >= here:
                return i
        return 0

    def uses_text_lines(self) -> bool:
        """Whether / search should walk self.viewer.text_lines as one blob
        (line_idx, start, end) matches rather than a PDF page/bbox
        index. True for plain text files (always in text mode) and for
        handlers like MarkdownDocument whose text mode shows the whole
        raw source at once - but False in image mode even for those,
        where a real PDF delegate's bbox index should still be used."""
        return self.viewer.text_mode and not self.viewer.doc_handler.text_mode_is_paginated()

    def active_page_match(self) -> BBoxMatch | None:
        """The currently-selected search match (self.pos), but
        only if it's on the page being displayed right now - this is
        what lets the box marker (image mode) and highlight (text mode)
        follow each other across a `t` toggle: both are derived from this
        same bit of state, recomputed fresh on every draw, rather than
        each mode tracking its own separate "is a match showing" flag."""
        if self.pos is None:
            return None
        match = self.matches[self.pos]
        if len(match) != 5:
            return None  # a text-lines (line, start, end) match has no page
        return match if match[0] == self.viewer.page else None

    def visible_match(self) -> BBoxMatch | None:
        """What to actually draw a match marker/highlight for: the same
        as active_page_match(), except that under
        -c/--continuous the match can be on any page that's on screen
        too, not just the one at the top - in image mode, any page in
        self.viewer._layout; in the continuous text view, any page at all,
        since the whole document's text is loaded there and the
        highlight is simply drawn wherever that line is. Used only for
        drawing: everything that repositions the view around a match
        still goes by active_page_match(), since it works from
        the current page's own coordinates."""
        if self.pos is None:
            return None
        match = self.matches[self.pos]
        if len(match) != 5:
            return None  # a text-lines (line, start, end) match has no page
        if match[0] == self.viewer.page:
            return match
        if not self.viewer.continuous:
            return None
        if self.viewer.text_mode:
            return match if self.viewer._text_continuous() else None
        return match if self.viewer._visible_page_top(match[0]) is not None else None

    def _active_occurrence_index(self) -> int | None:
        """How many other matches with the same page precede
        self.matches[self.pos] (0-indexed) - i.e. this is
        the Nth occurrence of the query on that page, in reading order.
        Used to correlate the same occurrence between the bbox-based
        document search index and the independently pdftotext -layout
        -extracted text-mode lines: when a page has the query more than
        once, matching by *position* between the two isn't reliable
        (their coordinate systems and line-splitting differ), but
        reading order should still agree between them."""
        if self.pos is None:
            return None
        page = self.matches[self.pos][0]
        if page != self.viewer.page and not self.viewer._text_continuous():
            return None  # (the continuous text view has every page loaded)
        return sum(1 for m in self.matches[: self.pos] if m[0] == page)

    def match_bbox_px(self, match: BBoxMatch) -> tuple[float, float, float, float]:
        """Pixel bounding box (in its own page's image, at the current
        zoom - self.viewer.img when it's on the current page) of a
        (page, xMin, yMin, xMax, yMax) search match, in points."""
        page, xmin_pt, ymin_pt, xmax_pt, ymax_pt = match
        img = self.viewer.img if page == self.viewer.page else self.viewer._page_image(page)
        assert self.index is not None  # a bbox match came from it
        page_info = self.index[page - 1]
        scale_x = img.width / page_info["width_pt"]
        scale_y = img.height / page_info["height_pt"]
        return (
            xmin_pt * scale_x,
            ymin_pt * scale_y,
            xmax_pt * scale_x,
            ymax_pt * scale_y,
        )

    def scroll_image_to_match(self, match: BBoxMatch) -> None:
        """Scroll/pan the image view so `match` is visible, landing it a
        little below the top-left rather than jammed against the edge."""
        px_left, px_top, px_right, px_bottom = self.match_bbox_px(match)
        margin = self.viewer.avail_height_px // 4
        self.viewer.scroll = self.viewer._clamp_image_scroll(round(px_top) - margin)
        if px_left < self.viewer.x_offset or px_right > self.viewer.x_offset + self.viewer.crop_width:
            self.viewer._set_x_offset(round((px_left + px_right) / 2 - self.viewer.crop_width / 2))

    def scroll_text_to_match(self, match: BBoxMatch) -> None:
        """Scroll/pan the text view so `match` is visible, landing it a
        little below the top rather than jammed against the top edge -
        panning horizontally into view too, in case the terminal is too
        narrow for the line and it's off to the side of the truncated
        view (the text-mode equivalent of scroll_image_to_match()).
        Wrapped text has no pan to speak of - Viewer._row_for_line() converts
        the raw line position into a display-row scroll target instead."""
        highlight = self._text_highlight_for_match(match)
        if highlight:
            self.viewer._scroll_text_to_highlight(*highlight)
        else:
            self.viewer._scroll_text_to_row(self.viewer._text_row_for_line(self._approx_text_line(match)))

    def find_all_text_matches(self, line_range: tuple[int, int] | None = None) -> list[TextMatch]:
        """Every occurrence of self.query within self.viewer.text_lines
        (the current page's pdftotext -layout text, or the whole
        document's), as a list of (line_idx, start, end), in reading
        order - only lines line_range[0] up to (not including)
        line_range[1] of it, if given (one page's share of the
        continuous text view - see Viewer._text_page_range()).

        The lines are searched joined together, so a match can run across
        a line break - a PDF's text comes one printed line at a time, so
        a paragraph's words are often split between two ("ござ" / "います").
        That's only for a literal query (see search_query_is_literal()),
        or with self.multiline on: otherwise a regex is matched
        one line at a time.
        The line break is whitespace like any other to
        normalize_for_search(): dropped next to Japanese or Chinese, one
        space between Latin words. Such a match's `end` counts on past
        its own line (see TextMatch). A page separator line (continuous
        view) is never searched across."""
        if not self.query:
            return []
        pattern = compile_search_pattern(self.query)
        start, end = line_range if line_range else (0, len(self.viewer.text_lines))
        results = []
        if not (self.multiline or search_query_is_literal(self.query)):
            # A regex - "foo.*bar" - stays within one line, as in less(1),
            # unless ^T at the prompt said otherwise: joined up, ".*" would
            # reach from one "foo" to a "bar" any number of lines on.
            for i in range(start, end):
                if i not in self.viewer._text_separator_lines:
                    results.extend(self._find_text_matches_in_block(pattern, i, i + 1))
            return results
        block_start = start
        # Blocks of consecutive lines between separators, each searched
        # as one string.
        for i in range(start, end + 1):
            if i < end and i not in self.viewer._text_separator_lines:
                continue
            if block_start < i:
                results.extend(self._find_text_matches_in_block(pattern, block_start, i))
            block_start = i + 1
        return results

    def _find_text_matches_in_block(
        self, pattern: re.Pattern[str], first: int, stop: int,
    ) -> list[TextMatch]:
        """find_all_text_matches() for lines first..stop-1, none of them
        a separator.

        Args:
            pattern: from compile_search_pattern().
            first, stop: the lines to search, as one.

        Returns:
            (line_idx, start, end) of each match, `start` within line
            line_idx and `end` counted from that same line's start.
        """
        lines = self.viewer.text_lines[first:stop]
        # Joined by a space rather than "\n" - the same to
        # normalize_for_search() (either is dropped next to Japanese or
        # Chinese), but a single space between two Latin words needs no
        # folding at all, which keeps a long plain-English text fast.
        text = " ".join(lines)
        normalized = normalize_lines_for_search(lines)
        # Where each line starts in `text`.
        line_starts = []
        offset = 0
        for line in lines:
            line_starts.append(offset)
            offset += len(line) + 1
        results = []
        for match_start, match_end in search_spans(pattern, text, normalized):
            k = bisect.bisect_right(line_starts, match_start) - 1
            results.append((first + k, match_start - line_starts[k], match_end - line_starts[k]))
        return results

    def text_highlight(self) -> TextMatch | None:
        """(line_idx, start, end) to highlight while drawing text mode,
        or None. Never returns a PDF bbox tuple."""
        if self.pos is None or not self.matches:
            return None
        match = self.matches[self.pos]
        if self.uses_text_lines():
            if len(match) == 3:
                return match
            return self._text_highlight_for_match(match)
        return self._text_highlight_for_match(self.visible_match())

    def _text_highlight_for_match(self, match: BBoxMatch | None) -> TextMatch | None:
        """(line_idx, start, end) of `match` within self.viewer.text_lines, or
        None if the query doesn't appear there at all (a real
        possibility, given the two extractions can differ)."""
        if match is None:
            return None
        page_start, page_end = self.viewer._text_page_range(match[0])
        all_matches = self.find_all_text_matches((page_start, page_end))
        if not all_matches:
            return None

        occurrence_index = self._active_occurrence_index()
        if occurrence_index is not None and occurrence_index < len(all_matches):
            return all_matches[occurrence_index]

        # Fall back to a proportional-position guess, for the rare case
        # where the two extractions disagree on how many times the query
        # appears on this page.
        approx_line = self._approx_text_line(match)
        return min(all_matches, key=lambda c: abs(c[0] - approx_line))

    def _approx_text_line(self, match: tuple) -> int:
        """A guess at the raw text_lines index a (page, xMin, yMin, xMax,
        yMax) bbox match falls on - its vertical position on the page,
        as the same fraction of that page's own lines - for when the
        text extraction can't pin it down exactly (see
        _text_highlight_for_match())."""
        page, _xmin_pt, ymin_pt, _xmax_pt, _ymax_pt = match
        assert self.index is not None  # a bbox match came from it
        height_pt = self.index[page - 1]["height_pt"]
        page_start, page_end = self.viewer._text_page_range(page)
        return page_start + (
            round((ymin_pt / height_pt) * (page_end - page_start)) if height_pt else 0
        )


class _Links:
    """A PDF's hyperlinks, and the back/forward history of the jumps
    internal links and the table of contents make: a click on a link
    opens a URL in the browser or jumps to the link's target, and `[`/`]`
    (or a mouse's back/forward buttons) walk back and forth through the
    positions jumped from - the same "back stack, forward stack, a fresh
    jump clears forward" model a web browser uses. Image mode only.

    Attributes:
        viewer: the Viewer the links are clicked in.
        index: every page's links (see PdfDocument.build_link_index()),
            built on first use - or None.
        back: the positions to go back to, latest last, each
            (page, scroll, x_offset).
        forward: the positions gone back from, to go forward to again.
    """

    def __init__(self, viewer: Viewer) -> None:
        """Args:
            viewer: the Viewer the links are clicked in.
        """
        self.viewer = viewer
        self.index: list[dict[str, Any]] | None = None
        self.back: list[tuple[int, int, int]] = []
        self.forward: list[tuple[int, int, int]] = []

    def forget_index(self) -> None:
        """Drop the links (they're read again on the next click), keeping
        the history - for a reload of the same file (see Viewer.reload()),
        whose pages, and links, may have changed."""
        self.index = None

    def reset(self) -> None:
        """Drop the links and the history, for a switch to another file."""
        self.index = None
        self.back = []
        self.forward = []

    def at(self, col: int, row: int) -> dict[str, Any] | None:
        """The link under a click at 1-based terminal cell (col, row) of
        the page image, if any - the smallest one, where links overlap.

        Args:
            col, row: the clicked cell.

        Returns:
            The link, as PdfDocument.build_link_index() gives it, or None."""
        viewer = self.viewer
        link_index = self.ensure_index()
        # Which page the click landed on, and how far down it the top
        # of the viewport is - always the current page normally; under
        # -c/--continuous, whichever page in the layout covers the
        # clicked row (none, if it's in a gap between pages).
        page, img, origin = viewer.page, viewer.img, viewer.scroll
        if viewer.continuous:
            click_y = (row - 1) * viewer.cell_h_px
            for p, top, p_img in viewer._layout:
                if top <= click_y < top + p_img.height:
                    page, img, origin = p, p_img, -top
                    break
            else:
                return None  # a gap between pages
        page_info = link_index[page - 1]
        if not page_info["links"] or not page_info["width_pt"] or not page_info["height_pt"]:
            return None

        # The clicked cell -> the pixel rectangle it covers on the full
        # page raster (undoing the current pan/scroll) -> a PDF-point
        # rectangle, in the same coordinate system build_link_index()
        # stored link rects in. A whole-cell rectangle, not just its
        # center point, matters here: a citation-style link (e.g. just
        # "5.7" in "figure 5.7") is often tightly boxed around the
        # digits by whatever generated the PDF, so its rect can be
        # thinner than one terminal row is tall - a single sampled point
        # would frequently land just outside it and miss the click.
        scale_x = page_info["width_pt"] / img.width
        scale_y = page_info["height_pt"] / img.height
        cell_xmin = (viewer.x_offset + (col - viewer._image_col0()) * viewer.cell_w_px) * scale_x
        cell_xmax = cell_xmin + viewer.cell_w_px * scale_x
        cell_ymin = (origin + (row - 1) * viewer.cell_h_px) * scale_y
        cell_ymax = cell_ymin + viewer.cell_h_px * scale_y

        best = None
        best_area = None
        for link in page_info["links"]:
            if (
                link["xmax"] < cell_xmin or link["xmin"] > cell_xmax
                or link["ymax"] < cell_ymin or link["ymin"] > cell_ymax
            ):
                continue  # no overlap between the link and the clicked cell
            area = (link["xmax"] - link["xmin"]) * (link["ymax"] - link["ymin"])
            if best is None or area < best_area:
                best, best_area = link, area
        return best

    def ensure_index(self) -> list[dict[str, Any]]:
        """self.index (see PdfDocument.build_link_index()), built on
        first use - and returned, for the caller to use directly."""
        viewer = self.viewer
        if self.index is None:
            pdf_source = viewer._pdf_source()
            if pdf_source is not None:
                self.index = pdf_source.build_link_index(viewer.npages)
            else:
                # No hyperlinks outside a PDF, but a multi-page "office"
                # preview (or, in principle, a multi-page "image") still
                # needs one empty entry per page - _Mouse.click() indexes
                # this by viewer.page, which can be > 1 for those kinds.
                self.index = [
                    {"width_pt": 0.0, "height_pt": 0.0, "links": []}
                    for _ in range(viewer.npages)
                ]
        return self.index

    def activate(self, link: dict[str, Any]) -> None:
        """Follow `link` (from at()): open a URL in the system browser,
        or jump to an internal link's target, recording where the jump
        left from (see push_history())."""
        viewer = self.viewer
        if link["kind"] == "uri":
            try:
                opened = webbrowser.open(link["uri"])
            except webbrowser.Error:
                opened = False
            viewer.draw_status(
                f"opened {link['uri']}" if opened else f"couldn't open {link['uri']}"
            )
        else:
            self.push_history()
            viewer.go_to_link_target(link["page"], link["top_pt"])
            viewer.refresh()

    def push_history(self) -> None:
        """Record the position an internal-link jump is about to leave,
        so `[`/`]` (or a mouse back/forward button) can return to it -
        the same "back stack, forward stack, a fresh jump clears
        forward" model a web browser uses."""
        viewer = self.viewer
        self.back.append((viewer.page, viewer.scroll, viewer.x_offset))
        self.forward.clear()

    def go_back(self) -> None:
        """`[` (or a mouse's back button): return to where the last jump left."""
        self._go_history(self.back, self.forward, "earlier")

    def go_forward(self) -> None:
        """`]` (or a mouse's forward button): redo a jump undone by go_back()."""
        self._go_history(self.forward, self.back, "later")

    def _go_history(
        self, from_stack: list[tuple[int, int, int]], to_stack: list[tuple[int, int, int]],
        label: str,
    ) -> None:
        viewer = self.viewer
        if viewer.text_mode or viewer.overlays.active is not None:
            return
        if not from_stack:
            viewer.draw_status(f"no {label} position")
            return
        to_stack.append((viewer.page, viewer.scroll, viewer.x_offset))
        page, scroll, x_offset = from_stack.pop()
        viewer._restore_position(page, scroll, x_offset)
        viewer.refresh()


class _Mouse:
    """What mouse events do (image mode only - text mode leaves mouse
    reporting off so the terminal's own text selection works): a click
    on the scrollbar jumps there and dragging it follows the pointer, a
    click on a link follows it (see _Links), the wheel scrolls, and the
    back/forward buttons walk the jump history. While a box is up over
    the page (the help, the table of contents, the file list), it takes
    every event instead, and over the page thumbnails, they take the
    clicks and the wheel (see _Sidebar.handle_mouse()).

    Attributes:
        viewer: the Viewer the events act on.
        dragging: whether a press landed on the scrollbar and the button
            hasn't come back up yet (see drag()).
        drag_row: the drag's latest position, not acted on until
            flush_drag() - or None.
    """

    def __init__(self, viewer: Viewer) -> None:
        """Args:
            viewer: the Viewer the events act on.
        """
        self.viewer = viewer
        self.dragging = False
        self.drag_row: int | None = None

    def handle(self, kind: str, col: int, row: int) -> None:
        """One decoded mouse event (see decode_sgr_mouse()): the help or
        the table of contents takes it while one is up (see
        _Overlays.handle_mouse()), and otherwise clicks/drags/the wheel/
        the back and forward buttons act on the page."""
        viewer = self.viewer
        if viewer.overlays.active is not None:
            viewer.overlays.handle_mouse(kind, col, row)
        elif viewer.sidebar.dragging and kind in ("MOUSE_DRAG", "MOUSE_RELEASE"):
            viewer.sidebar.handle_mouse(kind, col, row)  # its scrollbar's drag
        elif (col <= viewer.sidebar.columns() and not viewer.text_mode
                and kind in ("MOUSE_CLICK", "MOUSE_WHEEL_UP", "MOUSE_WHEEL_DOWN")):
            # (A drag or a release there otherwise still ends the page
            # scrollbar's drag.)
            viewer.sidebar.handle_mouse(kind, col, row)
        elif kind == "MOUSE_CLICK":
            self.click(col, row)
        elif kind == "MOUSE_DRAG":
            if self.drag(row):
                # A drag arrives as a burst of motion events, and acting
                # on one costs a page rasterize - so let the burst drain
                # first and only act on where the pointer actually ended up.
                ready, _, _ = select.select([viewer.fd], [], [], 0)
                if not ready:
                    self.flush_drag()
        elif kind == "MOUSE_RELEASE":
            self.end_drag()
        elif kind == "MOUSE_WHEEL_UP":
            self.wheel(-1)
        elif kind == "MOUSE_WHEEL_DOWN":
            self.wheel(1)
        elif kind == "MOUSE_BACK":
            viewer.links.go_back()
        elif kind == "MOUSE_FORWARD":
            viewer.links.go_forward()

    def click(self, col: int, row: int) -> None:
        """A left-click at 1-based terminal cell (col, row): on the
        scrollbar, jump to the position it points at; otherwise, if it
        landed on a PDF hyperlink in the currently displayed crop,
        follow it - open a URL in the system browser, or jump to an
        internal link's target page/position."""
        viewer = self.viewer
        if viewer.text_mode or viewer.overlays.active is not None or row >= viewer.rows:
            return  # row == viewer.rows is the status bar
        # Any press starts a fresh gesture: one that lands on the
        # scrollbar keeps following the pointer until the button comes
        # back up (see drag()), one anywhere else doesn't.
        self.dragging = bool(viewer.scrollbar and col == viewer.cols)
        if self.dragging:
            self._jump_to_scrollbar_row(row)
            return
        link = viewer.links.at(col, row)
        if link is not None:
            viewer.links.activate(link)

    def drag(self, row: int) -> bool:
        """Left-button motion, while a scrollbar drag is in progress -
        i.e. the press that started it landed on the scrollbar (see
        click()); dragging anywhere else is left alone, so a
        stray drag across the page image doesn't send it jumping.

        Only records where the pointer is: acting on it costs a page
        rasterize, and a drag arrives as a burst of motion events, so
        flush_drag() applies the last one once the burst lets
        up rather than walking every page in between. Returns whether
        the drag was taken."""
        if not self.dragging or self.viewer.text_mode or self.viewer.overlays.active is not None:
            return False
        self.drag_row = row
        return True

    def flush_drag(self) -> None:
        """Act on the position drag() last recorded, if any."""
        if self.drag_row is None:
            return
        row = self.drag_row
        self.drag_row = None
        self._jump_to_scrollbar_row(row)

    def end_drag(self) -> None:
        """The button came back up - act on wherever it was let go."""
        self.flush_drag()
        self.dragging = False

    def _jump_to_scrollbar_row(self, row: int) -> None:
        """Jump to wherever a click at `row` points on the scrollbar -
        the clicked cell's own place in the track is read as where the
        visible window should start, undoing what Viewer._scrollbar_fractions()
        did to put the thumb there. Image mode only: mouse reporting
        stays off in text mode so the terminal's own click-drag text
        selection keeps working (see Viewer.enter_text_mode())."""
        viewer = self.viewer
        avail_rows = max(1, viewer.rows - 1)
        start_frac = max(0.0, min(1.0, (row - 1) / avail_rows))
        # In "page units" - e.g. 3.5 is halfway down page 4 - so the
        # page and the position within it fall out of the same number.
        doc_pos = start_frac * max(1, viewer.npages)
        page = max(1, min(viewer.npages, int(doc_pos) + 1))
        within_frac = max(0.0, min(1.0, doc_pos - (page - 1)))
        if page != viewer.page:
            # Skipped when it's the page already showing, so a click
            # that only scrolls within it doesn't reload and rescale
            # the very same image for nothing.
            viewer.go_page(page, 0)
        viewer.scroll = viewer._clamp_image_scroll(round(within_frac * viewer.img.height))
        viewer.refresh()

    def wheel(self, direction: int) -> None:
        """A scroll-wheel step: direction -1 (up) or +1 (down), the
        viewer's wheel_scroll_step lines each (--wheel-scroll-step) - the
        same page-boundary roll-over as e/y. Only meaningful in the page
        image (mouse reporting is off in text mode, so this shouldn't
        normally fire there, but the guard is cheap insurance)."""
        viewer = self.viewer
        if viewer.text_mode or viewer.overlays.active is not None:
            return
        step = viewer.cell_h_px * viewer.wheel_scroll_step
        if direction < 0:
            viewer.scroll_up(step)
        else:
            viewer.scroll_down(step)
        viewer.refresh()


class _Sidebar:
    """S/--sidebar: the pages' thumbnails in a column down the left edge
    of image mode, the page being viewed framed and kept in view (see
    _follow()), a click on one going to its page and the wheel over them
    scrolling the column. The page image itself starts right of it (see
    Viewer._image_col0()).

    Thumbnails are rendered on a thread of their own, only the ones the
    column shows, into a PageCache of their own - poll(), on every pass
    of run_viewer()'s input loop, starts that and draws what it has
    finished - so a long document isn't rendered up front, and turning
    pages never waits for one.

    Each page gets a slot SIDEBAR_COLS - 1 columns wide: a row for the
    frame's top edge, the thumbnail's rows (a column's margin either
    side, for the frame's sides), and a row with its page number (in
    the frame's bottom edge, for the page being viewed). The column's
    last column is its own scrollbar, when there are more thumbnails
    than fit (see _scrollbar_cells()) - just a separator line when
    there aren't.

    The keys can pick one too (see handle_key()): s moves them to the
    column (putting it up first if it isn't), and S shows or hides it.

    Attributes:
        viewer: the Viewer the thumbnails are shown beside.
        on: whether they're wanted - see columns() for whether they're
            actually up.
        first: the page whose thumbnail is at the top.
        picking: whether the keys are picking a thumbnail (see
            handle_key()), rather than acting on the page.
        picked: the page whose thumbnail they've picked.
    """

    def __init__(self, viewer: Viewer, on: bool) -> None:
        """Args:
            viewer: the Viewer the thumbnails are shown beside.
            on: whether to start with them (--sidebar).
        """
        self.viewer = viewer
        self.on = on
        self.first = 1
        self.picking = False
        self.picked = 1
        self._followed_page: int | None = None  # see _follow()
        self._cache: PageCache | None = None  # for this file's handler, made on first use
        # (page, width) -> the rendered thumbnail, or None if it failed -
        # filled in by the rendering thread (see poll()).
        self._images: dict[tuple[int, int], Image.Image | None] = {}
        self._encoded: dict[tuple[int, int, int], tuple[bytes, int, int]] = {}
        self._thread: threading.Thread | None = None
        self._fresh = False  # the thread has finished thumbnails not drawn yet
        # A press on the column's scrollbar, and the button not up yet -
        # the drag's latest row, if not acted on yet (see handle_mouse()).
        self.dragging = False
        self._drag_row: int | None = None
        self._drawn: tuple[Any, ...] | None = None  # what escapes() last drew

    def columns(self) -> int:
        """How many columns the sidebar takes: SIDEBAR_COLS while it's on
        and the terminal is wide enough (see SIDEBAR_MIN_TERM_COLS), else
        0. Text mode draws none of it, but keeps the columns reserved,
        so the page image has the same width whichever mode it's
        switched back from."""
        return SIDEBAR_COLS if self.on and self.viewer.cols >= SIDEBAR_MIN_TERM_COLS else 0

    def reset(self) -> None:
        """Forget every thumbnail, for another file or a reload."""
        self._cache = None
        self._images = {}  # a new dict: a thread still running keeps filling the old one
        self._encoded = {}
        self._drawn = None
        self.first = 1
        self._followed_page = None
        self.picking = False

    def forget_drawn(self) -> None:
        """Something else has drawn over the screen - see
        Viewer._invalidate_screen() - so draw it all afresh next time."""
        self._drawn = None

    def _layout(self) -> tuple[int, int, int, int]:
        """The column's geometry, from the terminal's cell size and the
        current page's shape (a document's pages are mostly all the
        same one).

        Returns:
            (thumb_w_px, thumb_rows, slot_rows, slots): a thumbnail's
            width in pixels and the rows it gets, the rows each page's
            slot takes, and how many slots fit.
        """
        viewer = self.viewer
        thumb_w_px = (SIDEBAR_COLS - 3) * viewer.cell_w_px
        img = viewer.img
        aspect = img.height / img.width if img is not None and img.width else 1.414
        avail = max(1, viewer.rows - 1)  # the bottom row is the status line
        thumb_rows = max(1, min(avail - 2, round(thumb_w_px * aspect / viewer.cell_h_px)))
        slot_rows = thumb_rows + 2
        return thumb_w_px, thumb_rows, slot_rows, max(1, avail // slot_rows)

    def _follow(self, slots: int) -> None:
        """Keep the page being viewed in the column - or, while picking
        one with the keys, the picked one: scroll it into view when it
        has changed since last time - but not otherwise, so the wheel can
        scroll the column away from it (see handle_mouse())."""
        viewer = self.viewer
        target = self.picked if self.picking else viewer.page
        if target != self._followed_page:
            self._followed_page = target
            if target < self.first:
                self.first = target
            elif target >= self.first + slots:
                self.first = target - slots + 1
        self.first = max(1, min(self.first, viewer.npages - slots + 1))

    def _visible_pages(self, slots: int) -> range:
        """The pages the column shows, from self.first."""
        return range(self.first, min(self.viewer.npages, self.first + slots - 1) + 1)

    def _thumbnail(self, page: int, thumb_w_px: int, box_h_px: int) -> tuple[bytes, int, int] | None:
        """`page`'s thumbnail, encoded, fitted into thumb_w_px x box_h_px
        - or None if it isn't rendered (yet, or at all).

        Returns:
            (data, width, height): the JPEG and its size in pixels.
        """
        key = (page, thumb_w_px, box_h_px)
        if key not in self._encoded:
            img = self._images.get((page, thumb_w_px))
            if img is None:
                return None
            if img.height > box_h_px:  # a page taller than most: fit the height instead
                img = img.resize((max(1, round(img.width * box_h_px / img.height)), box_h_px))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            self._encoded[key] = (buf.getvalue(), img.width, img.height)
        return self._encoded[key]

    def escapes(self, full: bool) -> str:
        """The escape sequences that draw the column - "" when it isn't
        up, or when nothing in it has changed since last drawn, unless
        `full` (the screen was cleared under it).

        Args:
            full: draw it even if it looks the same as last time.

        Returns:
            The escape sequences.
        """
        viewer = self.viewer
        cols = self.columns()
        if not cols or viewer.text_mode:
            return ""
        thumb_w_px, thumb_rows, slot_rows, slots = self._layout()
        self._follow(slots)
        pages = self._visible_pages(slots)
        box_h_px = thumb_rows * viewer.cell_h_px
        ready = tuple(p for p in pages if (p, thumb_w_px) in self._images)
        framed = self.picked if self.picking else viewer.page
        # What the thumbnails depend on, and what the frame and numbers
        # around them do: turning a page (or moving the pick) only
        # changes the latter, which never overlap a thumbnail's cells -
        # so the thumbnails aren't sent again for it.
        images = (self.first, thumb_w_px, thumb_rows, viewer.rows, ready)
        marks = (viewer.page, framed, self.picking)
        if not full and (images, marks) == self._drawn:
            return ""
        redraw_images = full or self._drawn is None or self._drawn[0] != images
        self._drawn = (images, marks)

        out = [SGR_RESET]
        width = cols - 1  # the slots', left of the separator
        bar = self._scrollbar_cells(slots)
        for row in range(1, max(1, viewer.rows - 1) + 1):
            # Erase the slots' cells (a thumbnail left from a scroll
            # included) - or, keeping the thumbnails, just the frame's
            # and numbers' rows and columns - then draw the scrollbar's
            # (or separator's).
            if redraw_images or (row - 1) % slot_rows in (0, slot_rows - 1):
                out.append(f"\x1b[{row};1H\x1b[{width}X")
            else:
                out.append(f"\x1b[{row};1H \x1b[{row};{width}H ")
            out.append(f"\x1b[{row};{cols}H{bar[row - 1]}")
        for i, page in enumerate(pages):
            top = 1 + i * slot_rows  # the frame's top edge
            thumb = self._thumbnail(page, thumb_w_px, box_h_px) if redraw_images else None
            if thumb is not None:
                data, w, h = thumb
                # Centered in its slot, by whole cells.
                col = 2 + ((SIDEBAR_COLS - 3) * viewer.cell_w_px - w) // 2 // viewer.cell_w_px
                row = top + 1 + (box_h_px - h) // 2 // viewer.cell_h_px
                out.append(f"\x1b[{row};{col}H" + _inline_image(data, w, h))
            number = str(page)
            bottom = top + thumb_rows + 1
            if page == framed:
                inner = width - 2
                left = (inner - len(number) - 2) // 2
                right = inner - len(number) - 2 - left
                color = SIDEBAR_PICK_COLOR if self.picking else SIDEBAR_FRAME_COLOR
                out.append(color + f"\x1b[{top};1H┏{'━' * inner}┓")
                for row in range(top + 1, bottom):
                    out.append(f"\x1b[{row};1H┃\x1b[{row};{width}H┃")
                out.append(f"\x1b[{bottom};1H┗{'━' * left} {number} {'━' * right}┛" + SGR_RESET)
            else:
                # The page being viewed keeps its amber number while
                # another one is picked.
                color = SIDEBAR_FRAME_COLOR if page == viewer.page else SIDEBAR_LABEL_COLOR
                out.append(f"\x1b[{bottom};{1 + (width - len(number)) // 2}H"
                           + color + number + SGR_RESET)
        return "".join(out)

    def _scrollbar_cells(self, slots: int) -> list[str]:
        """The column's last column, one cell per row: a scrollbar like
        the page's own (see Viewer._scrollbar_column()), its thumb the
        thumbnails in view out of every page's - or, when they all fit,
        a plain separator line.

        Args:
            slots: how many thumbnails fit (see _layout()).
        """
        viewer = self.viewer
        avail = max(1, viewer.rows - 1)
        if viewer.npages <= slots:
            return [SIDEBAR_SEPARATOR] * avail
        return viewer._scrollbar_column(
            avail, (self.first - 1) / viewer.npages, slots / viewer.npages,
        )

    def _scroll_to_row(self, row: int) -> None:
        """A click or drag at `row` on the column's scrollbar: scroll the
        column so its thumb starts there (the inverse of
        _scrollbar_cells()), and draw it - the page stays as it is."""
        viewer = self.viewer
        _thumb_w_px, _thumb_rows, _slot_rows, slots = self._layout()
        avail = max(1, viewer.rows - 1)
        first = 1 + round(max(0, row - 1) / avail * viewer.npages)
        first = max(1, min(first, viewer.npages - slots + 1))
        if first != self.first:
            self.first = first
            drawn = self.escapes(full=False)
            sys.stdout.write(SYNC_BEGIN + drawn + SYNC_END)
            sys.stdout.flush()

    def poll(self, prompt_open: bool = False) -> None:
        """run_viewer()'s every pass: draw the thumbnails the rendering
        thread has finished, and start it on the ones the column shows
        that aren't rendered yet. Nothing while the column isn't up, or
        something else has the screen (text mode, a box over the page, a
        missing file).

        Args:
            prompt_open: whether the status line is taking input (the
                search prompt, a ":"/"-" prefix) - drawing then waits,
                as it would move the cursor away from the prompt (and an
                input method's composition window with it)."""
        viewer = self.viewer
        if (not self.columns() or viewer.text_mode or viewer.file_missing
                or viewer.overlays.active is not None or viewer.img is None):
            return
        if prompt_open:
            if self._thread is None or not self._thread.is_alive():
                self._start_rendering()
            return
        if self._drag_row is not None:  # the last of a burst of drag events
            row, self._drag_row = self._drag_row, None
            self._scroll_to_row(row)
        if self._fresh:
            self._fresh = False
            drawn = self.escapes(full=False)
            if drawn:
                sys.stdout.write(SYNC_BEGIN + drawn + SYNC_END)
                sys.stdout.flush()
        if self._thread is not None and self._thread.is_alive():
            return
        self._start_rendering()

    def _start_rendering(self) -> None:
        """Start the rendering thread (see _render()) on the thumbnails
        the column shows that aren't rendered yet, if any - with no
        thread running, so it's also the moment to let go of ones long
        scrolled past (see SIDEBAR_KEPT_THUMBNAILS)."""
        viewer = self.viewer
        thumb_w_px, _thumb_rows, _slot_rows, slots = self._layout()
        self._follow(slots)
        visible = self._visible_pages(slots)
        self._forget_old(visible, thumb_w_px)
        missing = [p for p in visible if (p, thumb_w_px) not in self._images]
        if not missing:
            return
        if self._cache is None:
            self._cache = PageCache(viewer.tmpdir, viewer.doc_handler, size=4 * slots)
        self._thread = threading.Thread(
            target=self._render, args=(self._cache, self._images, missing, thumb_w_px),
            name="pdfless-thumbnails", daemon=True,
        )
        self._thread.start()

    def _forget_old(self, visible: range, thumb_w_px: int) -> None:
        """Keep at most SIDEBAR_KEPT_THUMBNAILS rendered thumbnails (and
        their encodings), the first-rendered going first - never the
        ones in view - so scrolling through a long document doesn't
        hold on to all of it. Only called with no rendering thread
        running (see _start_rendering()), which fills the same dict.

        Args:
            visible: the pages in view.
            thumb_w_px: the thumbnails' width now.
        """
        excess = len(self._images) - SIDEBAR_KEPT_THUMBNAILS
        if excess <= 0:
            return
        for key in list(self._images):
            if excess <= 0:
                break
            page, width = key
            if width == thumb_w_px and page in visible:
                continue
            del self._images[key]
            excess -= 1
        kept = {page for page, _width in self._images}
        for encoded in [k for k in self._encoded if k[0] not in kept]:
            del self._encoded[encoded]

    def _render(
        self, cache: PageCache, images: dict[tuple[int, int], Image.Image | None],
        pages: list[int], thumb_w_px: int,
    ) -> None:
        """The rendering thread's work (see poll()): each of `pages` at
        thumb_w_px wide, into `images` - the dict of the file it was
        started for, even if reset() has replaced it since.

        Args:
            cache: the thumbnails' PageCache.
            images: where to put them.
            pages: which to render.
            thumb_w_px: how wide.
        """
        for page in pages:
            try:
                img: Image.Image | None = cache.get(page, thumb_w_px, "width")
            except BaseException:  # SystemExit too - see die(); no thumbnail then
                img = None
            # A page that's an image of its own (an image file, a Quick
            # Look screenshot) was decoded at full size to make it - the
            # thumbnail is all that's needed of it here.
            cache.forget_native(page)
            images[(page, thumb_w_px)] = img
            self._fresh = True

    def start_picking(self) -> None:
        """s (see Viewer.focus_sidebar()): let the keys pick a thumbnail,
        starting from the page being viewed (see handle_key())."""
        self.picking = True
        self.picked = self.viewer.page
        self._redraw()

    def stop_picking(self) -> None:
        """Back to the keys acting on the page, the column staying up."""
        self.picking = False
        self._redraw()
        self.viewer.draw_status()

    def _redraw(self) -> None:
        """Draw the column now (if anything in it changed), and while
        picking, say on the status line what the keys do."""
        drawn = self.escapes(full=False)
        if drawn:
            sys.stdout.write(SYNC_BEGIN + drawn + SYNC_END)
            sys.stdout.flush()
        if self.picking:
            self.viewer.draw_status(
                f"ENTER to go to page {self.picked}, q to stop - {self.picked}/{self.viewer.npages}"
            )

    def handle_key(self, key: str) -> None:
        """A key pressed while picking a thumbnail - the same keys as the
        table of contents and the file list (see _Overlays.handle_key()):

        - ENTER goes to the picked page, and stops picking;
        - t or T goes to it in text mode (see Viewer.handle_global_key());
        - q, ESC or s stops picking, the column staying up; S closes it;
        - ^L (or a focus change) repaints the screen;
        - the usual line/window keys, d/u and g/G/</>/HOME/END move the
          pick.

        Everything else is swallowed, as the page is waiting underneath."""
        viewer = self.viewer
        _thumb_w_px, _thumb_rows, _slot_rows, slots = self._layout()
        moves = {"g": -viewer.npages, "<": -viewer.npages, "HOME": -viewer.npages,
                 "G": viewer.npages, ">": viewer.npages, "END": viewer.npages,
                 "d": slots, "\x04": slots, "u": -slots, "\x15": -slots}
        # ENTER before the line keys: "\r" is one of FORWARD_LINE_KEYS.
        if key in ("\r", "\n"):
            self.picking = False
            if self.picked != viewer.page:
                viewer.go_page(self.picked, 0)
            viewer.refresh()
        elif key in ("q", "\x1b"):
            self.stop_picking()
        elif key == "S":
            viewer.toggle_sidebar()
        elif key == "s":
            self.stop_picking()  # back to the page, the column staying up
        elif key in ("t", "T"):
            # Into text mode (T: cleared for copying) at the picked page.
            self.picking = False
            if self.picked != viewer.page:
                viewer.go_page(self.picked, 0)
            viewer.handle_global_key(key)
        elif key in ("\x0c", "FOCUS_IN"):
            viewer._invalidate_screen()
            viewer.refresh()
            self._redraw()
        else:
            if key in FORWARD_LINE_KEYS:
                delta = 1
            elif key in BACKWARD_LINE_KEYS:
                delta = -1
            elif key in FORWARD_WINDOW_KEYS:
                delta = slots
            elif key in BACKWARD_WINDOW_KEYS:
                delta = -slots
            else:
                delta = moves.get(key, 0)
            picked = max(1, min(viewer.npages, self.picked + delta))
            if picked != self.picked:
                self.picked = picked
                self._redraw()

    def handle_mouse(self, kind: str, col: int, row: int) -> None:
        """A mouse event over the column (or a drag its scrollbar
        started): a click on a thumbnail goes to its page (and stops
        picking one with the keys, if they were), a click or drag on the
        scrollbar scrolls the column there, and the wheel scrolls it a
        page at a time.

        Args:
            kind: the event (see decode_sgr_mouse()).
            col, row: the cell it's at.
        """
        viewer = self.viewer
        _thumb_w_px, _thumb_rows, slot_rows, slots = self._layout()
        if kind == "MOUSE_DRAG":
            # A drag arrives as a burst of motion events, and each redraw
            # sends every thumbnail in view - so only the last is acted
            # on, once the burst lets up (here, or in poll()).
            self._drag_row = row
            ready, _, _ = select.select([viewer.fd], [], [], 0)
            if not ready:
                self._drag_row = None
                self._scroll_to_row(row)
        elif kind == "MOUSE_RELEASE":
            self.dragging = False
            if self._drag_row is not None:
                row, self._drag_row = self._drag_row, None
                self._scroll_to_row(row)
        elif kind == "MOUSE_CLICK" and col == self.columns() and viewer.npages > slots:
            self.dragging = True
            self._scroll_to_row(row)
        elif kind == "MOUSE_CLICK":
            page = self.first + (row - 1) // slot_rows
            if (row - 1) // slot_rows < slots and page <= viewer.npages:
                self.picking = False
                if page != viewer.page:
                    viewer.go_page(page, 0)
                viewer.refresh()
        elif kind in ("MOUSE_WHEEL_UP", "MOUSE_WHEEL_DOWN"):
            step = -1 if kind == "MOUSE_WHEEL_UP" else 1
            first = max(1, min(self.first + step, viewer.npages - slots + 1))
            if first != self.first:
                self.first = first
                drawn = self.escapes(full=False)
                sys.stdout.write(SYNC_BEGIN + drawn + SYNC_END)
                sys.stdout.flush()


class Viewer:
    def __init__(
        self, files: list[DocumentHandler | str], file_index: int, page: int, tmpdir: str, fd: int,
        fit: str | None = 'width', options: ViewerOptions | None = None, **option_kwargs: Any,
    ) -> None:
        """`options` (a ViewerOptions) says how to start up; for
        convenience - the tests build Viewers this way throughout - the
        individual options can be given as keyword arguments instead
        (`fit` positionally, as ever), which make up the ViewerOptions."""
        if options is None:
            options = ViewerOptions(fit=fit, **option_kwargs)
        elif option_kwargs:
            raise TypeError("pass either options= or individual option keywords, not both")
        self.options = options
        self.files = files  # [DocumentHandler | path str, ...] - one per
        # CLI argument. main() only actually sniffs the one file it's
        # about to display first (a DocumentHandler there already,
        # including in every test that constructs a Viewer directly);
        # every other entry is still a bare path string, sniffed lazily
        # by _classify() the moment a later go_to_file()/next_file()/
        # previous_file() actually navigates to it - see there. This is
        # what keeps startup with a large batch of files from decoding
        # every single one of them just to show the first.
        self.file_index = file_index
        self._handler_cache: dict[int, DocumentHandler | None] = {}
        # Background preparation of the file after the one on screen (see
        # _schedule_prefetch()): each index it was started for, with an
        # Event set once it's finished, and the thread doing it (at most
        # one at a time).
        self._prefetching: dict[int, threading.Event] = {}
        # And of the page next to the one(s) on screen (see
        # _schedule_page_prefetch()): the thread doing it (at most one at
        # a time), and which way pages were last turned (+1/-1), so the
        # page that way is prepared first.
        self._page_prefetch_thread: threading.Thread | None = None
        self._page_direction = 1
        self._page_direction_from = 1  # the page that direction was last judged from
        self._prefetch_thread: threading.Thread | None = None  # file_index -> DocumentHandler | None,
        # populated by _classify() the first time each lazy (path-string)
        # entry above is actually visited - None means that one turned
        # out not to be a usable file at all.
        self.tmpdir = tmpdir
        self.follow = options.follow  # -f/--follow, or toggled at runtime with F
        # (see toggle_follow()) - poll_follow() reloads the file whenever
        # its mtime changes while this is on
        self._displayed_mtime: float | None = None  # the current file's mtime when
        # what's on screen was read from it - set by _set_current_file(),
        # and moved on by _reload_if_changed() whenever it reloads
        self.file_missing = False  # the file was found deleted or moved
        # since it was opened - see mark_file_missing(); while True, the
        # screen is left blank but for a status line saying so
        self._follow_path: str | None = None  # the file poll_follow() is watching...
        self._follow_checked = 0.0  # ...and when it last looked (monotonic)
        self.quit_if_one_screen = options.quit_if_one_screen  # -F/--quit-if-one-
        # screen - set before _set_current_file() below so _ViewerProgress
        # can already see it: whether this first file ends up dumped-and-
        # quit or interactive isn't known until after that render
        # completes, so its progress reporting stays silent either way
        # rather than risk printing "converting via LibreOffice..."-style
        # status lines into what might turn out to be a plain dump
        # (see _ViewerProgress and dump_and_quit()).
        self.entered_alt_screen = False  # set True by run_viewer() once it
        # switches into the alternate screen buffer - False on -F/--quit-
        # if-one-screen's dump-and-quit path, which never does, so
        # main()'s teardown knows there's nothing to restore
        self._dump_margin_rows = 0  # extra rows _recompute_geometry()/
        # _text_avail_rows() hold back beyond the usual status-line one -
        # set to 1 by run_viewer()'s quit_if_one_screen dump path (see
        # dump_and_quit()), which draws no status line but does write one
        # trailing \r\n of its own; without this margin, content sized to
        # exactly fill the terminal would make that \r\n force a one-line
        # scroll, pushing the dump's own top row out of view the instant
        # it's written
        self.debug = options.debug  # -d/--debug: print office-preview stage timing
        self.office_render_scale = options.office_render_scale  # --rendering-scale
        self.continuous = options.continuous  # -c/--continuous, or toggled at
        # runtime with c: stack consecutive pages one after another
        # (image mode), or the whole document's text with a separator
        # row between pages (a paginated text mode), instead of showing
        # one page at a time - see _normalize_continuous() and
        # _text_continuous()
        self.fd = fd
        self.fit = options.fit
        self.eol_mark = options.eol_mark  # --no-eol-mark: mark a real end-of-line
        # (NEWLINE_MARKER) in text mode - independent of text_wrap/-S, on
        # by default either way (see _draw_text_wrapped()/_unwrapped())
        self.wheel_scroll_step = options.wheel_scroll_step
        self.incremental_scroll = options.incremental_scroll  # --no-incremental-
        # scroll: force every redraw through _draw()'s full-viewport
        # path, skipping the _scroll_shift_rows()/_draw_shifted()
        # shortcut - an escape hatch for a terminal where that shortcut
        # (confirmed by hand on iTerm2 - see _scroll_shift_rows()) turns
        # out not to hold
        # A real (not just placeholder-0) self.rows/cols before
        # _set_current_file() below is what lets its lazy office-preview
        # render (if the first file needs one) show live status-line
        # progress the same as any later one (e.g. a :n/:p switch) does -
        # draw_status() needs both to lay out that line. The rest of the
        # geometry (avail_height_px, cell sizes, ...) still isn't known
        # this early - that needs self.cache, set up moments from now -
        # but the status line alone doesn't touch any of that.
        # S/--sidebar - made first: every file switch resets it (see
        # _set_current_file()), the very first one below included.
        self.sidebar: _Sidebar = _Sidebar(self, options.sidebar)  # S/--sidebar
        self.rows, self.cols, _, _ = get_term_cells(fd)
        self.page = page
        self._set_current_file()  # sets path/name/npages/cache
        if self.follow:
            self._start_following()
        # For an office-kind first file, self.npages was just determined
        # above (by _ensure_office_pages(), lazily) rather than known
        # ahead of time the way main() clamps a PDF/image/text file's
        # start page against - so clamp it here instead, now that it's
        # known.
        self.page = max(1, min(self.npages, self.page))
        self.encode_cache = EncodeCache()
        self.scroll = 0
        self.zoom = 1.0
        self.resized = True
        # Loaded by _load_page() before image mode ever reads it - never
        # read as None, so not typed as optional.
        self.img: Image.Image = None  # type: ignore[assignment]
        self.avail_height_px = 0
        self.cell_h_px = 1
        self.cell_w_px = 1
        # self.rows/self.cols are already set, above - see the comment there
        self.crop_width = 0
        self.x_offset = 0
        # -c/--continuous image mode's current arrangement of pages on
        # screen, rebuilt by _normalize_continuous() before every draw:
        # [(page, top_px, img), ...], top_px being where that page's top
        # edge sits relative to the top of the viewport (negative for
        # the first one once it's scrolled partway out of view). Unused
        # (left empty) outside continuous mode.
        self._layout: list[tuple[int, int, Image.Image]] = []
        self._view_height = 0  # how much of the viewport the layout fills
        self._view_width = 0  # the widest page in it - the pan range
        # -c/--continuous text mode (see _text_continuous()): every
        # page's text, fetched once per file via extract_text_pages() and
        # kept so toggling in and out of text mode doesn't re-run
        # pdftotext each time - None until first needed.
        self._text_pages: list[list[str]] | None = None
        # Where each page's block starts in self.text_lines while the
        # continuous text view is showing - for page 2 onward, that's
        # the separator row just above its first line - or None when it
        # isn't (one page's text at a time, or a non-paginated handler).
        self._text_page_starts: list[int] | None = None
        # (page, text_scroll) of the last go-to in that view - see
        # _sync_text_page().
        self._text_page_gone_to: tuple[int, int] | None = None
        self._text_separator_lines: frozenset[int] = frozenset()
        self._text_max_page_lines = 0  # the -N gutter's width, per page
        self.overlays: _Overlays = _Overlays(self)  # the help and the table of contents
        self.links: _Links = _Links(self)  # hyperlinks, and the [/] jump history
        self.search: _Search = _Search(self)  # "/" and "?"
        # The last `t`/`T`: where it left the view in the mode it
        # switched to (see _view_spot()), and the (page, scroll) of the
        # mode it came from - so switching straight back returns there
        # exactly (see _switch_back_spot()).
        self._mode_switch: tuple[tuple[bool, int, int], tuple[int, int]] | None = None
        # A plain text file has no image view at all - it's permanently
        # "in text mode", the same rendering PDF's `t` key switches to.
        self.text_mode = self.doc_handler.starts_in_text_mode()
        self.text_lines: list[str] = []
        self.text_scroll = 0
        self.text_scroll_min = 0
        self.text_scroll_max = 0
        self.text_x_offset = 0
        self.text_x_offset_min = 0
        self.text_x_offset_max = 0
        self.text_max_line_width = 0
        self.text_border = self._default_text_border()  # border around the
        # page's edges, in text mode; can be swept up along with the text
        # if you select-and-copy it, so it's toggled off with --no-border
        # (or on/off any time with the B key) - see _default_text_border()
        self.text_wrap = self._default_text_wrap()  # soft-wrap long lines
        # instead of panning across them (h/l/H/L) - see
        # _default_text_wrap(); no border while wrapped (see
        # _draw_text_wrapped()), regardless of text_border
        self._display_rows: list[tuple[int, int, int]] | None = None  # lazily built by _ensure_display_rows(),
        # only while text_wrap is on - [(line_idx, start, end), ...], one
        # entry per on-screen row
        self.line_numbers = options.line_numbers  # -N/--line-numbers: right-
        # aligned gutter at the start of each row - see
        # _line_number_gutter_width(); no per-kind default (unlike
        # border/wrap/eol_mark) since there's no kind numbering wouldn't
        # make sense for
        self._copy_mode_saved: tuple[bool, bool, bool, bool] | None = None  # while "C" has the decorations
        # off, what to put back on the next press - see toggle_copy_mode()
        self.mouse: _Mouse = _Mouse(self)
        self.scrollbar = options.scrollbar  # --no-scrollbar: a column on the
        # terminal's right edge showing scroll position - in both image
        # mode (_draw()) and text mode (_draw_text_wrapped()/
        # _draw_text_unwrapped()); "r" toggles it either way (see
        # toggle_scrollbar()), applying uniformly to both since it's
        # handled at run_viewer()'s top level rather than per-mode
        self._last_viewport_w = 0
        self._last_viewport_h = 0
        self._last_viewport_set = False
        self._last_char_h = 0
        self._last_marker_bounds: tuple[int, int, int, int] | None = None  # (row0, col0, row1, col1) or None
        # What _draw() last actually put on screen, for _scroll_shift_rows()
        # to compare against - only trusted while _last_viewport_set is
        # True, which every place that overwrites the screen with
        # something else (help, text mode, a resize, ...) already turns
        # off, so these never need resetting anywhere but here.
        self._last_page: int | None = None
        self._last_x_offset: int | None = None
        self._last_zoom_key: int | None = None
        self._last_scroll: int | None = None
        self._last_fit_key: tuple[str | None, bool] | None = None

    def _invalidate_screen(self) -> None:
        """Forget what's on screen, so the next image-mode draw starts
        from a full clear (no incremental shift - see
        _scroll_shift_rows()) and doesn't try to erase a search marker
        that something else has already painted over - for anything that
        overwrites the screen with something else (help, text mode, a
        file switch, a mode toggle)."""
        self._last_viewport_set = False
        self._last_marker_bounds = None
        self.sidebar.forget_drawn()

    def request_resize(self) -> None:
        self.resized = True
        # A box's (the help's, the table of contents', the file list's)
        # size/position and the underlying page raster are both stale after a resize; simplest
        # is to just drop back to the normal view, which always does a
        # full redraw at the new size.
        self.overlays.active = None

    def _classify(self, index: int, wait: bool = True, debug: bool | None = None) -> DocumentHandler | None:
        """files[index]'s DocumentHandler - already one (the file about
        to be shown first, or a test harness's direct handler) is
        returned as-is; a bare path string (every other file - see
        __init__) is sniffed once, via _sniff_file(), and the result
        cached, so a later revisit to the same file doesn't repeat the
        work. None means that file turned out not to be usable at all -
        go_to_file() treats it the same as an out-of-range index.

        If that file is being prepared in the background right now (see
        _schedule_prefetch()), this waits for it to finish - with a
        spinner on the status line - rather than classifying and
        rendering it a second time alongside. `wait=False` is the
        background prefetch itself asking; `debug` overrides self.debug
        for the sniff (the prefetch keeps -d's output off the screen)."""
        pending = self._prefetching.get(index)
        if wait and pending is not None and not pending.is_set():
            name = os.path.basename(str(self.files[index]))
            with _ViewerProgress(self).spin(f"{name}: finishing its background render..."):
                while not pending.wait(0.1):
                    flush_background_debug()
            flush_background_debug()
        entry = self.files[index]
        if isinstance(entry, DocumentHandler):
            return entry
        if index not in self._handler_cache:
            try:
                self._handler_cache[index] = _sniff_file(
                    entry, self.tmpdir, debug=self.debug if debug is None else debug,
                )
            except UnusableFile:
                self._handler_cache[index] = None
        return self._handler_cache[index]

    def _schedule_prefetch(self) -> None:
        """With several files open, start preparing the one after the
        file now on screen in the background - classifying it and, for a
        RenderedDocument (Word/Excel/.../SVG/Markdown), rendering it - so
        that :n/} lands on it without waiting for soffice/Quick Look/
        Chrome/WeasyPrint. Called once a file is on screen. Only one
        prefetch runs at a time, and each file is prefetched at most
        once; switching to a file still being prepared waits for it (see
        _classify()). A daemon thread, so quitting never waits for it."""
        nxt = self.file_index + 1
        if nxt >= len(self.files) or nxt in self._prefetching:
            return
        if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
            return
        done = threading.Event()
        self._prefetching[nxt] = done
        self._prefetch_thread = threading.Thread(
            target=self._prefetch_file, args=(nxt, done),
            name=f"pdfless-prefetch-{nxt}", daemon=True,
        )
        self._prefetch_thread.start()

    def _prefetch_file(self, index: int, done: threading.Event) -> None:
        """_schedule_prefetch()'s background work for files[index]: no
        progress line, so nothing is drawn over the file on screen - but
        under -d/--debug, the same stage timings a foreground render
        prints, bracketed by a start and a finish line, queued for the
        main thread to print (see _debug_log()). Any failure is left for
        the real switch to that file to hit again, visibly."""
        name = os.path.basename(str(self.files[index]))
        where = f"file {index + 1}/{len(self.files)}"
        if self.debug:
            _debug_log(f"{name}: preparing in the background ({where})")
        t_start = time.monotonic()
        try:
            handler = self._classify(index, wait=False, debug=self.debug)
            if isinstance(handler, RenderedDocument):
                handler.ensure_pages(
                    self.tmpdir, debug=self.debug, render_scale=self.office_render_scale,
                    progress=_ProgressLine(enabled=False),
                )
            outcome = (
                "not a usable file" if handler is None
                else f"prepared in the background in {time.monotonic() - t_start:.2f}s"
            )
        except Exception as e:
            outcome = f"background preparation failed ({e})"
        finally:
            done.set()
        if self.debug:
            _debug_log(f"{name}: {outcome}")

    def _is_usable(self, index: int) -> bool:
        """Whether files[index] can actually be switched to -
        go_to_file() treats a False return exactly like an
        out-of-range index. Beyond just being classifiable
        (_classify()), this also forces page_count() to resolve for
        real: for a password-protected PdfDocument, that's where the
        user is actually prompted (see PdfDocument._ensure_unlocked()),
        right here as part of landing on that file, rather than a
        moment later inside _set_current_file() - go_to_file()'s own
        step logic then treats a cancelled prompt the same as any
        other unusable file (skipped over by :n/:p's auto-skip, or
        reported for a direct jump)."""
        handler = self._classify(index)
        if handler is None:
            return False
        try:
            handler.page_count()
        except UnusableFile:
            return False
        return True

    def _set_current_file(self) -> None:
        """Point path/name/npages/doc_handler/cache at
        self.files[self.file_index] - just the file's identity, not the
        page/zoom/search/etc. state, which __init__ sets up once and
        go_to_file() resets explicitly on every later switch. Assumes
        self.file_index already names a usable file - go_to_file() (and
        __init__, for the first one) only ever lands here once
        _classify() has confirmed that."""
        handler = self._classify(self.file_index)
        assert handler is not None  # see the docstring
        self.path = handler.path
        self.name = os.path.basename(handler.path)
        self.doc_handler = handler
        # Taken before reading/rendering anything, so a change made while
        # that's under way still shows up as newer than what's displayed.
        self._displayed_mtime = self._current_mtime()
        self.file_missing = False  # whatever the previous file's state
        # page_count() is None for a RenderedDocument until
        # _ensure_office_pages() below actually renders it - 0 until then.
        self.npages = handler.page_count() or 0
        self._ensure_office_pages()  # a no-op unless doc_handler is a
        # RenderedDocument, and sets self.npages for real in that case
        # (memoized on the handler itself - see RenderedDocument.pages -
        # so a revisit to an already-rendered file is still cheap)
        self.cache = PageCache(self.tmpdir, handler)
        self.sidebar.reset()
        self._text_pages = None  # the previous file's text
        self._text_page_starts = None
        self._text_separator_lines = frozenset()
        self._layout = []

    def _ensure_office_pages(self) -> None:
        """With multiple files on the command line, an office-kind one
        (a RenderedDocument - Word/Excel/PowerPoint/etc. via Quick Look,
        ODF via soffice, SVG, Markdown) is only actually
        rendered the moment it's about to be displayed - not upfront for
        every such file regardless of whether it's ever looked at - so
        this is where that render happens, the first time doc_handler is
        a RenderedDocument. A no-op every time after that (doc_handler.
        ensure_pages() remembers its own result on the handler itself,
        the same as a file that's always been rendered up front would
        be) other than resetting self.npages, which is cheap.

        self.quit_if_one_screen means this is the very first file, under
        -F/--quit-if-one-screen, and whether the session ends up
        dump-and-quit or interactive isn't decided yet - a plain
        _ProgressLine (stderr, \\r-overwriting in place, no absolute
        cursor positioning) posts progress there instead of the usual
        _ViewerProgress (the interactive status line), so a slow
        LibreOffice/Quick Look conversion still shows something moving
        without leaving anything for a subsequent dump to clean up - see
        Viewer.dump_and_quit() and run_viewer()'s own quit_if_one_screen
        handling, which resets this flag once the outcome is known."""
        if not isinstance(self.doc_handler, RenderedDocument):
            return
        progress = (
            _ProgressLine(not self.debug) if self.quit_if_one_screen
            else _ViewerProgress(self)
        )
        pages = self.doc_handler.ensure_pages(
            self.tmpdir, debug=self.debug,
            render_scale=self.office_render_scale, progress=progress,
        )
        if not pages:
            missing = [tool for tool in ("pdfinfo", "pdftoppm") if shutil.which(tool) is None]
            if missing:
                # Only a PDF given on the command line checks for poppler
                # up front (see check_deps()), but this document is shown
                # through a PDF of its own - LibreOffice's, Chrome's or
                # WeasyPrint's - so it needs it too.
                message = f"needs poppler's {' and '.join(missing)} ({POPPLER_INSTALL_HINT})"
            else:
                message = "rendering failed - see -d/--debug for details"
            pages = self.doc_handler._render_error_placeholder(self.tmpdir, message)
            self.doc_handler.pages = pages  # remember the placeholder too - don't retry every revisit
        self.npages = len(pages)

    @property
    def is_pdf(self) -> bool:
        return isinstance(self.doc_handler, PdfDocument)

    def next_file(self) -> None:
        self.go_to_file(self.file_index + 1, "no next file", step=1)

    def previous_file(self) -> None:
        self.go_to_file(self.file_index - 1, "no previous file", step=-1)

    def go_to_file(
        self, index: int, boundary_message: str = 'no such file', step: int = 0,
    ) -> None:
        """Switch to files[index], starting fresh at its first page -
        zoom, search, link history, and text mode all reset, the same
        as if pdfless had been started fresh on that file. A no-op
        (with a status message) if index is out of range.

        `step` (+-1 from next_file()/previous_file(), 0 from a direct
        jump like x/X) says what to do if files[index] turns out not to
        be usable at all (_is_usable() says no - a lazily-sniffed file,
        not yet known either way, or a password-protected PDF whose
        prompt (see _is_usable()) was cancelled): 0 just reports
        boundary_message right there, the same as an out-of-range index;
        +-1 instead keeps stepping in that direction looking for the
        next usable one, only giving up once the index itself runs out
        of range."""
        while True:
            if index < 0 or index >= len(self.files):
                self.draw_status(boundary_message)
                return
            if self._is_usable(index):
                break
            if step == 0:
                self.draw_status(boundary_message)
                return
            index += step
        if self._copy_mode_saved is not None:
            # Copy mode ("C", or "T") belongs to the file it was turned on
            # in: put back what it hid, as exit_text_mode() would, rather
            # than carry it (and the stale values to restore) over - the
            # next file's own defaults for the border/wrap follow below.
            scrollbar = self.scrollbar
            (
                self.eol_mark, self.text_border, self.scrollbar, self.line_numbers
            ) = self._copy_mode_saved
            self._copy_mode_saved = None
            if self.scrollbar != scrollbar:
                self._recompute_geometry()  # its column comes back from the page's width
        self.file_index = index
        self._set_current_file()
        self.encode_cache.clear()
        self.search.reset()
        self._mode_switch = None
        self.links.reset()
        self.overlays.forget_outline()
        self.text_mode = self.doc_handler.starts_in_text_mode()
        self.text_border = self._default_text_border()
        self.text_wrap = self._default_text_wrap()
        self._display_rows = None
        # Mouse reporting is only useful in the page image (clicking
        # hyperlinks, wheel scroll); off in any kind of text view, the
        # same as enter_text_mode()/exit_text_mode() do for a PDF's `t`
        # toggle - needed here too since a text-kind file goes straight
        # into text_mode without ever calling those.
        sys.stdout.write(MOUSE_OFF if self.text_mode else MOUSE_ON)
        self.zoom = 1.0
        self.page = 1
        self.scroll = 0
        self.x_offset = 0
        self._invalidate_screen()
        if self.text_mode:
            self._load_text_page()
        else:
            self._load_page()
        self.refresh()  # its normal status line already includes "file i/N"
        self._schedule_prefetch()

    def _recompute_geometry(self) -> None:
        rows, cols, _, _ = get_term_cells(self.fd)
        width_px, height_px = get_pixel_size(self.fd)
        cell_h = max(1, height_px // max(1, rows))
        cell_w = max(1, width_px // max(1, cols))
        self.rows = rows
        self.cols = cols
        # One cell's width held back for the scrollbar (see _draw()),
        # while it's on - crop_width/x_offset/pan bounds/click hit-
        # testing are all derived from base_width_px (see _load_page()),
        # so reserving it here is enough to keep the image itself out
        # of that column everywhere else.
        self.base_width_px = width_px - (cell_w if self.scrollbar else 0)
        # The sidebar's columns likewise - see _image_col0() for where
        # the page image starts then.
        self.base_width_px -= self.sidebar.columns() * cell_w
        self.cell_h_px = cell_h
        self.cell_w_px = cell_w
        self.avail_height_px = cell_h * max(1, rows - 1 - self._dump_margin_rows)
        self.cache.clear()
        self.encode_cache.clear()
        self._invalidate_screen()
        self._last_char_h = 0
        self._display_rows = None  # stale - self.cols may have changed,
        # which is what wrapping is measured against

    def _image_col0(self) -> int:
        """The terminal column (1-based) the page image starts at - right
        of the sidebar, when it's up (see _Sidebar.columns())."""
        return self.sidebar.columns() + 1

    def _page_target(self) -> tuple[int, str]:
        """(target_px, fit) for PageCache.get() at the current fit mode
        and zoom - the same for every page."""
        if self.fit == "height":
            return max(1, round(self.avail_height_px * self.zoom)), "height"
        return max(1, round(self.base_width_px * self.zoom)), "width"

    def _page_image(self, page: int) -> Image.Image:
        """`page`'s raster at the current fit mode and zoom - the one
        _load_page() shows, and, under -c/--continuous, each of the
        other pages stacked around it (see _build_continuous_layout())."""
        target_px, fit = self._page_target()
        return self.cache.get(page, target_px, fit=fit)

    def _schedule_page_prefetch(self) -> None:
        """In image mode, start rendering a page next to the one(s) on
        screen in the background - the one after the last page shown and
        the one before the first, whichever way pages were last turned
        first - so that turning to it only has to encode what's already
        rasterized. Called on every pass of run_viewer()'s input loop:
        a no-op while one is already under way, or once both neighbors
        are in the page cache (see PageCache.has()). Showing a page
        still being rendered waits for that render instead of starting
        a second one (see PageCache.get()). A daemon thread, so quitting
        never waits for it."""
        # The help or the table of contents being up doesn't matter: the
        # page under the help is still the one read next, and one page's
        # render is all a jump from the table of contents can waste.
        if self.text_mode or self.file_missing or self.npages <= 1:
            return
        if self._page_prefetch_thread is not None and self._page_prefetch_thread.is_alive():
            return
        # Which way the pages were last turned, judged from how far
        # self.page moved since the last call.
        if self.page != self._page_direction_from:
            self._page_direction = 1 if self.page > self._page_direction_from else -1
            self._page_direction_from = self.page
        last_shown = self._layout[-1][0] if self.continuous and self._layout else self.page
        after, before = last_shown + 1, self.page - 1
        target_px, fit = self._page_target()
        for page in ((after, before) if self._page_direction > 0 else (before, after)):
            if 1 <= page <= self.npages and not self.cache.has(page, target_px, fit):
                break
        else:
            return
        self._page_prefetch_thread = threading.Thread(
            target=self._prefetch_page, args=(self.cache, page, target_px, fit),
            name=f"pdfless-page-prefetch-{page}", daemon=True,
        )
        self._page_prefetch_thread.start()

    def _prefetch_page(self, cache: PageCache, page: int, target_px: int, fit: str) -> None:
        """_schedule_page_prefetch()'s background work: render `page`
        into `cache` (the one it was scheduled for - by the time this
        runs, self.cache may already be another file's). Under
        -d/--debug, a start and a finish line, queued for the main
        thread to print (see _debug_log()). Any failure is left for the
        real visit to that page to hit again, visibly."""
        if self.debug:
            _debug_log(f"page {page}: rendering in the background")
        t_start = time.monotonic()
        try:
            cache.get(page, target_px, fit)
            outcome = f"rendered in the background in {time.monotonic() - t_start:.2f}s"
        except BaseException as e:  # SystemExit too - see die()
            outcome = f"background render failed ({e})"
        if self.debug:
            _debug_log(f"page {page}: {outcome}")

    def _load_page(self) -> None:
        self.encode_cache.clear()
        self.img = self._page_image(self.page)
        self.crop_width = min(self.img.width, self.base_width_px)
        # Keep whatever horizontal position you panned to (h/l/H/L),
        # only clamping it to the image that just got loaded. Turning a
        # page mustn't move the view sideways: with -h on a wide
        # document (a slide deck, say) every page is wider than the
        # terminal, so re-centering here would undo an "H" the moment
        # you pressed j - and j is a page turn there, since a
        # height-fitted page has nothing left to scroll. Zoom anchors
        # itself deliberately instead; see set_zoom().
        self.x_offset = max(0, min(self.x_offset, self.img.width - self.crop_width))
        self.scroll_max = max(0, self.img.height - self.avail_height_px)
        if self.continuous:
            # scroll_max still means "this page's bottom edge at the
            # bottom of the screen" here (J/G), but scrolling itself may
            # carry on past it into the gap and the next page -
            # _normalize_continuous() moves on to that page once the
            # position actually leaves this one.
            self.scroll = min(self.scroll, self.img.height)
        else:
            self.scroll = min(self.scroll, self.scroll_max)

    def _continuous_gap_px(self) -> int:
        """Height of the gray band between two stacked pages under
        -c/--continuous - a third of a text row, so it reads as a
        boundary without costing much vertical space."""
        return max(2, self.cell_h_px // 3)

    def _set_top_page(self, page: int) -> None:
        """Make `page` the one at the top of the viewport (self.page/
        self.img/self.scroll_max) without _load_page()'s other resets -
        _normalize_continuous()'s own step as scrolling crosses from
        one page into the next, which mustn't throw away the encode
        cache (the viewport's content hasn't changed, just which page
        its position is counted from) or touch self.scroll."""
        self.page = page
        self.img = self._page_image(page)
        self.scroll_max = max(0, self.img.height - self.avail_height_px)

    def _normalize_continuous(self) -> None:
        """Under -c/--continuous, bring (self.page, self.scroll) back
        to its one canonical form and rebuild the on-screen layout from
        it. Anything is free to leave self.scroll anywhere - past the
        bottom of self.page (scroll_down()), negative (scroll_up()), or
        so near the end of the document that the screen wouldn't be
        full - and this sorts it out afterwards, the same way for every
        caller:

        1. While the position is past this page and its gap, move on to
           the next page (counting the position from its top instead);
           while it's negative, move back to the previous one.
        2. If what's left from here to the end of the document is
           shorter than the screen, pull the position back by the
           difference (possibly into earlier pages) - the continuous
           equivalent of scroll_max, so the last page's bottom edge
           stops at the bottom of the screen instead of scrolling off.
        3. Rebuild self._layout for the result.

        Only ever touches pages that end up on screen (or were about to
        be), so it never has to rasterize the whole document just to
        know where everything is."""
        gap = self._continuous_gap_px()
        avail = self.avail_height_px
        while self.page < self.npages and self.scroll >= self.img.height + gap:
            self.scroll -= self.img.height + gap
            self._set_top_page(self.page + 1)
        while self.scroll < 0 and self.page > 1:
            self._set_top_page(self.page - 1)
            self.scroll += self.img.height + gap
        self.scroll = max(0, self.scroll)

        # Step 2: how much content there is from the top of the screen
        # down to the end of the document - only as far as needed to
        # know whether it fills the screen.
        remaining = self.img.height - self.scroll
        page = self.page
        while remaining < avail and page < self.npages:
            page += 1
            remaining += gap + self._page_image(page).height
        if remaining < avail:
            self.scroll -= avail - remaining
            while self.scroll < 0 and self.page > 1:
                self._set_top_page(self.page - 1)
                self.scroll += self.img.height + gap
            self.scroll = max(0, self.scroll)
        self._build_continuous_layout()

    def _build_continuous_layout(self) -> None:
        """self._layout (see __init__) for the current, already
        normalized, position - plus what depends on it: the pan range
        (self._view_width, the widest page on screen - pages can differ
        in width under fit-to-height), self.crop_width/x_offset clamped
        to it, and self._view_height."""
        gap = self._continuous_gap_px()
        layout = []
        top = -self.scroll
        page = self.page
        while page <= self.npages and top < self.avail_height_px:
            img = self.img if page == self.page else self._page_image(page)
            layout.append((page, top, img))
            top += img.height + gap
            page += 1
        self._layout = layout
        _, last_top, last_img = layout[-1]
        self._view_height = max(1, min(self.avail_height_px, last_top + last_img.height))
        self._view_width = max(img.width for _, _, img in layout)
        self.crop_width = min(self._view_width, self.base_width_px)
        self.x_offset = max(0, min(self.x_offset, self._view_width - self.crop_width))
        # Every page on screen has to stay in the page cache from one
        # draw to the next, or a zoomed-out view with many small pages
        # would re-rasterize some of them on every single scroll step.
        self.cache.size = max(self.cache.size, len(layout) + 2)

    def _content_width(self) -> int:
        """How wide the pannable content is - the one page's own width,
        or the widest page on screen under -c/--continuous (see
        _build_continuous_layout())."""
        return self._view_width if self.continuous and self._layout else self.img.width

    def _max_x_offset(self) -> int:
        """The furthest right the pan can go - the content's own width
        (see _content_width()) past what fits on screen."""
        return max(0, self._content_width() - self.crop_width)

    def _set_x_offset(self, x_offset: int) -> None:
        """Pan to `x_offset`, clamped to 0.._max_x_offset()."""
        self.x_offset = max(0, min(self._max_x_offset(), x_offset))

    def _clamp_image_scroll(self, scroll: int) -> int:
        """A target scroll position for the current page, clamped to
        what the view allows: 0..scroll_max normally, or left as-is
        (apart from the top of the document) under -c/--continuous,
        where _normalize_continuous() sorts out whatever lands past this
        page's own end on the next draw."""
        if self.continuous:
            return scroll
        return max(0, min(self.scroll_max, scroll))

    def _view_crop(self, top: int, height: int) -> Image.Image:
        """The strip of the viewport from `top` to `top + height`
        (pixels down from its top edge), self.crop_width wide at the
        current pan - one page's own crop normally, or under
        -c/--continuous a fresh image with every page in self._layout
        that overlaps the strip pasted in, over a CONTINUOUS_GAP_COLOR
        background that shows through between pages (and beside any
        narrower than the widest one)."""
        if not self.continuous:
            return self.img.crop((
                self.x_offset, self.scroll + top,
                self.x_offset + self.crop_width, self.scroll + top + height,
            ))
        canvas = Image.new("RGB", (self.crop_width, height), CONTINUOUS_GAP_COLOR)
        for _page, page_top, img in self._layout:
            y0 = max(top, page_top)
            y1 = min(top + height, page_top + img.height)
            x1 = min(img.width, self.x_offset + self.crop_width)
            if y1 <= y0 or x1 <= self.x_offset:
                continue  # this page doesn't reach into the strip
            piece = img.crop((self.x_offset, y0 - page_top, x1, y1 - page_top))
            canvas.paste(piece, (0, y0 - top))
        return canvas

    def _visible_page_top(self, page: int) -> int | None:
        """Where `page`'s top edge sits relative to the top of the
        viewport (see _view_crop()), or None if it isn't on screen."""
        if not self.continuous:
            return -self.scroll if page == self.page else None
        for p, top, _img in self._layout:
            if p == page:
                return top
        return None

    def set_zoom(self, new_zoom: float) -> None:
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, new_zoom))
        if new_zoom == self.zoom:
            return
        # Zoom around the middle of what's on screen, rather than around
        # the image's left edge: remember where the viewport's center
        # sits as a fraction of the page's width, then put it back there
        # once the resized image is in. _load_page() only clamps
        # x_offset now, so this is what decides where a zoom lands.
        # The same for the height - a zoom otherwise kept the scroll in
        # pixels, so at 50% down a page, + left you 43% down, and every
        # further + or - drifted further from what was being read.
        center_frac = (self.x_offset + self.crop_width / 2) / max(1, self.img.width)
        shown_h = min(self.avail_height_px, self.img.height)
        middle_frac = (self.scroll + shown_h / 2) / max(1, self.img.height)
        self.zoom = new_zoom
        self._load_page()
        self.x_offset = max(0, min(
            self.img.width - self.crop_width,
            round(center_frac * self.img.width - self.crop_width / 2),
        ))
        shown_h = min(self.avail_height_px, self.img.height)
        self.scroll = self._clamp_image_scroll(round(middle_frac * self.img.height - shown_h / 2))

    def reset_view(self) -> None:
        """"0": back to the untouched view of this page - zoom 1 and the
        left edge. The pan has to be put back by hand: _load_page() only
        clamps x_offset these days, and at zoom 1 a -h page can still be
        wider than the terminal, so there'd be nothing to clamp it to."""
        self.zoom = 1.0
        self._load_page()
        self.x_offset = 0

    def set_fit(self, fit: str) -> None:
        self.fit = fit
        self.zoom = 1.0
        self.scroll = 0
        self._load_page()
        self.x_offset = 0

    def pan(self, dx: int) -> None:
        self._set_x_offset(self.x_offset + dx)

    def _relayout(self) -> None:
        """Redo the layout after something on screen changed how much
        room is left for the content itself (the scrollbar's column,
        copy mode's four decorations) - everything a terminal resize
        recomputes, except that it keeps you where you were reading.
        refresh()'s resize path can't be reused for this: for a file
        that starts in text mode it re-reads the whole file, which
        starts you over at the top - fine when the terminal really did
        change size, but not for a keystroke that just hid a column."""
        # Read before the recompute, put back after it: a narrower or
        # wider content area re-splits every wrapped line, so the row
        # number text_scroll holds stops meaning the same place.
        top_line = self._top_text_line() if self.text_mode else 0  # unused otherwise
        self._recompute_geometry()
        self.resized = False
        if self.text_mode:
            self._scroll_to_text_line(top_line)
        else:
            self._load_page()

    def _text_line_at_row(self, row: int) -> int:
        """The raw text_lines index showing at scroll position `row` (see
        _text_row_for_line() for the unit)."""
        row = max(0, row)
        if not self.text_wrap:
            return min(row, max(0, len(self.text_lines) - 1))
        display_rows = self._ensure_display_rows()
        if not display_rows:
            return 0
        return display_rows[min(row, len(display_rows) - 1)][0]

    def _text_reading_line(self) -> int:
        """The text mode line being read: the one a quarter of the way
        down the screen, where a jump puts its target (see
        _scroll_text_to_row())."""
        return self._text_line_at_row(self.text_scroll + self._text_avail_rows() // 4)

    def _image_point_for_text(self) -> tuple[int, float | None] | None:
        """Where on the page image text mode's reading line (see
        _text_reading_line()) is, for `t` back to image mode to show the
        same place. In a text mode split into pages, it's the printed
        line with the same text on the same page, found through the
        search index (see PdfDocument.screen_lines()) - or, failing that,
        as far down the page as the line is down its text. In one
        showing the document's source (Markdown), it's as far between
        the bookmarks of the headings before and after it as the line is
        between those headings (see _Overlays.source_anchors()).

        Returns:
            (page, y_pt) - y_pt in PDF points from the page's top, None
            for its top - or None if there's no telling.
        """
        line = self._text_reading_line()
        if not self.doc_handler.text_mode_is_paginated():
            anchors = self.overlays.source_anchors()
            if anchors is None:
                return None
            return self._page_point(_interpolate(anchors, line))
        page = self._text_page_of_line(line)
        start, end = self._text_page_range(page)
        line = max(line, start)
        # A blank line (or a separator) says nothing about where it is:
        # the next one with text on it does.
        text_line = next((i for i in range(line, end) if self.text_lines[i].strip()), line)
        height_pt = self._page_height_pt(page)
        if not height_pt:
            return page, None
        y_pt = (text_line - start) / max(1, end - start) * height_pt
        if text_line < end and self.doc_handler.supports_search():
            index = self.search.ensure_index()
            if index and page <= len(index):
                matched = _screen_line_for_text(
                    PdfDocument.screen_lines(index[page - 1]), self.text_lines[text_line], y_pt,
                )
                if matched is not None:
                    y_pt = matched
        return page, y_pt

    def _page_point(self, position: float) -> tuple[int, float | None]:
        """A position in pages from the document's start (2.5 is halfway
        down page 3) as (page, y_pt) - y_pt in PDF points from its top."""
        page = max(1, min(self.npages, int(position) + 1))
        height_pt = self._page_height_pt(page)
        return page, (max(0.0, min(1.0, position - (page - 1))) * height_pt if height_pt else None)

    def _text_line_for_image_point(self, page: int, y_pt: float | None) -> int | None:
        """The text mode line for a point of the page image (see
        _image_reading_point()) - the inverse of _image_point_for_text(),
        for `t` into text mode to show the same place. Moves text mode
        to `page` first if it shows one page at a time.

        Args:
            page: the point's page.
            y_pt: its position on it in PDF points from the top, or None.

        Returns:
            The line, or None if there's no telling.
        """
        height_pt = self._page_height_pt(page)
        down = max(0.0, min(1.0, y_pt / height_pt)) if y_pt is not None and height_pt else 0.0
        if not self.doc_handler.text_mode_is_paginated():
            anchors = self.overlays.source_anchors()
            if anchors is None:
                return None
            inverse = [(position, float(line)) for line, position in anchors]
            return min(len(self.text_lines) - 1, round(_interpolate(inverse, page - 1 + down)))
        if y_pt is None or not height_pt:
            return None
        if page != self.page and self._text_page_starts is None:
            self.go_to_page_text(page, 0)
        start, end = self._text_page_range(page)
        if start >= end:
            return None
        if self.doc_handler.supports_search():
            index = self.search.ensure_index()
            if index and page <= len(index):
                # The first printed line reaching down to the point.
                lines = PdfDocument.screen_lines(index[page - 1])
                below = next((text for _top, bottom, text in lines if bottom >= y_pt), None)
                if below is not None:
                    found = find_title_line(self.text_lines, start, end, below, down)
                    if found is not None:
                        return found
        return start + min(end - start - 1, round(down * (end - start)))

    def _top_text_line(self) -> int:
        """The raw text_lines index showing at the top of the screen:
        text_scroll itself when text is unwrapped, and the line the top
        display row belongs to when it's wrapped (text_scroll counts
        display rows then - see _ensure_display_rows())."""
        if not self.text_wrap:
            return max(0, self.text_scroll)  # -1 is the border's own row
        self._ensure_display_rows()
        if not self._display_rows:
            return 0
        row = max(0, min(self.text_scroll, len(self._display_rows) - 1))
        return self._display_rows[row][0]

    def _scroll_to_text_line(self, line_idx: int) -> None:
        """Put raw line `line_idx` back at the top of the screen, in
        whichever unit the current mode scrolls in - the inverse of
        _top_text_line(), so the pair of them carry a reading position
        across anything that changes the layout underneath it."""
        self.text_scroll = self._text_row_for_line(line_idx)
        self._clamp_text_scroll()

    def _load_content(self) -> None:
        """Geometry + page/text load half of refresh() - split out so
        run_viewer()'s quit_if_one_screen check can run it without also
        triggering the other half (the actual interactive draw, which
        assumes an already-entered alternate screen: clears the
        viewport, writes the status line, etc.)."""
        if self.resized:
            # Where text mode is reading, to put back at the new size: a
            # new width re-splits every wrapped line, so the row number
            # text_scroll holds stops meaning the same place (the same as
            # _relayout() does for a toggle) - and at the very top (the
            # border's row, say), the top it stays.
            loaded = self.text_mode and bool(self.text_lines)
            at_top = self.text_scroll <= self.text_scroll_min
            top_line = self._top_text_line() if loaded else 0
            self._recompute_geometry()
            self.resized = False
            if loaded:
                # (Not read again: a resize, or ^Z and fg - see
                # _suspend() - isn't a change to the file; -f/--follow
                # is what notices those.)
                self._scroll_to_text_line(top_line)
                if at_top:
                    self.text_scroll = self.text_scroll_min
            elif self.text_mode:
                self._load_text_page()  # the first time: read it
            else:
                self._load_page()

    def refresh(self) -> None:
        if self.file_missing:
            if not os.path.exists(self.path):
                self._draw_file_missing()
                return
            # It's back (without follow mode noticing first, or with it
            # off) - show what's in it now, not what was there before.
            self.file_missing = False
            self._displayed_mtime = self._current_mtime()
            try:
                self.reload()  # redraws, and says so on the status line
            except Exception:
                # Back, but not readable yet (e.g. still being written) -
                # stay blank, and try again on the next redraw.
                self.mark_file_missing()
            return
        self._load_content()
        if self.overlays.active is not None:
            self.overlays.draw()
        elif self.text_mode:
            self._draw_text()
        else:
            self._draw()

    def dump_and_quit(self) -> None:
        """-F/--quit-if-one-screen's own one-shot output: print the
        already-loaded page/text and return - the caller is responsible
        for having called _load_content() first, and for never having
        entered the alternate screen at all, so this lands in the
        terminal's real scrollback like `cat` would, the same as real
        less(1)'s own -F. -N line numbers and the eol_mark marker still
        apply, same as interactively - real content-display options, not
        pager-only chrome, unlike the status line/border/scrollbar,
        which this skips."""
        if self.text_mode:
            gutter_width = self._line_number_gutter_width()
            rows = []
            for i, line in enumerate(self.text_lines):
                if gutter_width:
                    line = self._gutter_text(i + 1, gutter_width) + line
                if self.eol_mark:
                    line += EOL_MARK
                rows.append(line)
            # \r\n, not just \n: the terminal is in raw mode (tty.setraw()
            # - see RawTerminal) for the whole run regardless of which
            # path run_viewer() takes, and raw mode turns off the normal
            # tty-driver translation of a bare \n into \r\n - every other
            # draw path avoids this by cursor-positioning each line
            # explicitly instead of relying on it.
            sys.stdout.write("\r\n".join(rows) + "\r\n")
        else:
            data = self._encode_crop(self.img)
            b64 = base64.b64encode(data).decode("ascii")
            osc = (
                f"\x1b]1337;File=inline=1;size={len(data)};"
                f"width={self.img.width}px;height={self.img.height}px;"
                f"preserveAspectRatio=0:{b64}\x07"
            )
            # \r\n, not just \n - same raw-mode reasoning as the text
            # branch above: without the \r, the shell prompt that
            # follows lands one column short of column 1, which zsh
            # flags with its own "%" no-trailing-newline marker.
            sys.stdout.write(wrap_for_tmux(osc) + "\r\n")
        sys.stdout.flush()

    def can_enter_text_mode(self) -> bool:
        """Whether this file has any text-mode content to show at all -
        enter_text_mode()'s own precondition, and what run_viewer()'s
        startup fallback on a terminal without inline images checks
        before switching to text mode for you."""
        return (
            self.doc_handler.supports_text_mode()
            and self.doc_handler.extract_text(self.page) is not None
        )

    def enter_text_mode(self) -> bool:
        """Switch to text mode - False (no-op) if there's no text to
        show at all, which for an OfficeDocument means textutil
        couldn't extract anything from this particular file (e.g. a
        spreadsheet or slide deck - see extract_office_text())."""
        if not self.can_enter_text_mode():
            return False
        back = self._switch_back_spot()
        left = self._view_spot()
        reading = self._image_reading_point()
        self.text_mode = True
        # Mouse reporting is only useful (and only turned on) for
        # clicking hyperlinks in the page image; leave it off here so
        # the terminal's own click-drag text selection works normally.
        sys.stdout.write(MOUSE_OFF)
        self._invalidate_screen()
        # The query to search again for in the new mode, if the match
        # itself can't carry over - see search_resets_on_text_mode_toggle().
        reindex_query = (
            self.search.query if self.doc_handler.search_resets_on_text_mode_toggle() else None
        )
        self._load_text_page()
        if reindex_query:
            # image mode and text mode search different extractions here
            # (see search_resets_on_text_mode_toggle()) - the match object
            # itself can't carry over, so re-run the same query against
            # this mode's own text instead, landing on the nearest hit.
            self.search.start(reindex_query)
        else:
            # If there's a search match highlighted/boxed on this same
            # page, follow it across into text mode too, scrolled into
            # view.
            match = self.search.active_page_match()
            if match:
                self.search.scroll_text_to_match(match)
            elif back is not None:
                # Straight back from image mode: where text mode was.
                page, scroll = back
                if page != self.page and self._text_page_starts is None:
                    self.go_to_page_text(page, 0)
                self._set_text_scroll(scroll)
                self._sync_text_page()
            else:
                # Otherwise, the same place image mode was showing.
                line = self._text_line_for_image_point(*reading)
                if line is not None:
                    self._scroll_text_to_row(self._text_row_for_line(line))
        self.refresh()
        self._mode_switch = (self._view_spot(), (left[1], left[2]))
        return True

    def exit_text_mode(self) -> None:
        # Where text mode is - taken before copy mode (below) changes
        # what's on which row.
        back = self._switch_back_spot()
        left = self._view_spot()
        reading = self._image_point_for_text() if back is None else None
        if self._copy_mode_saved is not None:
            # Copy mode (however it was turned on - "C" inside text
            # mode, or "T"'s own combined enter) only makes sense while
            # in text mode - restore it here, on every way out,
            # regardless of which key got us into text mode in the
            # first place. Skipping this would leak the decorations it
            # hid (most importantly the scrollbar, which also applies
            # in image mode - see toggle_scrollbar()) into the image
            # view and leave them stuck off on a later re-entry too,
            # since _copy_mode_saved would still be holding the
            # original values from however long ago copy mode was
            # first turned on.
            self.toggle_copy_mode()
        self.text_mode = False
        sys.stdout.write(MOUSE_ON)
        self._invalidate_screen()
        # self.page may have moved while browsing in text mode (n/p, g/G,
        # <N>g all update it), but self.img was never touched during that
        # - refresh() only reloads it on a resize - so without this it'd
        # redraw whatever page/scroll was last loaded before entering text
        # mode instead of following you back to where you navigated to.
        # The query to search again for in the new mode, if the match
        # itself can't carry over - see search_resets_on_text_mode_toggle().
        reindex_query = (
            self.search.query if self.doc_handler.search_resets_on_text_mode_toggle() else None
        )
        self.scroll = 0
        self._load_page()
        if reindex_query:
            # Symmetric with enter_text_mode(): the two modes search
            # different extractions here, so re-run the same query
            # against image mode's own (bbox) index instead of trying to
            # carry the match object across.
            self.search.start(reindex_query)
        else:
            # Symmetric with enter_text_mode(): carry a highlighted match
            # back into the box marker on the rendered page.
            match = self.search.active_page_match()
            if match:
                self.search.scroll_image_to_match(match)
            elif back is not None:
                # Straight back from text mode: where image mode was.
                page, scroll = back
                if page != self.page:
                    self.page = page
                    self._load_page()
                self.scroll = self._clamp_image_scroll(scroll)
            elif reading is not None:
                # Otherwise, the same place text mode was showing.
                self._show_image_at(*reading)
        self.refresh()
        self._mode_switch = (self._view_spot(), (left[1], left[2]))

    def _view_spot(self) -> tuple[bool, int, int]:
        """Where the view is: the mode, the page, and how far it's
        scrolled (text_scroll in text mode, scroll in image mode)."""
        return self.text_mode, self.page, self.text_scroll if self.text_mode else self.scroll

    def _switch_back_spot(self) -> tuple[int, int] | None:
        """If the view hasn't moved since the last `t`/`T` switched to the
        mode showing now, the (page, scroll) the other mode was at - for
        switching back to exactly where it was, rather than to what
        the view's position maps onto there, which can differ (e.g. a
        page whose text fits the screen can't scroll to match at all).
        None otherwise."""
        if self._mode_switch is not None and self._mode_switch[0] == self._view_spot():
            return self._mode_switch[1]
        return None

    def toggle_text_mode(self) -> bool:
        """Returns False if switching (specifically *into* text mode)
        failed for lack of any text to show - see enter_text_mode()."""
        if self.text_mode:
            self.exit_text_mode()
            return True
        return self.enter_text_mode()

    def toggle_clean_text_mode(self) -> bool:
        """T: t and C in one press - enter text mode with the copy-mode
        decorations (border/EOL marks/scrollbar/line numbers) already
        cleared, and undo both together on a second press, back to a
        plain image view (exit_text_mode() itself restores copy mode
        before leaving, however it was turned on - see there). Returns
        False if there was no text to switch to (same as
        toggle_text_mode()) - copy mode is left untouched in that
        case."""
        if self.text_mode:
            self.exit_text_mode()  # restores copy mode, then refreshes
            return True
        if not self.enter_text_mode():  # refreshes into "undecorated" text mode
            return False
        if self._copy_mode_saved is None:
            self.toggle_copy_mode()  # relayout only - no redraw of its own
            self.refresh()  # so draw the now-decoration-free view here
        return True

    def _text_continuous(self) -> bool:
        """Whether text mode is showing (or would show) the continuous
        text view: every page's text in one scrollable run, a separator
        row between each page and the next, instead of one page at a
        time - -c/--continuous, for a handler whose text mode is
        paginated in the first place (a PDF, or an Office document
        rendered to one). Anything else already shows its whole text as
        one flowing blob, so -c has nothing to change there."""
        return self.continuous and self.doc_handler.text_mode_is_paginated()

    def _build_continuous_text(self, pages: list[list[str]]) -> None:
        """Stitch `pages` (extract_text_pages()'s per-page line lists)
        into self.text_lines for the continuous text view, recording
        where each page's block starts (self._text_page_starts) and
        which rows are the separators - one heading every page,
        page 1 included, so each page's own number is shown right above
        it (see _text_separator_rule()). A separator is
        stored as an empty line - so wrapping, the pan range, and the
        search all leave it alone for free - and drawn as a rule by
        _text_separator_rule() instead. Each page's trailing blank
        lines are dropped first: pdftotext -layout pads out to the
        page's own bottom margin, which would otherwise leave a
        screenful of nothing before every separator."""
        lines: list[str] = []
        starts: list[int] = []
        separators: set[int] = set()
        max_page_lines = 0
        for i, page_lines in enumerate(pages):
            page_lines = list(page_lines)
            while page_lines and not page_lines[-1].strip():
                page_lines.pop()
            starts.append(len(lines))
            separators.add(len(lines))
            lines.append("")
            lines.extend(page_lines)
            max_page_lines = max(max_page_lines, len(page_lines))
        self.text_lines = lines
        self._text_page_starts = starts
        self._text_separator_lines = frozenset(separators)
        self._text_max_page_lines = max_page_lines

    def _text_page_of_line(self, line_idx: int) -> int:
        """The page (1-based) raw line `line_idx` belongs to in the
        continuous text view - a separator row counting as part of the
        page it introduces - or just self.page outside it."""
        if self._text_page_starts is None:
            return self.page
        page = bisect.bisect_right(self._text_page_starts, line_idx)
        return max(1, min(len(self._text_page_starts), page))

    def _text_page_range(self, page: int) -> tuple[int, int]:
        """(start, end) raw line indices of `page`'s own text within
        self.text_lines - the whole of it when only one page's text is
        loaded, or that page's share of the continuous text view
        (excluding the separator row above it)."""
        if self._text_page_starts is None:
            return 0, len(self.text_lines)
        page = max(1, min(len(self._text_page_starts), page))
        start = self._text_page_starts[page - 1] + 1
        if page < len(self._text_page_starts):
            end = self._text_page_starts[page]
        else:
            end = len(self.text_lines)
        return start, max(start, end)

    def _text_row_for_line(self, line_idx: int, last: bool = False) -> int:
        """The scroll position (text_scroll's unit: a raw line index
        unwrapped, a display row wrapped) of raw line `line_idx` - its
        first display row, or its last one if `last`."""
        if not self.text_wrap:
            return line_idx
        if not last:
            return self._row_for_line(line_idx)
        rows = self._ensure_display_rows()
        row = self._row_for_line(line_idx)
        while row + 1 < len(rows) and rows[row + 1][0] == line_idx:
            row += 1
        return row

    def _text_page_top_row(self, page: int) -> int:
        """Where to scroll to put `page`'s top at the top of the screen
        in the continuous text view: its separator row, so the page
        number it shows is the first thing you see."""
        if self._text_page_starts is None:
            return self.text_scroll_min
        return self._text_row_for_line(self._text_page_starts[page - 1])

    def _sync_text_page(self) -> None:
        """In the continuous text view, keep self.page following the
        page at the top of the screen (for the status line, n/p, and
        which page image mode comes back to) - a no-op otherwise, where
        self.page only ever changes by loading a different page.

        A page shorter than the screen near the end can never be at the
        top, though - the text stops scrolling before that - so it would
        never be the current one: n or > to it would snap back to the
        page before, and G, <N>g and t would all act on that one. So a
        page just gone to stays the current one until the view moves on
        (see _go_to_page_continuous_text()), and at the very end of text
        that scrolls at all, the last page is."""
        if self._text_page_starts is None:
            return
        if self._text_page_gone_to == (self.page, self.text_scroll):
            return
        if self.text_scroll_max > self.text_scroll_min and self.text_scroll >= self.text_scroll_max:
            self.page = self.npages
            return
        self.page = self._text_page_of_line(self._top_text_line())

    def _load_text_page(self) -> None:
        if self._text_continuous():
            # extract_text_pages() is one pdftotext run over the whole
            # document - fetched once per file and kept (see __init__),
            # since toggling t or c shouldn't have to wait for it again.
            if self._text_pages is None:
                pages = self.doc_handler.extract_text_pages(self.npages)
                # Made safe to show - see display_safe().
                self._text_pages = (
                    [[display_safe(line) for line in lines] for lines in pages]
                    if pages is not None else None
                )
            if self._text_pages is not None:
                self._build_continuous_text(self._text_pages)
                self._display_rows = None
                self.text_scroll = 0
                self.text_x_offset = 0
                self._clamp_text_scroll()
                self.text_scroll = self._text_page_top_row(self.page)
                self.text_x_offset = self.text_x_offset_min
                self._clamp_text_scroll()
                return
        self._text_page_starts = None
        self._text_separator_lines = frozenset()
        # For a handler whose text isn't paginated (office/text/rtf),
        # extract_text() ignores `page` and returns the whole document
        # every time - simplest to just always ask fresh here rather
        # than trying to cache it, since nothing else calls this often
        # enough for that to matter (see enter_text_mode()/reload(),
        # the only other places that ask for this same text).
        # Made safe to show: a PDF's or an Office document's text (via
        # pdftotext or textutil) can carry escape sequences - see
        # display_safe().
        self.text_lines = [display_safe(line) for line in self.doc_handler.extract_text(self.page) or []]
        self._display_rows = None  # stale - built fresh from the new text_lines
        self.text_scroll = 0
        self.text_x_offset = 0
        self._clamp_text_scroll()
        if self.text_border and not self.text_wrap:
            # Default to showing the new page's top-left corner - and so
            # its border, since that's otherwise off past the default
            # (0, 0) position. go_to_page_text() below still overrides
            # this for scroll=None (continuous backward scroll wants the
            # bottom of the page instead). No border to reveal at all
            # while wrapped - see _draw_text_wrapped().
            self.text_scroll = self.text_scroll_min
            self.text_x_offset = self.text_x_offset_min

    def _text_avail_rows(self) -> int:
        return max(1, self.rows - 1 - self._dump_margin_rows)  # bottom row is the status bar

    def _text_avail_cols(self) -> int:
        # One column held back for the scrollbar (see
        # _scrollbar_column()) while it's on - regardless of whether the
        # current file actually needs scrolling, so every other column
        # reservation built on top of this (border, EOL marker, line
        # numbers) never has to special-case it.
        return max(1, self.cols - (1 if self.scrollbar else 0))

    def toggle_text_border(self) -> None:
        self.text_border = not self.text_border
        self._clamp_text_scroll()

    def _default_text_border(self) -> bool:
        """Whether text mode's border should be on by default for the
        current file - delegated to doc_handler.default_text_border()
        (e.g. always off for a plain text file, regardless of
        --no-border, since there's usually no real "page" boundary in
        one worth bordering; otherwise whatever --no-border asked for).
        The B key can still toggle either way, on top of this default."""
        return self.doc_handler.default_text_border(self.options.border)

    def toggle_text_wrap(self) -> None:
        """Switches between soft-wrapping long lines and panning across
        them (h/l/H/L) - "W", or the less(1)-style "-S" (see
        _PREFIX_BINDINGS). Keeps your place across
        the switch: the two modes scroll in different units (a display
        row, once wrapping has split a line across several, vs. the raw
        text_lines index), so what carries over is the line currently at
        the top of the screen, translated into the other mode's terms.
        Horizontal pan doesn't carry over - there's nothing to pan while
        wrapped, so unwrapping starts back at the left edge."""
        top_line = self._top_text_line()  # read in the mode being left...
        self.text_wrap = not self.text_wrap
        self._display_rows = None  # rebuilt against the new mode's layout
        self.text_x_offset = 0
        self._scroll_to_text_line(top_line)  # ...written in the one entered

    def toggle_eol_mark(self) -> None:
        """Switches NEWLINE_MARKER on/off - bound to "E" (see
        handle_key_text()). text_max_line_width/_display_rows both
        reserve a column for the marker only while it's on, so both
        need recomputing here - and, since that re-splits every wrapped
        line, so does the scroll position (_top_text_line())."""
        top_line = self._top_text_line()
        self.eol_mark = not self.eol_mark
        self._display_rows = None
        self._scroll_to_text_line(top_line)

    def toggle_line_numbers(self) -> None:
        """Switches the -N/--line-numbers gutter on/off - "#" (see
        handle_key_text()), or "-N"/"-n" (less(1)-style, see
        run_viewer()'s dash_pending). Its width changes what's left for
        content, so the wrapped segments (_display_rows), the scroll
        bounds and the scroll position itself (_top_text_line()) all
        need recomputing."""
        top_line = self._top_text_line()
        self.line_numbers = not self.line_numbers
        self._display_rows = None
        self._scroll_to_text_line(top_line)

    def toggle_copy_mode(self) -> None:
        """Clear the way for a terminal select-and-copy, and put things
        back on a second press - "C" (see handle_key_text()). Text mode
        exists largely to copy text out of a document, and the EOL
        markers, the border, the scrollbar and the line-number gutter
        all sit in the way of that: a drag across the text sweeps them
        up along with it. Rather than hunting down E/B/r/# one at a
        time and remembering which of them were on to begin with, this
        turns off all four at once and remembers that for you."""
        if self._copy_mode_saved is not None:
            (
                self.eol_mark, self.text_border, self.scrollbar, self.line_numbers
            ) = self._copy_mode_saved
            self._copy_mode_saved = None
        else:
            self._copy_mode_saved = (
                self.eol_mark, self.text_border, self.scrollbar, self.line_numbers
            )
            self.eol_mark = self.text_border = False
            self.scrollbar = self.line_numbers = False
        # Each of the four frees up (or takes back) room of its own -
        # let _relayout() work out what that means rather than spelling
        # out which widths moved here.
        self._relayout()

    def _line_number_gutter_width(self) -> int:
        """Columns reserved for the -N gutter - 0 when it's off. Right-
        aligned digits sized to the largest line number currently in
        self.text_lines (a PDF's per-page text, or the whole document
        for anything else - see _load_text_page()), plus one separator
        column."""
        if not self.line_numbers or not self.text_lines:
            return 0
        if self._text_page_starts is not None:
            # Numbered from 1 on every page (see _text_line_number()), so
            # only the longest page matters, not the whole document.
            return len(str(max(1, self._text_max_page_lines))) + 1
        return len(str(len(self.text_lines))) + 1

    def _text_line_number(self, line_idx: int) -> int | None:
        """The -N gutter's number for raw line `line_idx`: its position
        in self.text_lines, or in the continuous text view its position
        within its own page - the same number it'd have with only that
        page loaded, so it still matches <N>g (see go_to_text_line()).
        None for a separator row, which gets no number at all."""
        if self._text_page_starts is None:
            return line_idx + 1
        if line_idx in self._text_separator_lines:
            return None
        start, _end = self._text_page_range(self._text_page_of_line(line_idx))
        return line_idx - start + 1

    def _text_separator_rule(self, line_idx: int, width: int) -> str:
        """The continuous text view's separator row for raw line
        `line_idx`, `width` columns of it: a horizontal rule with the
        number of the page it introduces ("N/M") near its left end
        (dropped if there's no room for it) - the rule in
        PAGE_SEPARATOR_COLOR, the number itself in PAGE_NUMBER_COLOR."""
        width = max(0, width)
        label = f" {self._text_page_of_line(line_idx)}/{self.npages} "
        if len(label) + 2 > width:
            return PAGE_SEPARATOR_COLOR + "─" * width + SGR_RESET
        return (
            PAGE_SEPARATOR_COLOR + "──" + SGR_RESET
            + PAGE_NUMBER_COLOR + label + SGR_RESET
            + PAGE_SEPARATOR_COLOR + "─" * (width - 2 - len(label)) + SGR_RESET
        )

    def toggle_continuous(self) -> None:
        """c: switch -c/--continuous on/off, staying where you were -
        the same page (and, in text mode, the same line of it) -
        rather than starting over at the top."""
        page = self.page
        if self.text_mode:
            # Measured against the current layout, before it changes:
            # how many lines into its page the top of the screen is.
            start, _end = self._text_page_range(page)
            line_offset = max(0, self._top_text_line() - start)
        self.continuous = not self.continuous
        self._invalidate_screen()
        if self.text_mode:
            if self.doc_handler.text_mode_is_paginated():
                self.page = page
                self._load_text_page()
                if line_offset:
                    start, _end = self._text_page_range(page)
                    self._scroll_to_text_line(start + line_offset)
        else:
            # Re-clamps the scroll position for whichever mode is now
            # on - to this page's own range when turning it off; turning
            # it on, _draw()'s _normalize_continuous() takes it from here.
            self._load_page()
        self.refresh()
        self.draw_status("continuous view " + ("on" if self.continuous else "off"))

    def toggle_sidebar(self) -> None:
        """S: show or hide the page thumbnails (see _Sidebar) - in text
        mode too, which doesn't show them, for when it's left. Their
        columns are taken from the page's width, so the layout is redone
        around them, as for the scrollbar (see toggle_scrollbar())."""
        sidebar = self.sidebar
        if sidebar.on and not sidebar.columns():
            # On, but the terminal has been narrowed since: say why
            # there's nothing to see, rather than just turning it off.
            sidebar.on = sidebar.picking = False
            self.draw_status("page thumbnails off (too narrow a terminal to show them)")
            return
        sidebar.picking = False
        sidebar.on = not sidebar.on
        if sidebar.on and self.cols < SIDEBAR_MIN_TERM_COLS:
            sidebar.on = False
            self.draw_status("too narrow a terminal for the page thumbnails")
            return
        self._relayout()
        self._invalidate_screen()
        self.refresh()
        if self.text_mode:
            self.draw_status(f"page thumbnails {'on' if sidebar.on else 'off'} (in image mode)")

    def focus_sidebar(self) -> None:
        """s: move the keys to the page thumbnails, to pick one (see
        _Sidebar.handle_key()) - putting them up first if they aren't.
        Image mode only, as text mode doesn't show them."""
        sidebar = self.sidebar
        if self.text_mode:
            self.draw_status("the page thumbnails are in image mode (t to go back to it)")
            return
        if not sidebar.columns():
            self.toggle_sidebar()  # (or says why it can't)
            if not sidebar.columns():
                return
        sidebar.start_picking()

    def toggle_scrollbar(self) -> None:
        """Switches the scrollbar on/off - "r" (see run_viewer(), which
        handles it at the top level since it applies in both image mode
        (_draw()) and text mode). Its column is reserved from
        base_width_px (image mode - see _recompute_geometry()) or
        _text_avail_cols() (text mode), so the whole layout has to be
        redone around it - see _relayout()."""
        self.scrollbar = not self.scrollbar
        self._relayout()

    def _scrollbar_fractions(
        self, start: float, avail_extent: float, total_extent: float, page: int, npages: int,
    ) -> tuple[float, float]:
        """(start_frac, visible_frac), both in [0, 1]: where the visible
        window starts, and how much of it is visible, as a fraction of
        the WHOLE document (all `npages` pages) - not just the current
        page/screen. `start`/`avail_extent`/`total_extent` share any one
        consistent unit (pixels in image mode, rows in text mode):
        `start` is how far into the current page the window's top edge
        sits, `total_extent` the page's own full size, `avail_extent`
        how much of it fits on screen at once. A 10-page PDF, showing
        the top half of page 1, is start_frac=0, visible_frac=0.5/10 -
        the thumb sits in the top 5% of the track, not top-50%, which
        was this method's whole reason for existing (see toggle_scrollbar()'s
        callers) - a single-page view (page=npages=1) reduces exactly to
        "this page/screen's own fraction", unaffected."""
        total_extent = max(1, total_extent)
        page_start_frac = max(0.0, min(1.0, start / total_extent))
        page_visible_frac = max(0.0, min(1.0, avail_extent / total_extent))
        npages = max(1, npages)
        return (page - 1 + page_start_frac) / npages, page_visible_frac / npages

    def _text_scrollbar_fractions(self, avail_rows: int) -> tuple[float, float]:
        """(start_frac, visible_frac) for the text-mode scrollbar - see
        _scrollbar_fractions(). Only a PDF's text mode pages separately
        (doc_handler.text_mode_is_paginated()) - anything else shows
        the whole document in one continuous text_scroll range
        already, so page/npages are fixed at 1/1 there (self.page/
        self.npages might otherwise still reflect an unrelated slide/
        page count from image mode - e.g. a multi-slide OfficeDocument
        - that text mode's own single flowing view doesn't split on)."""
        total_extent = avail_rows + (self.text_scroll_max - self.text_scroll_min)
        start = self.text_scroll - self.text_scroll_min
        if self.doc_handler.text_mode_is_paginated() and self._text_page_starts is None:
            page, npages = self.page, self.npages
        else:
            page, npages = 1, 1
        return self._scrollbar_fractions(start, avail_rows, total_extent, page, npages)

    def _scrollbar_column(
        self, avail_rows: int, start_frac: float, visible_frac: float,
    ) -> list[str]:
        """One rendered cell (color + char) per screen row, top to
        bottom, for the scrollbar column - the terminal's last column,
        reserved (while self.scrollbar is on) from base_width_px in
        image mode (see _draw()) or _text_avail_cols() in text mode
        (see _draw_text_wrapped()/_draw_text_unwrapped()). `start_frac`/
        `visible_frac` (see _scrollbar_fractions()) are already
        normalized to the whole document, so this is just laying them
        out over avail_rows screen cells. A file that fits on screen
        entirely (visible_frac >= 1) shows a thumb spanning the whole
        track, rather than an arbitrary track/thumb split that would
        suggest otherwise."""
        visible_frac = max(0.0, min(1.0, visible_frac))
        if visible_frac >= 1.0:
            return [SCROLLBAR_THUMB] * avail_rows
        thumb_size = max(1, min(avail_rows, round(visible_frac * avail_rows)))
        thumb_start = max(0, min(avail_rows - thumb_size, round(start_frac * avail_rows)))
        return [
            SCROLLBAR_THUMB if thumb_start <= i < thumb_start + thumb_size
            else SCROLLBAR_TRACK
            for i in range(avail_rows)
        ]

    def _default_text_wrap(self) -> bool:
        """Whether text mode should default to wrapping long lines for
        the current file - delegated to doc_handler.default_text_wrap()
        (on for a plain text file, unless -S/--chop-long-lines said
        otherwise; off for a PDF/Office's own derived text view, which
        pans instead, regardless of -S). The -S key sequence can still
        toggle either way, on top of this default."""
        return self.doc_handler.default_text_wrap(self.options.wrap)

    def _set_text_scroll(self, row: int) -> None:
        """Scroll text mode to `row`, clamped to the current
        text_scroll_min..text_scroll_max (see _clamp_text_scroll(),
        which works those out)."""
        self.text_scroll = max(self.text_scroll_min, min(self.text_scroll_max, row))

    def _clamp_text_scroll(self) -> None:
        avail_rows = self._text_avail_rows()
        if self.text_wrap:
            # No border while wrapped (see _draw_text_wrapped()) - the
            # scroll range is over display rows (self._display_rows,
            # built fresh here since content_width may have changed),
            # not raw text_lines, and there's no pan to speak of.
            rows = self._ensure_display_rows()
            self.text_scroll_min = 0
            self.text_scroll_max = max(0, len(rows) - avail_rows)
            self._set_text_scroll(self.text_scroll)
            self.text_x_offset_min = 0
            self.text_x_offset_max = 0
            self.text_x_offset = 0
            return

        # The border sits at the page's actual edges - one row above the
        # first line, one below the last; one column left of column 0,
        # one right of the widest line - which is usually off-screen at
        # the default scroll/pan position. It only comes into view by
        # scrolling/panning one step past the content itself, so with the
        # border on, the scroll/pan range is widened by exactly that much;
        # with it off, the range is exactly what it was before this
        # feature existed.
        if self.text_border:
            # The continuous text view has no separate top border row:
            # page 1's own separator row is drawn as the border's top
            # edge instead (see _draw_text_unwrapped()), so there's
            # nothing above row 0 to scroll up to.
            self.text_scroll_min = -1 if self._text_page_starts is None else 0
            self.text_scroll_max = max(
                self.text_scroll_min, len(self.text_lines) - avail_rows + 1
            )
        else:
            self.text_scroll_min = 0
            self.text_scroll_max = max(0, len(self.text_lines) - avail_rows)
        self._set_text_scroll(self.text_scroll)

        # The -N gutter (if on) lives outside this space entirely - see
        # _draw_text_unwrapped() - so the "page" is narrower by that much.
        avail_cols = max(1, self._text_avail_cols() - self._line_number_gutter_width())
        self.text_max_line_width = max(
            (display_width(l) for l in self.text_lines), default=0
        )
        if self.eol_mark:
            # Otherwise the widest line's own marker (see
            # _draw_text_unwrapped()) would have nowhere to go without
            # overflowing past the border - this pretends the page is 1
            # column wider than its content actually is, the same way
            # _ensure_display_rows() reserves a column when wrapped.
            self.text_max_line_width += 1
        if self.text_border:
            self.text_x_offset_min = -1
            self.text_x_offset_max = max(-1, self.text_max_line_width - avail_cols + 1)
        else:
            self.text_x_offset_min = 0
            self.text_x_offset_max = max(0, self.text_max_line_width - avail_cols)
        self.text_x_offset = max(
            self.text_x_offset_min, min(self.text_x_offset, self.text_x_offset_max)
        )

    def _text_highlight_segments(self, highlight: TextMatch | None) -> dict[int, tuple[int, int]]:
        """The part of each line a (line_idx, start, end) highlight covers,
        for drawing it - one line's worth, or several for a match that
        runs across line breaks (see TextMatch).

        Args:
            highlight: from _Search.text_highlight(), or None.

        Returns:
            {line_idx: (start, end)} with offsets into that line, the
            newline between lines left out - {} for no highlight.
        """
        if highlight is None:
            return {}
        line_idx, start, end = highlight
        segments = {}
        # `offset` is where line_idx starts, counted from the first line's start.
        offset = 0
        while line_idx < len(self.text_lines) and offset < end:
            line_len = len(self.text_lines[line_idx])
            seg_start, seg_end = max(start - offset, 0), min(end - offset, line_len)
            if seg_start < seg_end:
                segments[line_idx] = (seg_start, seg_end)
            offset += line_len + 1
            line_idx += 1
        return segments

    def _scroll_text_to_row(self, row: int) -> None:
        """Scroll text mode so `row` lands a quarter of the screen down
        rather than jammed against the top edge - where a search match
        is put, so there's context above it."""
        self._set_text_scroll(row - self._text_avail_rows() // 4)

    def _scroll_text_to_highlight(self, line_idx: int, start: int, end: int) -> None:
        """Bring characters start..end of raw line `line_idx` into view:
        pan them to the middle if they're off to either side of an
        unwrapped view (wrapped text has no pan to speak of), then scroll
        their row into place (_scroll_text_to_row())."""
        if not self.text_wrap:
            avail_cols = max(1, self._text_avail_cols() - self._line_number_gutter_width())
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
        self._scroll_text_to_row(self._text_row_for_line(line_idx))

    def go_to_page_text(self, page: int, scroll: int | None) -> None:
        """Show `page` in text mode: scroll=0 is its top, None its
        bottom (continuous scroll-up wants that), and any other number
        that many lines down into it."""
        if self._text_page_starts is not None:
            self._go_to_page_continuous_text(page, scroll)
            return
        if not self.doc_handler.text_mode_is_paginated():
            # One flowing blob (a Markdown file's raw source, an Office
            # document via textutil, ...) - text mode is a single page
            # however many the image view has, so there's no other page
            # to go to: only the top or bottom of this one. self.page is
            # left alone, still the image-mode page `t` returns to.
            if scroll is None:
                self.text_scroll = self.text_scroll_max
            elif scroll == 0:
                self.text_scroll = self.text_scroll_min
            else:
                self._scroll_to_text_line(scroll)
            return
        self.page = max(1, min(self.npages, page))
        self._load_text_page()  # already leaves text_scroll at text_scroll_min
        if scroll is None:
            self.text_scroll = self.text_scroll_max  # continuous scroll-up wants the bottom
        elif scroll != 0:
            # 0 means "top of page", which _load_text_page() already set
            # up (text_scroll_min, revealing the border if there is one);
            # anything else is a specific line to land on (e.g. <N>g).
            self._set_text_scroll(scroll)

    def _go_to_page_continuous_text(self, page: int, scroll: int | None) -> None:
        """go_to_page_text() for the continuous text view - where every
        page is already loaded, so it's only ever a scroll. The bottom
        (scroll=None) puts the page's last line at the bottom of the
        screen, but never so far that the page's own top leaves the top
        of it - which would make the previous page the current one, and
        a second J/G walk back another page."""
        page = max(1, min(self.npages, page))
        self.page = page
        start, end = self._text_page_range(page)
        top_row = self._text_page_top_row(page)
        if scroll is None:
            if page == self.npages:
                target = self.text_scroll_max
            else:
                bottom_row = self._text_row_for_line(max(start, end - 1), last=True)
                target = max(top_row, bottom_row - self._text_avail_rows() + 1)
        elif scroll == 0:
            target = top_row
        else:
            target = self._text_row_for_line(min(max(start, end - 1), start + scroll))
        self._set_text_scroll(target)
        # The page stays the current one while the view stays here, even
        # if it can't be at the top (see _sync_text_page()).
        self._text_page_gone_to = (page, self.text_scroll)

    def go_to_text_line(self, n: int) -> None:
        """Jump to line `n` (1-based) within the current page's text -
        <N>g/<N>G in text mode. Lands it at the very top of the screen,
        same as less(1)'s own <N>g, even if that leaves blank space
        below near the end of the page - unlike normal scrolling, which
        never scrolls past showing a full screen of content, in order
        to guarantee the requested line is the one that ends up on top.
        While wrapped, "line n" still means the same raw line - it just
        lands on whichever display row that line's wrapping starts at."""
        # Line numbers count from the top of each page in the continuous
        # text view too (see _draw_text_unwrapped()), so "line n" is
        # still the current page's own line n there.
        start, end = self._text_page_range(self.page)
        target_line = max(start, min(max(start, end - 1), start + n - 1))
        row = self._text_row_for_line(target_line)
        self.text_scroll = max(self.text_scroll_min, row)

    def text_scroll_down(self, n: int) -> None:
        if self._text_page_starts is not None:
            # The continuous text view: one scroll range for the whole
            # document, no page turn at either end of a page.
            self.text_scroll = min(self.text_scroll_max, self.text_scroll + n)
            return
        if self.text_scroll < self.text_scroll_max:
            self.text_scroll = min(self.text_scroll_max, self.text_scroll + n)
        elif self.doc_handler.text_mode_is_paginated() and self.page < self.npages:
            # Only a paginated text mode has a next page to turn to -
            # anything else already shows the whole document, and
            # "turning" to image page 2 would just show it all again.
            self.go_to_page_text(self.page + 1, 0)

    def text_scroll_up(self, n: int) -> None:
        if self._text_page_starts is not None:
            self.text_scroll = max(self.text_scroll_min, self.text_scroll - n)
            return
        if self.text_scroll > self.text_scroll_min:
            self.text_scroll = max(self.text_scroll_min, self.text_scroll - n)
        elif self.doc_handler.text_mode_is_paginated() and self.page > 1:
            self.go_to_page_text(self.page - 1, None)

    def _row_for_line(self, line_idx: int) -> int:
        """The first display-row index (into self._display_rows, see
        _ensure_display_rows()) covering raw line `line_idx` - lets
        anything that thinks in terms of a raw text_lines index (search
        highlighting, <N>g) target the right scroll position once
        wrapping has split that line across one or more screen rows."""
        rows = self._ensure_display_rows()
        for row, (li, _start, _end) in enumerate(rows):
            if li == line_idx:
                return row
        return max(0, len(rows) - 1)

    @staticmethod
    def _wrap_line_segments(line: str, width: int) -> list[tuple[int, int]]:
        """Split `line` into consecutive (start, end) character-index
        segments, each at most `width` display columns wide - the
        wrap-mode equivalent of slice_by_width()'s single pan window,
        but covering the whole line instead of just one panned slice of
        it. An empty line still yields exactly one (empty) segment, so
        it still occupies one display row, same as an unwrapped blank
        line does."""
        width = max(1, width)
        if not line:
            return [(0, 0)]
        segments = []
        start = 0
        n = len(line)
        while start < n:
            piece = truncate_to_width(line[start:], width)
            if not piece:
                # a single character wider than the whole available
                # width - it still has to go somewhere
                piece = line[start:start + 1]
            end = start + len(piece)
            segments.append((start, end))
            start = end
        return segments

    def _ensure_display_rows(self) -> list[tuple[int, int, int]]:
        """self._display_rows - one (line_idx, start, end) entry per
        on-screen row while wrapped - built lazily, since it's
        invalidated (set to None) whenever self.text_lines, text_wrap, or
        the terminal width changes, and rebuilding it is O(total document
        length). Returned as well, for the caller to use directly."""
        if self._display_rows is not None:
            return self._display_rows
        # One column held back for the real-newline marker (see
        # _draw_text_wrapped()), if it's on - reserved on every row, not
        # just one that ends up actually drawing it, so the marker never
        # has to compete with content for the same column. The -N gutter
        # (if on) is held back the same way, drawn outside this width.
        width = (
            self._text_avail_cols()
            - (1 if self.eol_mark else 0)
            - self._line_number_gutter_width()
        )
        width = max(1, width)
        rows = []
        for i, line in enumerate(self.text_lines):
            for start, end in self._wrap_line_segments(line, width):
                rows.append((i, start, end))
        self._display_rows = rows
        return rows

    @staticmethod
    def _gutter_text(number: int | None, gutter_width: int) -> str:
        """The -N gutter cell for a row: `number` right-aligned in gray,
        or just blanks for a row that gets none (a wrapped continuation,
        a border row, a page separator, ...)."""
        if number is None:
            return " " * gutter_width
        return LINE_NUMBER_COLOR + str(number).rjust(gutter_width - 1) + " " + SGR_RESET

    @staticmethod
    def _splice_highlight(rendered: str, start: int, end: int) -> str:
        """`rendered` with characters start..end (clipped to it) wrapped
        in TEXT_HIGHLIGHT_COLOR - the search match's highlight. The
        offsets are relative to `rendered` itself, so either caller
        shifts them from the raw line first (by where its wrapped
        segment or pan window starts)."""
        start, end = max(start, 0), min(end, len(rendered))
        if start >= len(rendered) or end <= start:
            return rendered
        return rendered[:start] + TEXT_HIGHLIGHT_COLOR + rendered[start:end] + SGR_RESET + rendered[end:]

    def _scrollbar_escapes(
        self, avail_rows: int, start_frac: float, visible_frac: float,
    ) -> list[str]:
        """The scrollbar column's cells as positioned escape strings -
        [] while it's off - for either mode's draw."""
        if not self.scrollbar:
            return []
        cells = self._scrollbar_column(avail_rows, start_frac, visible_frac)
        return [f"\x1b[{i + 1};{self.cols}H{cell}" for i, cell in enumerate(cells)]

    def _finish_text_frame(self, rows: list[str], avail_rows: int) -> None:
        """Write one text-mode frame: clear the screen, then `rows` (the
        positioned row escapes _draw_text_wrapped()/_unwrapped() built),
        the scrollbar and the status line. Attributes are reset *before*
        clearing, not after: a still-active SGR state (e.g. a background
        color left on by draw_search_prompt(), which doesn't reset it
        since it's mid-edit) is what \x1b[2J fills the newly-blanked
        cells with - resetting only afterwards colors future writes but
        leaves every cell the clear itself touched stuck in that stale
        color."""
        out = [SGR_RESET, "\x1b[H\x1b[2J", *rows]
        out += self._scrollbar_escapes(avail_rows, *self._text_scrollbar_fractions(avail_rows))
        out.append(self.format_status())
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _draw_text(self) -> None:
        self._sync_text_page()
        if self.text_wrap:
            self._draw_text_wrapped()
        else:
            self._draw_text_unwrapped()

    def _draw_text_wrapped(self) -> None:
        """Wrap-mode rendering: self._display_rows (built by
        _ensure_display_rows()) already breaks the document into
        on-screen rows, each guaranteed to fit the terminal width - so,
        unlike _draw_text_unwrapped(), there's no pan position to
        account for. The border (text_border) isn't drawn here either,
        regardless of its own on/off state: a border only makes sense
        around a fixed page shape, and wrapped text has no edges of its
        own to border - it just keeps flowing to fill the width."""
        display_rows = self._ensure_display_rows()
        avail_rows = self._text_avail_rows()
        highlight = self._text_highlight_segments(self.search.text_highlight())

        out = []
        n_rows = len(display_rows)
        gutter_width = self._line_number_gutter_width()

        for i in range(avail_rows):
            virtual_row = self.text_scroll + i
            screen_row = i + 1
            if not (0 <= virtual_row < n_rows):
                continue  # above/below the document entirely

            line_idx, start, end = display_rows[virtual_row]
            rendered = self.text_lines[line_idx][start:end]
            number = self._text_line_number(line_idx)

            if gutter_width:
                # Only the line's first display row gets a number - a
                # wrapped continuation row (start != 0) stays blank.
                gutter_text = self._gutter_text(number if start == 0 else None, gutter_width)
                out.append(f"\x1b[{screen_row};1H{gutter_text}")

            if line_idx in self._text_separator_lines:
                # Left blank while copy mode is on, so a select-and-copy
                # across a page boundary picks up nothing but the text.
                if self._copy_mode_saved is None:
                    rule = self._text_separator_rule(line_idx, self._text_avail_cols() - gutter_width)
                    out.append(f"\x1b[{screen_row};{gutter_width + 1}H{rule}")
                continue

            if line_idx in highlight:
                # start/end are character offsets into the raw line;
                # shift them into this segment's own coordinates.
                h_start, h_end = highlight[line_idx]
                rendered = self._splice_highlight(rendered, h_start - start, h_end - start)

            if self.eol_mark and end == len(self.text_lines[line_idx]):
                # This segment reaches the actual end of the raw line -
                # a real newline, not just where this row's wrapping
                # happened to cut it - see NEWLINE_MARKER.
                rendered += EOL_MARK

            out.append(f"\x1b[{screen_row};{gutter_width + 1}H{rendered}")

        self._finish_text_frame(out, avail_rows)

    def _draw_text_unwrapped(self) -> None:
        # The border sits at the page's own edges in this same scrollable/
        # pannable space the text lines live in - virtual row -1 (top)
        # and row len(text_lines) (bottom), virtual column -1 (left) and
        # column text_max_line_width (right) - rather than around
        # whatever happens to be on screen. So depending on text_scroll/
        # text_x_offset, any side of it may be scrolled out of view; each
        # row below independently figures out which of border/content/
        # nothing falls at its current position.
        avail_rows = self._text_avail_rows()
        # The -N gutter (if on) lives outside this whole border/pan
        # space, in columns of its own - the "page" here is simply
        # narrower by that much, same as the border/content geometry
        # below never needs to know it exists.
        gutter_width = self._line_number_gutter_width()
        avail_cols = max(1, self._text_avail_cols() - gutter_width)
        highlight = self._text_highlight_segments(self.search.text_highlight())
        out = []

        left_col = -1 - self.text_x_offset
        right_col = self.text_max_line_width - self.text_x_offset
        left_visible = self.text_border and 0 <= left_col < avail_cols
        right_visible = self.text_border and 0 <= right_col < avail_cols

        for i in range(avail_rows):
            virtual_row = self.text_scroll + i
            screen_row = i + 1

            if gutter_width:
                # None (blank) for a border row, page separator, or off the page
                number = (
                    self._text_line_number(virtual_row)
                    if 0 <= virtual_row < len(self.text_lines) else None
                )
                out.append(f"\x1b[{screen_row};1H{self._gutter_text(number, gutter_width)}")

            if virtual_row in self._text_separator_lines:
                # The continuous text view's heading row for a page: a
                # rule spanning the same columns the border's own top/
                # bottom edges do (joining up with its sides as ├/┤ -
                # or, for page 1's, as the border's top corners ┌/┐,
                # since it takes the top edge's place), or the whole
                # visible width with no border. Left blank in copy mode,
                # the same as in _draw_text_wrapped().
                if self._copy_mode_saved is not None:
                    continue
                if self.text_border:
                    line_start = max(0, left_col)
                    line_end = min(avail_cols - 1, right_col)
                else:
                    line_start, line_end = 0, avail_cols - 1
                if line_end < line_start:
                    continue  # panned out of view
                first = virtual_row == 0
                left_end = ("┌" if first else "├") if left_visible else ""
                right_end = ("┐" if first else "┤") if right_visible else ""
                rule = self._text_separator_rule(
                    virtual_row, line_end - line_start + 1 - len(left_end) - len(right_end)
                )
                out.append(
                    f"\x1b[{screen_row};{line_start + 1 + gutter_width}H{left_end}{rule}{right_end}"
                )
                continue

            if self.text_border and virtual_row in (-1, len(self.text_lines)):
                line_start = max(0, left_col)
                line_end = min(avail_cols - 1, right_col)
                if line_end < line_start:
                    continue  # this border edge is panned out of view
                chars = ["─"] * (line_end - line_start + 1)
                if left_visible:
                    chars[0] = "┌" if virtual_row == -1 else "└"
                if right_visible:
                    chars[-1] = "┐" if virtual_row == -1 else "┘"
                out.append(f"\x1b[{screen_row};{line_start + 1 + gutter_width}H{''.join(chars)}")
                continue

            if not (0 <= virtual_row < len(self.text_lines)):
                continue  # above/below the border entirely - nothing there

            line = self.text_lines[virtual_row]
            content_start = (left_col + 1) if left_visible else 0
            content_end = right_col if right_visible else avail_cols
            content_width = max(0, content_end - content_start)
            rendered, base = slice_by_width(
                line, max(0, self.text_x_offset), content_width
            )
            reaches_end = base + len(rendered) == len(line)
            # Measured before any color codes (highlight, marker) are
            # spliced in below - display_width() would miscount those
            # escape bytes as visible columns otherwise. When bordered,
            # text_max_line_width's own +1 (above) already guarantees
            # room for the marker on every row (the widest line just
            # uses all of it, up against the border); unbordered, there's
            # no such guarantee, so only draw it if there's room to spare.
            shown_width = display_width(rendered)
            show_marker = self.eol_mark and reaches_end and shown_width < content_width

            if virtual_row in highlight:
                # start/end are character offsets into the original,
                # unpanned `line`; `rendered` starts partway through it
                # (at character index `base`, i.e. wherever text_x_offset
                # columns in falls) once panned, so shift them into
                # rendered's own coordinates before slicing it up to
                # splice in color codes.
                start, end = highlight[virtual_row]
                rendered = self._splice_highlight(rendered, start - base, end - base)

            if show_marker:
                # Right after the real content - i.e. at the actual
                # newline position - not padded out to the border (see
                # below), which would misleadingly suggest the line
                # itself reaches all the way to the page edge.
                rendered += EOL_MARK
                shown_width += 1

            if right_visible:
                # Pad with plain spaces (not pad_to_width(), which would
                # re-measure `rendered` and miscount the escape codes
                # just spliced in) so the border still lines up straight
                # at the page's actual right edge, same as before.
                rendered += " " * max(0, content_width - shown_width)

            parts = []
            if left_visible:
                parts.append("│")
            parts.append(rendered)
            if right_visible:
                parts.append("│")
            start_col = (left_col if left_visible else content_start) + 1 + gutter_width
            out.append(f"\x1b[{screen_row};{start_col}H{''.join(parts)}")

        self._finish_text_frame(out, avail_rows)

    def _encode_crop(self, crop: Image.Image) -> bytes:
        buf = io.BytesIO()
        if iterm2_like():
            # JPEG encodes much faster than PNG; iTerm2 accepts it inline.
            crop.convert("RGB").save(buf, format="JPEG", quality=90)
        else:
            crop.save(buf, format="PNG", compress_level=1)
        return buf.getvalue()

    def _format_viewport_clear(self, crop_w: int, crop_h: int, full_clear: bool) -> str:
        char_w = max(1, -(-crop_w // self.cell_w_px))
        char_h = max(1, -(-crop_h // self.cell_h_px))
        prev_char_h = self._last_char_h or char_h

        if full_clear:
            self._last_char_h = char_h
            return "\x1b[H\x1b[2J"

        # Rows are cleared from the page's first column (right of the
        # sidebar, if it's up) to the end of the line, the sidebar left be.
        col0 = self._image_col0()
        out = [f"\x1b[1;{col0}H"]
        if self._last_viewport_set and crop_h > self._last_viewport_h:
            out.append(_strip_leading_home(_format_ech_clear(char_w, char_h)))
        if prev_char_h > char_h:
            for row in range(char_h, min(prev_char_h, self.rows - 1)):
                out.append(f"\x1b[{row + 1};{col0}H\x1b[K")
        total_rows = max(1, -(-self.avail_height_px // self.cell_h_px))
        blank_from = char_h
        if prev_char_h > char_h:
            blank_from = max(char_h, prev_char_h)
        for row in range(blank_from, min(self.rows - 1, total_rows)):
            out.append(f"\x1b[{row + 1};{col0}H\x1b[K")
        self._last_char_h = char_h
        return "".join(out)

    def _needs_full_clear(self, crop_w: int, crop_h: int) -> bool:
        if not self._last_viewport_set:
            return True
        if self._last_viewport_w != crop_w:
            return True
        if crop_h > self._last_viewport_h:
            return True
        return False

    def _encode_key(self, top: int, width: int, height: int) -> tuple:
        """EncodeCache key for the viewport strip _view_crop(top, height)
        would produce - counted from self.page's own top edge, which
        together with the rest pins down exactly what's in it (under
        -c/--continuous too, since _normalize_continuous() always leaves
        (page, scroll) in one canonical form)."""
        return (
            self.page, self.scroll + top, self.x_offset, width, height,
            round(self.zoom * 100), self.fit, self.continuous,
        )

    def _draw(self) -> None:
        if self.continuous:
            self._normalize_continuous()
            crop_h = self._view_height
        else:
            crop_h = min(self.scroll + self.avail_height_px, self.img.height) - self.scroll

        shift = self._scroll_shift_rows(self.crop_width, crop_h)
        if shift is not None:
            self._draw_shifted(shift, crop_h)
            return

        crop = self._view_crop(0, crop_h)
        crop_w, crop_h = crop.width, crop.height

        encode_key = self._encode_key(0, crop_w, crop_h)
        data = self.encode_cache.get(encode_key)
        if data is None:
            data = self._encode_crop(crop)
            self.encode_cache.put(encode_key, data)

        image = _inline_image(data, crop_w, crop_h)

        full_clear = self._needs_full_clear(crop_w, crop_h)
        self._last_viewport_w = crop_w
        self._last_viewport_h = crop_h
        self._last_viewport_set = True
        self._remember_drawn_position()

        match = self.search.visible_match()
        new_bounds = (
            self._match_marker_bounds(
                *self.search.match_bbox_px(match), page_top=self._visible_page_top(match[0])
            )
            if match else None
        )

        # Reset *before* any clearing/erasing below, not after: a
        # still-active SGR state (e.g. a background color left on by
        # draw_search_prompt(), which doesn't reset it since it's
        # mid-edit) is what a \x1b[2J/ECH fills the newly-blanked cells
        # with - see _draw_text()'s longer version of this comment.
        out = [SYNC_BEGIN, SGR_RESET, self._format_viewport_clear(crop_w, crop_h, full_clear)]
        # Erase the previous marker before the new image lands; otherwise
        # box-drawing chars linger on iTerm2 inline-image cells (especially
        # when search is cleared or n/p jumps to another match).
        if self._last_marker_bounds and self._last_marker_bounds != new_bounds:
            out.append(self._format_marker_erase(*self._last_marker_bounds))
        out.extend([
            # Erase leaves the cursor elsewhere; doNotMoveCursor=1 draws
            # the inline image at the current cell - the page's top-left
            # one, right of the sidebar if it's up.
            f"\x1b[1;{self._image_col0()}H",
            image,
        ])
        if new_bounds:
            out.append(self._format_marker_at_bounds(*new_bounds))
        self._last_marker_bounds = new_bounds
        out.append(self.sidebar.escapes(full=full_clear))

        out.extend(self._scrollbar_column_escapes())
        out.append(self.format_status())
        out.append(SYNC_END)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _remember_drawn_position(self) -> None:
        """Bookkeeping shared by _draw()'s full redraw and _draw_shifted()'s
        incremental one - what _scroll_shift_rows() compares the *next*
        draw's position against, to tell a plain scroll apart from a page
        turn, a zoom, or a pan (see there)."""
        self._last_page = self.page
        self._last_x_offset = self.x_offset
        self._last_zoom_key = round(self.zoom * 100)
        self._last_scroll = self.scroll
        self._last_fit_key = (self.fit, self.continuous)

    def _scroll_delta_px(self) -> int | None:
        """How far down (in pixels; negative for up) the viewport moved
        since the last draw, or None if that can't be told from here.
        Same page: just the scroll difference. Under -c/--continuous,
        also across a boundary into the page right after or before the
        last one (its height plus the gap is the distance between their
        origins) - anything further is at least a whole page's worth of
        movement, and never worth shifting for anyway."""
        # Only asked once a first draw has set these - see _scroll_shift_rows().
        assert self._last_page is not None and self._last_scroll is not None
        if self._last_page == self.page:
            return self.scroll - self._last_scroll
        if not self.continuous:
            return None
        gap = self._continuous_gap_px()
        if self._last_page == self.page - 1:
            return self._page_image(self._last_page).height + gap - self._last_scroll + self.scroll
        if self._last_page == self.page + 1:
            return -(self.img.height + gap - self.scroll + self._last_scroll)
        return None

    def _scroll_shift_rows(self, crop_w: int, crop_h: int) -> int | None:
        """Whole character-rows the viewport shifted since the last
        _draw(), or None if a full redraw is required.

        A vertical-only scroll within the same page/zoom/pan can reuse
        what's already on screen: the terminal's own scroll-region
        primitive (DECSTBM + CSI S/T) shifts an already-placed iTerm2
        inline image right along with plain text (confirmed by hand -
        see scroll_spike.py in this repo's history), so _draw_shifted()
        only has to transmit the newly-exposed strip instead of
        re-encoding and re-sending the whole viewport. This is what
        decides whether that shortcut applies; every condition here
        falls back to the always-correct full redraw when in doubt:

        - _last_viewport_set is the same "do we know anything about the
          previous frame" flag _needs_full_clear() uses - every place
          that overwrites the screen with something else (help, text
          mode, a resize, ...) already clears it, so relying on it here
          for free means _last_page/_last_x_offset/etc. never need
          resetting anywhere but _remember_drawn_position().
        - TMUX / not iterm2_like(): the shortcut depends on inline-image
          placements riding along with a scroll-region shift, which is
          unverified (and, for tmux, actively suspect - tmux owns the
          pane's own scrolling and may not replay image content the way
          a real terminal's internal grid does).
        - page/x_offset/zoom/crop size all matching: anything else means
          self.img itself or the crop rectangle changed shape, so there
          may be nothing valid left on screen to shift.
        - no active search marker: its box-drawing overlay would need
          shifting (or erasing) too, and matches are rare enough that
          it's simplest to just fall back when one's showing.
        - self.incremental_scroll: --no-incremental-scroll's escape
          hatch, for a terminal where the above turns out not to hold.
        - no sidebar: the scroll region spans the full width, so the
          thumbnails would scroll along with the page.
        """
        if (not self.incremental_scroll
                or self.sidebar.columns()
                or not self._last_viewport_set
                or os.environ.get("TMUX")
                or not iterm2_like()
                or crop_w != self._last_viewport_w
                or crop_h != self._last_viewport_h
                or self._last_x_offset != self.x_offset
                or self._last_zoom_key != round(self.zoom * 100)
                or self._last_fit_key != (self.fit, self.continuous)
                or self._last_marker_bounds is not None
                or self.search.visible_match() is not None):
            return None
        delta = self._scroll_delta_px()
        if delta is None or delta == 0 or delta % self.cell_h_px != 0:
            return None
        shift = delta // self.cell_h_px
        avail_rows = max(1, self.rows - 1)
        if abs(shift) >= avail_rows:
            return None  # no overlap left - a full redraw is just as cheap
        return shift

    def _draw_shifted(self, shift: int, crop_h: int) -> None:
        """The incremental path _draw() takes for a plain vertical
        scroll (see _scroll_shift_rows()): shift whatever's already
        displayed with the terminal's own scroll region instead of
        redrawing it, and transmit only the strip of pixels that just
        became visible. `crop_h` is the height of the whole viewport
        image, the same as _draw() would otherwise have sent."""
        avail_rows = max(1, self.rows - 1)
        strip_rows = abs(shift)
        strip_h = strip_rows * self.cell_h_px
        if shift > 0:
            # Scrolled forward: content moves UP, revealing new rows at
            # the BOTTOM.
            strip_top = crop_h - strip_h
            screen_row = avail_rows - strip_rows + 1
        else:
            # Scrolled backward: content moves DOWN, revealing new rows
            # at the TOP.
            strip_top = 0
            screen_row = 1
        strip = self._view_crop(strip_top, strip_h)

        encode_key = self._encode_key(strip_top, strip.width, strip.height)
        data = self.encode_cache.get(encode_key)
        if data is None:
            data = self._encode_crop(strip)
            self.encode_cache.put(encode_key, data)

        self._remember_drawn_position()

        # All in one synchronized frame (see SYNC_BEGIN): the scroll
        # region spans the full width, so the shift below moves the
        # scrollbar column along with the image - shown on its own, that
        # displaced scrollbar is a visible flicker until it's repainted.
        out = [
            SYNC_BEGIN,
            SGR_RESET,
            f"\x1b[1;{avail_rows}r",
            f"\x1b[{shift}S" if shift > 0 else f"\x1b[{strip_rows}T",
            "\x1b[r",  # back to a full-screen scroll region right away -
            # nothing past this point should be confined by it.
            f"\x1b[{screen_row};1H",
            _inline_image(data, strip.width, strip.height),
        ]
        out.extend(self._scrollbar_column_escapes())
        out.append(self.format_status())
        out.append(SYNC_END)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _scrollbar_column_escapes(self) -> list[str]:
        """The scrollbar column's cells, as a list of positioned escape
        strings - shared by _draw()'s full redraw and _draw_shifted()'s
        incremental one, both of which repaint it every frame regardless
        (it's cheap plain text, not part of what differential drawing
        is meant to optimize)."""
        if not self.scrollbar:
            return []
        # Same row count avail_height_px was computed from (see
        # _recompute_geometry()) - the column itself was already
        # held back from base_width_px, so the image never reaches
        # into it regardless of zoom/pan. self.page/self.npages fold
        # this page's own (self.scroll, avail_height_px, img.height)
        # fraction into a whole-document position - see
        # _scrollbar_fractions() - rather than just this page's own.
        start_frac, visible_frac = self._scrollbar_fractions(
            self.scroll, self.avail_height_px, self.img.height, self.page, self.npages
        )
        return self._scrollbar_escapes(max(1, self.rows - 1), start_frac, visible_frac)

    def status_segments(self) -> list[tuple[str, str]]:
        """The default status line, as (text, color) fields in order."""
        if self.text_mode:
            # Measured from text_scroll_min (-1 while the border's row
            # shows), and held to 0-100: <N>g can put a line at the top
            # past text_scroll_max (see go_to_text_line()).
            span = self.text_scroll_max - self.text_scroll_min
            pct = (
                100
                if span <= 0
                else max(0, min(100, int(100 * (self.text_scroll - self.text_scroll_min) / span)))
            )
            mode_field = " text "
        elif self.continuous:
            # No per-page scroll range to take a percentage of - how far
            # the bottom of the screen is through the whole document
            # instead, in the same page units the scrollbar uses (see
            # _scrollbar_fractions()), so the last screenful reads 100%.
            start_frac, visible_frac = self._scrollbar_fractions(
                self.scroll, self.avail_height_px, self.img.height, self.page, self.npages
            )
            pct = min(100, int(100 * (start_frac + visible_frac)))
        else:
            pct = (
                100
                if self.scroll_max == 0
                else int(100 * self.scroll / self.scroll_max)
            )
        if not self.text_mode:
            # self.zoom alone isn't comparable across m/M (fit height/
            # width): it's always "1.0" right after switching fit mode
            # (see set_fit()), which would show "100%" for either one
            # even though a fit-height page is rarely the same actual
            # size as its fit-width rendering. Comparing the page's
            # current pixel width (self.img.width - fit="height" still
            # yields a real width, just one implied by the page's
            # aspect ratio rather than target_px directly - see
            # get_page_image()) against self.base_width_px (M's own
            # 100% reference) instead makes the percentage mean the
            # same thing - "size relative to fit-to-width" - no matter
            # which fit mode or zoom level produced it.
            zoom_pct = round(100 * self.img.width / max(1, self.base_width_px))
            mode_field = f" zoom {zoom_pct}% "
        if self.continuous and (not self.text_mode or self._text_continuous()):
            # -c/--continuous (or toggled with c) - only where it
            # actually changes anything: not in the text mode of a
            # handler whose text is one flowing blob either way.
            mode_field += "cont "
        page, npages = self.page, self.npages
        if self.text_mode and not self.doc_handler.text_mode_is_paginated():
            # The whole document is one page of text here, whatever the
            # image view's own page count is (a Markdown file rendered
            # to 15 PDF pages is still just its one raw source).
            page, npages = 1, 1
        segments = [(f" {display_safe(self.name)} ", STATUS_COLOR_FILENAME)]
        if len(self.files) > 1:
            segments.append((
                f" file {self.file_index + 1}/{len(self.files)} ",
                STATUS_COLOR_FILE_INDEX,
            ))
        segments += [
            (f" page {page:>{len(str(npages))}}/{npages} ", STATUS_COLOR_PAGE),
            (f" {pct:>3}% ", STATUS_COLOR_LOC),
            (mode_field, STATUS_COLOR_ZOOM),
        ]
        if self.follow:
            segments.append((" follow ", STATUS_COLOR_FOLLOW))
        segments.append((" F1 or :h for help ", STATUS_COLOR_HELP))
        return segments

    def format_status(self, text: str | None = None) -> str:
        """Return escape sequence for the status line (no write)."""
        if text is not None:
            # A message can carry a file name or a document's text (see
            # display_safe()).
            status = pad_to_width(truncate_to_width(f" {display_safe(text)} ", self.cols), self.cols)
            return (
                f"\x1b[{self.rows};1H{STATUS_COLOR_ON}\x1b[2K"
                f"{status}{SGR_RESET}"
            )

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
            out.append(f"{SGR_RESET}{' ' * (self.cols - width_used)}")

        return f"\x1b[{self.rows};1H\x1b[2K{''.join(out)}{SGR_RESET}"

    def draw_status(self, text: str | None = None) -> None:
        # \x1b[?25l re-hides the real terminal cursor draw_search_prompt()
        # shows while a "/"/"?" query is being typed - every other status
        # line (including the "isn't available"/no-match ones shown right
        # after a search prompt closes) goes back to the normal paging
        # UI, which never shows a cursor of its own.
        sys.stdout.write(self.format_status(text) + "\x1b[?25l")
        sys.stdout.flush()

    def draw_search_prompt(
        self, buf: str, cursor: int, backward: bool = False, multiline: bool = False,
    ) -> None:
        """The search pattern being typed, echoed on the status line
        behind the prompt character it was opened with - "/" forward,
        "?" backward, the same as less(1) shows them - and "Multi-line"
        ahead of that while ^T is on (the way less(1) names its own
        search modifiers there), with a reminder of ^T at the right end
        while there's room for it. `cursor` is the
        index into `buf` (in Python characters, not columns) the next
        inserted/deleted character applies at - not necessarily
        len(buf), since ^B/^F/LEFT/RIGHT can move it back into the
        middle of an already-typed query."""
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
        prefix = ("Multi-line " if multiline else "") + ("?" if backward else "/")
        text = truncate_to_width(f"{prefix}{buf}", self.cols)
        # The terminal cursor's column, not buf's: it sits after the
        # prompt character plus every column `buf[:cursor]` occupies -
        # using display_width() (not len()) since a wide character (e.g.
        # Japanese) covers 2 columns, same reasoning as
        # format_status()'s own width accounting.
        col = min(self.cols, 1 + display_width(prefix) + display_width(buf[:cursor]))
        # \x1b[?25h shows the real terminal cursor - normally hidden the
        # whole time pdfless owns the screen (see the entry-screen write
        # in main()) - positioned at `col` so it tracks mid-string edits
        # too (draw_status() hides it again once the prompt closes).
        # A reminder of ^T at the right end, saying what pressing
        # it would do next - left out once the query itself needs the room.
        # Drawn before `text`, and the cursor put back after `text` as
        # always, so it's never where typing (or the IME) goes.
        hint = "^T single-line" if multiline else "^T multi-line"
        hint_out = ""
        if display_width(text) + 2 + display_width(hint) <= self.cols:
            hint_col = self.cols - display_width(hint) + 1
            hint_out = f"\x1b[{self.rows};{hint_col}H{hint}"
        sys.stdout.write(
            f"\x1b[{self.rows};1H{STATUS_COLOR_ON}\x1b[2K{hint_out}"
            f"\x1b[{self.rows};1H{text}"
            f"\x1b[{self.rows};{col}H\x1b[?25h"
        )
        sys.stdout.flush()

    def go_page(self, page: int, scroll: int | None) -> None:
        """Show `page` in image mode: `scroll` is how far down into it
        to start, or None for its bottom (scroll_max - valid only once
        _load_page() has loaded it, which is why the caller can't just
        pass self.scroll_max itself), the same as go_to_page_text()."""
        self.page = max(1, min(self.npages, page))
        self._load_page()
        self.scroll = self.scroll_max if scroll is None else scroll

    def _pdf_source(self) -> PdfDocument | None:
        """The PdfDocument to defer to for anything that only makes
        sense against a real PDF (page_size_pt(), build_link_index()) -
        either self.doc_handler itself, or the PdfDocument a
        RenderedDocument rendered to under the hood (see
        RenderedDocument.get_page_image()'s _pdf_delegate - a plain
        <a href> in the original Word/RTF document survives
        Chrome's --print-to-pdf as a real PDF link annotation, so this
        lets it be treated exactly like a real PDF's hyperlinks
        wherever this is used), or None if neither applies."""
        if isinstance(self.doc_handler, PdfDocument):  # i.e. self.is_pdf
            return self.doc_handler
        return getattr(self.doc_handler, "_pdf_delegate", None)

    def _restore_position(self, page: int, scroll: int, x_offset: int) -> None:
        self.page = max(1, min(self.npages, page))
        self._load_page()  # loads the image and recomputes scroll_max
        self.scroll = self._clamp_image_scroll(scroll)
        self._set_x_offset(x_offset)

    def go_to_link_target(self, page: int, top_pt: float | None) -> None:
        """Jump to `page`, scrolled so the link's target y-position
        (`top_pt`, in PDF points, bottom-up - or None if the link didn't
        specify one) lands a little below the top of the window, the
        same placement _Search.scroll_image_to_match() uses for search matches."""
        page = max(1, min(self.npages, page))
        height_pt = self._page_height_pt(page) if top_pt is not None else None
        # bottom-up -> top-down
        self._show_image_at(page, height_pt - top_pt if height_pt and top_pt is not None else None)

    def _show_image_at(self, page: int, y_pt: float | None) -> None:
        """Show `page` in image mode, scrolled so `y_pt` (in PDF points,
        down from the page's top) lands a quarter of the way down the
        window - or at its top, for None.

        Args:
            page: the page to show.
            y_pt: the position on it, or None.
        """
        self.page = max(1, min(self.npages, page))
        self._load_page()
        height_pt = self._page_height_pt(self.page)
        if y_pt is None or not height_pt or self.img is None:
            self.scroll = 0
            return
        px_top = y_pt * self.img.height / height_pt
        self.scroll = self._clamp_image_scroll(round(px_top) - self.avail_height_px // 4)

    def _page_height_pt(self, page: int) -> float | None:
        """`page`'s height in PDF points, or None without a real PDF
        behind the view (see _pdf_source())."""
        pdf_source = self._pdf_source()
        return (pdf_source.page_size_pt(page)[1] or None) if pdf_source else None

    def _image_reading_point(self) -> tuple[int, float | None]:
        """Where image mode is being read: the point a quarter of the way
        down the window, where a jump puts its target (see
        _show_image_at()) - on whichever page is there under
        -c/--continuous.

        Returns:
            (page, y_pt): the page, and the point's position on it in PDF
            points from its top - None without a real PDF behind it.
        """
        if self.img is None:  # no page shown yet
            return self.page, None
        reading = self.avail_height_px // 4
        page, img, y_px = self.page, self.img, self.scroll + reading
        if self.continuous and self._layout:
            for p, top, p_img in self._layout:
                if top + p_img.height > reading:  # the first page reaching down that far
                    page, img, y_px = p, p_img, max(0, reading - top)
                    break
        height_pt = self._page_height_pt(page)
        if not height_pt or not img.height:
            return page, None
        return page, min(y_px, img.height) * height_pt / img.height

    def reload(self) -> None:
        """Re-read the PDF from disk (e.g. -f/--follow noticed it changed
        underneath us) and redraw, staying on the same page number and
        in the same mode. The rasterized-page cache and search index are
        both keyed off content that's now stale, so both get dropped;
        any in-progress search is cleared too, since its match list may
        no longer correspond to anything in the new file."""
        if isinstance(self.doc_handler, PdfDocument):  # i.e. self.is_pdf
            self.doc_handler.forget_page_sizes()
            self.npages = self.doc_handler.page_count()
            self.page = max(1, min(self.npages, self.page))
        elif isinstance(self.doc_handler, RenderedDocument):
            # Unlike a PDF, an office-preview's pages are pre-rendered
            # PNGs on disk (see OfficeDocument._render_office_pages()) rather than
            # generated on demand - those need regenerating too, not
            # just dropping from the cache, or a changed file would just
            # redisplay the same stale pages.
            pages = self.doc_handler.build_pages(
                self.tmpdir,
                debug=self.debug, render_scale=self.office_render_scale,
                progress=_ViewerProgress(self),
            )
            if pages:
                # build_pages() already updated self.doc_handler.pages
                # (see OfficeDocument._remember_pages()) - just
                # self.cache.clear() below is needed to drop any now-stale
                # resized/cached images.
                self.npages = len(pages)
                self.page = max(1, min(self.npages, self.page))
        self.cache.clear()
        self.sidebar.reset()
        self.search.reset()
        self.links.forget_index()  # the pages (and their links) may have changed
        self._mode_switch = None
        self._text_pages = None  # stale too - re-extracted on demand
        self.overlays.forget_outline()  # the outline may have changed too
        if self.text_mode:
            self._load_text_page()
        else:
            self._load_page()
        self.refresh()
        self.draw_status(f"reloaded (file changed) - page {self.page}/{self.npages}")

    def _match_marker_bounds(
        self, px_left: float, px_top: float, px_right: float, px_bottom: float,
        page_top: int | None = None,
    ) -> tuple[int, int, int, int] | None:
        """Screen-cell bounds for a search-match box, or None if off-screen.
        The px_* coordinates are within the match's own page image;
        `page_top` is where that page's top edge sits in the viewport
        (see _visible_page_top()) - by default the current page's."""
        available_rows = max(1, self.rows - 1)  # bottom row is the status bar
        if page_top is None:
            page_top = -self.scroll

        col0 = (px_left - self.x_offset) // self.cell_w_px
        col1 = -(-(px_right - self.x_offset) // self.cell_w_px) - 1  # ceil - 1
        row0 = (px_top + page_top) // self.cell_h_px
        row1 = -(-(px_bottom + page_top) // self.cell_h_px) - 1

        col0, col1 = col0 - 1, col1 + 1  # border sits one cell outside the text
        row0, row1 = row0 - 1, row1 + 1
        left = self._image_col0() - 1  # (0-based) right of the sidebar
        col0, col1 = col0 + left, col1 + left

        col0, col1 = max(left, int(col0)), min(self.cols - 1, int(col1))
        row0, row1 = max(0, int(row0)), min(available_rows - 1, int(row1))
        if col0 > col1 or row0 > row1:
            return None
        return row0, col0, row1, col1

    def _format_marker_erase(self, row0: int, col0: int, row1: int, col1: int) -> str:
        """Wipe a previously drawn search-match box without clearing the screen."""
        width = col1 - col0 + 1
        out = [SGR_RESET]
        for row in range(row0, row1 + 1):
            out.append(f"\x1b[{row + 1};{col0 + 1}H\x1b[{width}X")
        return "".join(out)

    def _format_marker_at_bounds(self, row0: int, col0: int, row1: int, col1: int) -> str:
        """Return escape sequence for a search-match box overlay."""
        width = col1 - col0 + 1
        out = [SEARCH_MARKER_COLOR, f"\x1b[{row0 + 1};{col0 + 1}H┏{'━' * (width - 2)}┓"]
        for row in range(row0 + 1, row1):
            out.append(f"\x1b[{row + 1};{col0 + 1}H┃")
            out.append(f"\x1b[{row + 1};{col1 + 1}H┃")
        if row1 > row0:
            out.append(f"\x1b[{row1 + 1};{col0 + 1}H┗{'━' * (width - 2)}┛")
        out.append(SGR_RESET)
        return "".join(out)

    def _half_page_step(self) -> int:
        """"d"/"u"'s step size: half a screenful, rounded to a whole
        number of terminal rows rather than avail_height_px // 2 (which
        can land mid-row when the row count is odd). Keeping it a clean
        multiple of cell_h_px is also what lets _scroll_shift_rows()
        treat a "d"/"u" press the same as any other scroll - otherwise
        it'd fall back to a full redraw exactly on terminals with an
        odd number of rows, for no reason a user could see."""
        half_rows = max(1, (self.rows - 1) // 2)
        return self.cell_h_px * half_rows

    def scroll_down(self, step: int) -> None:
        if self.continuous:
            # No page turn to speak of - just move, and let
            # _normalize_continuous() carry the position over into the
            # next page (or stop it at the end of the document).
            self.scroll += step
            self._normalize_continuous()
            return
        if self.scroll < self.scroll_max:
            self.scroll = min(self.scroll_max, self.scroll + step)
        elif self.page < self.npages:
            self.go_page(self.page + 1, 0)

    def scroll_up(self, step: int) -> None:
        if self.continuous:
            self.scroll -= step
            self._normalize_continuous()
            return
        if self.scroll > 0:
            self.scroll = max(0, self.scroll - step)
        elif self.page > 1:
            self.go_page(self.page - 1, None)

    def handle_key_text(self, key: str) -> bool:
        """Key handling while in text mode: page/line navigation plus
        horizontal pan (for lines too wide for the terminal) - no
        zoom/fit, since there's no image here to resize."""
        avail_rows = self._text_avail_rows()
        if key == "B":
            # The border is opt-in precisely because its border
            # characters would get swept up in a terminal select-and-
            # copy, so it needs its own key - uppercase since lowercase
            # "b" is already BACKWARD_WINDOW_KEYS.
            self.toggle_text_border()
        elif key == "E":
            # Same idea as "B" above - uppercase, since lowercase "e"
            # is already FORWARD_LINE_KEYS (and stays that way here).
            self.toggle_eol_mark()
        elif key == "W":
            # The primary way to toggle wrap - uppercase, like B/E/C, and
            # clear of less(1)'s own "w" (back a window); "-S" (see
            # _PREFIX_BINDINGS) is kept for less(1) compatibility.
            self.toggle_text_wrap()
        elif key == "C":
            self.toggle_copy_mode()
        elif key == "#":
            # "N"/"n" are both already taken (next search match, next
            # page) - "-N"/"-n" (see run_viewer()'s dash_pending, kept
            # for less(1) compatibility) still work, but "#" is the
            # primary key for this one.
            self.toggle_line_numbers()
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
        elif key in ("K", "U", "SHIFT-UP", "g"):
            if self._text_page_starts is not None:
                self.go_to_page_text(self.page, 0)  # the current page, not the document
            else:
                self.text_scroll = self.text_scroll_min
        elif key in ("J", "D", "SHIFT-DOWN", "G"):
            if self._text_page_starts is not None:
                self.go_to_page_text(self.page, None)
            else:
                self.text_scroll = self.text_scroll_max
        elif key == "n":
            # A handler whose text isn't paginated (office/text/rtf -
            # see _load_text_page()) shows the whole document at once,
            # not one page/slide at a time - nothing for "next" to do.
            if self.doc_handler.text_mode_is_paginated() and self.page < self.npages:
                self.go_to_page_text(self.page + 1, 0)
        elif key == "p":
            if self.doc_handler.text_mode_is_paginated() and self.page > 1:
                self.go_to_page_text(self.page - 1, 0)
        elif key == "q":
            return False
        return True

    def mark_file_missing(self) -> None:
        """The file has been found deleted or moved since it was opened
        (by follow mode's check, or by a read failing - see
        _report_unreadable_file()): stop showing what was read from it
        before - blank the screen, with a status line saying why - until
        it's back (see refresh(), and poll_follow() for follow mode's
        own catching up)."""
        self.file_missing = True
        self._invalidate_screen()
        self._draw_file_missing()

    def _draw_file_missing(self) -> None:
        """What refresh() draws while file_missing: a blank screen and a
        status line saying why - and, with follow mode on, that it'll
        come back by itself."""
        note = " - reloads when it's back" if self.follow else ""
        sys.stdout.write(
            SGR_RESET + "\x1b[H\x1b[2J"
            + self.format_status(f"{self.name}: deleted or moved{note}") + "\x1b[?25l"
        )
        sys.stdout.flush()

    def _current_mtime(self) -> float | None:
        """The current file's mtime right now, or None if it can't be
        read (e.g. mid save-as-replace, when it briefly doesn't exist)."""
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def _start_following(self) -> None:
        """(Re)start follow mode's watch on the current file - whenever
        follow is turned on (-f, F, v) or switches to watching a
        different file (:n/:p). The next periodic check is due
        FOLLOW_INTERVAL seconds from now; what it compares against is
        _displayed_mtime, the version actually on screen."""
        self._follow_path = self.path
        self._follow_checked = time.monotonic()

    def _reload_if_changed(self) -> bool:
        """Reload the file if it changed on disk since what's on screen
        was read from it (_displayed_mtime) - True if it did. Counts as
        follow mode's latest check either way."""
        self._follow_checked = time.monotonic()
        mtime = self._current_mtime()
        if mtime is None:
            if not os.path.exists(self.path) and not self.file_missing:
                self.mark_file_missing()
            return False  # gone (or unreadable); try again next time
        if self.file_missing:
            # Back again - whatever its mtime, since what's on screen is
            # a blank, not the version _displayed_mtime refers to.
            self.file_missing = False
        elif mtime == self._displayed_mtime:
            return False
        # Moved on before reloading, not after: if the reload fails (see
        # below), this same version isn't retried on every check - only
        # a later write, with a newer mtime, is.
        self._displayed_mtime = mtime
        try:
            self.reload()
        except Exception:
            # The file may have been mid-write when we noticed the mtime
            # change (e.g. pdftoppm/pdfinfo saw a truncated file); keep
            # showing the last good render and pick up the change on a
            # later, now-complete write.
            pass
        return True

    def poll_follow(self) -> None:
        """Follow mode's periodic check, called on every pass of
        run_viewer()'s loop: every FOLLOW_INTERVAL seconds, reload the
        file if it changed since what's on screen was read from it."""
        if not self.follow:
            return
        if self.path != self._follow_path:
            self._start_following()  # :n/:p switched files
            return
        if time.monotonic() - self._follow_checked >= FOLLOW_INTERVAL:
            self._reload_if_changed()

    def toggle_follow(self) -> None:
        """F: -f/--follow switched on/off at runtime. Turning it on
        catches up at once - if the file changed while follow was off,
        that change is reloaded right away rather than FOLLOW_INTERVAL
        seconds later - and checks every FOLLOW_INTERVAL seconds from
        then on."""
        self.follow = not self.follow
        if self.follow:
            self._start_following()
            if self._reload_if_changed():
                return  # reload() has already redrawn
        self.refresh()

    def open_in_default_app(self) -> None:
        """v: hand the current file off to macOS's own default app for
        it (Preview/Word/Excel/...), and switch follow mode on (if it
        wasn't already) so an edit made there comes back automatically.
        self.path is always the original file, never a temporary
        rendered PDF, so this opens the same thing pdfless was pointed
        at in the first place, even for a soffice/Chrome-rendered format."""
        if not _open_in_default_app(self.path):
            self.draw_status(
                "opening the file in its own app needs macOS"
                if sys.platform != "darwin" else f"couldn't open {self.path}"
            )
            return
        if not self.follow:
            self.follow = True
            self._start_following()
        self.refresh()

    def text_toggle_refusal(self) -> str | None:
        """Why t/T can't switch modes right now, as a status message - or
        None if they can try (enter_text_mode() may still find there's
        no text after all)."""
        if self.doc_handler.starts_in_text_mode():
            # Always-on text mode already - toggling would try to switch
            # to an image view this kind doesn't have.
            return "this is already a plain text file"
        if self.text_mode and not iterm2_like():
            # Already in text mode - possibly run_viewer()'s startup
            # fallback put it there - and image mode wouldn't show
            # anything on this terminal anyway, so refuse to cross back
            # rather than switching to a blank screen.
            return "image mode needs iTerm2/WezTerm - not supported on this terminal"
        if not self.doc_handler.supports_text_mode():
            return "text mode isn't available for this file type"
        return None

    def handle_global_key(self, key: str) -> bool:
        """The keys that mean the same in image and text mode and need
        nothing from run_viewer()'s own loop state - True if `key` was
        one of them (and has been handled, redraw included)."""
        if key in ("\x0c", "FOCUS_IN"):
            # ^L: repaint the screen (e.g. after other output garbled it)
            # without otherwise changing anything. FOCUS_IN is the same
            # fix, triggered automatically - see FOCUS_ON's comment for
            # why a focus change (under tmux, especially) can otherwise
            # leave this pane blank, and for why only this direction,
            # not FOCUS_OUT, is safe to redraw on.
            self.refresh()
        elif key == "FOCUS_OUT":
            pass
        elif key == "r":
            self.toggle_scrollbar()
            self.refresh()
        elif key == "c":
            self.toggle_continuous()
        elif key == "F":
            self.toggle_follow()
        elif key == "v":
            self.open_in_default_app()
        elif key == "F1":
            # less(1) puts its help on "h"/"H", which pdfless can't - both
            # are panning keys here (less has nothing to pan). "?" isn't
            # free either, being less's backward search, so help lives on
            # F1, with ":h" as a second way in for terminals that send
            # something unexpected for F1.
            self.overlays.show_help()
        elif key in ("o", "\t"):
            self.overlays.show_outline()
        elif key == "O":
            self.overlays.show_files()
        elif key == "S":
            self.toggle_sidebar()
        elif key == "s":
            self.focus_sidebar()
        elif key in ("t", "T"):
            # T is t and C combined into one press/undo - see
            # toggle_clean_text_mode().
            refusal = self.text_toggle_refusal()
            toggle = self.toggle_text_mode if key == "t" else self.toggle_clean_text_mode
            if refusal is None and not toggle():
                refusal = "no text could be extracted from this file"
            if refusal is not None:
                self.draw_status(refusal)
        else:
            return False
        return True

    def handle_count_key(self, key: str, count: int | None) -> bool:
        """The keys a typed-in number (`count`, or None) can come before,
        other than g/G - True if `key` was one of them (and has been
        handled)."""
        if key in ("<", ">", "HOME", "END"):
            # "<"/">" (HOME/END are aliases): jump to the first/last page
            # of the whole document - "<number><" or "<number>>" jumps
            # straight to that page instead, in either mode.
            go = self.go_to_page_text if self.text_mode else self.go_page
            if count is not None:
                go(count, 0)
            elif key in ("<", "HOME"):
                go(1, 0)
            else:
                go(self.npages, None)
            self.refresh()
        elif key in ("{", "}"):
            # One-keystroke equivalents of ":p"/":n" - mirroring "["/"]"
            # (PDF link-history back/forward), the shifted key just above
            # each on a US keyboard.
            if key == "{":
                self.previous_file()
            else:
                self.next_file()
        elif key == "x":
            # "x" jumps to the first file in the list; "<number>x" jumps
            # straight to that file (1-based, matching "<number><"'s page
            # numbering) - meaningful only with more than one file, but
            # harmless otherwise (go_to_file() just reports there's
            # nowhere to go).
            self.go_to_file(count - 1 if count is not None else 0, "no such file")
        elif key == "X":
            self.go_to_file(len(self.files) - 1, "no such file")
        else:
            return False
        return True

    def _view_state(self) -> tuple:
        """Everything handle_key()/handle_key_text() can change that
        affects what's on screen - run_viewer() compares it before and
        after a key to decide whether a redraw is needed."""
        return (
            self.page, self.scroll, self.zoom, self.x_offset, self.fit,
            self.text_mode, self.text_scroll, self.text_x_offset, self.text_border,
            self.text_wrap, self.eol_mark, self.line_numbers, self.scrollbar,
        )

    def handle_key(self, key: str) -> bool:
        if self.text_mode:
            return self.handle_key_text(key)
        if key in FORWARD_WINDOW_KEYS:
            self.scroll_down(self.avail_height_px)
        elif key in BACKWARD_WINDOW_KEYS:
            self.scroll_up(self.avail_height_px)
        elif key in ("d", "\x04"):
            self.scroll_down(self._half_page_step())
        elif key in ("u", "\x15"):
            self.scroll_up(self._half_page_step())
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
            self.x_offset = self._max_x_offset()
        elif key in ("K", "U", "SHIFT-UP", "g"):
            self.scroll = 0
        elif key in ("J", "D", "SHIFT-DOWN", "G"):
            self.scroll = self.scroll_max
        elif key == "n":
            if self.page < self.npages:
                self.go_page(self.page + 1, 0)
        elif key == "p":
            if self.page > 1:
                self.go_page(self.page - 1, 0)
        elif key == "[":
            self.links.go_back()
        elif key == "]":
            self.links.go_forward()
        elif key == "q":
            return False
        return True


def read_key(fd: int) -> str | tuple | None:
    """Read one keypress from `fd`: a plain character, a named key
    (decode_csi_key()/read_ss3_key() - "UP", "F1", ...; "ESC-v" for
    Meta-v, "backward one window"), or ("MOUSE", kind, col, row) for an
    SGR mouse event (see decode_sgr_mouse()). "" for an escape sequence
    nothing here recognizes, None once the input is gone for good."""
    key = read_utf8_char(fd)
    if key != "\x1b":
        return key
    # Possibly ESC-v, an SS3 sequence (F1), or a CSI one (arrow/Home/
    # End/PageUp/PageDown, plain or Shift-ed; F1 on some terminals; a
    # mouse event) - or just a lone Esc, if nothing follows it quickly.
    r, _, _ = select.select([fd], [], [], 0.1)
    if not r:
        return key
    nxt = os.read(fd, 1)
    if nxt == b"v":
        return "ESC-v"
    if nxt == b"O":
        return read_ss3_key(fd) or ""
    if nxt == b"[":
        seq = read_csi_sequence(fd)
        mouse = decode_sgr_mouse(seq) if seq else None
        if mouse:
            return ("MOUSE", *mouse)
        return decode_csi_key(seq) or ""
    return key


class _LineEditor:
    """The search prompt's one line of input after "/" or "?": typed
    characters go in at the cursor, with readline's own bindings for
    moving and deleting - ^B/^F/LEFT/RIGHT move the cursor, ^A/^E/HOME/
    END jump it to the start/end, backspace and ^D/DEL delete before/
    under it, and ^U/^K kill from it to the start/end (DEL is the one
    exception to readline, added for the plain Delete key on keyboards
    without an easy ^D). ^T turns on (or off again) matching a regex
    across line breaks (see _Search.find_all_text_matches()). Enter
    submits, Esc/^C cancel, and so does backspace on an already-empty
    line. Every other key is swallowed, so nothing leaks through as a
    page command while the prompt is up."""

    def __init__(self) -> None:
        self.text = ""
        self.cursor = 0  # index into text the next edit applies at
        self.multiline = False  # ^T - see _Search.start()

    def handle(self, key: str) -> str | None:
        """Apply `key`: returns "submit", "cancel", "changed" (redraw the
        prompt), or None if it did nothing."""
        text, cursor = self.text, self.cursor
        if key in ("\r", "\n"):
            return "submit"
        if key in ("\x1b", "\x03"):
            return "cancel"
        if key in ("\x7f", "\x08"):
            if cursor > 0:
                text, cursor = text[:cursor - 1] + text[cursor:], cursor - 1
            elif not text:
                return "cancel"
        elif key in ("\x02", "LEFT"):  # ^B
            cursor = max(0, cursor - 1)
        elif key in ("\x06", "RIGHT"):  # ^F
            cursor = min(len(text), cursor + 1)
        elif key in ("\x01", "HOME"):  # ^A: jump to the start
            cursor = 0
        elif key in ("\x05", "END"):  # ^E: jump to the end
            cursor = len(text)
        elif key in ("\x04", "DEL"):  # ^D / Delete: delete under the cursor
            text = text[:cursor] + text[cursor + 1:]
        elif key == "\x15":  # ^U: kill from the cursor to the start
            text, cursor = text[cursor:], 0
        elif key == "\x0b":  # ^K: kill from the cursor to the end
            text = text[:cursor]
        elif key == "\x14":  # ^T: toggle matching across line breaks
            self.multiline = not self.multiline
            return "changed"
        elif len(key) == 1 and (key.isprintable() or unicodedata.category(key) == "Zs"):
            # Zs too: str.isprintable() is False for every space but " " -
            # the full-width "　" a Japanese input method types included.
            text, cursor = text[:cursor] + key + text[cursor:], cursor + 1
        if (text, cursor) == (self.text, self.cursor):
            return None
        self.text, self.cursor = text, cursor
        return "changed"


def _toggle_and_refresh(toggle: Callable[[Viewer], None]) -> Callable[[Viewer], None]:
    """A _PREFIX_BINDINGS action: call Viewer method `toggle`, then redraw."""
    def action(viewer: Viewer) -> None:
        toggle(viewer)
        viewer.refresh()
    return action


# The keys that can follow a one-key prefix, each mapped to what it does
# (an action returning False means quit). Any other key cancels quietly.
# ":" is less(1)'s :n/:p (next/previous file - also on "}"/"{") and :q,
# plus pdfless's own :h for the help screen (F1 is the primary way in).
# "-" (text mode only - it's zoom-out in image mode) is less(1)'s own
# runtime "-<option-letter>" toggle syntax, kept only for
# -S/--chop-long-lines and -N/--line-numbers compatibility; "W"/"#"
# alone are the primary keys for those.
_PREFIX_BINDINGS: dict[str, dict[str, Callable[[Viewer], Any]]] = {
    ":": {
        "n": Viewer.next_file,
        "p": Viewer.previous_file,
        "h": lambda viewer: viewer.overlays.show_help(),
        "q": lambda viewer: False,
    },
    "-": {
        "S": _toggle_and_refresh(Viewer.toggle_text_wrap),
        "N": _toggle_and_refresh(Viewer.toggle_line_numbers),
        "n": _toggle_and_refresh(Viewer.toggle_line_numbers),
    },
}


def _enter_screen_seq(alt_screen: bool, mouse: bool) -> str:
    """What to write to take over the terminal for the viewer: the
    alternate screen (unless `alt_screen` is False - --keep's resume
    after ^Z), a hidden cursor, alternate scroll and focus reporting, and
    mouse reporting if `mouse` (image mode only - see MOUSE_ON)."""
    return (
        ("\x1b[?1049h" if alt_screen else "") + "\x1b[?25l"
        + ALT_SCROLL_ON + FOCUS_ON + (MOUSE_ON if mouse else "")
    )


def _leave_screen_seq(alt_screen: bool) -> str:
    """_enter_screen_seq() undone: every reporting mode off, the cursor
    back, and the alternate screen left (unless `alt_screen` is False -
    --keep, which leaves the last page on screen)."""
    return (
        MOUSE_OFF + ALT_SCROLL_OFF + FOCUS_OFF + "\x1b[?25h"
        + ("\x1b[?1049l" if alt_screen else "")
    )


def _report_unreadable_file(viewer: Viewer) -> None:
    """For run_viewer()'s loop, inside an `except` for a failed read
    (subprocess.CalledProcessError/OSError): if it failed because the
    file was deleted or moved out from under us, blank the screen and
    say so (Viewer.mark_file_missing()), and let the loop carry on - the
    view comes back once the file does (see Viewer.refresh()). With the
    file still there it's a real failure, not this, and the exception is
    re-raised as it was."""
    if os.path.exists(viewer.path):
        raise  # the exception being handled, unchanged
    viewer.mark_file_missing()


def _suspend(fd: int, old_termios: list[Any], keep: bool, viewer: Viewer) -> None:
    """^Z: suspend, like a normal shell job-control app would - raw mode
    disables the tty's own ^Z-to-SIGTSTP translation (see RawTerminal),
    so this does it by hand: give the terminal back and cooked-mode the
    tty before actually stopping, then reverse all of that once `fg`
    resumes us. --keep leaves the alternate screen buffer alone, so the
    page stays on screen while suspended (the same trick main() uses to
    leave it up after quitting); otherwise the shell prompt lands on the
    real scrollback."""
    sys.stdout.write(_leave_screen_seq(alt_screen=not keep))
    sys.stdout.flush()
    termios.tcsetattr(fd, termios.TCSADRAIN, old_termios)
    # SIGSTOP rather than SIGTSTP: the cleanup above already does
    # everything SIGTSTP's catchability would be for, so there's no
    # downside to using the one stop signal that's guaranteed to actually
    # stop the process - it can't be caught, blocked, or ignored, unlike
    # SIGTSTP (which, at least on some setups, can silently fail to stop
    # it on the first try). Sent to the whole process group (pid 0), not
    # just our own pid: when launched via `uv run --script` (its
    # shebang), this process is a *child* of uv, which is what the shell
    # actually sees as the foreground job - stopping only ourselves would
    # leave uv running and still attached to the tty, so the shell would
    # never notice anything stopped.
    os.kill(0, signal.SIGSTOP)
    # ... stopped here until `fg` sends SIGCONT ...
    tty.setraw(fd)
    sys.stdout.write(_enter_screen_seq(alt_screen=not keep, mouse=not viewer.text_mode))
    sys.stdout.flush()
    viewer.request_resize()  # the terminal may have been resized while
    # stopped, and its contents are gone either way


def _fall_back_to_text_mode(viewer: Viewer) -> None:
    """Start in text mode instead of image mode on a terminal that can't
    show inline images (see iterm2_like()), telling the user so on the
    real screen first - or, if the file has no text mode, just warn.

    Args:
        viewer: the Viewer about to start, in image mode."""
    # Image mode is drawn entirely via the OSC 1337 inline-image
    # protocol (see iterm2_like()) - on a terminal that doesn't
    # understand it, that escape sequence is either ignored or shown
    # as garbage, so nothing meaningful ever reaches the screen.
    # Warn on the real (not yet alternate) screen, then fall back to
    # text mode where this file has one; run_viewer()'s own "t"/"T"
    # handling keeps you there afterwards (see there).
    name = os.path.basename(viewer.path)
    has_text = viewer.can_enter_text_mode()
    if has_text:
        warning = f"{name}: this terminal doesn't support inline images (needs iTerm2/WezTerm) - showing text mode instead"
    else:
        warning = f"{name}: this terminal doesn't support inline images (needs iTerm2/WezTerm), and no text mode is available for this file"
    sys.stdout.write(warning + "\r\n")
    sys.stdout.flush()
    time.sleep(2)
    if has_text:
        viewer.text_mode = True
        # _load_content() only (re)loads text_mode content when
        # switching *into* it (starts_in_text_mode()) or via
        # enter_text_mode() - flipping the flag directly like this
        # would otherwise leave text_lines at its __init__ default
        # ([]), drawing an empty page (border/scrollbar/status line
        # only, no content) until something else happened to reload it.
        viewer._load_text_page()


def _dump_if_one_screen(viewer: Viewer) -> bool:
    """-F/--quit-if-one-screen: print the first file straight to the
    terminal, and have run_viewer() quit, if it fits on one screen -
    otherwise put the viewer back the way the interactive session needs.

    Args:
        viewer: the Viewer about to start.

    Returns:
        True if it was printed (and pdfless should quit)."""
    # -F/--quit-if-one-screen: real less(1)'s own -F. Only affects
    # whether/how this first file starts up - never entering the
    # alternate screen at all here is what leaves the dump in the
    # terminal's real scrollback, the same as less -F itself (as
    # opposed to -k/--keep, which stays parked in the alternate
    # screen instead - not real scrollback).
    viewer._dump_margin_rows = 1  # see Viewer.__init__ - applied to
    # the "does it fit" check itself (not just the eventual render),
    # so a file that only fits with no margin at all correctly falls
    # through to the interactive session instead of still being
    # dumped somewhere it would immediately scroll itself out of.
    if viewer.text_mode:
        # -h/fit doesn't apply to text mode; "fits" here means the
        # actual line count needs no scrolling - not just "1 page"
        # (every plain-text/RTF file reports npages==1 regardless of
        # length, so gating on that alone would dump-and-quit any
        # length of piped $PAGER input instead of paging it).
        viewer._load_content()
        if viewer.text_scroll_max <= 0:
            viewer.dump_and_quit()
            return True
    elif viewer.npages == 1:
        # Force fit-to-height for the dump regardless of -h, so a
        # single page is guaranteed to fit vertically; -h still
        # governs the multi-page (interactive) case untouched.
        viewer.fit = "height"
        viewer._load_content()
        viewer.dump_and_quit()
        return True
    # else/fallthrough: not dumping after all (more than one page,
    # or - in text mode - didn't fit even with the margin) - undo it
    # and force a fresh geometry recompute, so the interactive
    # session gets the terminal's full usable height back
    # rather than staying stuck with one row less than it should have.
    viewer._dump_margin_rows = 0
    viewer.resized = True
    # The outcome is decided now - a later :n/:p to another office
    # file should get the normal interactive status-line progress
    # (_ViewerProgress), not _ensure_office_pages()'s own stderr
    # _ProgressLine fallback, which only makes sense while this
    # first file's dump-or-interactive question was still open.
    viewer.quit_if_one_screen = False
    return False


class _KeyDispatcher:
    """run_viewer()'s input loop's decisions: what each key or mouse
    event means right now, given what's been typed so far, and the
    input state that carries over from one key to the next.

    At most one piece of that state is active at a time: a number being
    typed in (a count for the next key - see Viewer.handle_count_key()),
    a one-key prefix awaiting its second key (see _PREFIX_BINDINGS), or
    a search query being typed.

    Attributes:
        viewer: the Viewer the keys act on.
        num_buf: the digits of a count typed so far, or "".
        pending_prefix: ":" or "-" while its second key is awaited, or None.
        search_editor: a _LineEditor while the "/"/"?" prompt is up, or None.
        search_backward: whether that prompt was opened with "?".
        last_search_query: remembered across searches, for a bare "/"/"?".
    """

    def __init__(self, viewer: Viewer, fd: int, old_termios: list[Any], keep: bool) -> None:
        """Args:
            viewer: the Viewer the keys act on.
            fd, old_termios, keep: what ^Z needs to suspend and resume
                (see _suspend()).
        """
        self.viewer = viewer
        self._fd = fd
        self._old_termios = old_termios
        self._keep = keep
        self.num_buf = ""
        self.pending_prefix: str | None = None
        self.search_editor: _LineEditor | None = None
        self.search_backward = False
        self.last_search_query: str | None = None

    def handle(self, key: str | tuple) -> bool:
        """Act on one read_key() result.

        The order of the checks is what decides which meaning a key gets:
        ^Z beats everything (even typing a search query), a prompt or
        pending prefix swallows the next key whatever it is, and the help
        or the table of contents swallows every other key but its own.

        Args:
            key: a key name, or ("MOUSE", kind, col, row).

        Returns:
            False to quit pdfless, True to keep reading keys."""
        viewer = self.viewer
        if isinstance(key, tuple):  # ("MOUSE", kind, col, row)
            if self.search_editor is None:
                viewer.mouse.handle(*key[1:])
            return True
        if key == "\x1a":
            _suspend(self._fd, self._old_termios, self._keep, viewer)
            return True
        if self.search_editor is not None:
            self._search_prompt_key(self.search_editor, key)
            return True
        if self.pending_prefix is not None:
            return self._prefix_key(self.pending_prefix, key)
        if key == "\x03":
            return False
        if viewer.overlays.active is not None:
            viewer.overlays.handle_key(key)
            return True
        if viewer.sidebar.picking:
            if viewer.sidebar.columns():
                viewer.sidebar.handle_key(key)
                return True
            # The column went (the terminal was narrowed): so does picking.
            viewer.sidebar.picking = False
        if viewer.handle_global_key(key):
            return True
        if key == ":" or (key == "-" and viewer.text_mode):
            # In image mode, "-" already means zoom out (see
            # Viewer.handle_key()) - only text mode gets the less(1)-style
            # "-S"/"-N" toggles.
            self.pending_prefix = key
            viewer.draw_status(key)
            return True
        if key in ("/", "?"):
            self._open_search_prompt(backward=key == "?")
            return True
        if self._search_match_key(key):
            return True
        if self._count_key(key):
            return True
        return self._view_key(key)

    def _search_prompt_key(self, editor: _LineEditor, key: str) -> None:
        """A key typed at the "/"/"?" prompt: edit the query, or submit
        it (an empty one repeats the last query) or cancel it."""
        viewer = self.viewer
        outcome = editor.handle(key)
        if outcome == "submit":
            query = editor.text or self.last_search_query
            self.search_editor = None
            if query:
                self.last_search_query = query
                viewer.search.start(query, backward=self.search_backward, multiline=editor.multiline)
            else:
                viewer.draw_status()
        elif outcome == "cancel":
            self.search_editor = None
            viewer.draw_status()
        elif outcome == "changed":
            viewer.draw_search_prompt(
                editor.text, editor.cursor, backward=self.search_backward,
                multiline=editor.multiline,
            )

    def _prefix_key(self, prefix: str, key: str) -> bool:
        """The key after ":" or "-" (see _PREFIX_BINDINGS) - False if it
        was ":q"."""
        self.pending_prefix = None
        action = _PREFIX_BINDINGS[prefix].get(key)
        if action is None:
            self.viewer.draw_status()
        elif action(self.viewer) is False:
            return False
        return True

    def _open_search_prompt(self, backward: bool) -> None:
        """"/" or "?": put up the search prompt.

        less(1)'s pair: "/" searches forward from here, "?" backward.
        Either way the whole document is searched and N/P then walk every
        match - the direction only decides which match this search lands
        on first (see _Search._match_index_from()). Search always works in
        text mode (there's always a flat list of lines to search)
        regardless of what the file kind supports in image mode
        (doc_handler.supports_search() - a real PDF's bbox index)."""
        viewer = self.viewer
        if viewer.text_mode or viewer.doc_handler.supports_search():
            self.search_editor = _LineEditor()
            self.search_backward = backward
            viewer.draw_search_prompt("", 0, backward=backward)
        else:
            viewer.draw_status("search isn't available for this file type")

    def _search_match_key(self, key: str) -> bool:
        """n/N/p/P and q/ESC while a search is active - True if `key` was
        one of them."""
        viewer = self.viewer
        if viewer.search.query is None:
            return False
        if key in ("n", "N"):
            viewer.search.repeat(forward=True)
        elif key in ("p", "P"):
            viewer.search.repeat(forward=False)
        elif key in ("q", "\x1b"):
            # With a search active, "q"/Esc dismiss it (removing the
            # match box/highlight and its status line) rather than
            # quitting pdfless outright - quit still works normally on
            # a second press, once there's no longer a search to clear.
            viewer.search.clear()
            viewer.refresh()
        else:
            return False
        return True

    def _count_key(self, key: str) -> bool:
        """A digit of a count, or a key a count applies to - True if
        `key` was fully handled here. Any other key cancels a pending
        count and is left for _view_key()."""
        viewer = self.viewer
        # A lone "0" (no pending number) resets the zoom/pan instead of
        # starting a number entry. ASCII digits only: str.isdigit() takes
        # "²" (AltGr+2 on a German keyboard) or "①" (an input method) too,
        # which int() can't read.
        if key in "0123456789" and len(key) == 1 and not (key == "0" and not self.num_buf):
            self.num_buf += key
            viewer.draw_status(f"number: {self.num_buf}")
            return True

        count = int(self.num_buf) if self.num_buf else None
        if key in ("g", "G") and count is not None:
            # "<number>g"/"<number>G": in text mode, jump straight to that
            # line of the current page's text. There's no page-image
            # equivalent of "line", so in image mode the count is simply
            # dropped and this falls through to plain g/G (the top/bottom
            # of the current page).
            self.num_buf = ""
            if viewer.text_mode:
                viewer.go_to_text_line(count)
                viewer.refresh()
                return True
            # Dropped: take "number: N" off the status line, which g/G
            # itself only redraws if it moves the view.
            viewer.draw_status()
        elif viewer.handle_count_key(key, count):
            self.num_buf = ""
            return True

        if self.num_buf:
            # Any other key cancels a pending number.
            self.num_buf = ""
            viewer.draw_status()
        return False

    def _view_key(self, key: str) -> bool:
        """Every other key: Viewer.handle_key()'s, redrawing if it
        changed the view - False if it was the one to quit."""
        viewer = self.viewer
        border_before = viewer.text_border
        before = viewer._view_state()
        if not viewer.handle_key(key):
            return False
        if viewer._view_state() != before:
            viewer.refresh()
            if viewer.text_border != border_before and viewer.text_wrap:
                # "B" toggled text_border, but _draw_text_wrapped() never
                # draws a border regardless of it - without this, B looks
                # like it does nothing at all while wrapped.
                viewer.draw_status("no border while wrapped - see -S/-s")
        return True


def run_viewer(
    files: list[DocumentHandler | str], start_file_index: int, start_page: int, tmpdir: str,
    fd: int, old_termios: list[Any], options: ViewerOptions = ViewerOptions(),
    viewer_out: list[Viewer] | None = None,
) -> Viewer:
    """Run the interactive viewer loop. Returns the Viewer instance so the
    caller can inspect its final geometry (e.g. to tidy up the screen).

    `viewer_out`, if given, is a list this appends the Viewer to as soon
    as it's constructed - main()'s own try/finally can only restore the
    terminal (mouse reporting, the alternate screen, ...) once this
    returns *or raises*, and `viewer = run_viewer(...)`'s assignment
    never happens on the latter, so without this a bug anywhere in the
    interactive loop below (a page render, a keypress handler, ...)
    would leave the terminal in whatever raw/alternate-screen/mouse-
    reporting state it was in when the exception hit - see main()."""
    viewer = Viewer(files, start_file_index, start_page, tmpdir, fd, options=options)
    if viewer_out is not None:
        viewer_out.append(viewer)

    if not viewer.text_mode and not iterm2_like():
        _fall_back_to_text_mode(viewer)
    if options.quit_if_one_screen and _dump_if_one_screen(viewer):
        return viewer

    sys.stdout.write(_enter_screen_seq(alt_screen=True, mouse=not viewer.text_mode))
    sys.stdout.flush()
    viewer.entered_alt_screen = True

    def on_winch(signum: int, frame: Any) -> None:
        viewer.request_resize()

    signal.signal(signal.SIGWINCH, on_winch)

    dispatcher = _KeyDispatcher(viewer, fd, old_termios, options.keep)
    viewer.refresh()
    viewer._schedule_prefetch()
    while True:
        viewer._schedule_page_prefetch()
        # Thumbnails rendered in the background - drawn once the prompt,
        # if one is open, isn't taking input any more.
        viewer.sidebar.poll(
            prompt_open=dispatcher.search_editor is not None or dispatcher.pending_prefix is not None,
        )
        r, _, _ = select.select([fd], [], [], 0.3)
        flush_background_debug()  # -d lines from a background prefetch
        viewer.poll_follow()

        if viewer.resized:
            try:
                viewer.refresh()
            except (subprocess.CalledProcessError, OSError):
                # e.g. a plain text file re-read at the new size
                _report_unreadable_file(viewer)
            continue
        if not r:
            # Nothing waiting - a good moment to act on a scrollbar drag
            # whose motion events stopped without a release arriving
            # (see _Mouse.handle()); a no-op otherwise.
            viewer.mouse.flush_drag()
            continue
        key = read_key(fd)
        if key is None:
            break
        try:
            if not dispatcher.handle(key):
                break
        except (subprocess.CalledProcessError, OSError):
            # e.g. the text, or the search index, this key needed
            _report_unreadable_file(viewer)

    return viewer


def _build_arg_parser() -> argparse.ArgumentParser:
    """pdfless's command line: every option main() reads, and --help's
    text (KEY_TABLE included, as its epilog).

    Returns:
        The parser, not yet run on sys.argv."""
    parser = argparse.ArgumentParser(
        prog="pdfless",
        description=(
            "Display PDF, image, and text files in iTerm2 or WezTerm, "
            "less(1)-style. Office documents, SVG, and Markdown are "
            "supported with additional dependencies (see the README)."
        ),
        epilog=f"Cache directory (--no-cache/--clear-cache): {_office_cache_root()}\n\n{KEY_TABLE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "--help", action="help", help="show this help and exit"
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "files", nargs="*", metavar="file",
        help="PDF, image, text, or supported document files; read stdin "
             "if omitted or if \"-\" is given",
    )
    parser.add_argument(
        "-p", "--page", type=int, default=1,
        help="start on page PAGE in the first file (default: %(default)s)",
    )
    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="print debugging information to stderr",
    )
    parser.add_argument(
        "-s", "--rendering-scale", type=float, default=OFFICE_RENDER_SCALE, metavar="N",
        help="rendering scale for image-based Quick Look previews "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "-c", "--continuous",
        action="store_true",
        help="show consecutive pages continuously",
    )
    parser.add_argument(
        "-k", "--keep",
        action="store_true",
        help="leave the last page on screen when quitting",
    )
    parser.add_argument(
        "-h", "--fit-height",
        action="store_true",
        help="fit pages to the terminal height instead of its width",
    )
    parser.add_argument(
        "-F", "--quit-if-one-screen",
        action="store_true",
        help="quit after displaying a document that fits on one screen",
    )
    parser.add_argument(
        "-B", "--no-border",
        action="store_false",
        dest="border",
        default=True,
        help="hide page borders in text mode",
    )
    parser.add_argument(
        "-S", "--chop-long-lines",
        action="store_true",
        help="don't wrap long lines in text mode",
    )
    parser.add_argument(
        "-E", "--no-eol-mark",
        action="store_false",
        dest="eol_mark",
        default=True,
        help="hide end-of-line markers in text mode",
    )
    parser.add_argument(
        "-N", "--line-numbers",
        action="store_true",
        help="show line numbers in text mode",
    )
    parser.add_argument(
        "--no-scrollbar",
        action="store_false",
        dest="scrollbar",
        default=True,
        help="hide the scrollbar",
    )
    parser.add_argument(
        "--sidebar",
        action="store_true",
        help="show page thumbnails beside the page in image mode",
    )
    parser.add_argument(
        "--no-incremental-scroll",
        action="store_false",
        dest="incremental_scroll",
        default=True,
        help="redraw the full page image on every scroll",
    )
    parser.add_argument(
        "-f", "--follow",
        action="store_true",
        help="reload the current file when it changes",
    )
    parser.add_argument(
        "--wheel-scroll-step", type=positive_int, default=2, metavar="N",
        help="scroll N lines per mouse-wheel step in image mode "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="render without using the persistent cache",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="delete the persistent cache and exit",
    )
    return parser


def _capture_stdin(tmpdir: str) -> str:
    """Drain stdin (the document, when pdfless runs as $PAGER) into a
    file of its own - every DocumentHandler.sniff() reads a real path,
    not a stream.

    Args:
        tmpdir: the scratch directory to write it into.

    Returns:
        The file's path."""
    stdin_path = os.path.join(tmpdir, "stdin")
    with open(stdin_path, "wb") as f:
        shutil.copyfileobj(sys.stdin.buffer, f)
    return stdin_path


def _collect_files(
    paths: list[str], tmpdir: str, page: int, debug: bool = False,
) -> tuple[list[DocumentHandler | str], int, int]:
    """Decide what the viewer opens: the files among `paths` that exist,
    and the first of them pdfless can actually display - telling the
    user on stderr about each one skipped, and exiting via die() if
    none is left.

    Args:
        paths: the files as given on the command line (stdin's already
            captured to a file - see _capture_stdin()).
        tmpdir: the scratch directory handlers render into.
        page: -p/--page, the page asked to start on.
        debug: -d/--debug, passed on to the sniffing.

    Returns:
        (files, start_file_index, start_page): files as Viewer takes
        them - absolute paths, except files[start_file_index], already
        its DocumentHandler - and `page` clamped to that file's pages
        (when they're known yet)."""
    # Only checked for existence up front, not sniffed - an
    # expensive check for some kinds (ImageDocument.sniff() actually
    # decodes the whole image, to catch a format that passes a
    # lighter check but still fails to load) that would otherwise
    # run on every file given, before the very first page ever
    # appears, even for a large batch that's never actually paged
    # through to the end.
    candidates = []  # [abs_path, ...] - files that at least exist
    for arg in paths:
        if not os.path.isfile(arg):
            print(f"pdfless: no such file, skipping: {arg}", file=sys.stderr)
            continue
        candidates.append(os.path.abspath(arg))
    if any(PdfDocument.is_pdf_file(path) for path in candidates):
        check_deps()

    # Sniff candidates in order, stopping at the first one pdfless can
    # actually display - that's the only one that has to be known
    # before the viewer can even start. Every other candidate is left
    # as a bare path in `files` below, sniffed lazily by
    # Viewer._classify() the moment (if ever) you actually navigate to
    # it via next_file()/previous_file()/go_to_file(). A raised
    # UnusableFile means some handler positively identified a leading
    # candidate's format but couldn't actually use it (e.g. a corrupt
    # PDF, or a password-protected one whose prompt - see
    # PdfDocument._ensure_unlocked(), forced here by the page_count()
    # call below - was cancelled) - specific enough to report and
    # skip, rather than falling through to try treating it as some
    # other kind.
    for i, path in enumerate(candidates):
        try:
            handler = _sniff_file(path, tmpdir, debug=debug)
            if handler is None:
                print(
                    f"pdfless: not a PDF, image, text, or Quick-Look-previewable "
                    f"file, skipping: {path}",
                    file=sys.stderr,
                )
                continue
            # A None page_count() (an office-kind first file) means
            # its real page count isn't known until Viewer.__init__
            # actually renders it, which also clamps self.page
            # against it then; here just keep whatever page number
            # was asked for (>= 1). For a PdfDocument, this is also
            # where a password prompt (if the file turns out to be
            # encrypted) actually happens - forced here, rather than
            # left for Viewer.__init__ to trigger, so cancelling it
            # falls through to the next candidate exactly like any
            # other unusable file.
            npages = handler.page_count()
        except UnusableFile as e:
            print(f"pdfless: {e}, skipping: {path}", file=sys.stderr)
            continue
        # [DocumentHandler | path str, ...], in the given order - only
        # this one is an actual handler at this point; see Viewer.files.
        files: list[DocumentHandler | str] = list(candidates)
        files[i] = handler
        start_page = page if npages is None else min(npages, page)
        return files, i, max(1, start_page)

    die("no valid PDF, image, text, or Quick-Look-previewable files given")


def _open_key_input(reading_stdin: bool) -> tuple[int, int | None]:
    """The terminal to read keys and mouse events from - stdin if it's
    one, otherwise /dev/tty - exiting via die() if there's no usable
    one.

    Args:
        reading_stdin: whether the document came in on stdin.

    Returns:
        (fd, fd_to_close): the terminal's fd, and the same fd again if
        it was opened here (/dev/tty) for the caller to close when
        done, or None if it's stdin's own."""
    if not reading_stdin and sys.stdin.isatty():
        return sys.stdin.fileno(), None
    # Either stdin was the document (pdfless as $PAGER - drained, see
    # _capture_stdin(), and never read again), or it's no terminal at
    # all: `find ... | xargs pdfless` (BSD xargs gives the command
    # /dev/null for stdin), `pdfless a.pdf < /dev/null`. Either way
    # /dev/tty is the real keyboard, the same trick less(1)/most(1) use.
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
    except OSError as e:
        die(f"can't open /dev/tty for keyboard input: {e}")
    return fd, fd


def _leave_viewer_screen(viewer: Viewer | None, keep: bool) -> None:
    """Put the terminal's screen back the way run_viewer() found it -
    whether it returned or raised.

    Args:
        viewer: the Viewer run_viewer() ran, or None if it never got
            as far as making one.
        keep: -k/--keep: leave the last page on screen instead of
            switching back from the alternate screen."""
    if viewer is not None and viewer.entered_alt_screen:
        if keep:
            # Stay in the alternate screen buffer so the last
            # rendered page remains visible; just clear the
            # status line and bring the cursor back so the
            # shell prompt lands cleanly below the image.
            sys.stdout.write(
                _leave_screen_seq(alt_screen=False)
                + f"\x1b[{viewer.rows};1H\x1b[2K"
            )
        else:
            sys.stdout.write(_leave_screen_seq(alt_screen=True))
        sys.stdout.flush()
    # else: the alternate screen was never entered (-F/--quit-
    # if-one-screen's dump-and-quit path) - nothing to restore.


def main() -> None:
    """pdfless's entry point: parse the command line, open the files, and
    run the viewer on the terminal until it quits."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.clear_cache:
        shutil.rmtree(_office_cache_root(), ignore_errors=True)
        print("pdfless: cleared the persistent rendered-pages cache")
        return

    if args.no_cache:
        global _OFFICE_CACHE_ENABLED
        _OFFICE_CACHE_ENABLED = False

    # Reading from stdin - no file given at all, or "-" given in its
    # place - lets pdfless work as $PAGER: git/man/etc. invoke $PAGER
    # with nothing but the piped content on stdin. Every
    # DocumentHandler.sniff() reads from a real path, not a stream, so
    # the whole pipe has to be drained into a file of its own first -
    # before stdin's fd gets reused for keyboard/mouse input, further
    # down (see "reading_stdin" again below).
    reading_stdin = not args.files or "-" in args.files
    if reading_stdin and sys.stdin.isatty():
        die("no file given, and stdin is a terminal - pipe something "
            "into pdfless, or name a file")

    tmpdir = tempfile.mkdtemp(prefix="pdfless.")
    tty_fd = None  # /dev/tty, if _open_key_input() opened it - ours to close
    try:
        if reading_stdin:
            stdin_path = _capture_stdin(tmpdir)
            # "-" (if given) stands for this same captured file; bare
            # `pdfless.py` with no file arguments at all means just it.
            args.files = [stdin_path if a == "-" else a for a in args.files] or [stdin_path]

        files, start_file_index, start_page = _collect_files(
            args.files, tmpdir, args.page, debug=args.debug,
        )

        if not sys.stdout.isatty():
            die("stdout must be a terminal")
        fd, tty_fd = _open_key_input(reading_stdin)

        with RawTerminal(fd) as rt:
            # Entering the alternate screen (and enabling mouse/alt-
            # scroll/focus reporting) is now run_viewer()'s own call, not
            # unconditional here - -F/--quit-if-one-screen's dump-and-quit
            # path skips it entirely so its output lands in the terminal's
            # real scrollback (see run_viewer(), and viewer.entered_alt_
            # screen below).
            viewer = None
            keep = args.keep
            viewer_out: list[Viewer] = []
            try:
                viewer = run_viewer(
                    files, start_file_index, start_page, tmpdir, fd, rt.old,
                    options=ViewerOptions.from_args(args, len(files)),
                    viewer_out=viewer_out,
                )
            except BaseException:
                # run_viewer() raised (a bug mid-render/mid-keypress, or
                # ^C during a re-render - see run_subprocess()) rather
                # than returning normally, so the assignment above never
                # happened - viewer_out is what run_viewer() reported
                # its Viewer through instead (see there). Ignore --keep
                # here and fall through to a full restore regardless:
                # the traceback about to print needs the real screen and
                # cooked mouse reporting to actually be visible, not
                # left behind in the alternate screen this is leaving.
                viewer = viewer_out[0] if viewer_out else None
                keep = False
                raise
            finally:
                _leave_viewer_screen(viewer, keep)
    finally:
        if tty_fd is not None:
            os.close(tty_fd)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # ^C during a run_subprocess() call (see its docstring) unwinds
        # all the way up to here rather than being caught anywhere in
        # between - main()'s own try/finally blocks have already put the
        # terminal back to normal by this point, so there's nothing left
        # to clean up; just exit quietly instead of dumping a traceback
        # nobody asked for. 130 is the conventional exit code for a
        # SIGINT-terminated process (128 + SIGINT's number).
        sys.exit(130)
