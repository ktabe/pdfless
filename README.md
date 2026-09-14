# pdfless

*([日本語](README-ja.md))*

A `less(1)`-like full-screen pager for PDFs and images, for terminals
that support [iTerm2](https://iterm2.com)'s inline image protocol.
Confirmed working on iTerm2 and [WezTerm](https://wezterm.org).

Lets you view a PDF (or a plain image - PNG, JPEG, and whatever else
[Pillow](https://python-pillow.org) can decode) right in the terminal,
using almost the same keybindings as `less(1)` — scroll by line, by
half/full window, jump to a page, and so on.
On top of that, `pdfless` also supports zooming in/out and panning. The mouse wheel is supported too, for scrolling.

For a PDF, `pdfless` also has a plain-text mode, which extracts the
text from the page — handy for copying text out. You can switch
between image mode and text mode any time with `t`.

You can also search a PDF with a regex, jumping straight to each
match — works in both image mode and text mode.

PDF hyperlinks are clickable in the page image — both external URLs
(opened in your system browser) and internal links to another page in
the same document.

You can also open more than one file at once (`pdfless a.pdf b.png ...`)
and switch between them with `:n`/`:p`, `less(1)`-style.

(As a bonus, `pdfless` can open a plain text file too, shown straight in
that same text mode and searchable the same way as a PDF's.)

On macOS, with a local Chrome/Chromium install, `pdfless` can also open
Word/Excel/PowerPoint/Keynote/Pages/RTF files and more — see
[Office Document Support (Experimental)](#office-document-support-experimental)
below.

Since it's just a terminal program, it works the same way over SSH — no
X11 forwarding, and no need to copy the PDF to your local machine first.

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
  <img src="docs/screenshots/pdfless-help.png" alt="Help overlay"><br>
  <em>Help (<code>?</code>)</em>
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
- macOS + a local Chrome/Chromium install - only needed for opening
  Office/Keynote/Pages/etc. files via Quick Look; a file like that is
  skipped with a warning if either is missing.
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
pip install pillow pypdf
python3 pdfless.py some.pdf
```

## Usage

```
usage: pdfless [--help] [-v] [-p PAGE] [-d] [-s N] [-c] [-k] [-h] [-B] [-S]
               [-E] [-N] [--no-scrollbar] [-F] [--wheel-scroll-step N]
               [file ...]

positional arguments:
  file                  path to one or more PDF, image, text, or Quick-Look-
                        previewable files - reads from stdin instead if none
                        are given (or if "-" is given in their place), so
                        pdfless can also be used as $PAGER

options:
  --help                show this help message and exit
  -v, --version         show program's version number and exit
  -p, --page PAGE       page to start on, in the first file (default: 1)
  -d, --debug           print timing for each stage of Quick Look preview
                        rendering (qlmanage, pdftocairo, measuring, rendering,
                        splitting into pages) to stderr
  -s, --rendering-scale N
                        device-pixel-ratio to render Quick Look preview files
                        (Word/Excel/PowerPoint/etc., macOS only) at - higher
                        looks sharper when zoomed in but is slower to render
                        (default: 1)
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

### Using pdfless as `$PAGER`

With no `file` argument at all (or with `-` in its place), `pdfless`
reads from stdin instead - the shape most tools invoke `$PAGER` with:

```sh
export PAGER=/path/to/pdfless.py
git log     # or: man some-command, journalctl, ...
```

or one-off, without exporting anything:

```sh
git log | pdfless.py
```

The whole input is read up front and shown as a plain text file (so
it's a snapshot, not a live `tail -f`-style follow), and keyboard input
still comes from the real terminal even though stdin itself is a pipe
- the same trick `less(1)`/`most(1)` use. A colorized tool's raw ANSI
escape codes aren't interpreted - most tools already turn color off
when their output isn't a terminal, but one forced on with something
like `--color=always` will show up as `^[` noise rather than color.

## Office Document Support (Experimental)

On macOS, with a local Chrome/Chromium install, `pdfless` can also open
anything your Mac's Quick Look generators know how to preview — Word,
Excel, PowerPoint, Keynote, Pages, RTF, and more — by rendering that
Quick Look preview through a headless Chrome instead of poppler/pypdf.
This is experimental: unlike the PDF/image/text-file support above, it
depends entirely on what each app's own Quick Look generator exposes,
and that varies a lot from one format to the next — most noticeably in
whether a multi-page/multi-sheet/multi-slide document can actually be
paged through, or only ever shown as one long continuously-scrollable
image (the same as a plain image file, and the same as `-c`/`--continuous`
forces for any of these).

| Format | Extensions | Paging | Notes |
| --- | --- | --- | --- |
| Word | `.doc`, `.docx` | Always continuous | No page-boundary marker in Word's Quick Look preview |
| Excel | `.xls`, `.xlsx` | One page per sheet | |
| PowerPoint | `.ppt`, `.pptx` | One page per slide | The most reliably paginated of any format here |
| RTF | `.rtf` | Always continuous | Converted to `.docx` via macOS's own `textutil` first (Quick Look has no HTML preview of its own for RTF), then rendered the same way as Word |
| Pages | `.pages` | Always continuous | Same limitation as Word - no page-boundary marker |
| Numbers | `.numbers` | Only the first sheet is shown | Numbers' Quick Look tab strip is marked up differently from Excel's and isn't recognized yet - a known gap, not a deliberate design choice |
| Keynote | `.key` | Always continuous | Unlike PowerPoint, Keynote's Quick Look preview exposes no slide-boundary marker at all - a known gap |

A file in any of these formats is skipped with a warning if `qlmanage`
or a local Chrome/Chromium isn't available. `-s`/`--rendering-scale`
controls how sharp the rendered pages look when zoomed in; see
[Usage](#usage) for both options.

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

Search (PDFs and plain text files always; a Quick Look preview file -
Word/Excel/PowerPoint/etc. - once switched into text mode with `t`,
since there's no equivalent way to search its rendered page image; not
available at all for a plain image - a case-insensitive
[Python regex](https://docs.python.org/3/library/re.html) against the
extracted/file text, across the whole document — not just the current
page; falls back to a literal substring match if the pattern isn't
valid regex syntax, e.g. `C++`). Jumping to a match scrolls it into
view, boxed on a PDF page image or highlighted in text:

| Keys | Action |
| --- | --- |
| `/<regex>` `Enter` | search the whole document for `<regex>`, landing on the first match from here on |
| `?<regex>` `Enter` | the same search, landing on the last match before here instead - `less(1)`'s backward search |
| `N` / `P` | jump to the next / previous match |
| `n` / `p` | while a search is active, the same as `N` / `P` above (otherwise next / previous page) |

Misc:

| Keys | Action |
| --- | --- |
| click / drag | (page image, not text mode) on the scrollbar, jump to the position clicked - and keep following the pointer while you drag; otherwise open a PDF hyperlink under the pointer - a URL in the system browser, or an internal link by jumping to its target page/position |
| mouse wheel | scroll up / down - in the page image, two lines at a time (`--wheel-scroll-step` to change that); in text mode, one line at a time |
| `[` / `]` | back / forward, through the positions internal links have jumped from |
| `t` | toggle a plain-text view (scrollable by page/line, and `h`/`l`/`H`/`L` pan for lines wider than the terminal) - for a PDF, the current page's extracted text; for a Word-family Quick Look preview file (`.doc`/`.docx`/`.odt`/`.rtf`/...), the whole document's text via macOS's `textutil` (not paginated - `n`/`p` do nothing in this view). On a plain image, a spreadsheet, or a slide deck, this just reports that there's no text to show (a plain text file is already shown this way, with nothing to toggle; an `.rtf` file falls back to this too, shown as its actual text via `textutil` rather than its raw markup, only if Quick Look/Chrome rendering isn't available) |
| `B` | (text mode) toggle a border around the page's edges - on by default (`--no-border`/`-B` to start with it off); a plain text file starts with it off regardless, since there's usually no real "page" boundary in one worth bordering; no border while wrapped, regardless of `B` (`-S` to unwrap first) |
| `s` / `-S` | (text mode) toggle wrapping long lines instead of panning across them with `h`/`l`/`H`/`L` - on by default for a plain text file, off otherwise (`-S`/`--chop-long-lines` to start unwrapped); no border while wrapped, regardless of `B`. `s` is the primary key; `-S` (`less(1)`-style) is kept only for compatibility |
| `E` | (text mode) toggle marking a real end-of-line (↵) - on by default (`-E`/`--no-eol-mark` to start without it); uppercase, since lowercase `e` already scrolls forward one line |
| `#` / `-N` | (text mode) toggle a right-aligned, gray line-number gutter - off by default (`-N`/`--line-numbers` to start with it on) |
| `C` | (text mode) clear the way for a select-and-copy: turn off the EOL markers, the border, the scrollbar and the line numbers in one go, so a drag across the text picks up the text alone. A second press puts back whatever was on before - anything you'd already switched off stays off |
| `r` | toggle the scrollbar - a column on the terminal's right edge showing where you are in the whole document (image mode and text mode alike). In the page image you can click it to jump to that position, or drag it to move around; in text mode it's display only, so the terminal's own click-drag text selection keeps working. On by default (`--no-scrollbar` to start it off) |
| `^L` | redraw the screen |
| `F1` / `:h` | show a keybinding help box (`q` to close it). `less(1)` puts this on `h`/`H`, which are panning keys here, and `?` is its backward search - hence `F1`, with `:h` as a second way in |
| `q` / `^C` | quit |

## Testing

```sh
cd tests
uv run --with pytest --with pytest-timeout --with pillow --with pypdf python -m pytest
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
