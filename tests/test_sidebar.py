"""The page thumbnails down the left edge (S/--sidebar - _Sidebar): the
columns they take from the page image, rendering them in the
background, keeping the page being viewed in view and framed, and the
mouse over them."""

import fcntl
import pty
import re
import struct
import tempfile
import termios

import pdfless


def make_viewer(sample_pdf, monkeypatch, sidebar=True, cols=100, rows=30):
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    monkeypatch.delenv("TMUX", raising=False)
    _master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, cols * 8, rows * 16))
    viewer = pdfless.Viewer(
        [pdfless.PdfDocument(sample_pdf)], 0, 1, tempfile.mkdtemp(), slave, "width",
        options=pdfless.ViewerOptions(sidebar=sidebar),
    )
    viewer.refresh()
    return viewer


def render_thumbnails(viewer):
    """Let the sidebar render what it shows, as run_viewer()'s loop would."""
    viewer.sidebar.poll()
    if viewer.sidebar._thread is not None:
        viewer.sidebar._thread.join(20)


def test_it_takes_its_columns_from_the_page(sample_pdf, monkeypatch):
    off = make_viewer(sample_pdf, monkeypatch, sidebar=False)
    on = make_viewer(sample_pdf, monkeypatch)
    assert on.sidebar.columns() == pdfless.SIDEBAR_COLS
    assert off.base_width_px - on.base_width_px == pdfless.SIDEBAR_COLS * on.cell_w_px
    assert on._image_col0() == pdfless.SIDEBAR_COLS + 1


def test_the_page_image_starts_right_of_it(sample_pdf, monkeypatch, capsys):
    viewer = make_viewer(sample_pdf, monkeypatch)
    viewer._invalidate_screen()
    capsys.readouterr()
    viewer.refresh()
    out = capsys.readouterr().out
    assert f"\x1b[1;{pdfless.SIDEBAR_COLS + 1}H\x1b]1337;File=" in out


def test_not_in_a_narrow_terminal(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch, cols=pdfless.SIDEBAR_MIN_TERM_COLS - 1)
    assert viewer.sidebar.columns() == 0
    assert viewer._image_col0() == 1


def test_thumbnails_render_in_the_background_and_show_up(sample_pdf, monkeypatch, capsys):
    viewer = make_viewer(sample_pdf, monkeypatch)
    _w, _rows, _slot, slots = viewer.sidebar._layout()
    render_thumbnails(viewer)
    capsys.readouterr()
    viewer.sidebar.poll()  # draws what the thread finished
    out = capsys.readouterr().out
    shown = min(slots, viewer.npages)
    assert out.count("\x1b]1337;File=") == shown
    assert "┏" in out and "━ 1 ━" in out  # page 1, the one being viewed, framed


def test_the_page_being_viewed_is_kept_in_view(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)
    _w, _rows, _slot, slots = viewer.sidebar._layout()
    assert slots < viewer.npages  # (so it has to scroll)
    viewer.go_page(viewer.npages, 0)
    viewer.refresh()
    assert viewer.sidebar.first == viewer.npages - slots + 1
    viewer.go_page(1, 0)
    viewer.refresh()
    assert viewer.sidebar.first == 1


def test_a_click_on_a_thumbnail_goes_to_its_page(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)
    _w, _rows, slot_rows, _slots = viewer.sidebar._layout()
    viewer.mouse.handle("MOUSE_CLICK", 5, slot_rows + 2)  # the second slot
    assert viewer.page == 2


def test_the_wheel_over_it_scrolls_it_alone(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)
    viewer.mouse.handle("MOUSE_WHEEL_DOWN", 5, 3)
    assert viewer.sidebar.first == 2
    assert (viewer.page, viewer.scroll) == (1, 0)  # the page itself didn't move
    viewer.refresh()
    assert viewer.sidebar.first == 2  # nor does a redraw pull it back


def test_links_and_the_search_marker_are_offset_by_it(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)
    bounds = viewer._match_marker_bounds(0, 100, 50, 120)
    assert bounds is not None and bounds[1] == pdfless.SIDEBAR_COLS  # 0-based: right of it
    off = make_viewer(sample_pdf, monkeypatch, sidebar=False)
    assert off._match_marker_bounds(0, 100, 50, 120)[1] == 0


def test_s_toggles_it(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch, sidebar=False)
    width = viewer.base_width_px
    assert viewer.handle_global_key("S")
    assert viewer.sidebar.columns() and viewer.base_width_px < width
    assert viewer.handle_global_key("S")
    assert not viewer.sidebar.columns() and viewer.base_width_px == width

    narrow = make_viewer(sample_pdf, monkeypatch, sidebar=False, cols=pdfless.SIDEBAR_MIN_TERM_COLS - 1)
    narrow.handle_global_key("S")
    assert narrow.sidebar.on is False


