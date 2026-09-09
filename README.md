# pdfless

*([日本語](README-ja.md))*

A `less(1)`-like full-screen PDF pager for terminals that support
[iTerm2](https://iterm2.com)'s inline image protocol. Confirmed working on
iTerm2 and [WezTerm](https://wezterm.org).

Lets you view a PDF right in the terminal, using almost the same
keybindings as `less(1)` — scroll by line, by half/full window, jump to a
page, and so on. On top of that, since a PDF page is an image rather than
text, `pdfless` also supports zoom in and out.

You can search the whole document with a regex, jumping straight to each
match, and toggle (`t`) into a plain-text view of the current page any
time — handy for copying text out, or just reading it as text rather
than a rendered image.

Since it's just a terminal program, it works the same way over SSH — no
X11 forwarding, and no need to copy the PDF to your local machine first.

## Screenshots

| | |
| --- | --- |
| ![Fit to width](docs/screenshots/pdfless-width-fit.png)<br>Fit to width (default) | ![Fit to height](docs/screenshots/pdfless-height-fit.png)<br>Fit to height (`-h`) |
| ![Zoomed in](docs/screenshots/pdfless-zoom.png)<br>Zoomed in and panned | ![Search in PDF mode](docs/screenshots/pdfless-search-pdf-mode.png)<br>Search — PDF mode, match boxed |
| ![Search in text mode](docs/screenshots/pdfless-search-text-mode.png)<br>Search — text mode, match highlighted | |

## Requirements

- A terminal that supports iTerm2's inline image protocol — confirmed
  working on [iTerm2](https://iterm2.com) and
  [WezTerm](https://wezterm.org); others may work too, but terminals
  without this protocol will not display anything.
- [poppler](https://poppler.freedesktop.org) (`pdftoppm` / `pdfinfo`)
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

Without `uv`, install Pillow yourself and run it with `python3` directly:

```sh
pip install pillow
python3 pdfless.py some.pdf
```

## Usage

```
usage: pdfless [--help] [-v] [-p PAGE] [-k] [-h] [--no-frame] [-F] pdf

positional arguments:
  pdf               path to the PDF file

options:
  --help            show this help message and exit
  -v, --version     show program's version number and exit
  -p, --page PAGE   page to start on (default: 1)
  -k, --keep        leave the last page on screen when quitting (q or ^C)
                    instead of restoring the terminal screen
  -h, --fit-height  fit each page to the terminal's full height instead of its
                    full width (default: fit width)
  --no-frame        don't draw a border around the page's edges in text mode
                    (t); on by default, toggle any time with f
  -F, --follow      watch the PDF file and reload it if it changes on disk
                    (checked every 3s), staying on the same page and in the
                    same mode
```

`-F`/`--follow` is handy while editing/regenerating a PDF (e.g. from a build
script or LaTeX watch loop) — pdfless picks up each rebuild automatically,
without losing your place.

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
| `g` / `Home` | first page |
| `G` / `End` | last page |
| `<N> g` | jump straight to page `N` |
| `n` / `p` | next / previous page |

Zoom and pan (pdfless-specific, since a plain PDF page has no scrollable
"lines" of its own until it's rasterized):

| Keys | Action |
| --- | --- |
| `+` / `-` | zoom in / out |
| `0` | reset zoom and pan |
| `m` / `M` | fit page to terminal height / width |
| `h` / `l` / `Left` / `Right` | pan left / right (when zoomed in) |
| `H` / `L` / `Shift-Left` / `Shift-Right` | jump to left / right edge |
| `K` / `U` / `Shift-Up` | jump to top of the current page |
| `J` / `D` / `Shift-Down` | jump to bottom of the current page |

Search (a case-insensitive [Python regex](https://docs.python.org/3/library/re.html)
against the PDF's extracted text, across the whole document — not just
the current page; falls back to a literal substring match if the
pattern isn't valid regex syntax, e.g. `C++`. Note the case: uppercase
`N`/`P`, since lowercase `n`/`p` above already means next/previous
page). Jumping to a match scrolls it into view and draws a box around it:

| Keys | Action |
| --- | --- |
| `/<regex>` `Enter` | search the whole document for `<regex>` |
| `N` / `P` | jump to the next / previous match |

Misc:

| Keys | Action |
| --- | --- |
| `t` | toggle a plain-text view of the current page (its extracted text, scrollable by page/line, and `h`/`l`/`H`/`L` pan for lines wider than the terminal) |
| `f` | (text mode) toggle a border around the page's edges - on by default (`--no-frame` to start with it off) |
| `^L` | redraw the screen |
| `?` | show a keybinding help box (`q` to close it) |
| `q` / `^C` | quit |

## Acknowledgements

The code of this program was written by [Claude Code](https://claude.com/claude-code).

## License

[MIT](LICENSE)
