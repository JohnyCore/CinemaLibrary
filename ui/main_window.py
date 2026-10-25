from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Callable

import config
from database.db_manager import Database
from scanner.library_scanner import scan
from ui.details_view import DetailsView
from ui.dialogs import ConfirmDialog, PopupMenu, PromptDialog
from ui.folder_dialog import FolderDialog
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
        self._wake = threading.Event()
        self._images = ImageCache(config.THUMB_CACHE_DIR, self._post,
                                  config.PLACEHOLDER_PATH)
        self._scan_running = False
        self._rescan_requested = False
        self._closing = False
        self._drain_pending = False
        self._search_job: str | None = None
        self._search_text = ""
        self._section = "all"
        self._sort_key = 0
        self._all_items: list[dict] = []
        self._positions: dict[int, int] = {}
        self._chip_kind: str | None = None
        self._chip_value: str | None = None
        self._collections: list[dict] = []

        setup_styles(root)

        self.pack(fill="both", expand=True)
        self._build_sidebar()
        self._build_main()
        self._set_section(self._section)

        self._load_from_db()
        root.after(70, self._drain)
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
        self._nav: dict[str, tk.Button] = {}
        for key, label in SECTIONS:
            self._nav_button(bar, label, key)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", pady=(12, 8), padx=14)
        tk.Button(
            bar, text="+ Add Folder", command=self._add_folder,
            bg="#161d2a", fg=TEXT, activebackground="#243046",
            activeforeground=TEXT, bd=0, anchor="w", padx=16, pady=6,
            font=("Segoe UI Semibold", 11), cursor="hand2",
        ).pack(fill="x", padx=8)
        self._folder_box = tk.Frame(bar, bg=SIDEBAR_BG)
        self._folder_box.pack(fill="x")
        spacer = tk.Frame(bar, bg=SIDEBAR_BG)
        spacer.pack(fill="both", expand=True)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(0, 8))
        self._settings_btn = tk.Button(
            bar, text="Settings", command=self._open_settings,
            bg=SIDEBAR_BG, fg=MUTED, activebackground=SIDEBAR_BG,
            activeforeground=TEXT, bd=0, anchor="w", padx=16, pady=6,
            font=("Segoe UI Semibold", 11), cursor="hand2",
        )
        self._settings_btn.pack(fill="x", padx=8, pady=(0, 10))

    def _nav_button(self, parent, label: str, key: str) -> tk.Button:
        row = tk.Frame(parent, bg=SIDEBAR_BG)
        row.pack(fill="x", padx=8, pady=1)
        btn = tk.Button(
            row, text=label, command=lambda k=key: self._set_section(k),
            bg=SIDEBAR_BG, fg=MUTED, activebackground=SIDEBAR_BG,
            activeforeground=TEXT, bd=0, anchor="w", padx=8, pady=6,
            font=("Segoe UI Semibold", 11), cursor="hand2",
        )
        btn.pack(side="left", fill="x", expand=True)
        self._nav[key] = btn
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
            post=self._post,
        )
        self._library = LibraryView(body, self._images, self._open_item,
                                    self._show_context_menu)
        self._settings = SettingsView(body, self._on_settings_saved, self._db, self._images,
                                  on_changed=self._on_items_fetched, post=self._post,
                                  on_rescan=self._rescan_clicked,
                                  on_open_item=self._open_item)
        self._library.pack(fill="both", expand=True)

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
        """Queue a callback for the main thread.

        Safe to call from a worker thread: Tk is only ever touched from the
        main thread, which wakes up on the next tick and drains the queue.
        """
        self._q.put(fn)
        self._wake.set()

    def _drain(self) -> None:
        self._drain_pending = False
        for _ in range(300):
            try:
                fn = self._q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("queued callback failed")
        if not self._q.empty() and not self._closing:
            self._drain_pending = True
            self.after_idle(self._drain)

    def _tick(self) -> None:
        """Main-thread timer: wakes the drain and refreshes the counters."""
        if self._closing:
            return
        if self._wake.is_set():
            self._wake.clear()
            if not self._drain_pending:
                self._drain_pending = True
                self.after_idle(self._drain)
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
        if self._search_text or self._chip_kind:
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

    def _set_section(self, section: str) -> None:
        self._section = section
        for key, btn in self._nav.items():
            active = key == section
            btn.configure(
                fg=TEXT if active else MUTED,
                bg="#1a2130" if active else SIDEBAR_BG,
            )
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
        entries.append((None, None))
        entries.append(("Remove Metadata" if many == 1
                        else f"Remove Metadata of {many} items",
                        lambda: self._remove_metadata(chosen)))
        PopupMenu(self, x, y, entries)

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
        # Pack settings - this will be drawn on next frame
        self._settings.pack(fill="both", expand=True)
        self._settings_btn.configure(fg=TEXT, bg="#1a2130")

    def _on_settings_saved(self) -> None:
        # the new folders live in config; pick them up before rescanning
        self._movies_dir = config.MOVIES_DIR
        self._series_dir = config.SERIES_DIR
        self._show_library()
        self._rescan_clicked()

    def _show_library(self, event=None) -> None:
        self._details.hide()
        self._details.pack_forget()
        self._settings.pack_forget()
        self._settings_btn.configure(fg=MUTED, bg=SIDEBAR_BG)
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

        threading.Thread(target=worker, daemon=True).start()

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
        """Pick a random movie that was never watched and show it."""
        item = self._db.random_unwatched_movie()
        if item is None:
            self._status_left.configure(
                text="Every movie is marked as watched - nothing left to surprise "
                     "you with.")
            return
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