def test_no_shift_scrolling_under_it(sample_pdf, monkeypatch, capsys):
    """The terminal's scroll region spans the whole width - the
    thumbnails would scroll along with the page."""
    viewer = make_viewer(sample_pdf, monkeypatch)
    viewer.refresh()
    capsys.readouterr()
    viewer.scroll_down(viewer.cell_h_px * 2)
    viewer.refresh()
    out = capsys.readouterr().out
    assert not re.search(r"\x1b\[\d+S", out)  # no scroll-up of a region


def test_s_puts_it_up_ready_to_pick_one(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch, sidebar=False)
    viewer.handle_global_key("s")
    assert viewer.sidebar.columns() and viewer.sidebar.picking and viewer.sidebar.picked == 1
    d = pdfless._KeyDispatcher(viewer, -1, [], keep=False)
    for key in ("j", "j", "p"):  # "p" is swallowed: no page turn underneath
        assert d.handle(key)
    assert (viewer.sidebar.picked, viewer.page) == (3, 1)
    d.handle("\r")
    assert viewer.page == 3 and not viewer.sidebar.picking
    assert viewer.sidebar.columns()  # still up


def test_q_or_s_goes_back_to_the_page_and_capital_s_closes(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)  # --sidebar: up, not picking
    d = pdfless._KeyDispatcher(viewer, -1, [], keep=False)
    assert not viewer.sidebar.picking
    d.handle("s")
    assert viewer.sidebar.picking
    d.handle("G")
    assert viewer.sidebar.picked == viewer.npages
    d.handle("q")
    assert not viewer.sidebar.picking and viewer.page == 1 and viewer.sidebar.columns()
    d.handle("s")
    d.handle("s")  # s again: back to the page too
    assert not viewer.sidebar.picking and viewer.sidebar.columns()
    d.handle("s")
    d.handle("S")  # closes it, picking or not
    assert not viewer.sidebar.columns() and not viewer.sidebar.picking
    d.handle("S")  # S shows it, without moving the keys there
    assert viewer.sidebar.columns() and not viewer.sidebar.picking


def test_the_picked_thumbnail_is_kept_in_view_and_framed(sample_pdf, monkeypatch, capsys):
    viewer = make_viewer(sample_pdf, monkeypatch)
    _w, _rows, _slot, slots = viewer.sidebar._layout()
    viewer.sidebar.start_picking()
    capsys.readouterr()
    viewer.sidebar.handle_key("G")
    out = capsys.readouterr().out
    assert viewer.sidebar.first == viewer.npages - slots + 1
    assert pdfless.SIDEBAR_PICK_COLOR + "\x1b[" in out  # the teal frame
    assert f"ENTER to go to page {viewer.npages}" in out


def test_its_last_column_is_its_scrollbar(sample_pdf, monkeypatch, capsys):
    viewer = make_viewer(sample_pdf, monkeypatch)
    _w, _rows, _slot, slots = viewer.sidebar._layout()
    cells = viewer.sidebar._scrollbar_cells(slots)
    thumb = [i for i, cell in enumerate(cells) if cell == pdfless.SCROLLBAR_THUMB]
    assert thumb and thumb[0] == 0  # at the top, for the first pages
    assert len(thumb) == round(slots / viewer.npages * len(cells))
    viewer._invalidate_screen()
    capsys.readouterr()
    viewer.refresh()
    assert f"\x1b[1;{pdfless.SIDEBAR_COLS}H{pdfless.SCROLLBAR_THUMB}" in capsys.readouterr().out


def test_just_a_line_when_every_thumbnail_fits(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch, rows=200)
    _w, _rows, _slot, slots = viewer.sidebar._layout()
    assert slots >= viewer.npages
    assert set(viewer.sidebar._scrollbar_cells(slots)) == {pdfless.SIDEBAR_SEPARATOR}


def test_clicking_and_dragging_its_scrollbar_scroll_it_alone(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)
    _w, _rows, _slot, slots = viewer.sidebar._layout()
    avail = viewer.rows - 1
    bar = pdfless.SIDEBAR_COLS
    viewer.mouse.handle("MOUSE_CLICK", bar, avail)  # the very bottom
    assert viewer.sidebar.first == viewer.npages - slots + 1
    viewer.mouse.handle("MOUSE_DRAG", bar, 1)  # dragged back up to the top
    viewer.mouse.handle("MOUSE_RELEASE", bar, 1)
    assert viewer.sidebar.first == 1 and not viewer.sidebar.dragging
    assert (viewer.page, viewer.scroll) == (1, 0)  # the page itself never moved
    viewer.mouse.handle("MOUSE_DRAG", bar, avail)  # no longer following
    assert viewer.sidebar.first == 1


