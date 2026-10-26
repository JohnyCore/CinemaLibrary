from __future__ import annotations

import json
import logging
import queue
import random
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Callable

import config
from database.db_manager import Database
from metadata.person_profiles import fetch_person_profile, person_key
from scanner import scan_state
from scanner.library_scanner import scan
from ui.details_view import DetailsView
from ui.dialogs import ConfirmDialog, PopupMenu, PromptDialog
from ui.folder_dialog import FolderDialog
from ui.icons import photo as icon_photo
from ui.library_view import LibraryView
from ui.settings_view import SettingsView
from utils import media_ops
from utils.image_cache import ImageCache

log = logging.getLogger(__name__)

BG = "#0d0e12"
SIDEBAR_BG = "#101319"
TOPBAR_BG = "#101319"
BORDER = "#1e2430"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
ACCENT = "#e5484d"
ENTRY_BG = "#0a0c10"

SORTS = ("Title  A→Z", "Title  Z→A", "Year  new→old", "Year  old→new",
         "Rating  high→low", "Added  new→old", "Watched  recent→old",
         "My rating  high→low")
SEARCH_HINT = "Search title or year…"

SECTIONS = (
    ("all", "All Library"),
    ("movie", "Movies"),
    ("series", "TV Series"),
    ("insights", "Insights"),
)


def _flag(term: str, default: bool = True) -> bool:
    """yes / no, true / false, 1 / 0 - and nothing at all."""
    text = str(term or "").strip().lower()
    if not text:
        return default
    return text not in ("no", "false", "0", "n", "nein")


def _number_matches(value, term: str) -> bool:
    """Does a number match "7", "7+", ">7" or "<7"?"""
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    text = str(term or "").strip()
    try:
        if text.endswith("+"):
            return number >= float(text[:-1])
        if text.startswith(">"):
            return number > float(text[1:])
        if text.startswith("<"):
            return number < float(text[1:])
        return number == float(text)
    except ValueError:
        return False


def _is_missing(item: dict, term: str) -> bool:
    """What an item does not have yet."""
    if term == "poster":
        return not item.get("poster_path")
    if term == "poster2":
        return not item.get("poster2_path")
    if term in ("metadata", "plot"):
        return not item.get("plot")
    if term == "imdb":
        return not item.get("imdb_id")
    if term == "rating":
        return item.get("rating") is None
    if term == "runtime":
        return not item.get("runtime")
    if term == "cast":
        return not item.get("cast_list")
    if term == "director":
        return not item.get("director")
    if term == "writer":
        return not item.get("writer")
    if term == "country":
        return not item.get("country")
    if term == "year":
        return item.get("year") is None
    return False

# the query language: "1990-2000", "genre:Drama", "actor:Pfiffer"
_FIELD_RE = re.compile(r"(actor|cast|director|writer|genre|rating|myrating|type|missing):([^\s,]+)")
# the flags may stand alone: "unwatched:"
_FLAG_RE = re.compile(r"(watched|unwatched):([^\s,]*)")
_YEAR_RANGE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{4})(?!\d)")
_YEAR_BEFORE_RE = re.compile(r"(?<!\d)-(\d{4})(?!\d)")
_YEAR_AFTER_RE = re.compile(r"(?<!\d)(\d{4})-(?!\d)")



def setup_styles(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        "Dark.TCombobox", fieldbackground=ENTRY_BG, background=TOPBAR_BG,
        foreground=TEXT, arrowcolor=MUTED, bordercolor=BORDER,
        lightcolor=BORDER, darkcolor=BORDER, arrowpadding=(6, 0),
    )
    style.map("Dark.TCombobox",
              fieldbackground=[("readonly", ENTRY_BG)],
              foreground=[("readonly", TEXT)])
    style.configure(
        "Dark.Vertical.TScrollbar", background="#39445a", troughcolor="#0f1319",
        bordercolor="#0f1319", arrowcolor="#8b97a8", arrowsize=11,
        lightcolor="#39445a", darkcolor="#39445a",
    )
    style.map("Dark.Vertical.TScrollbar",
              background=[("active", "#4a5773")],
              lightcolor=[("active", "#4a5773")],
              darkcolor=[("active", "#4a5773")])
    style.configure(
        "Dark.Treeview", background="#12161d", fieldbackground="#12161d",
        foreground=TEXT, rowheight=26, borderwidth=0, bordercolor="#12161d",
        lightcolor="#12161d", darkcolor="#12161d",
    )
    style.map("Dark.Treeview",
              background=[("selected", "#243041")],
              foreground=[("selected", TEXT)])
    style.configure(
        "Dark.Treeview.Heading", background="#181d27", foreground="#c3cad6",
        borderwidth=0, relief="flat", padding=(6, 8),
        font=("Segoe UI", 9, "bold"), bordercolor="#181d27",
        lightcolor="#181d27", darkcolor="#181d27",
    )
    style.map("Dark.Treeview.Heading",
              background=[("active", "#1e2531")],
              foreground=[("active", TEXT)],
              bordercolor=[("active", "#1e2531")],
              lightcolor=[("active", "#1e2531")],
              darkcolor=[("active", "#1e2531")])
    style.configure(
        "Dark.Horizontal.TProgressbar", troughcolor="#12161d",
        background=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
        bordercolor="#12161d", thickness=8,
    )
    style.map("Dark.Horizontal.TProgressbar",
              background=[("active", "#f25f63")])


