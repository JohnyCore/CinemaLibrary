from __future__ import annotations

import logging
import threading
import webbrowser
import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont
from tkinter import ttk
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from database.db_manager import Database
from metadata import metadata_service
from utils import media_ops
from utils.image_cache import ImageCache, ImageJob
from utils.image_utils import find_artwork
from ui.window_utils import ScrollArea, setup_popup
from ui.icons import font as icon_font, glyph as icon_glyph, photo as icon_photo
from ui.dialogs import ImdbIdDialog

log = logging.getLogger(__name__)

BG = "#0b0d10"
PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
ACCENT = "#e5484d"
LINK = "#6aa4ff"
POSTER_BG = "#0f1216"
PANEL_TINT = "#121a27"
PANEL_TINT_STRENGTH = 0.68

TREE_STYLE = "Dark.Treeview"

BTN = dict(
    bg="#1a2130", fg=TEXT, activebackground="#243046", activeforeground=TEXT,
    disabledforeground="#5a6472", bd=0, padx=14, pady=6,
    font=("Segoe UI", 10), cursor="hand2",
)
BTN_SMALL = dict(
    bg="#1a2130", fg=TEXT, activebackground="#243046", activeforeground=TEXT,
    disabledforeground="#5a6472", bd=0, padx=12, pady=5,
    font=("Segoe UI", 9), cursor="hand2",
)