def test_t_while_picking_goes_to_the_picked_page_in_text_mode(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch, sidebar=False)
    d = pdfless._KeyDispatcher(viewer, -1, [], keep=False)
    d.handle("s")
    d.handle("j")
    d.handle("t")
    assert viewer.text_mode and viewer.page == 2
    assert not viewer.sidebar.picking


def test_nothing_is_drawn_while_the_prompt_takes_input(sample_pdf, monkeypatch, capsys):
    """Drawing would move the cursor away from the search prompt (and an
    input method's composition window with it)."""
    viewer = make_viewer(sample_pdf, monkeypatch)
    capsys.readouterr()
    viewer.sidebar.poll(prompt_open=True)  # starts rendering, draws nothing
    assert viewer.sidebar._thread is not None
    viewer.sidebar._thread.join(20)
    viewer.sidebar.poll(prompt_open=True)
    assert capsys.readouterr().out == ""
    viewer.sidebar.poll()  # the prompt closed
    assert "\x1b]1337;File=" in capsys.readouterr().out


def test_picking_ends_when_the_column_goes(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch, sidebar=False)
    d = pdfless._KeyDispatcher(viewer, -1, [], keep=False)
    d.handle("s")
    viewer.cols = pdfless.SIDEBAR_MIN_TERM_COLS - 1  # the terminal was narrowed
    d.handle("n")  # a page key again, not a pick
    assert not viewer.sidebar.picking and viewer.page == 2
    d.handle("S")  # on but not shown: says why, and turns it off
    assert viewer.sidebar.on is False


def test_turning_a_page_redraws_the_frame_not_the_thumbnails(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)
    render_thumbnails(viewer)
    assert "\x1b]1337;File=" in viewer.sidebar.escapes(full=True)
    viewer.page = 2  # still in view (the first two slots)
    marks = viewer.sidebar.escapes(full=False)
    assert "━ 2 ━" in marks and "\x1b]1337;File=" not in marks


def test_only_so_many_thumbnails_are_kept(sample_pdf, monkeypatch):
    monkeypatch.setattr(pdfless, "SIDEBAR_KEPT_THUMBNAILS", 3)
    viewer = make_viewer(sample_pdf, monkeypatch)
    sidebar = viewer.sidebar
    for page in range(1, 8):
        sidebar._images[(page, 100)] = None
        sidebar._encoded[(page, 100, 50)] = (b"", 1, 1)
    sidebar._forget_old(range(6, 8), 100)
    assert list(sidebar._images) == [(5, 100), (6, 100), (7, 100)]  # the newest, those in view kept
    assert {k[0] for k in sidebar._encoded} == {5, 6, 7}


def test_the_thumbnail_cache_lets_go_of_full_size_images(sample_image, tmp_path):
    cache = pdfless.PageCache(str(tmp_path), pdfless.ImageDocument(sample_image))
    cache.get(1, 50, "width")
    assert cache._native_images
    cache.forget_native(1)
    assert not cache._native_images


def test_s_in_text_mode_says_the_thumbnails_are_in_image_mode(sample_pdf, monkeypatch, capsys):
    viewer = make_viewer(sample_pdf, monkeypatch)
    assert viewer.enter_text_mode()
    capsys.readouterr()
    viewer.handle_global_key("s")
    assert not viewer.sidebar.picking
    assert "in image mode" in capsys.readouterr().out


def test_a_thumbnail_cut_off_at_the_bottom_shows_its_top(sample_pdf, monkeypatch):
    """The rows under the last whole thumbnail used to stay empty; they
    show the top of the next one now, cut off at the status line."""
    viewer = make_viewer(sample_pdf, monkeypatch, rows=34)
    _w, thumb_rows, slot_rows, slots = viewer.sidebar._layout()
    left = (viewer.rows - 1) - slots * slot_rows
    assert left >= 2  # (room for the next one's frame top and a row of it)
    assert list(viewer.sidebar._shown_pages(slot_rows, slots)) == list(range(1, slots + 2))
    render_thumbnails(viewer)
    drawn = viewer.sidebar.escapes(full=True)
    heights = [int(h) for h in re.findall(r"height=(\d+)px", drawn)]
    assert len(heights) == slots + 1
    assert heights[-1] < heights[0]  # the cut-off one: only what fits
    assert heights[-1] <= (left - 1) * viewer.cell_h_px

    viewer.mouse.handle("MOUSE_CLICK", 5, viewer.rows - 1)  # on the cut-off one
    assert viewer.page == slots + 1


def test_no_cut_off_thumbnail_without_room_for_it(sample_pdf, monkeypatch):
    viewer = make_viewer(sample_pdf, monkeypatch)  # 30 rows: one row left over
    _w, _rows, slot_rows, slots = viewer.sidebar._layout()
    assert (viewer.rows - 1) - slots * slot_rows < 2
    assert list(viewer.sidebar._shown_pages(slot_rows, slots)) == list(range(1, slots + 1))