class MainWindow(tk.Frame):
    def __init__(self, root: tk.Tk, db: Database,
                 movies_dir: Path | None,
                 series_dir: Path | None) -> None:
        super().__init__(root, bg=BG)
        self.root = root
        self._db = db
        self._movies_dir = movies_dir
        self._series_dir = series_dir
        self._q: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._images = ImageCache(config.THUMB_CACHE_DIR, self._post,
                                  config.PLACEHOLDER_PATH)
        self._scan_running = False
        self._rescan_requested = False
        self._closing = False
        self._search_job: str | None = None
        self._search_text = ""
        self._section = "all"
        self._insights_query = ""
        self._insights_genre: str | None = None
        self._insights_continue_only = False
        self._sort_key = 0
        self._all_items: list[dict] = []
        self._visible_items: list[dict] = []
        self._positions: dict[int, int] = {}
        self._chip_kind: str | None = None
        self._chip_value: str | None = None
        self._collections: list[dict] = []
        self._person_profile_generation = 0

        setup_styles(root)

        self.pack(fill="both", expand=True)
        self._build_sidebar()
        self._build_main()
        self._show_library()

        self._load_from_db()
        root.after(16, self._drain)
        root.bind("<Escape>", self._escape)
        root.after(300, self._rescan_clicked)

    # --- layout -------------------------------------------------------

    def _build_sidebar(self) -> None:
        bar = tk.Frame(self, bg=SIDEBAR_BG, width=184)
        bar.pack(side="left", fill="y")
        bar.pack_propagate(False)
        tk.Label(bar, text="CINEMA", bg=SIDEBAR_BG, fg=TEXT,
                 font=("Segoe UI", 17, "bold")).pack(anchor="w", padx=16, pady=(18, 0))
        tk.Label(bar, text="local media library", bg=SIDEBAR_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", pady=14, padx=14)
        self._nav: dict[str, tk.Label] = {}
        for key, label in SECTIONS:
            self._nav_button(bar, label, key)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", pady=(12, 8), padx=14)
        self._sidebar_action(
            bar, "Add Folder", self._add_folder, bg="#161d2a", fg=TEXT,
            image=icon_photo("folder", size=14, color=TEXT),
        ).pack(fill="x", padx=8)
        self._folder_box = tk.Frame(bar, bg=SIDEBAR_BG)
        self._folder_box.pack(fill="x")
        spacer = tk.Frame(bar, bg=SIDEBAR_BG)
        spacer.pack(fill="both", expand=True)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(0, 8))
        self._settings_btn = self._sidebar_action(
            bar, "Settings", self._open_settings, bg=SIDEBAR_BG, fg=MUTED,
            image=icon_photo("gear", size=14, color=MUTED),
            leave_bg=lambda: (
                "#1a2130" if self._settings.winfo_ismapped() else SIDEBAR_BG
            ),
        )
        self._settings_btn.pack(fill="x", padx=8, pady=(0, 10))

    def _sidebar_action(
        self, parent, label: str, command, *, bg: str, fg: str,
        image=None, padx: int = 16, leave_bg: Callable[[], str] | None = None,
    ) -> tk.Label:
        widget = tk.Label(
            parent, text=label, image=image, compound="left",
            bg=bg, fg=fg, bd=0, anchor="w", padx=padx, pady=6,
            font=("Segoe UI Semibold", 11), cursor="hand2", takefocus=True,
        )

        def invoke(_event=None):
            command()
            return "break"

        widget.bind("<Button-1>", invoke)
        widget.bind("<Return>", invoke)
        widget.bind("<space>", invoke)
        widget.bind("<Enter>", lambda _event: widget.configure(bg="#1a2130"))
        widget.bind(
            "<Leave>",
            lambda _event: widget.configure(bg=leave_bg() if leave_bg else bg),
        )
        return widget

    def _nav_button(self, parent, label: str, key: str) -> tk.Label:
        row = tk.Frame(parent, bg=SIDEBAR_BG)
        row.pack(fill="x", padx=8, pady=1)
        btn = self._sidebar_action(
            row, label, lambda k=key: self._navigate_section(k),
            bg="#1a2130" if self._section == key else SIDEBAR_BG,
            fg=TEXT if self._section == key else MUTED, padx=8,
        )
        btn.pack(side="left", fill="x", expand=True)
        self._nav[key] = btn
        btn.bind(
            "<Enter>",
            lambda _event, b=btn: b.configure(bg="#243046"),
            add="+",
        )
        btn.bind("<Leave>", lambda _event, k=key, b=btn: b.configure(
            bg="#1a2130" if self._section == k else SIDEBAR_BG,
        ), add="+")
        return btn

    def _rebuild_folder_nav(self) -> None:
        """Show one sidebar entry per folder the user created."""
        for key in [k for k in self._nav if k.startswith("collection:")]:
            self._nav.pop(key).destroy()
        for child in self._folder_box.winfo_children():
            child.destroy()
        for collection in self._collections:
            key = f"collection:{collection['id']}"
            btn = self._nav_button(self._folder_box,
                                   f"{collection['name']}  ({collection['n']})", key)
            btn.bind("<Button-3>",
                     lambda e, c=collection: self._folder_menu(c, e.x_root, e.y_root))

    def _build_main(self) -> None:
        column = tk.Frame(self, bg=BG)
        column.pack(side="left", fill="both", expand=True)

        top = tk.Frame(column, bg=TOPBAR_BG)
        top.pack(fill="x")

        search_box = tk.Frame(top, bg=ENTRY_BG, highlightthickness=1,
                              highlightbackground=BORDER)
        search_box.pack(side="left", padx=(14, 8), pady=9)
        self._search = tk.Entry(
            search_box, bg=ENTRY_BG, fg=TEXT, insertbackground=TEXT, bd=0,
            highlightthickness=0, font=("Segoe UI", 10), width=30,
        )
        self._search.pack(side="left", fill="both", expand=True, ipady=4, padx=8)
        self._hint = tk.Label(search_box, text=SEARCH_HINT, bg=ENTRY_BG, fg=MUTED,
                              font=("Segoe UI", 10))
        self._hint.place(x=8, y=4)
        self._hint.bind("<Button-1>", lambda _e: self._search.focus_set())
        self._search.bind("<KeyRelease>", self._on_search_key)
        self._search.bind("<FocusIn>", self._focus_in)
        self._search.bind("<FocusOut>", self._focus_out)

        self._chip_bar = tk.Frame(top, bg=TOPBAR_BG)
        chip_box = tk.Frame(self._chip_bar, bg=ENTRY_BG, highlightthickness=1,
                            highlightbackground=BORDER)
        self._chip_label = tk.Label(
            chip_box, bg=ENTRY_BG, fg=ACCENT, font=("Segoe UI", 9, "bold"), anchor="w",
        )
        self._chip_label.pack(side="left", padx=(9, 4))
        tk.Button(
            chip_box, text="✕", command=self._clear_chip,
            bg=ENTRY_BG, fg=MUTED, activebackground=ENTRY_BG, activeforeground=TEXT,
            bd=0, padx=5, cursor="hand2", font=("Segoe UI", 9),
        ).pack(side="left", padx=(0, 6))
        chip_box.pack(side="left", pady=9, ipady=2)

        self._sort = ttk.Combobox(top, style="Dark.TCombobox", state="readonly",
                                  values=SORTS, width=16)
        self._sort.current(0)
        self._sort.pack(side="right", padx=(8, 14), pady=9)
        self._sort.bind("<<ComboboxSelected>>", self._on_sort)

        self._rescan_btn = tk.Button(
            top, text="Rescan Library", command=self._rescan_clicked,
            bg="#161d2a", fg=TEXT, activebackground="#243046", activeforeground=TEXT,
            disabledforeground="#5a6472",
            bd=0, padx=14, pady=6, font=("Segoe UI", 10), cursor="hand2",
        )
        self._rescan_btn.pack(side="right", padx=(0, 14))

        self._surprise_btn = tk.Button(
            top, text="Surprise me", command=self._surprise_me,
            bg="#161d2a", fg=TEXT, activebackground="#243046", activeforeground=TEXT,
            disabledforeground="#5a6472",
            bd=0, padx=14, pady=6, font=("Segoe UI", 10), cursor="hand2",
        )
        self._surprise_btn.pack(side="right", padx=(0, 8))

        body = tk.Frame(column, bg=BG)
        body.pack(fill="both", expand=True)

        self._details = DetailsView(
            body, self._images, self._show_library, self._db,
            self._on_item_fetched, self._on_filter_from_details,
            post=self._post, on_person=self._show_person_profile,
        )
        self._library = LibraryView(body, self._images, self._open_item,
                                    self._show_context_menu)
        self._insights = tk.Frame(body, bg=BG)
        self._settings = SettingsView(body, self._on_settings_saved, self._db, self._images,
                                  on_changed=self._on_items_fetched, post=self._post,
                                  on_rescan=self._rescan_clicked,
                                  on_open_item=self._open_item)
        self._render_insights()

        status = tk.Frame(column, bg=TOPBAR_BG, height=30)
        status.pack(fill="x", side="bottom")
        status.pack_propagate(False)
        self._status_left = tk.Label(status, bg=TOPBAR_BG, fg=MUTED,
                                     font=("Segoe UI", 9), anchor="w")
        self._status_left.pack(side="left", padx=14)
        self._scan_progress = ttk.Progressbar(status, mode="determinate", length=140,
                                              style="Dark.Horizontal.TProgressbar")
        self._status_right = tk.Label(status, bg=TOPBAR_BG, fg=MUTED,
                                      font=("Segoe UI", 9), anchor="e")
        self._status_right.pack(side="right", padx=14)
        self._status_active = -1
        self._status_queued = -1
        self.root.after(1000, self._tick)

    # --- main thread plumbing -----------------------------------------

    def _post(self, fn: Callable[[], None]) -> None:
        """Queue a callback for the main thread without touching Tk from a worker."""
        self._q.put(fn)

    def _drain(self) -> None:
        for _ in range(300):
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("queued callback failed")
        if self._closing:
            return
        if not self._q.empty():
            self.after_idle(self._drain)
        else:
            self.after(16, self._drain)

    def _tick(self) -> None:
        """Refresh the background-work counters."""
        if self._closing:
            return
        stats = self._images.stats
        active = stats["active"]
        queued = max(stats["queued"], 0)
        if active != self._status_active or queued != self._status_queued:
            self._status_active = active
            self._status_queued = queued
            self._status_right.configure(
                text=f"thumbs: {active} active · {queued} queued")
        self.after(1000, self._tick)

    # --- data ---------------------------------------------------------

    def _load_from_db(self) -> None:
        self._collections = self._db.list_collections()
        self._rebuild_folder_nav()
        # the episode counts come with the items - the database counts them
        self._all_items = self._db.load_all()
        for item in self._all_items:
            item["_search"] = self._search_blob(item)
            item["_sort"] = str(item.get("title") or "").lower()
        # the position of every item - a bulk fetch changes hundreds of
        # them and a linear search for each one would be slow
        self._positions = {item.get("id"): i for i, item in enumerate(self._all_items)}
        self._render_insights()
        self._apply_filters()

    def _search_blob(self, item: dict) -> str:
        parts = [
            str(item.get("title") or ""),
            " ".join(DetailsView._as_list(item.get("cast_list"))),
            str(item.get("director") or ""),
            str(item.get("writer") or ""),
            " ".join(DetailsView._as_list(item.get("genres"))),
            str(item.get("country") or ""),
            str(item.get("year") or ""),
            str(item.get("imdb_id") or ""),
        ]
        return " ".join(p for p in parts if p).lower()

    # --- filtering ----------------------------------------------------

    def _filter_by_query(self, items: list[dict], query: str) -> list[dict]:
        """Parse query and filter items.
        Supported syntax:
          - year range: 1900-2000, -2000, 2000-
          - field:prefix: actor:Tom, director:Nolan, writer:Kaufman, genre:Action
          - flags: watched:yes, unwatched:, rating:7+, myrating:3, type:series
          - missing: missing:poster, missing:metadata, missing:imdb
          - free text: matches title, cast, director, writer, genres
        """

        def _as_list(val):
            if val is None:
                return []
            if isinstance(val, list):
                return val
            if isinstance(val, str) and val.strip().startswith("["):
                try:
                    loaded = json.loads(val)
                    if isinstance(loaded, list):
                        return loaded
                except ValueError:
                    pass
            return [val] if val else []

        q = query.strip()

        # extract field:value tokens first (anywhere in query)
        field_tokens = [(m.group(1), m.group(2).lower()) for m in _FIELD_RE.finditer(q)]
        field_tokens += [(m.group(1), m.group(2).lower()) for m in _FLAG_RE.finditer(q)]

        # remove field tokens from query
        q_no_fields = _FIELD_RE.sub("", _FLAG_RE.sub("", q)).strip()

        # year range anywhere in remaining query
        year_lo = year_hi = None
        m = _YEAR_RANGE_RE.search(q_no_fields)
        if m:
            year_lo, year_hi = int(m.group(1)), int(m.group(2))
        else:
            m = _YEAR_BEFORE_RE.search(q_no_fields)
            if m:
                year_hi = int(m.group(1))
            else:
                m = _YEAR_AFTER_RE.search(q_no_fields)
                if m:
                    year_lo = int(m.group(1))

        # remaining free-text terms
        free_text = q_no_fields
        free_text = _YEAR_RANGE_RE.sub("", free_text)
        free_text = _YEAR_BEFORE_RE.sub("", free_text)
        free_text = _YEAR_AFTER_RE.sub("", free_text)
        free_text = free_text.strip()
        free_terms = [t for t in free_text.split() if t]

        def match(item: dict) -> bool:
            y = item.get("year")
            if year_lo is not None and (y is None or y < year_lo):
                return False
            if year_hi is not None and (y is None or y > year_hi):
                return False

            for field, term in field_tokens:
                if field in ("actor", "cast"):
                    haystack = " ".join(_as_list(item.get("cast_list")))
                elif field == "director":
                    haystack = item.get("director") or ""
                elif field == "writer":
                    haystack = item.get("writer") or ""
                elif field == "genre":
                    haystack = " ".join(_as_list(item.get("genres")))
                elif field == "watched":
                    if _flag(term) != bool(item.get("seen")):
                        return False
                    continue
                elif field == "unwatched":
                    if _flag(term) == bool(item.get("seen")):
                        return False
                    continue
                elif field == "rating":
                    if not _number_matches(item.get("rating"), term):
                        return False
                    continue
                elif field == "myrating":
                    if not _number_matches(item.get("my_rating"), term):
                        return False
                    continue
                elif field == "type":
                    if (item.get("media_type") or "") != term:
                        return False
                    continue
                elif field == "missing":
                    if not _is_missing(item, term):
                        return False
                    continue
                if term not in haystack.lower():
                    return False

            if free_terms:
                searchable = item.get("_search")
                if searchable is None:
                    searchable = self._search_blob(item)
                if not all(t in searchable for t in free_terms):
                    return False

            return True

        return [i for i in items if match(i)]

    def _apply_filters(self, reset_scroll: bool = False) -> None:
        """Re-filter the library.

        `reset_scroll` is set when the *result set* changed (a new search, a
        different section, a new sort). It stays False when only an item
        changed, so marking something watched does not throw the user back
        to the first page.
        """
        items = list(self._all_items)
        if self._section.startswith("collection:"):
            collection_id = int(self._section[11:])
            items = [i for i in items if i.get("collection_id") == collection_id]
        elif self._section != "all":
            items = [i for i in items if i.get("media_type") == self._section]
        if self._chip_kind:
            items = [i for i in items if self._chip_matches(i)]
        query = self._search_text.lower().strip()
        if query:
            items = self._filter_by_query(items, query)
        if self._insights_query:
            items = self._filter_by_query(items, self._insights_query)
        if self._insights_genre:
            genre = self._insights_genre.casefold()
            items = [
                item for item in items
                if any(
                    str(value).strip().casefold() == genre
                    for value in DetailsView._as_list(item.get("genres"))
                )
            ]
        if self._insights_continue_only:
            items = [
                item for item in items
                if item.get("media_type") == "series"
                and 0 < int(item.get("episodes_watched") or 0)
                < int(item.get("episodes_total") or 0)
            ]
        if self._sort_key == 0:
            items.sort(key=lambda i: i.get("_sort") or "")
        elif self._sort_key == 1:
            items.sort(key=lambda i: i.get("_sort") or "", reverse=True)
        elif self._sort_key == 2:
            # the items without a year go last, not first
            items.sort(key=lambda i: (i.get("year") is None, -(i.get("year") or 0)))
        elif self._sort_key == 3:
            items.sort(key=lambda i: (i.get("year") is None, i.get("year") or 0))
        elif self._sort_key == 4:
            items.sort(key=lambda i: -(i.get("rating") or 0))
        elif self._sort_key == 5:
            items.sort(key=lambda i: -(i.get("added_at") or 0))
        elif self._sort_key == 6:
            items.sort(key=lambda i: -(i.get("last_watched") or 0))
        else:
            items.sort(key=lambda i: -(i.get("my_rating") or 0))
        if self._insights_continue_only:
            items.sort(key=lambda i: -(i.get("last_watched") or 0))
        self._visible_items = items
        self._library.show_items(items, reset_scroll=reset_scroll)
        if not items:
            self._library.set_empty(*self._empty_state())

    def _empty_state(self) -> tuple[str, str | None, object]:
        """What the grid shows when it holds nothing.

        An empty folder gets a button that files items into it; a search
        that found nothing only gets the message.
        """
        if self._section.startswith("collection:"):
            return "Folder is empty.", "Add Item", self._add_item_to_folder
        if (self._search_text or self._chip_kind or self._insights_query
                or self._insights_genre or self._insights_continue_only):
            return "No items match your filters.", None, None
        if self._section == "movie":
            return "No movies in the library yet.", None, None
        if self._section == "series":
            return "No series in the library yet.", None, None
        return "Your library is empty - point it at your media folders in Settings.", \
            None, None

    def _add_item_to_folder(self) -> None:
        """File items from the library into the folder that is open."""
        if not self._section.startswith("collection:"):
            return
        collection_id = int(self._section[11:])
        if not any(c["id"] == collection_id for c in self._collections):
            return
        others = [i for i in self._all_items
                  if i.get("collection_id") != collection_id]
        dialog = FolderDialog(self, self._collections, others, create=False,
                              add_to=collection_id)
        dialog.grab_set()
        dialog.wait_window()
        if not dialog.chosen:
            return
        self._db.set_collection(dialog.item_ids, collection_id)
        self._load_from_db()

    def _navigate_section(self, section: str) -> None:
        if section == "all":
            self._search_text = ""
            self._search.delete(0, "end")
            self._clear_chip()
        self._set_section(section)

    def _set_section(self, section: str) -> None:
        self._insights_query = ""
        self._insights_genre = None
        self._insights_continue_only = False
        self._section = section
        for key, btn in self._nav.items():
            active = key == section
            btn.configure(
                fg=TEXT if active else MUTED,
                bg="#1a2130" if active else SIDEBAR_BG,
            )
        if section == "insights":
            self._details.hide()
            self._details.pack_forget()
            self._settings.pack_forget()
            self._settings_btn.configure(fg=MUTED, bg=SIDEBAR_BG)
            self._library.pack_forget()
            self._insights.pack(fill="both", expand=True)
            self._render_insights()
            return
        self._settings_btn.configure(fg=MUTED, bg=SIDEBAR_BG)
        if not self._library.winfo_ismapped():
            self._show_library()
        self._apply_filters(reset_scroll=True)

    def _on_search_key(self, event=None) -> None:
        if self._search_job is not None:
            try:
                self.after_cancel(self._search_job)
            except Exception:
                pass
        self._search_job = self.after(250, self._flush_search)

    def _flush_search(self) -> None:
        self._search_job = None
        text = self._search.get().strip()
        if text != self._search_text:
            self._search_text = text
            self._apply_filters(reset_scroll=True)

    def _focus_in(self, event=None) -> None:
        self._hint.place_forget()

    def _focus_out(self, event=None) -> None:
        if not self._search.get().strip():
            self._hint.place(x=8, y=4)

    def _on_sort(self, event=None) -> None:
        try:
            self._sort_key = SORTS.index(self._sort.get())
        except ValueError:
            self._sort_key = 0
        self._apply_filters(reset_scroll=True)

    # --- navigation ---------------------------------------------------

    def _open_item(self, item: dict) -> None:
        self._settings.pack_forget()
        self._insights.pack_forget()
        self._settings_btn.configure(fg=MUTED, bg=SIDEBAR_BG)
        self._library.pack_forget()
        # pack the view before it is filled: if building the panel fails,
        # the details view is still there instead of an empty black area
        self._details.pack(fill="both", expand=True)
        self._details.show(item)
        self._details.lift()

    def _show_context_menu(self, item: dict, x: int, y: int) -> None:
        """The menu of a card - it works on the whole selection."""
        chosen = self._library.selected_items()
        if len(chosen) <= 1:
            chosen = [item]
        many = len(chosen)
        folder_id = None
        if self._section.startswith("collection:"):
            folder_id = int(self._section[11:])
        entries: list[tuple[str | None, object]] = [
            ("Open", lambda: self._open_item(item)),
        ]
        folder = media_ops.media_folder(item)
        if folder is not None:
            entries.append(("Open Folder", lambda: media_ops.reveal(folder)))
        if self._collections:
            entries.append((f"Move {many} item(s) to folder…",
                            lambda: self._move_to_folder(chosen)))
        if item.get("media_type") == "series":
            entries.append(("Mark all episodes watched",
                            lambda: self._mark_series_watched(chosen)))
        else:
            watched = all(bool(i.get("watched")) for i in chosen)
            label = "Mark as Unwatched" if watched else "Mark as Watched"
            if many > 1:
                label = f"Mark {many} items as {'Unwatched' if watched else 'Watched'}"
            entries.append((label, lambda: self._set_watched(chosen, not watched)))
        if folder_id is not None:
            folder_items = [
                media for media in chosen
                if media.get("collection_id") == folder_id
            ]
            if folder_items:
                entries.append((None, None))
                entries.append((
                    "Remove from Folder" if len(folder_items) == 1
                    else f"Remove {len(folder_items)} items from Folder",
                    lambda selected=folder_items, cid=folder_id:
                        self._remove_from_folder(selected, cid),
                ))
        entries.append((None, None))
        entries.append(("Remove Metadata" if many == 1
                        else f"Remove Metadata of {many} items",
                        lambda: self._remove_metadata(chosen)))
        PopupMenu(self, x, y, entries)

    def _remove_from_folder(self, items: list[dict], collection_id: int) -> None:
        """Remove selected items from their current folder, keeping them in the library."""
        media_ids = [item["id"] for item in items]
        if not media_ids:
            return
        removed = self._db.remove_from_collection(media_ids, collection_id)
        if removed != len(media_ids):
            log.warning(
                "removed %s of %s selected items from folder %s; membership may have changed",
                removed, len(media_ids), collection_id,
            )
        self._load_from_db()
        if removed == len(media_ids):
            self._status_left.configure(
                text=f"Removed {removed} item(s) from folder; still in All Library."
            )
        else:
            self._status_left.configure(
                text=f"Removed {removed} of {len(media_ids)} item(s); "
                     "some folder assignments may have changed."
            )

    def _folder_menu(self, collection: dict, x: int, y: int) -> None:
        PopupMenu(self, x, y, [
            ("Rename folder…", lambda: self._rename_folder(collection)),
            (None, None),
            ("Delete folder", lambda: self._delete_folder(collection)),
        ])

    def _add_folder(self) -> None:
        """Create a folder in the app and file the chosen items into it."""
        dialog = FolderDialog(self, self._collections, self._all_items, create=True)
        dialog.grab_set()
        dialog.wait_window()
        if not dialog.chosen:
            return
        collection_id = self._db.create_collection(dialog.name)
        if dialog.item_ids:
            self._db.set_collection(dialog.item_ids, collection_id)
        self._load_from_db()

    def _move_to_folder(self, items: list[dict]) -> None:
        """File items into one of the folders, or take them out of one."""
        if not items:
            return
        dialog = FolderDialog(self, self._collections, items, item=items[0], create=False)
        dialog.grab_set()
        dialog.wait_window()
        if not dialog.chosen:
            return
        self._db.set_collection([i["id"] for i in items], dialog.collection_id)
        self._load_from_db()

    def _delete_folder(self, collection: dict) -> None:
        if not ConfirmDialog(
            self, "Delete folder",
            f"Delete the folder “{collection['name']}”?\n\n"
            f"The {collection['n']} item(s) in it stay in your library.",
            "Delete",
        ).ask():
            return
        if self._section == f"collection:{collection['id']}":
            self._set_section("all")
        self._db.delete_collection(collection["id"])
        self._load_from_db()

    def _rename_folder(self, collection: dict) -> None:
        name = PromptDialog(self, "Rename folder", "New name:",
                            collection["name"]).ask()
        if not name or name == collection["name"]:
            return
        if any(name.lower() == c["name"].lower() for c in self._collections
               if c["id"] != collection["id"]):
            return
        self._db.rename_collection(collection["id"], name)
        self._load_from_db()

    def _set_watched(self, items: list[dict], watched: bool) -> None:
        """Mark items as watched (or as unwatched)."""
        for item in items:
            self._db.set_media_watched(item["id"], watched)
        self._on_items_fetched([i["id"] for i in items])

    def _mark_series_watched(self, items: list[dict]) -> None:
        """Mark every episode of the series as watched."""
        ids = []
        for item in items:
            paths = [e["path"] for e in self._db.episodes(item["id"])]
            self._db.set_episodes_watched(paths, True)
            ids.append(item["id"])
        self._on_items_fetched(ids)

    def _remove_metadata(self, items: list[dict]) -> None:
        """Throw the fetched metadata of the items away."""
        for item in items:
            self._db.clear_metadata(item["id"])
            media_ops.remove_about(item)
        self._on_items_fetched([i["id"] for i in items])

    def _on_item_fetched(self, media_id: int) -> None:
        self._on_items_fetched([media_id])

    def _on_items_fetched(self, media_ids: "list[int]") -> None:
        """Refresh one or many items in the library list.

        A bulk fetch changes hundreds of items; refreshing them one by one
        re-filters and re-sorts the whole library every time.
        """
        fresh_items = self._db.get_many(media_ids)
        for media_id in media_ids:
            fresh = fresh_items.get(media_id)
            if fresh is None:
                continue
            fresh["_search"] = self._search_blob(fresh)
            fresh["_sort"] = str(fresh.get("title") or "").lower()
            position = self._positions.get(media_id)
            if position is not None:
                self._all_items[position] = fresh
        if self._library.winfo_ismapped():
            self._apply_filters()
        # Also refresh details view if it's showing this item
        if self._details.winfo_ismapped() and self._details._item is not None:
            if self._details._item.get("id") in media_ids:
                self._details._refresh_from_db(self._details._item["id"])

    def _on_filter_from_details(self, kind: str, value: str) -> None:
        self._show_library()
        self._set_section("all")
        self._set_chip(kind, value)

    def _render_insights(self) -> None:
        """Render an interactive, scrollable overview of the local library."""
        for child in self._insights.winfo_children():
            child.destroy()
        items = [
            item for item in self._all_items
            if item.get("media_type") in ("movie", "series")
        ]
        movies = [item for item in items if item.get("media_type") == "movie"]
        series = [item for item in items if item.get("media_type") == "series"]
        watched = sum(bool(item.get("seen")) for item in items)
        episodes_total = sum(int(item.get("episodes_total") or 0) for item in series)
        episodes_watched = sum(int(item.get("episodes_watched") or 0) for item in series)
        runtime_total = sum(int(item.get("runtime") or 0) for item in movies)
        rated = sum(item.get("my_rating") is not None for item in items)

        viewport = tk.Frame(self._insights, bg=BG)
        viewport.pack(fill="both", expand=True)
        canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(
            viewport, orient="vertical", command=canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        wheel_bindings = []
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            binding_id = canvas.bind_all(
                sequence,
                lambda event, v=viewport, c=canvas: self._on_insights_wheel(
                    event, v, c,
                ),
                add="+",
            )
            wheel_bindings.append((sequence, binding_id))
        canvas.bind(
            "<Destroy>",
            lambda event, c=canvas, bindings=wheel_bindings:
                self._unbind_insights_wheel(event, c, bindings),
        )
        layout = tk.Frame(canvas, bg=BG, padx=26, pady=24)
        window = canvas.create_window((0, 0), window=layout, anchor="nw")
        layout.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )

        heading = tk.Frame(layout, bg=BG)
        heading.pack(fill="x", pady=(0, 20))
        tk.Label(
            heading, text="Your library", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 22),
        ).pack(anchor="w")
        tk.Label(
            heading,
            text=f"{len(items):,} movies and series · a snapshot of your collection",
            bg=BG, fg=MUTED, font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(4, 0))

        cards = tk.Frame(layout, bg=BG)
        cards.pack(fill="x", pady=(0, 18))
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1, uniform="insight-card")

        def metric_card(column: int, title: str, value: str, note: str,
                        query: str, accent: str) -> None:
            card = tk.Frame(
                cards, bg="#12161d", highlightthickness=1,
                highlightbackground=BORDER, padx=16, pady=13,
            )
            card.grid(row=0, column=column, sticky="nsew",
                      padx=(0 if column == 0 else 6, 6 if column < 3 else 0))
            tk.Frame(card, bg=accent, height=3).pack(fill="x", pady=(0, 11))
            title_label = tk.Label(
                card, text=title.upper(), bg="#12161d", fg=MUTED,
                font=("Segoe UI Semibold", 8), anchor="w",
            )
            title_label.pack(fill="x")
            value_label = tk.Label(
                card, text=value, bg="#12161d", fg=TEXT,
                font=("Segoe UI Semibold", 23), anchor="w",
            )
            value_label.pack(fill="x", pady=(4, 0))
            note_label = tk.Label(
                card, text=note, bg="#12161d", fg=MUTED,
                font=("Segoe UI", 9), anchor="w",
            )
            note_label.pack(fill="x", pady=(2, 0))

            def open_filter(_event=None, q=query) -> None:
                self._insights_filter(q)

            def hover(_event=None, frame=card, labels=(title_label, value_label, note_label)):
                frame.configure(bg="#1a2230")
                for label in labels:
                    label.configure(bg="#1a2230")

            def leave(_event=None, frame=card, labels=(title_label, value_label, note_label)):
                frame.configure(bg="#12161d")
                for label in labels:
                    label.configure(bg="#12161d")

            for widget in (card, title_label, value_label, note_label):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", open_filter)
                widget.bind("<Enter>", hover)
                widget.bind("<Leave>", leave)

        metric_card(0, "Movies", f"{len(movies):,}", "Browse your films", "movie", ACCENT)
        metric_card(1, "Series", f"{len(series):,}", "Browse your shows", "series", "#7c9cff")
        metric_card(2, "Watched", f"{watched:,}", "Titles with viewing activity",
                    "watched:yes", "#55c49a")
        metric_card(3, "To watch", f"{len(items) - watched:,}", "Not started yet",
                    "unwatched:", "#e4b65c")

        progress_panel = tk.Frame(
            layout, bg="#12161d", highlightthickness=1, highlightbackground=BORDER,
            padx=18, pady=16,
        )
        progress_panel.pack(fill="x", pady=(0, 18))
        tk.Label(
            progress_panel, text="WATCHING PROGRESS", bg="#12161d", fg=TEXT,
            font=("Segoe UI Semibold", 10),
        ).pack(anchor="w")

        progress_rows = tk.Frame(progress_panel, bg="#12161d")
        progress_rows.pack(fill="x", pady=(12, 0))

        def progress_row(parent: tk.Frame, label: str, detail: str,
                         current: int, maximum: int) -> None:
            row = tk.Frame(parent, bg="#12161d")
            row.pack(fill="x", pady=(0, 9))
            top = tk.Frame(row, bg="#12161d")
            top.pack(fill="x", pady=(0, 5))
            tk.Label(top, text=label, bg="#12161d", fg=TEXT,
                     font=("Segoe UI", 9)).pack(side="left")
            tk.Label(top, text=detail, bg="#12161d", fg=MUTED,
                     font=("Segoe UI", 9)).pack(side="right")
            bar = ttk.Progressbar(
                row, maximum=max(maximum, 1), value=min(current, maximum),
                style="Dark.Horizontal.TProgressbar",
            )
            bar.pack(fill="x")

        progress_row(progress_rows, "Movies watched", f"{sum(bool(i.get('seen')) for i in movies):,} / {len(movies):,}",
                     sum(bool(i.get("seen")) for i in movies), len(movies))
        progress_row(progress_rows, "Episodes watched", f"{episodes_watched:,} / {episodes_total:,}",
                     episodes_watched, episodes_total)

        continue_items = sorted(
            (
                item for item in series
                if 0 < int(item.get("episodes_watched") or 0)
                < int(item.get("episodes_total") or 0)
            ),
            key=lambda item: item.get("last_watched") or 0,
            reverse=True,
        )
        continue_panel = tk.Frame(
            layout, bg="#12161d", highlightthickness=1,
            highlightbackground=BORDER, padx=18, pady=15,
        )
        continue_panel.pack(fill="x", pady=(0, 18))
        continue_header = tk.Frame(continue_panel, bg="#12161d")
        continue_header.pack(fill="x", pady=(0, 10))
        tk.Label(
            continue_header,
            text=f"Continue watching  ·  {len(continue_items):,}",
            bg="#12161d", fg=TEXT, font=("Segoe UI Semibold", 11),
        ).pack(side="left")
        tk.Button(
            continue_header, text="View all",
            command=self._insights_continue_filter,
            bg="#1a2130", fg=TEXT, activebackground="#243046",
            activeforeground=TEXT, bd=0, padx=12, pady=5,
            font=("Segoe UI", 8, "bold"), cursor="hand2",
        ).pack(side="right")
        continue_row = tk.Frame(continue_panel, bg="#12161d")
        continue_row.pack(fill="x")
        if not continue_items:
            tk.Label(
                continue_row, text="No series in progress right now.",
                bg="#12161d", fg=MUTED, font=("Segoe UI", 9),
            ).pack(anchor="w", pady=5)
        for index, item in enumerate(continue_items[:5]):
            card = tk.Frame(
                continue_row, bg="#171c25", highlightthickness=1,
                highlightbackground=BORDER, padx=11, pady=9,
            )
            card.pack(
                side="left", fill="both", expand=True,
                padx=(0 if index == 0 else 6, 6 if index < 4 else 0),
            )
            tk.Button(
                card, text=item.get("title") or "Untitled",
                command=lambda selected=item: self._open_item(selected),
                bg="#171c25", fg=TEXT, activebackground="#202938",
                activeforeground=TEXT, bd=0, anchor="w", justify="left",
                wraplength=150, font=("Segoe UI Semibold", 9), cursor="hand2",
            ).pack(fill="x", anchor="w")
            tk.Label(
                card,
                text=f"{int(item.get('episodes_watched') or 0)} of "
                     f"{int(item.get('episodes_total') or 0)} episodes watched",
                bg="#171c25", fg=MUTED, font=("Segoe UI", 8), anchor="w",
            ).pack(fill="x", pady=(5, 0))

        lower = tk.Frame(layout, bg=BG)
        lower.pack(fill="x")
        lower.grid_columnconfigure(0, weight=1, uniform="insight-panel")
        lower.grid_columnconfigure(1, weight=1, uniform="insight-panel")

        def panel(parent: tk.Frame, column: int, title: str) -> tk.Frame:
            frame = tk.Frame(
                parent, bg="#12161d", highlightthickness=1,
                highlightbackground=BORDER, padx=18, pady=15,
            )
            frame.grid(row=0, column=column, sticky="nsew",
                       padx=(0 if column == 0 else 7, 7 if column == 0 else 0))
            tk.Label(frame, text=title, bg="#12161d", fg=TEXT,
                     font=("Segoe UI Semibold", 11)).pack(anchor="w", pady=(0, 10))
            return frame

        recent_panel = panel(lower, 0, "Recently added")
        recent = sorted(items, key=lambda item: item.get("added_at") or 0, reverse=True)[:5]
        if not recent:
            tk.Label(recent_panel, text="Your library is empty so far.",
                     bg="#12161d", fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        for item in recent:
            year = item.get("year")
            subtitle = "Series" if item.get("media_type") == "series" else "Movie"
            if year:
                subtitle += f" · {year}"
            row = tk.Frame(recent_panel, bg="#12161d")
            row.pack(fill="x", pady=2)
            button = tk.Button(
                row, text=f"{item.get('title') or 'Untitled'}",
                command=lambda selected=item: self._open_item(selected),
                bg="#12161d", fg=TEXT, activebackground="#1a2230",
                activeforeground=TEXT, bd=0, anchor="w",
                font=("Segoe UI", 9, "bold"), cursor="hand2",
            )
            button.pack(side="left", fill="x", expand=True)
            tk.Label(row, text=subtitle, bg="#12161d", fg=MUTED,
                     font=("Segoe UI", 8)).pack(side="right")

        genre_panel = panel(lower, 1, "Popular genres")
        genre_counts: dict[str, int] = {}
        for item in items:
            genres = DetailsView._as_list(item.get("genres"))
            for genre in set(str(value).strip() for value in genres if str(value).strip()):
                genre_counts[genre] = genre_counts.get(genre, 0) + 1
        popular_genres = sorted(
            genre_counts.items(), key=lambda entry: (-entry[1], entry[0].casefold())
        )[:6]
        if not popular_genres:
            tk.Label(genre_panel, text="Genres appear here when metadata is available.",
                     bg="#12161d", fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        for genre, count in popular_genres:
            row = tk.Frame(genre_panel, bg="#12161d")
            row.pack(fill="x", pady=2)
            tk.Button(
                row, text=genre,
                command=lambda selected=genre: self._insights_genre_filter(selected),
                bg="#12161d", fg="#9bb8ff", activebackground="#1a2230",
                activeforeground=TEXT, bd=0, anchor="w",
                font=("Segoe UI", 9), cursor="hand2",
            ).pack(side="left", fill="x", expand=True)
            tk.Label(row, text=f"{count:,}", bg="#12161d", fg=MUTED,
                     font=("Segoe UI", 9)).pack(side="right")

        footer = tk.Frame(layout, bg=BG)
        footer.pack(fill="x", pady=(16, 0))
        tk.Label(
            footer,
            text=f"Known movie runtime  ·  {runtime_total // 60:,}h {runtime_total % 60:02d}m"
                 f"       ·       {rated:,} titles with your rating",
            bg=BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w")

    def _unbind_insights_wheel(self, event, canvas, bindings) -> None:
        if event.widget is not canvas:
            return
        from ui.window_utils import unbind_bindall
        for sequence, binding_id in bindings:
            unbind_bindall(canvas, sequence, binding_id)

    def _on_insights_wheel(self, event, viewport, canvas) -> str | None:
        if not viewport.winfo_ismapped():
            return None
        widget = event.widget
        while widget is not None and widget is not viewport:
            widget = getattr(widget, "master", None)
        if widget is not viewport:
            return None

        button = getattr(event, "num", None)
        if button == 4:
            units = -1
        elif button == 5:
            units = 1
        else:
            delta = getattr(event, "delta", 0)
            units = -int(delta / 120) if delta else 0
            if not units and delta:
                units = -1 if delta > 0 else 1
        if units:
            canvas.yview_scroll(units, "units")
        return "break"

    def _insights_filter(self, query: str) -> None:
        if query in ("movie", "series"):
            self._set_section(query)
            return
        self._clear_chip()
        self._set_section("all")
        self._insights_query = query.strip()
        self._apply_filters(reset_scroll=True)

    def _insights_continue_filter(self) -> None:
        self._clear_chip()
        self._set_section("all")
        self._insights_continue_only = True
        self._apply_filters(reset_scroll=True)

    def _insights_genre_filter(self, genre: str) -> None:
        self._clear_chip()
        self._set_section("all")
        self._insights_genre = genre
        self._apply_filters(reset_scroll=True)

    def _show_person_profile(self, name: str, role: str) -> None:
        """Show a person's cached or freshly fetched profile beside the grid."""
        self._show_library()
        self._set_section("all")
        self._set_chip(role, name)
        self._person_profile_generation += 1
        generation = self._person_profile_generation
        key = person_key(name)
        profile = self._db.get_person_profile(key)
        if profile is not None:
            self._library.show_person_profile(name, role, profile)
            return

        self._library.show_person_profile(name, role, status="Looking up Wikipedia…")

        def worker() -> None:
            error = None
            try:
                result = fetch_person_profile(name, role, config.PEOPLE_CACHE_DIR)
                self._db.save_person_profile(result)
            except Exception as exc:
                log.warning("Wikipedia profile lookup failed for %s: %s", name, exc)
                error = str(exc)

            def apply() -> None:
                if generation != self._person_profile_generation:
                    return
                profile = self._db.get_person_profile(key)
                if profile is not None:
                    self._library.show_person_profile(name, role, profile)
                else:
                    self._library.show_person_profile(
                        name, role, status=f"Could not load Wikipedia profile: {error}")

            self._post(apply)

        threading.Thread(target=self._db.thread_target(worker), daemon=True).start()

    def _set_chip(self, kind: str, value: str) -> None:
        self._chip_kind = kind
        self._chip_value = value
        self._chip_label.configure(text=value)
        self._chip_bar.pack(side="left", after=self._search.master)
        self._apply_filters(reset_scroll=True)

    def _clear_chip(self) -> None:
        self._chip_kind = None
        self._chip_value = None
        self._chip_bar.pack_forget()
        self._apply_filters(reset_scroll=True)

    def _chip_tokens(self, item: dict) -> list[str]:
        kind = self._chip_kind
        if kind == "genre":
            return [g.lower() for g in DetailsView._as_list(item.get("genres"))]
        if kind == "cast":
            fields = ("cast_list",)
        elif kind in ("director", "writer"):
            fields = (kind,)
        elif kind == "country":
            fields = ("country",)
        else:
            fields = ("director", "writer", "cast_list")
        names: list[str] = []
        for field in fields:
            raw = item.get(field)
            if not raw:
                continue
            for part in DetailsView._as_list(raw):
                names.extend(p.strip().lower() for p in str(part).split(",") if p.strip())
        return names

    def _chip_matches(self, item: dict) -> bool:
        value = str(self._chip_value or "").lower()
        if not value:
            return True
        return value in self._chip_tokens(item)

    def _open_settings(self) -> None:
        # Hide details first to clear backdrop immediately
        self._details.hide()
        self._details.pack_forget()
        self._library.pack_forget()
        self._insights.pack_forget()
        self._section = "all"
        self._insights_continue_only = False
        for btn in self._nav.values():
            btn.configure(fg=MUTED, bg=SIDEBAR_BG)
        # Pack settings - this will be drawn on next frame
        self._settings.pack(fill="both", expand=True)
        self._settings_btn.configure(fg=TEXT, bg="#1a2130")

    def _on_settings_saved(self) -> None:
        # the new folders live in config; pick them up before rescanning
        self._movies_dir = config.MOVIES_DIR
        self._series_dir = config.SERIES_DIR
        self._load_from_db()
        self._show_library()
        self._rescan_clicked()

    def _show_library(self, event=None) -> None:
        self._details.hide()
        self._details.pack_forget()
        self._settings.pack_forget()
        self._insights.pack_forget()
        self._settings_btn.configure(fg=MUTED, bg=SIDEBAR_BG)
        for key, button in self._nav.items():
            active = key == self._section
            button.configure(fg=TEXT if active else MUTED,
                             bg="#1a2130" if active else SIDEBAR_BG)
        self._library.pack(fill="both", expand=True)
        self._apply_filters()

    def _escape(self, event=None) -> None:
        if not self._library.winfo_ismapped():
            self._show_library()

    # --- scanning -----------------------------------------------------

    def _rescan_clicked(self) -> None:
        if self._closing:
            return
        if self._scan_running:
            # a scan is already on its way - run this one after it, so a
            # new folder is never left unindexed
            self._rescan_requested = True
            return
        self._scan_running = True
        self._rescan_btn.configure(state="disabled", text="Scanning…")
        self._status_left.configure(text="Scanning library…")
        self._scan_progress.pack(side="left", padx=(0, 12))
        started = time.perf_counter()
        movies_dir = self._movies_dir
        series_dir = self._series_dir
        if not movies_dir and not series_dir:
            # nothing to scan - that is not a failure
            self._scan_running = False
            self._rescan_btn.configure(state="normal", text="Rescan Library")
            self._scan_progress.pack_forget()
            self._status_left.configure(
                text="No media folder is set - choose one in Settings.")
            return
        configured = {str(d) for d in (movies_dir, series_dir) if d}
        previous = scan_state.load_scan_state()
        current = {}
        for root in (movies_dir, series_dir):
            if root is None:
                continue
            key = scan_state.root_key(root)
            sig = scan_state.root_signature(root)
            if sig is not None:
                current[key] = sig
        if previous and current and all(current.get(scan_state.root_key(root)) == previous.get(scan_state.root_key(root)) for root in (movies_dir, series_dir) if root is not None):
            self._scan_running = False
            self._rescan_btn.configure(state="normal", text="Rescan Library")
            self._scan_progress.pack_forget()
            self._status_left.configure(text="Library up to date - no changes detected.")
            return

        def worker() -> None:
            def progress(done: int, total: int) -> None:
                self._post(lambda d=done, t=total: self._scan_progress_state(d, t))

            walked: set[str] = set()
            unreadable: set[str] = set()
            try:
                items = scan(movies_dir, series_dir, progress=progress,
                             walked=walked, unreadable=unreadable)
            except Exception:
                log.exception("library scan failed")
                self._post(self._scan_failed)
                return
            seconds = time.perf_counter() - started
            if not walked:
                # not a single root could be read: the library is left alone
                # rather than emptied
                log.warning("scan read no root at all - library left untouched")
                self._post(self._scan_failed)
                return
            counts: dict[str, int] = {}
            if not self._closing:
                try:
                    counts = self._db.sync(items, configured, walked, unreadable)
                except Exception:
                    log.exception("library sync failed")

            def done() -> None:
                if self._closing:
                    return
                self._scan_running = False
                self._rescan_btn.configure(state="normal", text="Rescan Library")
                self._scan_progress.pack_forget()
                next_state = scan_state.load_scan_state()
                for root in (movies_dir, series_dir):
                    if root is None:
                        continue
                    sig = scan_state.root_signature(root)
                    if sig is not None:
                        next_state[scan_state.root_key(root)] = sig
                scan_state.save_scan_state(next_state)
                self._status_left.configure(
                    text=f"Scan finished in {seconds:.1f}s — "
                         f"{counts.get('movie', 0)} movies, "
                         f"{counts.get('series', 0)} series"
                         + self._diff_text(counts.get("diff"))
                )
                log.info("scan finished in %.1fs: %s items (%s)", seconds, len(items), counts)
                self._load_from_db()
                if self._rescan_requested:
                    self._rescan_requested = False
                    self._rescan_clicked()

            self._post(done)

        threading.Thread(target=self._db.thread_target(worker), daemon=True).start()

    @staticmethod
    def _diff_text(diff) -> str:
        """What the scan changed - only the parts that are worth a word."""
        if not diff:
            return ""
        parts = []
        if diff.get("new"):
            parts.append(f"{diff['new']} new")
        if diff.get("gone"):
            parts.append(f"{diff['gone']} gone")
        if diff.get("changed"):
            parts.append(f"{diff['changed']} changed")
        return " · " + ", ".join(parts) if parts else ""

    def _surprise_me(self) -> None:
        """Pick randomly from the current filtered set, preferring unwatched titles."""
        current_search = self._search.get().strip()
        if current_search != self._search_text:
            self._search_text = current_search
            self._apply_filters(reset_scroll=True)
        items = list(self._visible_items)
        if not items:
            self._status_left.configure(
                text="No results match the current library filters.")
            return
        explicit_watch_filter = (
            "watched:" in self._search_text.lower()
            or "unwatched:" in self._search_text.lower()
            or "watched:" in self._insights_query.lower()
        )
        candidates = (
            items if explicit_watch_filter
            else [item for item in items if not item.get("seen")]
        )
        if not candidates and explicit_watch_filter:
            self._status_left.configure(
                text="No titles match the current library filters.")
            return
        item = random.choice(candidates or items)
        self._open_item(item)

    def _scan_failed(self) -> None:
        """The scan could not read a single root - keep the library as it is."""
        if self._closing:
            return
        self._scan_running = False
        self._rescan_btn.configure(state="normal", text="Rescan Library")
        self._scan_progress.pack_forget()
        self._status_left.configure(
            text="Scan failed - the library was left unchanged. "
                 "Check the folder paths in Settings.")
        log.warning("library scan failed - library left unchanged")

    def _scan_progress_state(self, done: int, total: int) -> None:
        if self._closing:
            return
        self._scan_progress.configure(maximum=max(total, 1), value=done)

    def close(self) -> None:
        self._closing = True
        stats = self._images.snapshot_stats()
        log.info(
            "image cache stats: hits=%(hits)s misses=%(misses)s generated=%(generated)s "
            "disk_hits=%(disk_hits)s errors=%(errors)s gen_ms=%(gen_ms)s",
            stats,
        )
        self._images.close()
