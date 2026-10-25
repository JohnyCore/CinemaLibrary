from __future__ import annotations

import logging
import tkinter as tk
import webbrowser
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageTk

from utils.image_cache import ImageCache, ImageJob

BG = "#0d0e12"
CARD_BG = "#161a22"
BORDER = "#242a35"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
ACCENT = "#e5484d"

CARD_W = 158
CARD_H = 300
POSTER_W = CARD_W - 2
POSTER_H = 234
GAP_X = 16
GAP_Y = 20
PAD = 20
ROW_H = CARD_H + GAP_Y
COL_STEP = CARD_W + GAP_X
TEXT_WIDTH = CARD_W - 12

# every card is 5 canvas primitives: card rect, poster image, title text,
# meta text, watched badge
PRIMS_PER_CARD = 5

log = logging.getLogger(__name__)


class LibraryView(tk.Frame):
    """Scrollable poster grid drawn directly on the canvas.

    No child widgets are embedded, so fast scrolling cannot tear or ghost.
    Only rows within the viewport (plus a small buffer) have canvas items;
    everything further out is deleted and its pending thumbnail job cancelled.
    """

    def __init__(
        self,
        master,
        images: ImageCache,
        on_open: Callable[[dict], None],
        on_context: Callable[[dict, int, int], None] | None = None,
    ) -> None:
        super().__init__(master, bg=BG)
        self._images = images
        self._on_open = on_open
        self._on_context = on_context
        self._items: list[dict] = []
        self._selected: set[int] = set()
        self._anchor: int | None = None
        self._rendered: dict[tuple[int, int], list[int]] = {}
        self._rendered_items: dict[tuple[int, int], dict] = {}
        self._img_ids: dict[tuple[int, int], int] = {}
        # the picture a card shows: the card holds it, so the Tk image
        # survives the cache that handed it out
        self._photos: dict[tuple[int, int], object] = {}
        self._tokens: dict[tuple[int, int], int] = {}
        self._jobs: dict[tuple[int, int], ImageJob | None] = {}
        self._token_seq = 0
        self._cols = 1
        self._rows = 0
        self._empty_message = "No items match your filters."
        self._empty_button: str | None = None
        self._empty_command = None
        self._layout_job: str | None = None
        self._focus_idx: int | None = None
        self._title_font = tkfont.Font(family="Segoe UI Semibold", size=10)
        self._meta_font = tkfont.Font(family="Segoe UI", size=8)
        self._fit_cache: dict[str, str] = {}

        self._profile_panel = tk.Frame(
            self, bg="#12161d", width=360, highlightthickness=1,
            highlightbackground=BORDER,
        )
        self._profile_panel.pack_propagate(False)
        profile_content = tk.Frame(self._profile_panel, bg="#12161d", padx=16, pady=16)
        profile_content.pack(fill="both", expand=True)
        self._profile_name = tk.Label(
            profile_content, bg="#12161d", fg=TEXT,
            font=("Segoe UI Semibold", 15), justify="left", anchor="w",
            wraplength=310,
        )
        self._profile_name.pack(fill="x", pady=(0, 4))
        self._profile_roles = tk.Label(
            profile_content, bg="#12161d", fg=MUTED,
            font=("Segoe UI", 9), anchor="w",
        )
        self._profile_roles.pack(fill="x", pady=(0, 10))
        self._profile_photo = tk.Label(profile_content, bg="#12161d")
        self._profile_photo.pack(anchor="w", pady=(0, 10))
        profile_text_frame = tk.Frame(profile_content, bg="#12161d")
        profile_text_frame.pack(fill="both", expand=True)
        self._profile_text = tk.Text(
            profile_text_frame, bg="#12161d", fg=TEXT, font=("Segoe UI", 10),
            wrap="word", relief="flat", height=12, width=38,
            padx=0, pady=0, highlightthickness=0,
        )
        profile_text_scroll = ttk.Scrollbar(
            profile_text_frame, orient="vertical", command=self._profile_text.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self._profile_text.configure(yscrollcommand=profile_text_scroll.set)
        self._profile_text.pack(side="left", fill="both", expand=True)
        profile_text_scroll.pack(side="right", fill="y")
        self._profile_status = tk.Label(
            profile_content, bg="#12161d", fg=MUTED, font=("Segoe UI", 9),
            justify="left", anchor="w", wraplength=320,
        )
        self._profile_status.pack(fill="x", pady=(8, 0))
        profile_actions = tk.Frame(profile_content, bg="#12161d")
        profile_actions.pack(fill="x", pady=(12, 0))
        self._profile_wiki = tk.Button(
            profile_actions, text="Open Wikipedia", command=self._open_profile_wiki,
            bg="#1a2130", fg=TEXT, activebackground="#243046",
            activeforeground=TEXT, bd=0, padx=10, pady=6, cursor="hand2",
        )
        self._profile_wiki.pack(side="left")
        tk.Button(
            profile_actions, text="Close", command=self.hide_person_profile,
            bg="#1a2130", fg=TEXT, activebackground="#243046",
            activeforeground=TEXT, bd=0, padx=10, pady=6, cursor="hand2",
        ).pack(side="right")
        self._profile_url: str | None = None
        self._profile_image = None

        self._main_area = tk.Frame(self, bg=BG)
        self._main_area.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(
            self._main_area, bg=BG, highlightthickness=0,
            yscrollincrement=1, takefocus=True,
        )
        self.vsb = ttk.Scrollbar(self._main_area, orient="vertical", command=self._on_vsb,
                                 style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Key>", self._on_key)
        self.canvas.bind("<Control-a>", self._on_select_all)
        # the wheel handler is global, but it is only this view's: it is
        # removed again when the view goes away
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.bind("<Destroy>", self._unbind_wheel)

        # the empty state sits over the canvas: a message and a button
        self._empty_frame = tk.Frame(self._main_area, bg=BG)
        self._empty_label = tk.Label(self._empty_frame, text="", bg=BG, fg=MUTED,
                                     font=("Segoe UI", 12), justify="center")
        self._empty_label.pack(pady=(0, 12))
        self._empty_btn = tk.Button(
            self._empty_frame, text="", command=lambda: None,
            bg="#1a2130", fg=TEXT, activebackground="#243046",
            activeforeground=TEXT, bd=0, padx=16, pady=6,
            font=("Segoe UI", 10), cursor="hand2")

    def _unbind_wheel(self, event=None) -> None:
        """Drop the global wheel binding when the view goes away."""
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:
            pass

    # --- public API ---------------------------------------------------

    def show_items(self, items: list[dict], reset_scroll: bool = False) -> None:
        """Show a new list of items, re-drawing only the cards that changed.

        The scroll position and the keyboard focus are kept unless the
        result set changed (a new search, a different section) - marking an
        item as watched must not throw the user back to the first page.
        """
        previous = None
        if self._focus_idx is not None and 0 <= self._focus_idx < len(self._items):
            previous = self._items[self._focus_idx]
        self._items = items
        self._focus_idx = None
        # the items that are still there stay marked
        self._selected &= {i.get("id") for i in items}
        if previous is not None:
            for i, item in enumerate(items):
                if item is previous:
                    self._focus_idx = i
                    break
        if reset_scroll:
            self.canvas.yview_moveto(0.0)
        self._relayout()
        self._update_empty()

    def focus_item(self, index: int) -> None:
        self._set_focus(index)

    def show_person_profile(
        self,
        name: str,
        role: str,
        profile: dict | None = None,
        status: str = "",
    ) -> None:
        """Show a cached or loading person profile beside the library grid."""
        self._profile_name.configure(text=name)
        self._profile_roles.configure(text=role.capitalize())
        self._profile_status.configure(text=status)
        self._profile_image = None
        self._profile_photo.configure(image="", width=0, height=0)
        self._profile_text.configure(state="normal")
        self._profile_text.delete("1.0", "end")
        self._profile_url = None
        self._profile_wiki.configure(state="disabled")

        if profile is not None:
            self._profile_url = profile.get("url")
            if self._profile_url:
                self._profile_wiki.configure(state="normal")
            if profile.get("not_found"):
                self._profile_text.insert(
                    "1.0", "No matching Wikipedia biography was found.")
                self._profile_status.configure(
                    text="This result is cached. The library can still be filtered by this name.")
            else:
                self._profile_text.insert("1.0", profile.get("extract") or "")
                title = profile.get("name") or name
                self._profile_status.configure(text=f"Wikipedia · {title}")
                image_path = profile.get("image_path")
                if image_path:
                    try:
                        with Image.open(image_path) as source:
                            image = source.convert("RGB")
                        image.thumbnail((320, 240),
                                        getattr(Image, "Resampling", Image).LANCZOS)
                        self._profile_image = ImageTk.PhotoImage(image)
                        self._profile_photo.configure(
                            image=self._profile_image,
                            width=self._profile_image.width(),
                            height=self._profile_image.height(),
                        )
                    except (OSError, ValueError) as exc:
                        self._profile_status.configure(
                            text=f"Biography is cached; photo unavailable: {exc}")
        elif not status:
            self._profile_text.insert("1.0", "No biography is available.")
        self._profile_text.configure(state="disabled")
        if not self._profile_panel.winfo_ismapped():
            self._profile_panel.pack(
                side="right", fill="y", before=self._main_area,
            )

    def hide_person_profile(self) -> None:
        self._profile_panel.pack_forget()
        self._profile_image = None

    def _open_profile_wiki(self) -> None:
        if self._profile_url:
            try:
                webbrowser.open(self._profile_url)
            except Exception:
                log.exception("could not open person profile in browser")

    # --- the selection -------------------------------------------------

    def selected_items(self) -> list[dict]:
        """The items that are marked."""
        return [i for i in self._items if i.get("id") in self._selected]

    def clear_selection(self) -> None:
        """Unmark everything."""
        if not self._selected:
            return
        self._selected.clear()
        self._paint_selection()

    def select_all(self) -> None:
        """Mark every item of the view."""
        self._selected = {i.get("id") for i in self._items}
        self._paint_selection()

    def _toggle(self, item: dict) -> None:
        """Mark an item, or unmark it."""
        media_id = item.get("id")
        if media_id in self._selected:
            self._selected.discard(media_id)
        else:
            self._selected.add(media_id)

    def _extend(self, index: int) -> None:
        """Mark everything between the last click and this one."""
        if self._anchor is None:
            self._anchor = index
        lo, hi = sorted((self._anchor, index))
        self._selected = {self._items[i].get("id") for i in range(lo, hi + 1)}

    def _outline_for(self, pos: tuple[int, int]) -> str:
        """The border of a card: marked items keep the accent colour."""
        item = self._rendered_items.get(pos)
        focused = (self._focus_idx is not None
                   and pos == self._pos_of(self._focus_idx))
        selected = item is not None and item.get("id") in self._selected
        return ACCENT if (focused or selected) else BORDER

    def _paint_selection(self) -> None:
        """Show which cards are marked."""
        for pos in list(self._rendered):
            ids = self._rendered.get(pos)
            if not ids:
                continue
            try:
                self.canvas.itemconfig(ids[0], outline=self._outline_for(pos))
            except tk.TclError:
                pass

    # --- layout -------------------------------------------------------

    def _on_configure(self, event=None) -> None:
        if self._layout_job is not None:
            try:
                self.after_cancel(self._layout_job)
            except Exception:
                pass
        self._layout_job = self.after(120, self._relayout)

    def _relayout(self) -> None:
        self._layout_job = None
        if not self.winfo_exists():
            return
        width = self.canvas.winfo_width()
        if width < 50:
            return
        cols = max(1, (width - 2 * PAD + GAP_X) // COL_STEP)
        if cols != self._cols:
            self._cols = cols
            self._delete_all_cells()
        self._rows = max(1, (len(self._items) + cols - 1) // cols)
        self.canvas.configure(scrollregion=(0, 0, 1, PAD + self._rows * ROW_H + PAD))
        self.canvas.configure(cursor="hand2" if self._items else "")
        self._update_empty()
        self._sync_cells()

    def _update_empty(self) -> None:
        """Show the message (and the button) when the grid holds nothing."""
        if self._items:
            self._empty_frame.place_forget()
            return
        self._empty_label.configure(text=self._empty_message)
        if self._empty_button and self._empty_command:
            self._empty_btn.configure(text=self._empty_button,
                                      command=self._empty_command)
            self._empty_btn.pack(anchor="e")
        else:
            self._empty_btn.pack_forget()
        self._empty_frame.place(relx=0.5, rely=0.45, anchor="center")

    def set_empty(self, message: str, button: str | None = None,
                  command=None) -> None:
        """What the grid shows when it is empty."""
        self._empty_message = message
        self._empty_button = button
        self._empty_command = command
        # the cards of the view before have to go at once - the layout
        # would keep them for another moment
        self._delete_all_cells()
        self._update_empty()

    # --- scrolling ----------------------------------------------------

    def _on_vsb(self, *args) -> None:
        self.canvas.yview(*args)
        self.after_idle(self._sync_cells)

    def _on_wheel(self, event) -> None:
        if not self.winfo_ismapped():
            return
        widget = event.widget
        while widget is not None and widget is not self:
            widget = widget.master
        if widget is not self:
            return
        px = int(round(-event.delta * 0.4))
        if px == 0 and event.delta:
            px = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(px, "units")
        self.after_idle(self._sync_cells)

    # --- mouse --------------------------------------------------------

    def _item_at(self, event) -> dict | None:
        for item in self.canvas.find_withtag("current"):
            for tag in self.canvas.gettags(item):
                if tag.startswith("c:"):
                    idx = int(tag[2:])
                    if 0 <= idx < len(self._items):
                        return self._items[idx]
        return None

    def _on_click(self, event) -> None:
        self.canvas.focus_set()
        item = self._item_at(event)
        if item is None:
            return
        index = self._items.index(item)
        ctrl = bool(event.state & 0x4)
        shift = bool(event.state & 0x1)
        if ctrl:
            # mark one item, or unmark it
            self._toggle(item)
            self._anchor = index
            self._set_focus(index)
            self._paint_selection()
            return
        if shift:
            # mark everything between the last click and this one
            self._extend(index)
            self._set_focus(index)
            self._paint_selection()
            return
        self.clear_selection()
        self._set_focus(index)
        self._on_open(item)

    def _on_select_all(self, event=None) -> None:
        """Ctrl+A marks everything the view holds."""
        self.select_all()
        return "break"

    def _on_right_click(self, event) -> None:
        item = self._item_at(event)
        if item is None or self._on_context is None:
            return
        if item.get("id") not in self._selected:
            # a right click outside the selection works on that item alone
            self._selected = {item.get("id")}
            self._paint_selection()
        self._set_focus(self._items.index(item))
        self._on_context(item, event.x_root, event.y_root)

    def _on_motion(self, event=None) -> None:
        # the whole grid is clickable, so the pointer is always a hand and no
        # per-motion hit test is needed
        if self.canvas["cursor"] != ("hand2" if self._items else ""):
            self.canvas.configure(cursor="hand2" if self._items else "")

    # --- keyboard -----------------------------------------------------

    def _on_key(self, event) -> None:
        if not self._items:
            return
        keys = event.keysym
        if keys in ("Left", "Right", "Up", "Down"):
            step = -1 if keys == "Left" else 1
            if keys in ("Up", "Down"):
                step = -self._cols if keys == "Up" else self._cols
            current = self._focus_idx if self._focus_idx is not None else 0
            nxt = max(0, min(len(self._items) - 1, current + step))
            self._set_focus(nxt)
            self._scroll_into_view(nxt)
            return
        if keys in ("Return", "KP_Enter", "space"):
            if self._focus_idx is not None and 0 <= self._focus_idx < len(self._items):
                self._on_open(self._items[self._focus_idx])

    def _pos_of(self, index: int) -> tuple[int, int]:
        return divmod(index, self._cols)

    def _set_focus(self, index: int | None) -> None:
        old = self._focus_idx
        self._focus_idx = index
        for candidate in (old, index):
            if candidate is None:
                continue
            pos = self._pos_of(candidate)
            ids = self._rendered.get(pos)
            if not ids:
                continue
            self.canvas.itemconfig(ids[0], outline=self._outline_for(pos))

    def _scroll_into_view(self, index: int) -> None:
        row = index // self._cols
        top = self.canvas.canvasy(0)
        height = self.canvas.winfo_height() or 600
        y0 = PAD + row * ROW_H
        y1 = y0 + CARD_H
        if y0 < top:
            self.canvas.yview_moveto(max(0.0, (y0 - PAD) / (PAD + self._rows * ROW_H + PAD)))
        elif y1 > top + height:
            self.canvas.yview_moveto(
                max(0.0, (y1 - height) / (PAD + self._rows * ROW_H + PAD))
            )
        self.after_idle(self._sync_cells)

    # --- drawing ------------------------------------------------------

    def _fit_title(self, title: str) -> str:
        """Shorten a title to two lines, with as few font measurements as possible.

        Measuring the string once per removed character is a Tk round trip
        per character, and every scroll redraw pays it again - a binary
        search needs about six. The result is kept: the same title comes
        back on every redraw.
        """
        cached = self._fit_cache.get(title)
        if cached is not None:
            return cached
        limit = 2 * TEXT_WIDTH
        if self._title_font.measure(title) <= limit:
            self._remember_fit(title, title)
            return title
        low, high = 0, len(title)
        while low < high:
            mid = (low + high + 1) // 2
            if self._title_font.measure(title[:mid] + "…") <= limit:
                low = mid
            else:
                high = mid - 1
        cut = title[:low].rstrip()
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        result = (cut or title[:low]) + "…"
        self._remember_fit(title, result)
        return result

    def _remember_fit(self, title: str, result: str) -> None:
        if len(self._fit_cache) > 2000:
            self._fit_cache.clear()
        self._fit_cache[title] = result

    def _delete_cell(self, pos: tuple[int, int]) -> None:
        ids = self._rendered.pop(pos, None)
        if ids is None:
            return
        job = self._jobs.pop(pos, None)
        if job is not None:
            self._images.cancel(job)
        self._img_ids.pop(pos, None)
        self._photos.pop(pos, None)
        self._tokens.pop(pos, None)
        self._rendered_items.pop(pos, None)
        for iid in ids:
            self.canvas.delete(iid)

    def _delete_all_cells(self) -> None:
        for pos in list(self._rendered):
            self._delete_cell(pos)

    def _sync_cells(self) -> None:
        if not self._items:
            # the old cards have to go: an empty folder must not show the
            # items of the folder before it
            self._delete_all_cells()
            return
        width = self.canvas.winfo_width()
        if width < 50:
            return
        viewport = self.canvas.winfo_height() or 600
        top = self.canvas.canvasy(0)
        row_first = int(top // ROW_H)
        row_last = int((top + viewport) // ROW_H)
        lo = max(0, row_first - 1)
        hi = min(self._rows, row_last + 2)
        keep: set[tuple[int, int]] = set()
        for r in range(lo, hi):
            for c in range(self._cols):
                idx = r * self._cols + c
                if idx >= len(self._items):
                    break
                pos = (r, c)
                keep.add(pos)
                item = self._items[idx]
                if pos in self._rendered and self._rendered_items.get(pos) is item:
                    continue
                self._delete_cell(pos)
                priority = 0 if row_first <= r <= row_last else 1
                self._draw(pos, item, priority)
        for pos in [p for p in self._rendered if p not in keep]:
            self._delete_cell(pos)

    def _draw(self, pos: tuple[int, int], item: dict, priority: int) -> None:
        r, c = pos
        x0 = PAD + c * COL_STEP
        y0 = PAD + r * ROW_H
        x1 = x0 + CARD_W
        y1 = y0 + CARD_H
        idx = r * self._cols + c
        tag = f"c:{idx}"
        focused = self._focus_idx == idx
        selected = item.get("id") in self._selected
        ids = [
            self.canvas.create_rectangle(x0, y0, x1, y1, fill=CARD_BG,
                                         outline=ACCENT if (focused or selected)
                                         else BORDER,
                                         tags=tag),
        ]
        iid = self.canvas.create_image(x0 + 1, y0 + 1, anchor="nw", tags=tag)
        ids.append(iid)
        self._token_seq += 1
        token = self._token_seq
        self._img_ids[pos] = iid
        self._tokens[pos] = token
        title = self._fit_title(item.get("title") or "Untitled")
        ids.append(self.canvas.create_text(
            (x0 + x1) // 2, y0 + POSTER_H + 7, text=title, anchor="n",
            width=TEXT_WIDTH, justify="center", fill=TEXT, font=self._title_font,
            tags=tag,
        ))
        year = item.get("year")
        kind = "Series" if item.get("media_type") == "series" else "Movie"
        parts = ([str(year), kind] if year else [kind])
        rating = item.get("my_rating")
        if rating:
            parts.append(f"★{rating}")
        meta = "  ·  ".join(parts)
        ids.append(self.canvas.create_text(
            (x0 + x1) // 2, y1 - 9, text=meta, anchor="s", fill=MUTED,
            font=self._meta_font, tags=tag,
        ))
        ids.append(self.canvas.create_text(
            x1 - 13, y0 + 13, text="✓" if item.get("seen") else "",
            anchor="ne", fill=ACCENT, font=("Segoe UI", 11, "bold"), tags=tag,
        ))
        self._rendered[pos] = ids
        self._rendered_items[pos] = item
        self._request_image(pos, item, token, priority)

    def _request_image(self, pos: tuple[int, int], item: dict,
                       token: int, priority: int) -> None:
        src = item.get("poster_path")

        def deliver(image, pos=pos, token=token) -> None:
            self._apply_image(pos, token, image)

        # the cache hands out the "No poster" artwork until the real
        # picture arrives - and keeps it when there is no poster at all
        self._jobs[pos] = self._images.get_thumbnail(
            src, (POSTER_W, POSTER_H), deliver, priority=priority
        )

    def _apply_image(self, pos: tuple[int, int], token: int, image) -> None:
        if image is None or token != self._tokens.get(pos):
            return
        iid = self._img_ids.get(pos)
        if iid is None:
            return
        # the card keeps the picture: the Tk image must not die with the
        # cache entry it came from
        self._photos[pos] = image
        try:
            self.canvas.itemconfig(iid, image=image)
        except tk.TclError:
            pass