class DetailsView(tk.Frame):
    def __init__(self, master, images: ImageCache, on_back, db: Database,
                  on_fetched=None, on_filter=None,
                  post: Callable[[Callable[[], None]], None] | None = None,
                  on_person=None) -> None:
        super().__init__(master, bg=BG)
        self._images = images
        self._on_back = on_back
        self._db = db
        self._on_fetched = on_fetched
        self._on_filter = on_filter
        self._on_person = on_person
        self._post = post or (lambda fn: self.after(0, fn))
        self._item: dict | None = None
        self._bg_job: ImageJob | None = None
        self._poster_job: ImageJob | None = None
        self._bg_id: int | None = None
        self._bg_image = None
        self._poster_id: int | None = None
        self._poster_img_id: int | None = None
        self._resize_job: str | None = None
        self._last_geo: tuple[int, int] = (0, 0)
        self._fetch_btn: tk.Button | None = None
        self._fetch_gen: int = 0
        self._episodes: list[dict] = []
        # state of the episode browser, kept across a rebuild of the panel
        self._ep_open: dict[str, bool] = {}
        self._ep_tree_state: dict[str, tuple] = {}
        self._ep_body: tk.Frame | None = None
        self._bg_attempts: int = 0
        self._panel: dict[str, tk.Widget] = {}
        self._panel_item_id: int | None = None
        self._panel_signature: tuple | None = None

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)

        self.back_btn = tk.Button(
            self.canvas, text="Back", image=icon_photo("back", size=14, color=TEXT),
            compound="left", command=self._back,
            bg=PANEL_BG, fg=TEXT, activebackground="#1c2230", activeforeground=TEXT,
            bd=0, padx=14, pady=5, font=("Segoe UI", 10), cursor="hand2",
        )
        self.back_btn.place(relx=0.012, rely=0.03, anchor="nw")

        self.info = tk.Frame(self.canvas, bg=PANEL_BG, highlightthickness=1,
                             highlightbackground=BORDER)
        self.info.place(relx=1.0, x=-24, rely=0.06, relwidth=0.52, anchor="ne",
                        relheight=0.88)
        self.info.grid_columnconfigure(0, weight=1)
        self.info.grid_rowconfigure(0, weight=1)
        self._panel_backdrop = tk.Canvas(
            self.info, bg=PANEL_BG, highlightthickness=0, bd=0,
        )
        self._panel_backdrop.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._panel_backdrop_image = None
        # The panel has three rows: the top part scrolls (a series keeps its
        # plot, facts and episode browser in that one scroll), the buttons
        # and the location are pinned. Nothing below the scroll can clip or
        # push the meta out of view.
        self._upper = ScrollArea(self.info, PANEL_BG, padding=(16, 12))
        self._upper.grid(row=0, column=0, sticky="nsew")
        self._actions_frame = tk.Frame(self.info, bg=PANEL_BG, padx=16)
        self._actions_frame.grid(row=1, column=0, sticky="ew")
        self._loc_frame = tk.Frame(self.info, bg=PANEL_BG, padx=16, pady=8)
        self._loc_frame.grid(row=2, column=0, sticky="ew")
        self._link_font = tkfont.Font(family="Segoe UI", size=9)
        # the width of a name is measured once - a Tk round trip per name
        self._name_widths: dict[str, int] = {}

    def _measure(self, text: str) -> int:
        """The width of a name in the panel - measured once per name."""
        width = self._name_widths.get(text)
        if width is None:
            width = self._link_font.measure(text)
            if len(self._name_widths) > 2000:
                self._name_widths.clear()
            self._name_widths[text] = width
        return width

    # --- main view ----------------------------------------------------

    @staticmethod
    def _signature(item: dict) -> tuple:
        """The facts the panel is built from.

        A change means the panel has to be built again - a new poster, a
        new plot, new names. Marking an item watched or rating it does
        not change the signature, so those stay in place.
        """
        return (item.get("poster_path"), item.get("poster2_path"),
                item.get("plot"), item.get("director"), item.get("writer"),
                item.get("cast_list"), item.get("country"), item.get("genres"),
                item.get("imdb_id"), item.get("rating"), item.get("runtime"),
                item.get("year"), item.get("media_type"),
                item.get("collection_name"))

    def show(self, item: dict, episode_rows: list[dict] | None = None) -> None:
        """Show an item.

        Re-showing the *same* item - after a fetch, a watched toggle - only
        updates the values in place. A different item rebuilds the panel.
        """
        same_item = self._panel_item_id == item.get("id")
        if (same_item and self._panel
                and self._panel_signature == self._signature(item)):
            self._item = item
            self._update_panel(item)
            return

        self._item = item
        self._poster_id = None
        self._cancel_jobs()
        if same_item:
            # the same item with new facts: only the upper part is built
            # again - the buttons and the episode list stay where they are
            for child in self._upper.inner.winfo_children():
                child.destroy()
            keep = {k: self._panel[k] for k in ("watched", "ep_fill")
                    if k in self._panel}
            self._panel = keep
        else:
            # after the clear: it wipes the id of the panel it just destroyed
            self._clear_info()
        self._panel_item_id = item.get("id")
        self._panel_signature = self._signature(item)
        self._load_backdrop()
        try:
            self._build_panel(item, episode_rows, upper_only=same_item)
            if same_item:
                # the buttons and the episode list stayed - bring their
                # values in line with the new facts
                self._update_panel(item)
            self._upper.update_scrollregion()
            if not same_item:
                self._upper.scroll_to_top()
        except Exception:
            # a half-built panel is worse than an honest error message
            log.exception("could not build the details panel for %s", item.get("title"))
            self._panel = {}
            self._panel_item_id = None
            self._panel_signature = None
            tk.Label(self._actions_frame, text="This item could not be shown.",
                     bg=PANEL_BG, fg=ACCENT, font=("Segoe UI", 10)).pack(anchor="w")

    def _build_panel(self, item: dict, episode_rows: list[dict] | None,
                     upper_only: bool = False) -> None:
        inner = self._upper.inner

        top = tk.Frame(inner, bg=PANEL_BG)
        top.pack(fill="x")

        poster = tk.Canvas(top, width=148, height=222, bg=POSTER_BG,
                           highlightthickness=0, cursor="hand2")
        poster.pack(side="left", padx=(0, 16))
        poster.bind("<Button-1>", lambda _e: self._enlarge_poster())
        self._poster_v = poster
        self._panel["poster_canvas"] = poster
        self._load_poster(poster)

        head = tk.Frame(top, bg=PANEL_BG)
        head.pack(side="left", fill="both", expand=True)
        title_label = tk.Label(
            head, text=item.get("title") or "Untitled", bg=PANEL_BG, fg=TEXT,
            font=("Segoe UI Semibold", 20), wraplength=320, justify="left")
        title_label.pack(anchor="w")
        self._panel["title"] = title_label
        meta_label = tk.Frame(head, bg=PANEL_BG)
        self._fill_meta_line(meta_label, item)
        self._panel["meta"] = meta_label
        genres_bar = tk.Frame(head, bg=PANEL_BG)
        self._panel["genres"] = genres_bar
        self._fill_genres(genres_bar, item)
        self._credits(head, item)
        folder = item.get("collection_name")
        if folder:
            tk.Label(head, text=f"Folder:  {folder}", bg=PANEL_BG, fg=MUTED,
                     font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))
        is_series = item.get("media_type") == "series"
        # one panel for both: the facts live in the scroll flow under the
        # header - a series-like cramped column next to the poster clips its
        # rows beyond the scroll edge, so nothing is pinned beside it any more
        overview = tk.Frame(inner, bg=PANEL_BG)
        tk.Label(
            overview, text="OVERVIEW", bg=PANEL_BG, fg=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w", pady=(12, 5))
        tk.Frame(overview, height=1, bg=BORDER).pack(fill="x")
        self._stacked(overview, item)
        self._facts(overview, item)
        overview.pack(fill="x")
        if is_series:
            # the whole body shares the one scroll: the meta is the first
            # thing the page shows, the episodes follow under it
            tk.Frame(inner, height=1, bg=BORDER).pack(fill="x", pady=(12, 0))
            ep_body = tk.Frame(inner, bg=POSTER_BG, padx=16, pady=6)
            ep_body.pack(fill="x")
            self._ep_body = ep_body
            ep_body.grid_columnconfigure(0, weight=1)
            self._episode_browser(ep_body, item,
                                  episode_rows if episode_rows is not None
                                  else self._db.episodes(item["id"]))
        if upper_only:
            return
        self._actions(self._actions_frame, item)
        self._location_row(self._loc_frame, item)

    def _fill_genres(self, bar: tk.Frame, item: dict) -> None:
        """Fill the genre links; the bar is packed after its children."""
        for child in bar.winfo_children():
            child.destroy()
        genres = self._as_list(item.get("genres"))
        if not genres:
            bar.pack_forget()
            return
        for idx, g in enumerate(genres):
            self._link(
                bar, str(g),
                lambda g=str(g): self._dispatch_filter("genre", g),
            ).pack(side="left", padx=(0, 2))
            if idx < len(genres) - 1:
                tk.Label(bar, text=",  ", bg=PANEL_BG, fg=MUTED,
                         font=("Segoe UI", 9)).pack(side="left")
        # pack parent after children: wraplength buttons realize their
        # size late and a pre-packed parent freezes at the first row width
        bar.pack(anchor="w", pady=(4, 0))

    def _update_panel(self, item: dict) -> None:
        """Update the shown values of the current item in place."""
        panel = self._panel
        label = panel.get("title")
        if label is not None:
            label.configure(text=item.get("title") or "Untitled")
        label = panel.get("meta")
        if label is not None:
            self._fill_meta_line(label, item)
        label = panel.get("plot")
        if label is not None:
            text = str(item.get("plot") or "").strip()
            if text:
                label.configure(text=text)
                label.pack(anchor="w", pady=(12, 0))
            else:
                label.pack_forget()
        bar = panel.get("genres")
        if bar is not None:
            self._fill_genres(bar, item)
        if panel.get("stars"):
            self._update_stars(item.get("my_rating"))
        truth = panel.get("truestory")
        if truth is not None:
            if item.get("true_story"):
                truth.pack(anchor="w", pady=(panel.get("pady", 9), 0))
            else:
                truth.pack_forget()
        button = panel.get("watched")
        if button is not None:
            button.configure(
                text="Mark as Unwatched" if item.get("watched") else "Mark as Watched")
        fill = panel.get("ep_fill")
        if fill is not None:
            fill()

    def _cancel_jobs(self) -> None:
        for attr in ("_poster_job", "_bg_job"):
            job = getattr(self, attr)
            if job is not None:
                self._images.cancel(job)
                setattr(self, attr, None)

    def _meta_line(self, item: dict) -> list[tuple[str, str | None]]:
        parts = []
        if item.get("year"):
            parts.append((str(item["year"]), None))
        parts.append(("Series" if item.get("media_type") == "series" else "Movie", None))
        runtime = item.get("runtime")
        if runtime:
            parts.append((
                f"{runtime // 60}h {runtime % 60:02d}m"
                if runtime >= 60 else f"{runtime}m",
                None,
            ))
        if item.get("rating") is not None:
            parts.append((f"{item['rating']:.1f}", "star"))
        if item.get("media_type") == "series" and item.get("episodes_total"):
            parts.append((
                f"{item.get('episodes_watched', 0)}/{item['episodes_total']} watched",
                None,
            ))
        return parts

    def _fill_meta_line(self, row: tk.Frame, item: dict) -> None:
        for child in row.winfo_children():
            child.destroy()
        parts = self._meta_line(item)
        if not parts:
            row.pack_forget()
            return
        for index, (text, icon) in enumerate(parts):
            if index:
                tk.Label(row, text="·", bg=PANEL_BG, fg=MUTED,
                         font=("Segoe UI", 10)).pack(side="left", padx=6)
            if icon:
                self._icon_canvas(row, icon, 9, "#e0a33e").pack(
                    side="left", padx=(0, 3),
                )
            imdb_id = str(item.get("imdb_id") or "")
            if icon == "star" and imdb_id.startswith("tt"):
                self._link(
                    row, text, lambda value=imdb_id: self._open_imdb(value),
                ).pack(side="left")
            else:
                tk.Label(row, text=text, bg=PANEL_BG, fg=MUTED,
                         font=("Segoe UI", 10)).pack(side="left")
        row.pack(anchor="w", pady=(4, 0))

    def _stacked(self, inner: tk.Frame, item: dict) -> None:
        """The plot, in the full width of the panel."""
        plot = tk.Label(inner, text=str(item.get("plot") or "").strip(), bg=PANEL_BG,
                        fg=TEXT, font=("Segoe UI", 9), wraplength=460, justify="left")
        if item.get("plot"):
            plot.pack(anchor="w", pady=(12, 0))
        self._panel["plot"] = plot

    def _panel_width(self) -> int:
        width = self.info.winfo_width()
        return width if width > 50 else 549

    def _full_width(self) -> int:
        """The width of the whole panel."""
        return max(240, self._panel_width() - 32)

    def _facts(self, parent: tk.Frame, item: dict) -> None:
        """The personal rating and release date."""
        pady = 9
        # the rows are packed again when the panel is updated - they have to
        # keep the margin they were built with, or the panel jumps
        self._panel["pady"] = pady
        truth = tk.Label(parent, text="Based on a true story", bg=PANEL_BG,
                         fg=ACCENT, font=("Segoe UI", 9, "bold"))
        truth.pack(anchor="w", pady=(pady, 0))
        self._panel["truestory"] = truth
        if not item.get("true_story"):
            truth.pack_forget()

        release = self._fmt_date(item.get("release_date"))
        if release:
            self._fact_row(parent, "Released", release, pady)

    def _credits(self, parent: tk.Frame, item: dict) -> None:
        """Show director, writer, cast and country beneath the genre links."""
        width = max(180, self._full_width() - 164)
        rows: list[tuple[str, list[str], str, bool]] = []
        if item.get("director"):
            label = "Created by" if item.get("media_type") == "series" else "Director"
            rows.append((label, self._split_names(item["director"]), "director", False))
        if item.get("writer"):
            rows.append(("Writer", self._split_names(item["writer"]), "writer", False))
        cast_list = self._as_list(item.get("cast_list"))
        if cast_list:
            shown = cast_list[:8]
            rows.append(("Cast", shown, "cast", len(cast_list) > 8))
        if item.get("country"):
            rows.append(("Country", self._split_names(item["country"]), "country", False))
        for title, names, kind, truncated in rows:
            self._names_row(parent, title, names, kind, truncated,
                            width=width, pady=4)
        self._rating_row(parent, item, pady=4)

    @staticmethod
    def _fmt_date(value) -> str:
        """'2015-06-24' becomes '24 Jun 2015'; anything else stays as it is."""
        text = str(value or "").strip()
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().strftime("%d %b %Y")
        except ValueError:
            return text

    def _fact_row(self, parent: tk.Frame, title: str, text: str, pady: int) -> None:
        """'Awards: Won 4 Oscars' - a plain row for the fetched facts."""
        row = tk.Frame(parent, bg=PANEL_BG)
        tk.Label(row, text=f"{title}:", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        tk.Label(row, text=text, bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI", 9), wraplength=400,
                 justify="left").pack(side="left", anchor="w")
        row.pack(anchor="w", pady=(pady, 0))

    def _rating_row(self, parent: tk.Frame, item: dict, pady: int = 9) -> None:
        """The five stars of the personal rating."""
        row = tk.Frame(parent, bg=PANEL_BG)
        tk.Label(row, text="Your Rating:", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        star_font = icon_font(12)
        star_glyph = icon_glyph("star")
        star_width = star_font.measure(star_glyph) + 4
        star_height = star_font.metrics("linespace") + 2
        stars_canvas = tk.Canvas(
            row, width=5 * star_width, height=star_height, bg=PANEL_BG,
            highlightthickness=0, bd=0, cursor="hand2", takefocus=True,
        )
        stars: list[tuple[tk.Canvas, int]] = []
        for number in range(1, 6):
            x = (number - 1) * star_width + star_width // 2
            star_id = stars_canvas.create_text(
                x, star_height // 2, text=star_glyph, font=star_font,
                fill="#3a4150", tags=("rating-star", f"rating-{number}"),
            )
            stars.append((stars_canvas, star_id))
            stars_canvas.tag_bind(
                star_id, "<Button-1>",
                lambda _event, n=number: (stars_canvas.focus_set(), self._rate(n)),
            )
            stars_canvas.tag_bind(
                star_id, "<Enter>", lambda _event, n=number: self._hover_stars(n),
            )
            stars_canvas.tag_bind(
                star_id, "<Leave>",
                lambda _event: self._update_stars(
                    (self._item or {}).get("my_rating"),
                ),
            )
        for key, rating in (("1", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5)):
            stars_canvas.bind(
                key, lambda _event, n=rating: (self._rate(n), "break")[1],
            )
        stars_canvas.pack(side="left")
        tk.Label(row, text="  (click again to clear)", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 8)).pack(side="left")
        row.pack(anchor="w", pady=(pady, 0))
        self._panel["stars"] = stars
        self._update_stars(item.get("my_rating"))

    def _update_stars(self, rating) -> None:
        """Fill the stars of the personal rating."""
        stars = self._panel.get("stars") or []
        value = int(rating) if rating else 0
        for index, (canvas, star_id) in enumerate(stars):
            canvas.itemconfigure(
                star_id, fill="#e0a33e" if index < value else "#3a4150",
            )

    def _hover_stars(self, rating: int) -> None:
        for index, (canvas, star_id) in enumerate(self._panel.get("stars") or []):
            canvas.itemconfigure(
                star_id, fill="#f2c069" if index < rating else "#3a4150",
            )

    @staticmethod
    def _icon_canvas(
        parent: tk.Misc, icon: str, size: int, color: str,
    ) -> tk.Canvas:
        icon_font_value = icon_font(size)
        glyph = icon_glyph(icon)
        width = icon_font_value.measure(glyph) + 2
        height = icon_font_value.metrics("linespace") + 2
        canvas = tk.Canvas(
            parent, width=width, height=height, bg=PANEL_BG,
            highlightthickness=0, bd=0,
        )
        canvas.create_text(
            width // 2, height // 2, text=glyph,
            font=icon_font_value, fill=color,
        )
        return canvas

    def _rate(self, rating: int) -> None:
        """Rate the shown item - the same star twice clears the rating."""
        item = self._item
        if not item:
            return
        current = item.get("my_rating")
        value = None if current == rating else rating
        self._db.set_my_rating(item["id"], value)
        item["my_rating"] = value
        self._update_stars(value)
        self._notify_fetched(item["id"])

    def _location_row(self, parent: tk.Frame, item: dict) -> None:
        """The path of the item - on its own line at the bottom."""
        location = self._location(item)
        if not location:
            return
        tk.Label(parent, text=location, bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 8), wraplength=self._full_width(),
                 justify="left").pack(anchor="w")

    def _names_row(self, parent: tk.Frame, title: str, names: list[str],
                   kind: str, truncated: bool = False, width: int = 320,
                   pady: int = 9) -> None:
        """'Writer: Bridget Carpenter' - the names wrap when they do not fit."""
        entries = [(str(n), True) for n in (names or ())]
        if truncated:
            entries.append(("…", False))
        lines: list[list[tuple[str, bool]]] = []
        current: list[tuple[str, bool]] = []
        used = 0
        icon_name = {
            "director": "film",
            "writer": "pen",
            "cast": "users",
            "country": "globe",
        }.get(kind)
        header_text = icon_glyph(icon_name) if icon_name else f"{title}:"
        header_width = (
            icon_font(10).measure(header_text)
            if icon_name else self._measure(header_text)
        )
        for text, is_link in entries:
            need = (self._measure(text) + 16) if is_link else 18
            if current and used + need > width:
                lines.append(current)
                current = []
                used = 0
            current.append((text, is_link))
            used += need
        if current:
            lines.append(current)
        container = tk.Frame(parent, bg=PANEL_BG)
        for index, line in enumerate(lines):
            row = tk.Frame(container, bg=PANEL_BG)
            if index == 0:
                tk.Label(
                    row, text=header_text, bg=PANEL_BG, fg=MUTED,
                    font=icon_font(10) if icon_name else ("Segoe UI", 9),
                ).pack(side="left", padx=(0, 4))
            for pos, (text, is_link) in enumerate(line):
                if is_link:
                    self._link(
                        row, text,
                        lambda k=kind, n=text: (
                            self._on_person(n, k)
                            if self._on_person is not None
                            and k in ("cast", "director", "writer")
                            else self._dispatch_filter(k, n)
                        ),
                        wraplength=180,
                    ).pack(side="left", padx=(0, 2))
                else:
                    tk.Label(row, text=text, bg=PANEL_BG, fg=MUTED,
                             font=("Segoe UI", 9)).pack(side="left")
                if pos < len(line) - 1:
                    tk.Label(row, text=",", bg=PANEL_BG, fg=MUTED,
                             font=("Segoe UI", 9)).pack(side="left")
            if index:
                # the wrapped lines line up under the value column, not
                # under the label - the facts read as a grid
                row.pack(anchor="w", padx=(header_width + 4, 0))
            else:
                row.pack(anchor="w")
        container.pack(anchor="w", pady=(pady, 0))
    def _link(self, parent, text: str, command, wraplength: int = 200) -> tk.Label:
        link = tk.Label(
            parent, text=text, bg=PANEL_BG, fg=LINK,
            font=("Segoe UI", 9), cursor="hand2", takefocus=True,
            wraplength=wraplength, justify="left",
        )

        def invoke(_event=None):
            command()
            return "break"

        link.bind("<Button-1>", invoke)
        link.bind("<Return>", invoke)
        link.bind("<space>", invoke)
        return link

    def _open_imdb(self, imdb_id: str) -> None:
        try:
            webbrowser.open(f"https://www.imdb.com/title/{imdb_id}/")
        except Exception:
            log.exception("could not open browser for %s", imdb_id)

    def _dispatch_filter(self, kind: str, value: str) -> None:
        if self._on_filter is not None:
            try:
                self._on_filter(kind, value)
            except Exception:
                log.exception("filter dispatch failed for %s=%s", kind, value)

    @staticmethod
    def _split_names(value) -> list[str]:
        if value is None:
            return []
        text = str(value)
        return [p.strip() for p in text.split(",") if p.strip()]

    # --- actions ------------------------------------------------------

    def _actions(self, inner: tk.Frame, item: dict) -> None:
        tk.Frame(inner, height=1, bg=BORDER).pack(fill="x", pady=(0, 12))
        bar = tk.Frame(inner, bg=PANEL_BG)
        bar.pack(anchor="w", pady=(14, 0))
        if item.get("video_path") and Path(item["video_path"]).is_file():
            tk.Button(bar, text="Play", command=self._play_video, **BTN).pack(
                side="left", padx=(0, 10))
        if item.get("media_type") != "series":
            watched = bool(item.get("watched"))
            button = tk.Button(
                bar, text="Mark as Unwatched" if watched else "Mark as Watched",
                command=self._toggle_watched, **BTN,
            )
            button.pack(side="left", padx=(0, 10))
            self._panel["watched"] = button
        label = self._fetch_label()
        self._fetch_btn = tk.Button(
            bar, text=label, command=self._fetch_metadata, **BTN,
        )
        self._fetch_btn.pack(side="left", padx=(0, 10))
        tk.Button(bar, text="Open Folder", command=self._open_folder, **BTN).pack(side="left")
        self._action_status = tk.Label(inner, bg=PANEL_BG, fg=MUTED,
                                       font=("Segoe UI", 9), anchor="w", justify="left")
        self._action_status.pack(anchor="w", pady=(8, 0))

    def _fetch_label(self) -> str:
        item = self._item or {}
        return "Fetch this series" if item.get("media_type") == "series" else "Fetch this movie"

    def _set_status(self, text: str, ok: bool = False) -> None:
        label = getattr(self, "_action_status", None)
        if label is not None:
            label.configure(text=text, fg="#79c37d" if ok else MUTED)

    def _toggle_watched(self) -> None:
        item = self._item
        if not item:
            return
        self._db.set_media_watched(item["id"], not bool(item.get("watched")))
        self._notify_fetched(item["id"])

    # --- metadata fetch -----------------------------------------------

    def _provider_ready(self) -> bool:
        try:
            metadata_service.get_service()
        except metadata_service.MetadataError:
            self._set_status("No metadata provider is connected.")
            return False
        return True

    def _fetch_metadata(self, overwrite_artwork: bool = False) -> None:
        item = self._item
        btn = self._fetch_btn
        if item is None or btn is None:
            return
        if not self._provider_ready():
            return
        self._fetch_gen += 1
        generation = self._fetch_gen
        media_id = item["id"]
        btn.configure(state="disabled", text="Fetching…")
        self._set_status("Searching…")
        query = metadata_service.MetadataService.clean_query(
            str(item.get("title") or ""), item.get("year"))
        media_type = item.get("media_type")
        year = item.get("year")
        media_path = item.get("video_path") or item.get("path")

        def worker() -> None:
            result = None
            error = None
            art_error = None
            try:
                candidates = metadata_service.search_candidates(
                    query, year, media_type)
                if len(candidates) > 1:
                    self._post(lambda: self._ask_candidate(
                        candidates, media_id, generation, overwrite_artwork))
                    return
                if candidates:
                    chosen = candidates[0]
                    title = chosen.title or query
                    chosen_year = chosen.year or year
                    imdb_id = chosen.imdb_id
                else:
                    title, chosen_year, imdb_id = query, year, None
                result = metadata_service.fetch_metadata(
                    title, chosen_year, media_type, imdb_id)
                if result is not None:
                    # the metadata and the artwork are two separate steps:
                    # a failing artwork download must not report the whole
                    # fetch as failed
                    metadata_service.save_metadata(self._db, media_id, result)
                    try:
                        self._apply_artwork(media_id, media_path, media_type,
                                            result, overwrite_artwork)
                    except Exception as exc:
                        art_error = exc
                        log.warning("artwork step failed for %s: %s", query, exc)
            except Exception as exc:
                log.warning("metadata fetch failed for %s: %s", query, exc)
                result, error = None, exc
            self._post(self._fetch_finished(media_id, result, error,
                                            art_error, generation, overwrite_artwork))

        threading.Thread(target=self._db.thread_target(worker), daemon=True).start()

    def _apply_artwork(self, media_id: int, media_path: str, media_type: str,
                       result, overwrite_artwork: bool):
        """Download the artwork and point the item at it."""
        downloaded = metadata_service.download_artwork(
            {"video_path": media_path, "path": media_path},
            result.poster_url, result.backdrop_url,
            overwrite=overwrite_artwork,
        )
        metadata_service.write_about(
            {"video_path": media_path, "path": media_path, "media_type": media_type},
            result,
        )
        p = Path(media_path)
        folder = p.parent if p.is_file() else p
        if folder.exists():
            poster, backdrop = find_artwork(folder)
            if poster or backdrop:
                self._db.set_artwork_paths(
                    media_id,
                    str(poster) if poster else None,
                    str(backdrop) if backdrop else None,
                )
        return downloaded

    def _ask_candidate(self, candidates, media_id: int, generation: int,
                       overwrite_artwork: bool) -> None:
        if self._item is None or self._item.get("id") != media_id:
            return
        if self._fetch_gen != generation:
            return
        picker = tk.Toplevel(self)
        picker.title("Choose a title")
        picker.configure(bg=PANEL_BG)
        picker.transient(self)
        box = tk.Frame(picker, bg=PANEL_BG, padx=24, pady=20)
        box.pack(fill="both", expand=True)
        tk.Frame(box, bg=ACCENT, height=3).pack(fill="x", pady=(0, 16))
        tk.Label(box, text="METADATA MATCH", bg=PANEL_BG, fg=ACCENT,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(box, text="Choose a title", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 19)).pack(anchor="w", pady=(2, 2))
        tk.Label(
            box, text="Several matches were found. Select the correct film or series.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 14))

        list_frame = tk.Frame(
            box, bg="#0d1117", highlightthickness=1, highlightbackground=BORDER,
        )
        list_frame.pack(fill="both", expand=True)
        listbox = tk.Listbox(
            list_frame, bg="#0d1117", fg=TEXT, selectbackground="#293650",
            selectforeground=TEXT, activestyle="none", highlightthickness=0,
            bd=0, font=("Segoe UI", 10), width=52,
            height=min(8, max(3, len(candidates))), exportselection=False,
        )
        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=listbox.yview,
            style="Dark.Vertical.TScrollbar",
        )
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)
        for c in candidates:
            label = f"{c.title or '?'}"
            if c.year:
                label += f"  ({c.year})"
            if c.imdb_id:
                label += f"  [{c.imdb_id}]"
            listbox.insert("end", label)
        listbox.selection_set(0)
        listbox.activate(0)
        listbox.focus_set()

        def choose() -> None:
            sel = listbox.curselection()
            if not sel:
                picker.destroy()
                return
            candidate = candidates[sel[0]]
            picker.destroy()
            self._fetch_selected(candidate, media_id, generation, overwrite_artwork)

        def cancel() -> None:
            picker.destroy()
            if self._fetch_btn is not None:
                self._fetch_btn.configure(state="normal", text=self._fetch_label())
            self._set_status("Cancelled.")

        row = tk.Frame(box, bg=PANEL_BG)
        row.pack(fill="x", pady=(14, 0))
        self._dialog_action_label(
            row, "Use this title", choose, bg=ACCENT, fg=TEXT,
            hover_bg="#f05a60",
        ).pack(side="right", padx=(8, 0))
        self._dialog_action_label(
            row, "Cancel", cancel, bg="#1a2130", fg=TEXT,
            hover_bg="#243046",
        ).pack(side="right")
        listbox.bind("<Double-Button-1>", lambda _event: choose())
        listbox.bind("<Return>", lambda _event: choose())
        picker.bind("<Escape>", lambda _event: cancel())
        setup_popup(picker, self)

    @staticmethod
    def _dialog_action_label(parent, text: str, command, *, bg: str, fg: str,
                             hover_bg: str) -> tk.Label:
        widget = tk.Label(
            parent, text=text, bg=bg, fg=fg, padx=14, pady=7,
            font=("Segoe UI Semibold", 9), cursor="hand2", takefocus=True,
        )

        def invoke(_event=None):
            command()
            return "break"

        widget.bind("<Button-1>", invoke)
        widget.bind("<Return>", invoke)
        widget.bind("<space>", invoke)
        widget.bind("<Enter>", lambda _event: widget.configure(bg=hover_bg))
        widget.bind("<Leave>", lambda _event: widget.configure(bg=bg))
        return widget

    def _fetch_selected(self, candidate, media_id: int, generation: int,
                        overwrite_artwork: bool) -> None:
        item = self._item or {}
        media_path = item.get("video_path") or item.get("path")
        media_type = item.get("media_type")
        btn = self._fetch_btn
        if btn is not None:
            btn.configure(state="disabled", text="Fetching…")

        def worker() -> None:
            result = None
            error = None
            art_error = None
            try:
                result = metadata_service.fetch_metadata(
                    candidate.title or "", candidate.year, media_type,
                    candidate.imdb_id)
                if result is not None:
                    metadata_service.save_metadata(self._db, media_id, result)
                    try:
                        self._apply_artwork(media_id, media_path, media_type,
                                            result, overwrite_artwork)
                    except Exception as exc:
                        art_error = exc
                        log.warning("artwork step failed: %s", exc)
            except Exception as exc:
                log.warning("metadata fetch failed: %s", exc)
                result, error = None, exc
            self._post(self._fetch_finished(media_id, result, error,
                                            art_error, generation, overwrite_artwork))

        threading.Thread(target=self._db.thread_target(worker), daemon=True).start()

    def _fetch_finished(self, media_id, result, error, art_error, generation,
                        overwrite_artwork):
        def apply() -> None:
            if self._item is None or self._item.get("id") != media_id:
                return
            if self._fetch_gen != generation:
                return
            btn = self._fetch_btn
            if btn is None:
                return
            if result is not None:
                self._refresh_from_db(media_id)
                if self._on_fetched is not None:
                    try:
                        self._on_fetched(media_id)
                    except Exception:
                        log.exception("post-fetch refresh failed")
                # the panel was rebuilt above, so set the final state last
                if self._fetch_btn is not None:
                    self._fetch_btn.configure(state="normal", text="Fetch again")
                if art_error is not None:
                    self._set_status(
                        f"Metadata updated, but the artwork failed: {art_error}")
                else:
                    self._set_status("Metadata updated.", ok=True)
            else:
                msg = str(error or "not found")
                log.warning("no metadata applied for id %s: %s", media_id, msg)
                if btn is not None:
                    btn.configure(state="normal", text="Fetch failed — try again")
                self._set_status(f"Fetch failed: {msg}")
                # a search miss the provider describes as "not found" usually
                # means the folder name does not match the real title
                if self._fetch_gen == generation and error is None:
                    self._ask_imdb_id(media_id, generation, overwrite_artwork)
        return apply

    def _ask_imdb_id(self, media_id: int, generation: int,
                     overwrite_artwork: bool = False) -> None:
        """Let the user fetch by IMDb ID after a title search found nothing."""
        if self._item is None or self._item.get("id") != media_id:
            return
        item = self._item
        title = str(item.get("title") or "Unknown")
        year = item.get("year")
        if year:
            message = (f'There is no match found for "{title} ({year})". '
                       'Do you want to try to fetch using an IMDb ID?')
        else:
            message = (f'There is no match found for "{title}". '
                       'Do you want to try to fetch using an IMDb ID?')
        imdb_id = ImdbIdDialog(self, message).ask()
        if imdb_id is None or self._item is None or self._fetch_gen != generation:
            return
        candidate = metadata_service.MetadataResult(
            title=title, year=year, imdb_id=imdb_id)
        self._fetch_selected(candidate, media_id, generation, overwrite_artwork)

    def _notify_fetched(self, media_id: int) -> None:
        if self._on_fetched is not None:
            try:
                self._on_fetched(media_id)
            except Exception:
                log.exception("post-change refresh failed")

    def _refresh_from_db(self, media_id: int) -> None:
        fresh = self._db.get(media_id) or self._item
        fresh["video_path"] = self._item.get("video_path") or fresh.get("video_path")
        episode_rows = (
            self._db.episodes(media_id) if fresh.get("media_type") == "series" else None
        )
        self.show(fresh, episode_rows)

    def _play_video(self) -> None:
        item = self._item
        if not item:
            return
        media_ops.play(item.get("video_path") or "")

    # --- episode browser ----------------------------------------------

    def _episode_browser(self, parent: tk.Frame, item: dict, episodes: list[dict]) -> None:
        """The episode list: one row per episode, with a real play button.

        Every episode gets its own play and watched button - they are
        widgets, not characters in a table, so they can be pressed. The
        rows of a season are only built while the season is open, so a
        series with hundreds of episodes stays quick.
        """
        self._episodes = episodes
        groups: list[tuple[str, list[dict]]] = []
        by_season: dict[int, list[dict]] = {}
        untitled: list[dict] = []
        for episode in episodes:
            season = episode.get("season")
            if season is None:
                untitled.append(episode)
            else:
                by_season.setdefault(season, []).append(episode)
        for season in sorted(by_season):
            groups.append((f"Season {season}", by_season[season]))
        if untitled:
            groups.append(("Unsorted", untitled))
        if not groups:
            return

        self._ep_groups = groups
        self._ep_by_path = {e["path"]: e for e in episodes}
        self._ep_rows: dict[str, dict] = {}
        self._ep_seasons: dict[str, dict] = {}

        for index, (name, eps) in enumerate(groups):
            self._build_season(parent, name, eps, index * 2)
        self._panel["ep_fill"] = self._update_episode_rows

    def _build_season(self, parent: tk.Frame, name: str, eps: list[dict],
                      row: int) -> None:
        """The header of a season and the frame its rows go into."""
        header = tk.Frame(parent, bg=POSTER_BG)
        header.grid(row=row, column=0, sticky="ew", pady=(6, 0))
        count = tk.Label(header, text="", bg=POSTER_BG, fg=MUTED,
                         font=("Segoe UI", 8))
        count.pack(side="right", padx=(0, 8))
        button = tk.Button(
            header, text="", command=lambda n=name: self._toggle_season(n),
            bg=POSTER_BG, fg=TEXT, activebackground=POSTER_BG,
            activeforeground=TEXT, bd=0, anchor="w", padx=2, pady=4,
            font=("Segoe UI", 9, "bold"), cursor="hand2")
        button.pack(side="left", fill="x", expand=True)
        rows = tk.Frame(parent, bg=POSTER_BG)
        rows.grid(row=row + 1, column=0, sticky="ew")
        self._ep_seasons[name] = {"header": header, "button": button,
                                  "count": count, "rows": rows,
                                  "episodes": eps, "built": False}
        self._update_season_label(name)
        if self._ep_open.get(name, False):
            self._build_season_rows(name)
        else:
            rows.grid_remove()

    def _build_season_rows(self, name: str) -> None:
        """The rows of a season - built the first time the season is opened."""
        state = self._ep_seasons.get(name)
        if state is None or state.get("built"):
            return
        for episode in state["episodes"]:
            self._build_episode_row(state["rows"], episode)
        state["built"] = True

    def _build_episode_row(self, parent: tk.Frame, episode: dict) -> None:
        """One episode: its number, its name and the two buttons."""
        if parent.winfo_children():
            # a hair line above every row but the first - the rows wrap and
            # wrap again, the line keeps each episode apart
            tk.Frame(parent, height=1, bg=BORDER).pack(
                fill="x", padx=(8, 40), pady=(5, 3))
        path = episode["path"]
        row = tk.Frame(parent, bg=POSTER_BG)
        row.pack(fill="x")
        line = tk.Frame(row, bg=POSTER_BG)
        line.pack(fill="x", pady=(2, 0))
        tk.Label(line, text=self._episode_number(episode), bg=POSTER_BG, fg=MUTED,
                 font=("Segoe UI", 8), width=8, anchor="w").pack(
            side="left", padx=(8, 4))
        name = str(episode.get("name") or Path(episode["path"]).stem)
        tk.Label(line, text=name, bg=POSTER_BG, fg=TEXT, font=("Segoe UI", 9),
                 anchor="w").pack(side="left", fill="x", expand=True)
        watched = tk.Button(
            line, text="", command=lambda p=path: self._toggle_episode(p),
            bg=POSTER_BG, fg="#79c37d", activebackground=POSTER_BG,
            activeforeground=TEXT, bd=0, padx=4, width=3,
            font=icon_font(10), cursor="hand2")
        watched.pack(side="right")
        play = tk.Button(
            line, text=icon_glyph("play"), command=lambda p=path: media_ops.play(p),
            bg=POSTER_BG, fg="#6aa4ff", activebackground=POSTER_BG,
            activeforeground=TEXT, bd=0, padx=6, font=icon_font(10),
            cursor="hand2")
        play.pack(side="right")
        # a chained file ("S04E01E02") owns several rows - each keeps its
        # own buttons, all of them follow the state of the one file
        widgets = self._ep_rows.setdefault(
            path, {"row": [], "play": [], "watched": []})
        widgets["row"].append(row)
        widgets["play"].append(play)
        widgets["watched"].append(watched)
        # the air date and the summary, when a provider fetched them for it
        released = self._fmt_date(episode.get("released"))
        summary = str(episode.get("summary") or "").strip()
        extra = "  ·  ".join(part for part in (released, summary[:220]) if part)
        if extra:
            tk.Label(row, text=extra, bg=POSTER_BG, fg=MUTED,
                     font=("Segoe UI", 8), wraplength=520, justify="left",
                     anchor="w").pack(fill="x", padx=(58, 40), pady=(0, 2))
        self._update_episode_row(path)

    @staticmethod
    def _episode_number(episode: dict) -> str:
        season, number = episode["season"], episode["episode"]
        if number is None:
            return ""
        if season is None:
            return f"E{number:02d}"
        return f"S{season:02d}E{number:02d}"

    def _update_episode_row(self, path: str) -> None:
        """The watched mark of one episode."""
        widgets = self._ep_rows.get(path)
        episode = self._ep_by_path.get(path)
        if widgets is None or episode is None:
            return
        watched = bool(episode.get("watched"))
        for button in widgets["watched"]:
            button.configure(
                text=icon_glyph("check" if watched else "circle"),
                font=icon_font(10, outline=not watched),
                fg="#79c37d" if watched else "#4a5568")

    def _update_season_label(self, name: str) -> None:
        """The name and the count of a season."""
        state = self._ep_seasons.get(name)
        if state is None:
            return
        eps = next((e for n, e in self._ep_groups if n == name), [])
        watched = sum(1 for e in eps if e.get("watched"))
        opened = self._ep_open.get(name, False)
        arrow = "\u25be" if opened else "\u25b8"
        state["button"].configure(text=f"{arrow}  {name}")
        state["count"].configure(text=f"{watched}/{len(eps)}")

    def _update_episode_rows(self) -> None:
        """Bring the rows in line with the database."""
        for path in list(self._ep_rows):
            self._update_episode_row(path)
        for name in list(self._ep_seasons):
            self._update_season_label(name)

    def _toggle_season(self, name: str) -> None:
        """Open or close a season."""
        state = self._ep_seasons.get(name)
        if state is None:
            return
        opened = not self._ep_open.get(name, False)
        self._ep_open[name] = opened
        if opened:
            # the rows are built the first time the season is opened
            self._build_season_rows(name)
            state["rows"].grid()
        else:
            state["rows"].grid_remove()
        self._update_season_label(name)
        # the list is shorter or longer now - the flow region follows
        self._upper.update_scrollregion()
    def _toggle_episode(self, path: str) -> None:
        """Mark one episode as watched or unwatched."""
        episode = self._ep_by_path.get(path)
        if episode is None:
            return
        new_state = not bool(episode.get("watched"))
        self._db.set_episodes_watched([path], new_state)
        episode["watched"] = 1 if new_state else 0
        self._update_episode_row(path)
        for name, eps in self._ep_groups:
            if any(e["path"] == path for e in eps):
                self._update_season_label(name)
        self._notify_fetched(self._item["id"] if self._item else 0)

    # --- media --------------------------------------------------------

    def _play_file(self, path: str) -> None:
        media_ops.reveal(path)

    def _noop(self) -> None:
        pass

    def _load_backdrop(self) -> None:
        """Request a backdrop rendered to the canvas's current dimensions."""
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 2 or height < 2:
            # not laid out yet - try again once the widget has a size
            if self._bg_attempts < 12:
                self._bg_attempts += 1
                self.after(60, self._load_backdrop)
            return
        self._bg_attempts = 0
        if self._bg_job is not None:
            self._images.cancel(self._bg_job)
            self._bg_job = None
        self._last_geo = (width, height)
        item = self._item or {}
        source = item.get("poster2_path") or item.get("poster_path")
        self._bg_job = self._images.get_backdrop(
            source, (width, height), self._apply_bg, priority=0,
        )

    def _apply_bg(self, image) -> None:
        if not self.winfo_exists() or image is None:
            return
        self._bg_job = None
        # the view keeps the picture: the Tk image must not die with the
        # cache entry it came from
        self._bg_image = image
        if self._bg_id is None:
            self._bg_id = self.canvas.create_image(0, 0, anchor="nw", image=image)
        else:
            self.canvas.itemconfig(self._bg_id, image=image)
        self.canvas.tag_lower(self._bg_id)
        if self._resize_job is None:
            self._refresh_panel_backdrop()

    def _refresh_panel_backdrop(self) -> None:
        """Draw a tinted crop of the backdrop behind the panel's widgets."""
        if self._bg_image is None or not self.info.winfo_ismapped():
            return
        width, height = self.info.winfo_width(), self.info.winfo_height()
        canvas_width, canvas_height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if min(width, height, canvas_width, canvas_height) < 2:
            return

        source = ImageTk.getimage(self._bg_image).convert("RGB")
        if source.size != (canvas_width, canvas_height):
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            source = source.resize((canvas_width, canvas_height), resampling)
        x = self.info.winfo_rootx() - self.canvas.winfo_rootx()
        y = self.info.winfo_rooty() - self.canvas.winfo_rooty()
        crop = source.crop((x, y, x + width, y + height))
        tint = Image.new("RGB", crop.size, PANEL_TINT)
        tinted = Image.blend(crop, tint, PANEL_TINT_STRENGTH)
        photo = ImageTk.PhotoImage(tinted)
        self._panel_backdrop.delete("all")
        self._panel_backdrop.create_image(0, 0, anchor="nw", image=photo)
        self._panel_backdrop_image = photo

    def _load_poster(self, poster: tk.Canvas) -> None:
        if self._poster_job is not None:
            self._images.cancel(self._poster_job)
            self._poster_job = None
        item = self._item or {}
        self._poster_job = self._images.get_thumbnail(
            item.get("poster_path"), (148, 222), lambda img, c=poster: self._apply_poster(c, img),
            priority=0,
        )

    def _apply_poster(self, canvas: tk.Canvas, image) -> None:
        if image is None:
            return
        try:
            if not canvas.winfo_exists():
                return
            # the placeholder and the real poster land on the same canvas:
            # the previous image is removed instead of stacked on top
            old = getattr(canvas, "_poster_img_id", None)
            if old is not None:
                canvas.delete(old)
            # the canvas keeps the picture, so the Tk image outlives the
            # cache entry it came from
            canvas._poster_img = image
            canvas._poster_img_id = canvas.create_image(0, 0, anchor="nw", image=image)
        except tk.TclError:
            pass

    def _enlarge_poster(self) -> None:
        item = self._item or {}
        source = item.get("poster_path")
        if not source:
            return
        top = tk.Toplevel(self)
        top.title(str(item.get("title") or "Poster"))
        top.configure(bg=BG)
        canvas = tk.Canvas(top, bg=BG, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        size = (520, 780)
        top.geometry(f"{size[0]}x{size[1]}")
        self._images.get_thumbnail(
            source, size, lambda img, c=canvas: self._apply_poster(c, img), priority=0)
        setup_popup(top, self)

    def _on_resize(self, event=None) -> None:
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, self._check_resize)

    def _check_resize(self) -> None:
        self._resize_job = None
        if not self.winfo_exists() or self._item is None:
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 2 or height < 2:
            return
        if (width, height) != self._last_geo:
            self._load_backdrop()
        elif self._bg_job is None:
            self._refresh_panel_backdrop()

    def _back(self) -> None:
        self._on_back()

    def _open_folder(self) -> None:
        item = self._item
        if item is None:
            return
        folder = media_ops.media_folder(item)
        if folder is not None:
            media_ops.reveal(folder)

    def _location(self, item: dict) -> str:
        p = item.get("video_path") or item.get("path")
        return str(p) if p else ""

    @staticmethod
    def _as_list(value) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip().startswith("["):
            import json
            try:
                loaded = json.loads(value)
                if isinstance(loaded, list):
                    return [str(v) for v in loaded if str(v).strip()]
            except ValueError:
                pass
            return [value.strip()] if value.strip() else []
        if isinstance(value, str) and value.strip():
            # comma names and JSON lists land the same way: one link each
            return [p.strip() for p in value.split(",") if p.strip()]
        return []

    def hide(self) -> None:
        self._cancel_jobs()
        self._item = None
        if self._bg_id is not None:
            self.canvas.delete(self._bg_id)
            self._bg_id = None
            self._last_geo = (0, 0)
        self._clear_info()

    def _clear_info(self) -> None:
        for child in self._upper.inner.winfo_children():
            child.destroy()
        for child in self._actions_frame.winfo_children():
            child.destroy()
        for child in self._loc_frame.winfo_children():
            child.destroy()
        # the widgets the panel referred to are gone
        self._panel = {}
        self._panel_item_id = None
        self._panel_signature = None
        self._episodes = []
        self._ep_open = {}
        self._ep_rows = {}
        self._ep_seasons = {}
        self._ep_by_path = {}
        self._ep_body = None
