# pdfless

*([日本語](README-ja.md))*

A `less(1)`-like full-screen pager for PDFs and images, for terminals
that support [iTerm2](https://iterm2.com)'s inline image protocol.
Confirmed working on iTerm2 and [WezTerm](https://wezterm.org).

- View a PDF (or a plain image - PNG, JPEG, and whatever else
  [Pillow](https://python-pillow.org) can decode) right in the
  terminal, using almost the same keybindings as `less(1)` — scroll by
  line, by half/full window, jump to a page, and so on
- Zoom in/out and pan around a page; the mouse wheel scrolls too
- A scrollbar - a column on the terminal's right edge showing where
  you are in the whole document, in both image and text mode; click or
  drag it to jump around
- For a PDF, a plain-text mode that extracts the text from the page -
  handy for copying text out. Switch between image mode and text mode
  any time with `t`
- Search a PDF with a regex, jumping straight to each match - works in
  both image mode and text mode
- Clickable PDF hyperlinks in the page image - both external URLs
  (opened in your system browser) and internal links to another page
  in the same document
- Open more than one file at once (`pdfless a.pdf b.png ...`) and
  switch between them with `:n`/`:p`, `less(1)`-style
- Also opens a plain text file directly, shown straight in that same
  text mode and searchable the same way a PDF is
- On macOS, with a local Chrome/Chromium install, also opens
  Word/Excel/PowerPoint/Keynote/Pages/RTF files and more - see
  [Office Document Support (Experimental)](#office-document-support-experimental)
  below
- Works the same way over SSH, since it's just a terminal program - no
  need for VNC/Remote Desktop or to copy the file to your local machine
  first

## Screenshots

<p align="center">
  <img src="docs/screenshots/pdfless-width-fit.png" alt="Fit to width"><br>
  <em>Fit to width (default)</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-height-fit.png" alt="Fit to height"><br>
  <em>Fit to height (<code>-h</code>)</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-zoom.png" alt="Zoomed in"><br>
  <em>Zoomed in and panned</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-search-pdf-mode.png" alt="Search in PDF mode"><br>
  <em>Search — PDF mode, match boxed</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-search-text-mode.png" alt="Search in text mode"><br>
  <em>Search — text mode, match highlighted</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-hyperlinks.png" alt="Clickable hyperlinks"><br>
  <em>Clickable hyperlinks — external URLs and internal jumps</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-image.png" alt="A plain image file"><br>
  <em>A plain image file (PNG/JPEG/...)</em>
</p>

<p align="center">
  <img src="docs/screenshots/pdfless-help.png" alt="Help overlay"><br>
  <em>Help (<code>F1 / :h</code>)</em>
</p>

## Requirements

- A terminal that supports iTerm2's inline image protocol — confirmed
  working on [iTerm2](https://iterm2.com) and
  [WezTerm](https://wezterm.org); terminals
  without this protocol will not display anything.
- [poppler](https://poppler.freedesktop.org) (`pdftoppm`/`pdfinfo`/
  `pdftocairo`) - needed for viewing PDFs directly, and for rasterizing
  pictures embedded in a Quick Look preview file (Word/Excel/
  PowerPoint/etc.); not required at all if you only ever open plain
  image files.
- macOS + a local Chrome/Chromium install - needed for opening
  Office/Keynote/Pages/etc. files via Quick Look, and also (Chrome
  alone, no Quick Look involved) for opening an SVG file directly; a
  file like that is skipped with a warning if the dependency it needs
  is missing.
- [LibreOffice](https://www.libreoffice.org) (`soffice`) - optional;
  when installed, Word (`.doc`/`.docx`/`.docm`), RTF, and PowerPoint
  (`.ppt`/`.pptx`/`.pptm`) files render through it instead of the
  Quick Look + Chrome pipeline, for real page breaks and
  higher-fidelity output (see the Office/iWork table below). Falls
  back to Quick Look + Chrome when it isn't installed. Deliberately
  not used for Excel (`.xls`/`.xlsx`/`.xlsm`): soffice paginates a
  spreadsheet by its print area/page setup, which for a workbook
  never tuned for printing can fragment one sheet across several
  oddly-cut pages, unlike Quick Look's one-page-per-sheet view.
  Required (with no fallback) for OpenDocument (`.odt`/`.odp`/`.odg`/
  `.ods`), Visio (`.vsd`/`.vsdx`), and WMF, since macOS has no Quick
  Look generator for any of these at all.
- For Markdown (`.md`/`.markdown`): the `markdown` and `weasyprint`
  Python packages, both declared as dependencies below so `uv`
  installs them automatically - but `weasyprint` also needs
  Cairo/Pango/GLib/HarfBuzz as system libraries, which `pip`/`uv`
  can't install by themselves (e.g. `brew install cairo pango
  gdk-pixbuf libffi` on macOS). Without a working `weasyprint`, a
  Markdown file falls back to being shown as its own raw source, the
  same as any other optional renderer here.
- Python 3.9+
- [uv](https://docs.astral.sh/uv/)

## Installation

Install poppler, which provides the `pdftoppm`/`pdfinfo` binaries `pdfless`
shells out to:

```sh
# macOS (Homebrew)
brew install poppler

# Ubuntu/Debian (apt)
sudo apt install poppler-utils
```

Optionally, install [LibreOffice](https://www.libreoffice.org) for
higher-fidelity Word/RTF/PowerPoint rendering, and for OpenDocument/
Visio/WMF support, which needs it (see Requirements above):

```sh
# macOS (Homebrew)
brew install --cask libreoffice

# Ubuntu/Debian (apt)
sudo apt install libreoffice
```

Optionally, for rendered Markdown previews, install the system
libraries [WeasyPrint](https://doc.courtbouillon.org/weasyprint/) needs
(Cairo/Pango/GLib/GDK-Pixbuf) - the `markdown`/`weasyprint` Python
packages themselves are already declared as dependencies below, so `uv`
installs those automatically:

```sh
# macOS (Homebrew)
brew install cairo pango gdk-pixbuf libffi

# Ubuntu/Debian (apt)
sudo apt install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0
```

`pdfless.py` is a self-contained [PEP 723](https://peps.python.org/pep-0723/)
script — its Python dependencies are declared inline, so
[`uv`](https://docs.astral.sh/uv/) will install them automatically on
first run:

```sh
# macOS (Homebrew)
brew install uv

# Ubuntu/Debian and other Linux (uv isn't in apt; use the official installer)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```sh
git clone https://github.com/ktabe/pdfless.git
cd pdfless
./pdfless.py some.pdf
```

Or drop it on your `$PATH`:

```sh
cp pdfless.py /usr/local/bin/pdfless
chmod +x /usr/local/bin/pdfless
pdfless some.pdf
```

Without `uv`, install its Python dependencies yourself and run it with
`python3` directly:

```sh
pip install pillow pypdf markdown weasyprint
python3 pdfless.py some.pdf
```

## Usage

```
usage: pdfless [--help] [-v] [-p PAGE] [-d] [-s N] [-c] [-k] [-h] [-B] [-S]
               [-E] [-N] [--no-scrollbar] [--no-incremental-scroll] [-F]
               [--wheel-scroll-step N]
               [file ...]

positional arguments:
  file                  path to one or more PDF, image, text, or Quick-Look-
                        previewable files - reads from stdin instead if none
                        or "-" are given

options:
  --help                show this help message and exit
  -v, --version         show program's version number and exit
  -p, --page PAGE       page to start on, in the first file (default: 1)
  -d, --debug           print some debug information to stderr
  -s, --rendering-scale N
                        device-pixel-ratio to render Quick Look preview files
                        (Excel/PowerPoint/Keynote/Pages/etc., macOS only) at -
                        higher looks sharper when zoomed in but is slower to
                        render (default: 1). No effect on Word/RTF, which
                        render to a real PDF instead and are always sharp
                        regardless of zoom
  -c, --continuous      for a Quick Look preview file (macOS only), force
                        continuous scrolling instead of paginating
  -k, --keep            leave the last page on screen when quitting (q or ^C)
                        instead of restoring the terminal screen
  -h, --fit-height      fit each page to the terminal's full height instead of
                        its full width (default: fit width)
  -B, --no-border       don't draw a border around the page's edges in text
                        mode (t); on by default (except for a plain text
                        file, where it's off by default regardless of
                        this), toggle any time with B
  -S, --chop-long-lines
                        in text mode, don't wrap long lines - pan across
                        them instead with h/l/H/L, less(1)-style. Already
                        the default for anything but a plain text file,
                        which normally wraps; toggle any time by typing -S
  -E, --no-eol-mark     don't mark a real end-of-line (↵) in text mode -
                        shown by default (regardless of -S/--chop-long-
                        lines) to tell a genuine line ending apart from
                        where wrapping/panning simply ran out of room
  -N, --line-numbers    show line numbers in text mode, less(1)-style -
                        off by default; toggle any time with # (or -N,
                        kept for less(1) compatibility)
  --no-scrollbar        don't show the scrollbar (a column on the
                        terminal's right edge marking your position) -
                        shown by default, in both image and text mode;
                        toggle any time with r
  --no-incremental-scroll
                        always redraw the full page image on scroll, instead
                        of shifting the terminal's existing content and
                        transmitting only the newly-exposed strip
  -F, --follow          watch the file and reload it if it changes on disk
                        (checked every 3s), staying on the same page and in
                        the same mode
  --wheel-scroll-step N
                        scroll N lines per mouse wheel step, in the page image
                        (default: 2)
```

`-F`/`--follow` is handy while editing/regenerating a PDF or image (e.g.
from a build script, a LaTeX watch loop, or a script re-rendering a PNG) —
`pdfless` picks up each rebuild automatically, without losing your place.
With multiple files open, it only watches whichever one is currently
displayed, switching what it watches along with `:n`/`:p`.

## Office Document Support (Experimental)

On macOS, with a local Chrome/Chromium install, `pdfless` can also open
anything your Mac's Quick Look generators can preview — Word, Excel,
PowerPoint, Keynote, Pages, RTF, and more — by rendering that preview
through a headless Chrome instead of poppler/pypdf.

This is experimental: how well paging works depends entirely on what
each format's own Quick Look generator exposes, which varies a lot -
see the table below. A format that can't be paged is shown as one long
scrollable image instead (the same as a plain image file, or as
`-c`/`--continuous` forces for any of these).

Word and RTF render to a real PDF under the hood, so - like a normal
PDF, and unlike the other formats below - they stay sharp at any zoom
level, and a hyperlink in the original document is clickable, the same
as a real PDF's. When [LibreOffice](https://www.libreoffice.org)
(`soffice`) is installed, it's used to produce that PDF, natively and
with higher fidelity (real page breaks matching the original document,
correctly rendered embedded pictures of any format); otherwise it
falls back to rendering the Quick Look preview through headless
Chrome's `--print-to-pdf`, with paging coming from Chrome's own print
engine breaking real content flow across pages, rather than a
screen-mode page-boundary marker (which Word's Quick Look preview
doesn't have in the first place). `-c`/`--continuous` still collapses
any of these back to a single scrollable page, e.g. for a document
whose real page breaks land somewhere unhelpful.

PowerPoint gets the same soffice-rendered-PDF treatment when
`soffice` is installed (one slide per PDF page, matching the existing
one-page-per-slide paging exactly), gaining the same crisp-zoom/
clickable-hyperlink treatment, plus working text mode and real
search - neither of which Quick Look's own screenshot rendering can
offer at all. Without `soffice`, PowerPoint falls back to the
screenshot-based rendering it always used before, unlike Word/RTF
(which always end up as a real PDF either way, just via Chrome
instead). Excel is deliberately excluded from this - see Requirements
above. Macro-enabled Word/PowerPoint (`.docm`/`.pptm`) are treated
exactly like `.docx`/`.pptx` throughout, since macOS's Quick Look
generator doesn't distinguish them.

A second group of formats - OpenDocument (`.odt`/`.odp`/`.odg`/
`.ods`), Visio (`.vsd`/`.vsdx`), and WMF - has no Quick Look generator
on macOS at all (confirmed by hand: qlmanage either crashes outright
or produces no preview whatsoever for any of these), so they're
previewable only when `soffice` is installed, with no qlmanage-based
fallback to speak of; `-c`/`--continuous` has no effect on them
either, since soffice's own real pagination can't be collapsed back
into one page. `.ods` carries the same print-area/page-setup
pagination caveat Excel has (see Requirements above) - but unlike
Excel there's no working alternative, so it's supported anyway rather
than left unsupported. `.vsdx` specifically hasn't been verified by
hand (no sample was available), though LibreOffice's Visio import
handles `.vsd`/`.vsdx` through the same code.

SVG is the one exception that needs neither Quick Look nor `soffice`:
it renders via a real PDF produced by headless Chrome directly (which
`pdfless` already requires for everything else above), embedding the
SVG as an `<img>` and printing that to PDF - confirmed by hand that
this keeps the SVG's vector content as real vector PDF content, not a
flattened bitmap, so it stays sharp at any zoom like Word's PDF does.
The one limitation this approach has: a hyperlink inside the SVG
itself can't be clickable, since an `<img>` always strips
interactivity. Without a local Chrome, an SVG falls back to being
shown as its own raw XML source instead (the same as before this
feature existed).

Markdown (`.md`/`.markdown`) is rendered to a real PDF too, but
through neither Quick Look, Chrome, nor `soffice` - the `markdown`
and `weasyprint` Python libraries (see Requirements above) convert it
to HTML and then to a real PDF directly, dramatically faster than any
browser/office-suite round trip (confirmed by hand: comfortably under
a second, against a browser's own ~1-2s process startup alone) since
WeasyPrint has a real CSS pagination engine of its own - no measuring
step needed the way SVG/Word's Chrome path requires. A plain link
survives as a real, clickable PDF link the same way. Without those
libraries (or the system libraries WeasyPrint itself needs), a
Markdown file falls back to being shown as its own raw source instead.

| Format | Extensions | Paging | Text mode | Notes |
| --- | --- | --- | --- | --- |
| Word | `.doc`, `.docx`, `.docm` | Real page breaks | Yes | Renders to a PDF (see above) - via `soffice` when installed, else Chrome's `--print-to-pdf` |
| Excel | `.xls`, `.xlsx`, `.xlsm` | One page per sheet | No | |
| PowerPoint | `.ppt`, `.pptx`, `.pptm` | One page per slide | With `soffice` | With `soffice` installed, renders to a real PDF (see above); otherwise the original screenshot-based rendering, with no extractable text |
| RTF | `.rtf` | Real page breaks with `soffice`, otherwise always continuous | Yes | With `soffice` installed, rendered natively (real page breaks) the same as Word; otherwise converted to `.docx` via macOS's own `textutil` first (Quick Look has no HTML preview of its own for RTF), then rendered the same way as Word but always as one continuous page, since a converted RTF's page-height metadata doesn't correspond to anything in the original file |
| Pages | `.pages` | Always continuous | No | No page-boundary marker in Pages' Quick Look preview |
| Numbers | `.numbers` | Only the first sheet is shown | No | Numbers' Quick Look tab strip is marked up differently from Excel's and isn't recognized yet - a known gap, not a deliberate design choice |
| Keynote | `.key` | Always continuous | No | Unlike PowerPoint, Keynote's Quick Look preview exposes no slide-boundary marker at all - a known gap |
| OpenDocument Text | `.odt` | Real page breaks | Yes | Needs `soffice` - no Quick Look generator exists for this at all |
| OpenDocument Presentation | `.odp` | One page per slide | Yes | Needs `soffice` - same as `.odt` |
| OpenDocument Drawing | `.odg` | One page per Draw page | Yes | Needs `soffice` - same as `.odt` |
| OpenDocument Spreadsheet | `.ods` | Pages follow print layout | Yes | Needs `soffice`; carries the same print-area pagination caveat as Excel (see above), but unlike Excel has no alternative |
| Visio | `.vsd`, `.vsdx` | One page per Visio page | Yes | Needs `soffice` - no Quick Look generator exists for this at all; `.vsdx` unverified by hand |
| WMF | `.wmf` | Single page | Yes | Needs `soffice` - Quick Look can't preview it and no browser can decode it either |
| SVG | `.svg` | Single page | Yes | Renders via Chrome directly (see above), not `soffice` or Quick Look; falls back to raw XML source without a local Chrome |
| Markdown | `.md`, `.markdown` | Real pagination | Yes | Renders via `markdown` + `weasyprint` (see above), not Chrome, `soffice`, or Quick Look; falls back to raw Markdown source without those libraries |

Text mode (`t`) comes from macOS's own `textutil` for a Word-family
document without a real PDF behind it (`.doc`/`.docx`/`.docm`/`.rtf`)
- it has nothing to say about a spreadsheet, slide deck, or Apple's
own iWork bundle formats (Pages/Numbers/Keynote), so `t` reports no
text there even though the image view still works. Any format backed
by a real PDF (see above) instead gets its text straight from that
PDF, page by page.

A file needing Quick Look (most formats above) is skipped with a
warning if `qlmanage` or a local Chrome/Chromium isn't available. A
file needing only `soffice` (OpenDocument/Visio/WMF) or only Chrome
(SVG) is skipped only if that one dependency is missing.
`-s`/`--rendering-scale` controls how sharp the rendered pages look
when zoomed in for the formats that don't render to a real PDF (see
above); see [Usage](#usage) for both options.

## Keys

Navigation mirrors `less(1)`:

| Keys | Action |
| --- | --- |
| `e` `^E` `j` `^N` `Enter` `Down` | forward one line |
| `y` `^Y` `k` `^K` `^P` `Up` | backward one line |
| `f` `^F` `^V` `Space` `PageDown` | forward one window |
| `b` `^B` `Esc-v` `PageUp` | backward one window |
| `d` `^D` | forward half window |
| `u` `^U` | backward half window |
| `g` / `G` | jump to top / bottom of the current page (text mode: type a number first to jump to that line instead, e.g. `10g` → line 10) |
| `<` / `>` / `Home` / `End` | first / last page of the document (type a number first to jump to that page instead, e.g. `10<` → page 10) |
| `n` / `p` | next / previous page (see below for their other job during a search) |
| `:n` / `:p` | next / previous file, when more than one was given on the command line |
| `x` / `X` | jump to the first / last file in the list |
| `<N> x` | jump straight to file `N` |

Zoom and pan:

| Keys | Action |
| --- | --- |
| `+` / `-` | zoom in / out |
| `0` | reset zoom and pan |
| `m` / `M` | fit page to terminal height / width |
| `h` / `l` / `Left` / `Right` | pan left / right (when zoomed in) |
| `H` / `L` / `Shift-Left` / `Shift-Right` | jump to left / right edge |
| `K` / `U` / `Shift-Up` | jump to top of the current page (same as `g`) |
| `J` / `D` / `Shift-Down` | jump to bottom of the current page (same as `G`) |

Search always works for PDFs and plain text files, and for a Word/
RTF/PowerPoint document rendered via a real PDF (see Office Document
Support below) - directly in image mode, the same as a native PDF,
without needing `t` first. For any other Quick Look preview file
(Excel, or Word/RTF/PowerPoint without a real PDF available) it only
works once switched into text mode with `t`, since there's no way to
search its rendered page image directly; not available at all for a
plain image. It's a
case-insensitive [Python regex](https://docs.python.org/3/library/re.html)
against the extracted/file text, across the whole document (not just
the current page) - falling back to a literal substring match if the
pattern isn't valid regex syntax, e.g. `C++`. Jumping to a match
scrolls it into view, boxed on a PDF page image or highlighted in text:

| Keys | Action |
| --- | --- |
| `/<regex>` `Enter` | forward search for `<regex>` |
| `?<regex>` `Enter` | backward search for `<regex>` |
| `/` `Enter` / `?` `Enter` | with no pattern typed, repeat the last search pattern |
| `N` / `P` | jump to the next / previous match |
| `n` / `p` | while a search is active, the same as `N` / `P` above (otherwise next / previous page) |

Misc:

| Keys | Action |
| --- | --- |
| click / drag | (page image, not text mode) on the scrollbar, jump to the position clicked - and keep following the pointer while you drag; otherwise open a PDF hyperlink under the pointer - a URL in the system browser, or an internal link by jumping to its target page/position |
| mouse wheel | scroll up / down - in the page image, two lines at a time (`--wheel-scroll-step` to change that); in text mode, one line at a time |
| `[` / `]` | back / forward, through the positions internal links have jumped from |
| `t` | toggle plain-text view - the current page's extracted text for a PDF, or a Word/RTF/PowerPoint file rendered via a real PDF (see Office Document Support); otherwise the whole document's text for a Word-family file (`.doc`/`.docx`/`.rtf`), via macOS's `textutil`; a no-op for an image, spreadsheet, or a slide deck with no real PDF available, which have no text to extract |
| `B` | (text mode) toggle a border around the page's edges - on by default (`--no-border`/`-B` to start with it off); a plain text file starts with it off regardless; no border while wrapped, regardless of `B` (`-S` to unwrap first) |
| `s` / `-S` | (text mode) toggle wrapping long lines instead of panning across them with `h`/`l`/`H`/`L` - on by default for a plain text file, off otherwise (`-S`/`--chop-long-lines` to start unwrapped) |
| `E` | (text mode) toggle marking a real end-of-line (↵) - on by default (`-E`/`--no-eol-mark` to start without it) |
| `#` / `-N` | (text mode) toggle a line-number gutter - off by default (`-N`/`--line-numbers` to start with it on) |
| `C` | (text mode) clear the way for a select-and-copy: turn off the EOL markers, the border, the scrollbar and the line numbers in one go, so a drag across the text picks up the text alone. A second press puts back whatever was on before - anything you'd already switched off stays off |
| `r` | toggle the scrollbar - on by default (`--no-scrollbar` to start it off). In text mode it's display only, so the terminal's own click-drag text selection keeps working |
| `^L` | redraw the screen |
| `F1` / `:h` | show a keybinding help box (`q` to close it) |
| `q` / `:q` / `^C` | quit |

## Caveats

- Under tmux, nothing renders at all unless passthrough is turned on
  (tmux 3.3+): add `set -g allow-passthrough on` to `~/.tmux.conf`
  (and reload it, e.g. `tmux source-file ~/.tmux.conf`) — tmux drops
  the inline-image escape sequence by default otherwise.
- Also under tmux: switching away from a pane showing `pdfless` can
  leave it blank, since tmux's own screen model doesn't understand the
  passed-through image and repaints the pane without it. `pdfless`
  redraws automatically as soon as that pane is focused again, but only
  if tmux is told to forward focus events at all: add
  `set -g focus-events on` to `~/.tmux.conf` too. `^L` always redraws
  by hand if needed. (The pane can't redraw itself the moment it loses
  focus - by then tmux has already moved the terminal's one real cursor
  to the pane gaining focus, which is where a same-moment redraw would
  actually end up landing instead.)

## Testing

```sh
cd tests
uv run --with pytest --with pytest-timeout --with pillow --with pypdf --with markdown --with weasyprint python -m pytest
```

Most of the suite is fast (file-classification/caching unit tests); a
few tests drive a real `pdfless.py` through a pseudo-terminal to catch
crashes in the Viewer itself, since that needs an actual tty. One test
(`.docx` via a Quick Look preview) is skipped unless `qlmanage` and a
local Chrome/Chromium are both available.

## Acknowledgements

The code of this program was written by [Claude Code](https://claude.com/claude-code).

## License

[MIT](LICENSE)
