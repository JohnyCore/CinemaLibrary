from __future__ import annotations

import logging
import json
import threading
import time
import tkinter as tk
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from tkinter import filedialog, ttk

import config
from database.db_manager import Database
from metadata import metadata_service
from metadata import true_story
from metadata.person_profiles import fetch_person_profile, person_key
from utils import media_ops
from utils.image_cache import BACKDROP_SIZES, ImageCache
from utils.image_utils import find_artwork
from ui.dialogs import ConfirmDialog, DuplicateResolver, ImdbIdDialog, PlayerDialog
from ui.ep_renamer_view import EpRenamerView
from ui.library_view import POSTER_H, POSTER_W
from ui.organizer_view import OrganizerView

log = logging.getLogger(__name__)

BG = "#0b0d10"
PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
FIELD_BG = "#0a0c10"
BTN_BG = "#1a2130"
BTN_ACTIVE = "#243046"
ACCENT = "#e5484d"
LINK = "#6aa4ff"
SIDEBAR_BG = "#101319"

FETCH_WORKERS = 4


class SettingsView(tk.Frame):
    """Full-page settings, shown inside the main content area."""

    def __init__(self, master, on_saved, db: Database, images: ImageCache,
                 on_changed=None,
                 post=None,
                 on_rescan=None,
                 on_open_item=None) -> None:
        super().__init__(master, bg=BG, highlightthickness=0, bd=0)
        self._on_saved = on_saved
        self._on_changed = on_changed
        self._on_rescan = on_rescan
        self._on_open_item = on_open_item
        self._db = db
        self._images = images
        self._post = post or (lambda fn: self.after(0, fn))
        self._fetch_jobs: set[int] = set()
        self._bulk_items: list[dict] = []
        self._bulk_done: set[int] = set()
        self._bulk_changed: list[int] = []
        self._bulk_failures: list[tuple[str, str]] = []
        self._bulk_failed_ids: list[int] = []
        self._bulk_row_status: dict[int, str] = {}
        self._meta_items: list[dict] = []
        self._bulk_running = False
        self._bulk_cancel = threading.Event()
        self._people_items: list[dict] = []
        self._people_running = False
        self._people_cancel = threading.Event()
        self._drag_select_anchor: str | None = None

        header = tk.Frame(self, bg=BG)
        header.pack(fill="x")
        tk.Label(header, text="Settings", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 18)).pack(anchor="w", padx=32, pady=(24, 8))
        tk.Label(header, text="Manage library locations and cached artwork.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", padx=32)

        # Custom tab strip (plain tk buttons - no theme borders or scaling quirks)
        self._tab_strip = tk.Frame(self, bg=BG, highlightthickness=0, bd=0)
        self._tab_strip.pack(fill="x", padx=32, pady=(14, 0))
        self._tab_buttons: dict[str, tk.Button] = {}
        for key, text in (("general", "General"), ("bulk", "Bulk Fetch"),
                          ("meta", "Metadata Editor"), ("organizer", "Organizer"),
                          ("renamer", "EP Renamer"),
                          ("deeper", "Dig Deeper"), ("quotes", "Wikiquote"),
                          ("health", "Library Health")):
            btn = tk.Button(
                self._tab_strip, text=text, command=lambda k=key: self._select_tab(k),
                bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
                bd=0, highlightthickness=0, padx=18, pady=9,
                font=("Segoe UI", 10, "bold"), cursor="hand2",
            )
            btn.pack(side="left")
            self._tab_buttons[key] = btn
        self._tab_indicator = tk.Frame(self._tab_strip, bg=ACCENT, height=2,
                                        highlightthickness=0, bd=0)

        # Tab content area
        self._tab_body = tk.Frame(self, bg=PANEL_BG, highlightthickness=0, bd=0)
        self._tab_body.pack(fill="both", expand=True, padx=32, pady=(0, 24))
        self._tab_frames: dict[str, tk.Frame] = {}

        # --- General tab ---
        general = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["general"] = general
        self._build_general(general)

        # --- Bulk Fetch tab ---
        bulk = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["bulk"] = bulk
        self._build_bulk_fetch(bulk)

        # --- Metadata Editor tab ---
        meta = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["meta"] = meta
        self._build_metadata_editor(meta)

        # --- Organizer tab ---
        organizer = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["organizer"] = organizer
        self._organizer = OrganizerView(organizer, on_rescan=self._on_rescan,
                                         post=self._post)
        self._organizer.pack(fill="both", expand=True)

        # --- EP Renamer tab ---
        renamer = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["renamer"] = renamer
        self._renamer = EpRenamerView(renamer, db=self._db, post=self._post)
        self._renamer.pack(fill="both", expand=True)

        # --- Dig Deeper tab ---
        deeper = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["deeper"] = deeper
        self._build_dig_deeper(deeper)

        # --- Wikiquote tab ---
        quotes = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["quotes"] = quotes
        self._build_wikiquote(quotes)

        # --- Library Health tab ---
        health = tk.Frame(self._tab_body, bg=PANEL_BG)
        self._tab_frames["health"] = health
        self._build_library_health(health)

        self._select_tab("general")

        # List styling (scrollbar reuses the shared Dark.Vertical.TScrollbar style)
        style = ttk.Style(self)
        style.configure("Settings.Treeview",
                        background=PANEL_BG, fieldbackground=PANEL_BG,
                        foreground=TEXT, borderwidth=0, bordercolor=PANEL_BG,
                        lightcolor=PANEL_BG, darkcolor=PANEL_BG, rowheight=26)
        style.map("Settings.Treeview",
                  background=[("selected", "#243041")],
                  foreground=[("selected", TEXT)])
        style.configure("Settings.Treeview.Heading",
                        background="#181d27", foreground="#c3cad6",
                        borderwidth=0, relief="flat", padding=(6, 8),
                        font=("Segoe UI", 9, "bold"))
        style.map("Settings.Treeview.Heading",
                  background=[("active", "#1e2531")],
                  foreground=[("active", TEXT)])
        style.configure("Settings.Treeview.Heading", bordercolor="#181d27",
                        lightcolor="#181d27", darkcolor="#181d27")
        style.map("Settings.Treeview.Heading", bordercolor=[("active", "#1e2531")],
                  lightcolor=[("active", "#1e2531")], darkcolor=[("active", "#1e2531")])

    def _select_tab(self, key: str) -> None:
        """Switch the visible settings tab."""
        self._tab = key
        for name, btn in self._tab_buttons.items():
            btn.configure(fg=ACCENT if name == key else MUTED)
        for name, frame in self._tab_frames.items():
            if name == key:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        self.after_idle(self._place_tab_indicator)
        if key == "bulk":
            self._refresh_bulk_list()
            if self._people_page.winfo_ismapped():
                self._refresh_people_list()
        elif key == "meta":
            self._refresh_meta_list()
        elif key == "organizer":
            self._organizer.refresh()
        elif key == "renamer":
            self._renamer.refresh()
        elif key == "deeper":
            count = self._refresh_deeper_list()
            self._deeper_status.configure(
                text=f"{count} item(s) are based on a true story.")
        elif key == "quotes":
            self._refresh_quote_list()
        elif key == "health":
            self._refresh_health()

    def _place_tab_indicator(self) -> None:
        """Move the accent underline under the active tab."""
        btn = self._tab_buttons.get(self._tab)
        if btn is None:
            return
        self._tab_indicator.place(
            x=btn.winfo_x(), y=btn.winfo_height() - 2,
            width=btn.winfo_width(), height=2,
        )

    # --- General tab --------------------------------------------------

    def _build_general(self, parent: tk.Frame) -> None:
        inner = tk.Frame(parent, bg=PANEL_BG, padx=22, pady=20)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="Media folders", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(
            inner,
            text="Point Cinema at your existing collection.\n"
                 "Changes take effect after a rescan.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(2, 14))

        current = config.load_settings()
        self._movies_var = tk.StringVar(value=str(current.get("movies_dir", "")))
        self._series_var = tk.StringVar(value=str(current.get("series_dir", "")))
        self._organizer_var = tk.StringVar(value=str(current.get("organizer_dir", "")))
        self._folder_row(inner, "Movies folder", self._movies_var)
        self._folder_row(inner, "TV Series folder", self._series_var)
        self._folder_row(inner, "Organizer folder", self._organizer_var)

        btns = tk.Frame(inner, bg=PANEL_BG)
        btns.pack(anchor="w", pady=(12, 0))
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=16, pady=7, font=("Segoe UI", 10), cursor="hand2",
        )
        tk.Button(btns, text="Save", command=self._save, **btn_style).pack(
            side="left", padx=(0, 10)
        )
        tk.Button(btns, text="Clear Thumbnail Cache", command=self._clear_cache,
                  **btn_style).pack(side="left", padx=(0, 10))
        tk.Button(btns, text="Prune Stale Thumbnails", command=self._prune_cache,
                  **btn_style).pack(side="left")

        self._status = tk.Label(inner, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                anchor="w", justify="left")
        self._status.pack(anchor="w", pady=(12, 0))
        self._cache_info = tk.Label(inner, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                    anchor="w", justify="left")
        self._cache_info.pack(anchor="w")
        self._show_cache_info()

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(18, 14))
        tk.Label(inner, text="Video player", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(
            inner,
            text="Videos are opened with the player you choose here.\n"
                 "Add the program of any player you have installed.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(2, 8))
        player_row = tk.Frame(inner, bg=PANEL_BG)
        player_row.pack(anchor="w")
        self._player_combo = ttk.Combobox(
            player_row, state="readonly", width=36, style="Dark.TCombobox")
        self._player_combo.pack(side="left")
        self._player_combo.bind("<<ComboboxSelected>>", self._on_player)
        tk.Button(player_row, text="Add player…", command=self._add_player,
                  **btn_style).pack(side="left", padx=(10, 0))
        tk.Button(player_row, text="Remove", command=self._remove_player,
                  **btn_style).pack(side="left", padx=(8, 0))
        self._refresh_players()

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(18, 14))
        tk.Label(inner, text="Metadata provider", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        provider = self._provider_name()
        tk.Label(
            inner,
            text=f"Connected provider: {provider}\n"
                 "API keys, when a provider needs one, belong in settings.json "
                 "(never in source code).",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(2, 0))

    # --- the video player ---------------------------------------------

    def _refresh_players(self) -> None:
        """Fill the list of players and show the one in use."""
        players = config.PLAYERS
        names = ["System default"] + [p.get("name") or p.get("path") for p in players]
        self._player_combo.configure(values=names)
        index = 0
        for i, player in enumerate(players, start=1):
            if player.get("path") == config.PLAYER:
                index = i
                break
        self._player_combo.current(index)

    def _on_player(self, event=None) -> None:
        """Use the player that is picked from now on."""
        index = self._player_combo.current()
        players = config.PLAYERS
        path = players[index - 1].get("path") if 0 < index <= len(players) else ""
        if not config.save_settings({"player": path}):
            self._status.configure(
                text=f"Could not write {config.SETTINGS_PATH} - "
                     "the player was NOT changed.")

    def _add_player(self) -> None:
        """Add a player of your own: a name and the program."""
        dialog = PlayerDialog(self)
        dialog.grab_set()
        dialog.wait_window()
        if not dialog.result:
            return
        players = list(config.PLAYERS) + [dialog.result]
        if not config.save_settings({"players": players,
                                     "player": dialog.result["path"]}):
            self._status.configure(
                text=f"Could not write {config.SETTINGS_PATH} - "
                     "the player was NOT saved.")
            return
        self._refresh_players()
        self._status.configure(
            text=f"Videos are now opened with {dialog.result['name']}.")

    def _remove_player(self) -> None:
        """Forget a player of your own."""
        index = self._player_combo.current()
        players = config.PLAYERS
        if not (0 < index <= len(players)):
            self._status.configure(text="Pick one of your own players first.")
            return
        gone = players[index - 1]
        rest = [p for p in players if p.get("path") != gone.get("path")]
        chosen = "" if config.PLAYER == gone.get("path") else config.PLAYER
        if not config.save_settings({"players": rest, "player": chosen}):
            self._status.configure(
                text=f"Could not write {config.SETTINGS_PATH} - "
                     "the player was NOT removed.")
            return
        self._refresh_players()
        self._status.configure(text=f"{gone.get('name')} was removed.")

    def _provider_name(self) -> str:
        try:
            service = metadata_service.get_service()
        except metadata_service.MetadataError:
            return "none - the app runs offline"
        return getattr(service, "provider", None) or type(service).__name__

    def _folder_row(self, parent, label: str, var: tk.StringVar) -> None:
        tk.Label(parent, text=label, bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")
        row = tk.Frame(parent, bg=PANEL_BG)
        row.pack(anchor="w", pady=(2, 12))
        entry = tk.Entry(
            row, textvariable=var, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            bd=0, highlightthickness=1, highlightbackground=BORDER,
            font=("Segoe UI", 10), width=52,
        )
        entry.pack(side="left", ipady=5)
        tk.Button(
            row, text="Browse…", command=lambda: self._browse(var),
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=14, pady=5, font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

    def _browse(self, var: tk.StringVar) -> None:
        start = var.get()
        if not start or not Path(start).exists():
            start = str(Path.home())
        chosen = filedialog.askdirectory(parent=self, initialdir=start,
                                         title="Select folder")
        if chosen:
            var.set(chosen)

    def _save(self) -> None:
        folders = {
            "movies_dir": self._movies_var.get().strip(),
            "series_dir": self._series_var.get().strip(),
            "organizer_dir": self._organizer_var.get().strip(),
        }
        for label, path in folders.items():
            if path and not Path(path).is_dir():
                if not self._confirm(
                    "Folder missing",
                    f"The {label} does not exist or is not a folder:\n{path}\n\n"
                    "Save anyway? The library will appear empty.",
                    confirm_text="Save anyway",
                ):
                    return
        if not config.save_settings(folders):
            self._status.configure(
                text=f"Could not write {config.SETTINGS_PATH} - "
                     "the settings were NOT saved.")
            return
        self._status.configure(text="Settings saved. Library is being rescanned…")
        if self._on_saved is not None:
            self._on_saved()

    def _show_cache_info(self) -> None:
        """Show how big the thumbnail cache is."""
        count = 0
        size = 0
        try:
            for f in config.THUMB_CACHE_DIR.iterdir():
                if f.is_file():
                    count += 1
                    size += f.stat().st_size
        except OSError:
            pass
        self._cache_info.configure(
            text=f"Thumbnail cache: {count} file(s), {size / (1024 * 1024):.1f} MB")

    def _clear_cache(self) -> None:
        removed = self._images.clear_disk_cache()
        self._images.clear_memory()
        self._show_cache_info()
        self._status.configure(text=f"Removed {removed} cached thumbnails. "
                                    "They regenerate automatically while you browse.")

    def _prune_cache(self) -> None:
        removed = self._images.prune_disk_cache({
            f"_thumb_{POSTER_W}x{POSTER_H}",
            "_thumb_148x222",
            "_thumb_520x780",
            *(f"_backdrop_{w}x{h}" for w, h in BACKDROP_SIZES),
        })
        self._show_cache_info()
        self._status.configure(text=f"Removed {removed} stale cached images.")

    # --- Bulk Fetch tab -----------------------------------------------

    def _build_bulk_fetch(self, parent: tk.Frame) -> None:
        inner = tk.Frame(parent, bg=PANEL_BG, padx=16, pady=16)
        inner.pack(fill="both", expand=True)

        section_bar = tk.Frame(inner, bg=PANEL_BG)
        section_bar.pack(fill="x", pady=(0, 12))
        section_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE,
            activeforeground=TEXT, bd=0, padx=12, pady=6,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._bulk_section_btn = tk.Button(
            section_bar, text="Movies & Series",
            command=lambda: self._select_bulk_page("media"), **section_style,
        )
        self._bulk_section_btn.pack(side="left", padx=(0, 8))
        self._people_section_btn = tk.Button(
            section_bar, text="People Profiles",
            command=lambda: self._select_bulk_page("people"), **section_style,
        )
        self._people_section_btn.pack(side="left")

        media_page = tk.Frame(inner, bg=PANEL_BG)
        media_page.pack(fill="both", expand=True)
        self._bulk_media_page = media_page
        self._people_page = tk.Frame(inner, bg=PANEL_BG)

        header = tk.Frame(media_page, bg=PANEL_BG)
        header.pack(fill="x", pady=(0, 12))

        tk.Label(header, text="Unfetched items", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        self._bulk_status = tk.Label(header, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                     anchor="w", justify="left")
        self._bulk_status.pack(side="left", padx=(10, 0))

        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=12, pady=5, font=("Segoe UI", 9), cursor="hand2",
        )
        self._bulk_refresh_btn = tk.Button(
            header, text="Refresh", command=self._refresh_bulk_list, **btn_style)
        self._bulk_refresh_btn.pack(side="right", padx=(8, 0))
        self._cancel_btn = tk.Button(header, text="Stop", command=self._cancel_bulk,
                                     state="disabled", **btn_style, disabledforeground="#5a6472")
        self._cancel_btn.pack(side="right", padx=(8, 0))
        self._fetch_selected_btn = tk.Button(header, text="Fetch Selected",
                                             command=self._fetch_selected_bulk,
                                             state="disabled", **btn_style,
                                             disabledforeground="#5a6472")
        self._fetch_selected_btn.pack(side="right", padx=(8, 0))
        self._fetch_all_btn = tk.Button(header, text="Fetch All", command=self._fetch_all_bulk,
                                        state="disabled", **btn_style, disabledforeground="#5a6472")
        self._fetch_all_btn.pack(side="right", padx=(8, 0))
        self._retry_btn = tk.Button(header, text="Retry failed", command=self._retry_failed_bulk,
                                    state="disabled", **btn_style, disabledforeground="#5a6472")
        self._retry_btn.pack(side="right", padx=(8, 0))

        self._progress = ttk.Progressbar(header, mode="determinate", length=160,
                                         style="Dark.Horizontal.TProgressbar")

        # Treeview for performant scrolling
        vsb = ttk.Scrollbar(media_page, orient="vertical",
                             command=lambda *a: self._bulk_tree.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.pack(side="right", fill="y")
        wrap = tk.Frame(media_page, bg=PANEL_BG,
                        highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(side="left", fill="both", expand=True)
        columns = ("title", "type", "missing", "status")
        self._bulk_tree = ttk.Treeview(wrap, columns=columns, show="headings",
                                       style="Settings.Treeview", height=20,
                                       selectmode="extended")
        self._bulk_tree.heading("title", text="Title")
        self._bulk_tree.heading("type", text="Type")
        self._bulk_tree.heading("missing", text="Missing")
        self._bulk_tree.heading("status", text="Status")
        self._bulk_tree.column("title", width=330, stretch=True)
        self._bulk_tree.column("type", width=80, stretch=False)
        self._bulk_tree.column("missing", width=340, stretch=True)
        self._bulk_tree.column("status", width=200, stretch=True)
        self._bulk_tree.tag_configure("odd", background="#151a24")
        self._bulk_tree.configure(yscrollcommand=vsb.set)
        self._bulk_tree.pack(side="left", fill="both", expand=True)
        self._bulk_tree.bind("<Double-1>", self._on_bulk_double_click)
        self._bulk_tree.bind("<<TreeviewSelect>>", self._on_bulk_select)
        # drag selection: hold the mouse and drag over the rows
        self._bulk_tree.bind("<Button-1>", self._on_tree_drag_start, add="+")
        self._bulk_tree.bind("<B1-Motion>", self._on_tree_drag_select, add="+")

        self._build_people_fetch(self._people_page)
        self._select_bulk_page("media")

    def _build_people_fetch(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, bg=PANEL_BG)
        header.pack(fill="x", pady=(0, 8))
        tk.Label(header, text="People profiles", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        self._people_status = tk.Label(
            header, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
            anchor="w", justify="left",
        )
        self._people_status.pack(side="left", padx=(10, 0))
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=12, pady=5, font=("Segoe UI", 9), cursor="hand2",
            disabledforeground="#5a6472",
        )
        self._people_refresh_btn = tk.Button(
            header, text="Refresh", command=self._refresh_people_list, **btn_style)
        self._people_refresh_btn.pack(side="right", padx=(8, 0))
        self._people_stop_btn = tk.Button(
            header, text="Stop", command=self._cancel_people_fetch,
            state="disabled", **btn_style,
        )
        self._people_stop_btn.pack(side="right", padx=(8, 0))
        self._people_all_btn = tk.Button(
            header, text="Fetch All", command=self._fetch_all_people,
            state="disabled", **btn_style,
        )
        self._people_all_btn.pack(side="right", padx=(8, 0))
        self._people_selected_btn = tk.Button(
            header, text="Fetch Selected", command=self._fetch_selected_people,
            state="disabled", **btn_style,
        )
        self._people_selected_btn.pack(side="right", padx=(8, 0))
        self._people_progress = ttk.Progressbar(
            header, mode="determinate", length=150,
            style="Dark.Horizontal.TProgressbar",
        )

        tk.Label(
            parent,
            text="Profiles are optional. Wikipedia biographies and photos are saved locally "
                 "for offline viewing.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 10))
        vsb = ttk.Scrollbar(parent, orient="vertical",
                            command=lambda *a: self._people_tree.yview(*a),
                            style="Dark.Vertical.TScrollbar")
        vsb.pack(side="right", fill="y")
        wrap = tk.Frame(parent, bg=PANEL_BG,
                        highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(side="left", fill="both", expand=True)
        columns = ("name", "roles", "status")
        self._people_tree = ttk.Treeview(
            wrap, columns=columns, show="headings", style="Settings.Treeview",
            height=20, selectmode="extended",
        )
        for column, title in (("name", "Name"), ("roles", "Roles"),
                              ("status", "Profile")):
            self._people_tree.heading(column, text=title)
        self._people_tree.column("name", width=260, stretch=True)
        self._people_tree.column("roles", width=180, stretch=True)
        self._people_tree.column("status", width=120, stretch=False)
        self._people_tree.tag_configure("odd", background="#151a24")
        self._people_tree.configure(yscrollcommand=vsb.set)
        self._people_tree.pack(side="left", fill="both", expand=True)
        self._people_tree.bind("<<TreeviewSelect>>", self._on_people_select)
        self._people_tree.bind("<Button-1>", self._on_tree_drag_start, add="+")
        self._people_tree.bind("<B1-Motion>", self._on_tree_drag_select, add="+")

    def _select_bulk_page(self, page: str) -> None:
        if page == "people":
            self._bulk_media_page.pack_forget()
            self._people_page.pack(fill="both", expand=True)
            self._people_section_btn.configure(fg=TEXT)
            self._bulk_section_btn.configure(fg=MUTED)
            if not self._people_running:
                self._refresh_people_list()
        else:
            self._people_page.pack_forget()
            self._bulk_media_page.pack(fill="both", expand=True)
            self._bulk_section_btn.configure(fg=TEXT)
            self._people_section_btn.configure(fg=MUTED)
            if not self._people_running:
                self._refresh_bulk_list()

    def _refresh_people_list(self) -> None:
        """List unique people found in metadata already stored by the user."""
        if self._people_running:
            return
        people: dict[str, dict] = {}
        for item in self._db.get_all_items():
            for role, field in (
                ("cast", "cast_list"), ("director", "director"), ("writer", "writer"),
            ):
                value = item.get(field)
                if isinstance(value, str):
                    try:
                        decoded = json.loads(value)
                    except ValueError:
                        decoded = value
                    names = decoded if isinstance(decoded, list) else str(decoded).split(",")
                elif isinstance(value, list):
                    names = value
                else:
                    names = []
                for raw_name in names:
                    name = str(raw_name).strip()
                    key = person_key(name)
                    if not name or not key:
                        continue
                    person = people.setdefault(
                        key, {"name": name, "roles": [], "role": role})
                    if role not in person["roles"]:
                        person["roles"].append(role)
                    if role == "cast":
                        person["role"] = "cast"

        cached = self._db.get_person_profiles()
        self._people_items = sorted(people.values(), key=lambda p: p["name"].casefold())
        for iid in self._people_tree.get_children():
            self._people_tree.delete(iid)
        for index, person in enumerate(self._people_items):
            profile = cached.get(person_key(person["name"]))
            if profile is None:
                status = "Not fetched"
            elif profile.get("not_found"):
                status = "Not found"
            else:
                status = "Cached"
            person["cached"] = profile is not None
            self._people_tree.insert(
                "", "end", iid=person_key(person["name"]),
                values=(person["name"], ", ".join(person["roles"]), status),
                tags=("odd",) if index % 2 else (),
            )
        unfetched = sum(not person["cached"] for person in self._people_items)
        self._people_status.configure(
            text=f"{len(self._people_items)} people · {unfetched} to fetch")
        self._people_all_btn.configure(
            state="normal" if unfetched and not self._people_running
            and not self._bulk_running else "disabled")
        self._on_people_select()

    def _on_people_select(self, _event=None) -> None:
        selected = set(self._people_tree.selection())
        fetchable = any(
            person_key(person["name"]) in selected and not person["cached"]
            for person in self._people_items
        )
        self._people_selected_btn.configure(
            state="normal" if fetchable and not self._people_running
            and not self._bulk_running else "disabled")

    def _fetch_selected_people(self) -> None:
        selected = set(self._people_tree.selection())
        self._fetch_people([
            person for person in self._people_items
            if person_key(person["name"]) in selected and not person["cached"]
        ])

    def _fetch_all_people(self) -> None:
        self._fetch_people([person for person in self._people_items if not person["cached"]])

    def _cancel_people_fetch(self) -> None:
        self._people_cancel.set()
        self._people_status.configure(text="Stopping after the current lookup…")

    def _fetch_people(self, people: list[dict]) -> None:
        if self._people_running or self._bulk_running or not people:
            return
        self._people_running = True
        self._people_cancel.clear()
        self._people_refresh_btn.configure(state="disabled")
        self._people_selected_btn.configure(state="disabled")
        self._people_all_btn.configure(state="disabled")
        self._people_stop_btn.configure(state="normal")
        self._people_progress.configure(maximum=len(people), value=0)
        self._people_progress.pack(side="right", padx=(10, 0))
        self._people_status.configure(text=f"Fetching 0/{len(people)} people…")
        for button in (self._fetch_selected_btn, self._fetch_all_btn, self._retry_btn):
            button.configure(state="disabled")
        self._bulk_refresh_btn.configure(state="disabled")

        def worker() -> None:
            done = 0
            failures = 0
            for person in people:
                if self._people_cancel.is_set():
                    break
                name = person["name"]
                try:
                    profile = fetch_person_profile(
                        name, person["role"], config.PEOPLE_CACHE_DIR)
                    self._db.save_person_profile(profile)
                    status = "Not found" if profile.get("not_found") else "Cached"
                except Exception as exc:
                    log.warning("people profile fetch failed for %s: %s", name, exc)
                    failures += 1
                    status = "Failed"
                done += 1
                self._post(lambda n=name, s=status, d=done, total=len(people):
                           self._people_row_done(n, s, d, total))
            cancelled = self._people_cancel.is_set()
            self._post(lambda d=done, f=failures, t=len(people), c=cancelled:
                       self._people_fetch_finished(d, f, t, c))

        threading.Thread(target=worker, daemon=True).start()

    def _people_row_done(self, name: str, status: str, done: int, total: int) -> None:
        key = person_key(name)
        try:
            self._people_tree.set(key, "status", status)
        except tk.TclError:
            pass
        self._people_progress.configure(value=done)
        self._people_status.configure(text=f"Fetching {done}/{total} people…")

    def _people_fetch_finished(
        self, done: int, failures: int, total: int, cancelled: bool,
    ) -> None:
        self._people_running = False
        self._people_stop_btn.configure(state="disabled")
        self._people_progress.pack_forget()
        self._refresh_people_list()
        if cancelled:
            message = f"Stopped after {done}/{total} people."
        else:
            message = f"Fetched {done - failures}/{total} people."
            if failures:
                message += f" {failures} failed; refresh and retry later."
        self._people_status.configure(text=message)
        self._update_bulk_buttons_state()
        self._people_refresh_btn.configure(
            state="disabled" if self._bulk_running else "normal")
        self._bulk_refresh_btn.configure(
            state="disabled" if self._people_running else "normal")

    def _refresh_bulk_list(self) -> None:
        """Reload the list of unfetched items from DB."""
        if self._people_running:
            self._bulk_status.configure(text="Wait for the people profile fetch to finish.")
            return
        self._bulk_items = self._db.get_unfetched()
        self._bulk_done = set()
        self._bulk_row_status.clear()

        for iid in self._bulk_tree.get_children():
            self._bulk_tree.delete(iid)

        if not self._bulk_items:
            self._bulk_status.configure(text="All items have metadata.")
            self._fetch_all_btn.configure(state="disabled")
            self._progress.configure(maximum=1, value=0)
            return

        self._fetch_all_btn.configure(state="normal")
        self._bulk_status.configure(text=f"Found {len(self._bulk_items)} items without metadata.")

        for idx, item in enumerate(self._bulk_items):
            media_id = item["id"]
            title = item.get("title") or "Unknown"
            year = f" ({item['year']})" if item.get("year") else ""
            mtype = "Series" if item.get("media_type") == "series" else "Movie"
            missing = self._missing_fields(item)
            missing_text = "Missing: " + ", ".join(missing) if missing else ""
            self._bulk_tree.insert("", "end", iid=str(media_id),
                                   values=(f"{title}{year}", mtype, missing_text, ""),
                                   tags=("odd",) if idx % 2 else ())

    def _on_bulk_double_click(self, event) -> None:
        """Handle double-click: fetch the item, or offer an IMDb ID on errors."""
        row = self._bulk_tree.identify_row(event.y)
        if not row:
            return
        media_id = int(row)
        item = next((i for i in self._bulk_items if i["id"] == media_id), None)
        if item is None:
            return
        if self._bulk_row_status.get(media_id):
            # the fetch failed before: let the user retry by hand with the
            # exact IMDb ID, an automatically cleaned name is not enough
            self._ask_bulk_imdb_id(item)
        else:
            self._fetch_one_bulk(item)

    def _ask_bulk_imdb_id(self, item: dict) -> None:
        if self._bulk_running:
            return
        title = str(item.get("title") or "Unknown")
        year = f" ({item['year']})" if item.get("year") else ""
        message = (f'There is no match found for "{title}{year}". '
                   'Do you want to try to fetch using an IMDb ID?')
        imdb_id = ImdbIdDialog(self, message).ask()
        if imdb_id is None or self._bulk_running:
            return
        self._fetch_items([dict(item, _imdb_id=imdb_id)])

    def _on_bulk_select(self, event) -> None:
        """Handle selection change in bulk fetch tree."""
        if self._bulk_running or self._people_running:
            return
        state = "normal" if self._bulk_tree.selection() else "disabled"
        self._fetch_selected_btn.configure(state=state)

    def _missing_fields(self, item: dict) -> list[str]:
        missing = []
        if not item.get("imdb_id"):
            missing.append("IMDb ID")
        if not item.get("plot"):
            missing.append("Plot")
        if not item.get("genres"):
            missing.append("Genres")
        if item.get("rating") is None:
            missing.append("Rating")
        if not item.get("runtime"):
            missing.append("Runtime")
        if not item.get("director"):
            missing.append("Director")
        if not item.get("writer"):
            missing.append("Writer")
        if not item.get("cast_list"):
            missing.append("Cast")
        if not item.get("country"):
            missing.append("Country")
        return missing

    # --- bulk fetching ------------------------------------------------

    def _fetch_selected_bulk(self) -> None:
        """Fetch metadata for all selected items."""
        if self._bulk_running:
            return
        selection = self._bulk_tree.selection()
        if not selection:
            return
        wanted = {int(iid) for iid in selection}
        items = [i for i in self._remaining() if i["id"] in wanted]
        self._fetch_items(items)

    def _fetch_all_bulk(self) -> None:
        """Fetch metadata for all unfetched items."""
        if self._bulk_running:
            return
        items = self._remaining()
        if not items:
            return
        self._fetch_items(items)

    def _remaining(self) -> list[dict]:
        """The items that still need metadata."""
        return [i for i in self._bulk_items if i["id"] not in self._bulk_done]

    def _fetch_one_bulk(self, item: dict) -> None:
        """Fetch metadata for a single item."""
        if self._bulk_running or item["id"] in self._fetch_jobs:
            return
        self._fetch_jobs.add(item["id"])
        self._fetch_items([item])

    def _cancel_bulk(self) -> None:
        self._bulk_cancel.set()
        self._bulk_status.configure(text="Stopping…")

    def _fetch_items(self, items: list[dict]) -> None:
        """Fetch metadata for a list of items, with progress and a stop button."""
        if not items:
            return
        if self._people_running:
            self._bulk_status.configure(text="Wait for the people profile fetch to finish.")
            return
        self._bulk_running = True
        self._bulk_cancel.clear()
        self._bulk_failures = []
        self._bulk_failed_ids = []
        self._bulk_changed = []
        self._fetch_selected_btn.configure(state="disabled")
        self._fetch_all_btn.configure(state="disabled")
        self._retry_btn.configure(state="disabled")
        for button in (self._people_selected_btn, self._people_all_btn,
                       self._people_refresh_btn):
            button.configure(state="disabled")
        self._bulk_refresh_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress.configure(maximum=len(items), value=0)
        self._progress.pack(side="left", padx=(12, 0), pady=(2, 0))
        self._bulk_status.configure(text=f"Fetching {len(items)} item(s)…")

        def worker(item: dict):
            if self._bulk_cancel.is_set():
                return None
            media_id = item["id"]
            title = item.get("title") or "Unknown"
            try:
                query = metadata_service.MetadataService.clean_query(
                    str(item.get("title") or ""), item.get("year"))
                result = metadata_service.fetch_metadata(
                    query, item.get("year"), item.get("media_type"),
                    item.get("_imdb_id"))
                if result:
                    metadata_service.save_metadata(self._db, media_id, result)
                    metadata_service.download_artwork(
                        {"video_path": item.get("video_path"), "path": item.get("path")},
                        result.poster_url, result.backdrop_url,
                    )
                    metadata_service.write_about(
                        {"video_path": item.get("video_path"), "path": item.get("path"),
                         "media_type": item.get("media_type")},
                        result,
                    )
                    p = Path(item.get("video_path") or item.get("path") or "")
                    folder = p.parent if p.is_file() else p
                    if folder.exists():
                        poster, backdrop = find_artwork(folder)
                        if poster or backdrop:
                            self._db.set_artwork_paths(
                                media_id,
                                str(poster) if poster else None,
                                str(backdrop) if backdrop else None,
                            )
                return (media_id, result is not None, title, None)
            except Exception as exc:
                log.warning("bulk fetch failed for %s: %s", title, exc)
                return (media_id, False, title, str(exc))

        def run() -> None:
            done = 0
            failed = 0
            # The pool is fed by hand: `map` would queue every item up
            # front, and "Stop" would only end the reporting.
            pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
            try:
                pending: set = set()
                source = iter(items)
                exhausted = False
                while True:
                    while (len(pending) < FETCH_WORKERS and not exhausted
                           and not self._bulk_cancel.is_set()):
                        nxt = next(source, None)
                        if nxt is None:
                            exhausted = True
                            break
                        pending.add(pool.submit(worker, nxt))
                    if not pending:
                        break
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for f in finished:
                        try:
                            outcome = f.result()
                        except Exception:
                            outcome = None
                        if outcome is None:
                            continue
                        media_id, ok, title, err = outcome
                        done += 1
                        if ok:
                            self._bulk_changed.append(media_id)
                        else:
                            failed += 1
                            err_text = err or "not found"
                            self._bulk_failures.append((title, err_text))
                            self._bulk_failed_ids.append(media_id)
                            self._post(lambda m=media_id, e=err_text:
                                       self._set_bulk_row_status(m, e))
                        self._post(lambda m=media_id, d=done, t=title, o=ok, f=failed:
                                   self._on_bulk_done(m, d, t, o, len(items), f))
            finally:
                pool.shutdown(wait=True)
            self._post(lambda d=done, f=failed, t=len(items),
                       c=list(self._bulk_changed), fl=list(self._bulk_failures),
                       ids=list(self._bulk_failed_ids):
                       self._bulk_finished(d, f, t, c, fl, ids))

        threading.Thread(target=run, daemon=True).start()

    def _on_bulk_done(self, media_id: int, done: int, title: str, ok: bool,
                      total: int, failed: int) -> None:
        self._progress.configure(value=done)
        if ok:
            self._remove_bulk_row(media_id)
        self._bulk_status.configure(
            text=f"{done}/{total} done" + (f" · {failed} failed" if failed else ""))

    def _bulk_finished(self, done: int, failed: int, total: int,
                       changed: list[int], failures: list[tuple[str, str]],
                       failed_ids: list[int]) -> None:
        self._bulk_running = False
        self._fetch_jobs.clear()
        self._bulk_failures = failures
        self._bulk_failed_ids = failed_ids
        self._cancel_btn.configure(state="disabled")
        self._progress.pack_forget()
        self._bulk_refresh_btn.configure(state="normal")
        self._people_refresh_btn.configure(state="normal")
        # one refresh for the whole run, not one per item
        if changed and self._on_changed:
            self._on_changed(changed)
        if self._bulk_cancel.is_set():
            self._bulk_status.configure(text=f"Stopped after {done}/{total} item(s).")
        elif failed:
            names = ", ".join(title for title, _ in failures[:3])
            more = f" (+{len(failures) - 3} more)" if len(failures) > 3 else ""
            self._bulk_status.configure(
                text=f"Fetch complete - {done - failed}/{total} succeeded. "
                     f"Failed: {names}{more}")
        else:
            self._bulk_status.configure(
                text=f"Fetch complete — {done - failed}/{total} succeeded.")
        self._update_bulk_buttons_state()
        self._on_people_select()
        self._people_all_btn.configure(
            state="normal" if any(not p.get("cached") for p in self._people_items)
            and not self._people_running else "disabled")

    def _update_bulk_buttons_state(self) -> None:
        """Update bulk fetch button states."""
        enabled = not self._people_running and not self._bulk_running
        self._fetch_selected_btn.configure(
            state="normal" if self._bulk_tree.selection() and enabled else "disabled")
        self._fetch_all_btn.configure(
            state="normal" if self._remaining() and enabled else "disabled")
        self._retry_btn.configure(
            state="normal" if (self._bulk_failed_ids and enabled)
            else "disabled")

    def _retry_failed_bulk(self) -> None:
        """Try the items that failed the last time again."""
        if self._bulk_running or not self._bulk_failed_ids:
            return
        wanted = set(self._bulk_failed_ids)
        items = [i for i in self._bulk_items if i["id"] in wanted]
        if not items:
            self._bulk_status.configure(text="There is nothing to retry.")
            return
        self._fetch_items(items)

    def _remove_bulk_row(self, media_id: int) -> None:
        try:
            self._bulk_tree.delete(str(media_id))
        except tk.TclError:
            pass
        self._bulk_row_status.pop(media_id, None)
        self._bulk_done.add(media_id)
        if not self._remaining():
            self._bulk_status.configure(text="All items have metadata.")
            self._fetch_all_btn.configure(state="disabled")
        elif not self._bulk_running:
            self._bulk_status.configure(
                text=f"Found {len(self._remaining())} items without metadata.")

    def _set_bulk_row_status(self, media_id: int, text: str) -> None:
        self._bulk_row_status[media_id] = text
        try:
            self._bulk_tree.set(str(media_id), "status", text)
        except tk.TclError:
            pass

    # --- Metadata Editor tab ------------------------------------------

    def _build_metadata_editor(self, parent: tk.Frame) -> None:
        inner = tk.Frame(parent, bg=PANEL_BG, padx=16, pady=16)
        inner.pack(fill="both", expand=True)

        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", pady=(0, 12))

        tk.Label(header, text="All items", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(side="left")

        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=12, pady=5, font=("Segoe UI", 9), cursor="hand2",
        )
        tk.Button(header, text="Refresh", command=self._refresh_meta_list, **btn_style).pack(
            side="right", padx=(8, 0))

        # Selected action buttons (shown when items selected)
        self._meta_selected_frame = tk.Frame(header, bg=PANEL_BG)
        self._meta_selected_frame.pack(side="right", padx=(8, 0))

        danger_btn_style = dict(
            bg="#2b2226", fg=TEXT, activebackground="#cc3b3f", activeforeground=TEXT,
            bd=0, padx=10, pady=4, font=("Segoe UI", 8), cursor="hand2",
        )
        self._del_meta_sel_btn = tk.Button(self._meta_selected_frame,
                                           text="Remove Metadata Selected",
                                           command=lambda: self._delete_selected_metadata("meta"),
                                           state="disabled", **danger_btn_style,
                                           disabledforeground="#5a6472")
        self._del_meta_sel_btn.pack(side="left", padx=4)
        self._del_poster_sel_btn = tk.Button(self._meta_selected_frame,
                                             text="Remove Poster Selected",
                                             command=lambda: self._delete_selected_metadata("poster"),
                                             state="disabled", **danger_btn_style,
                                             disabledforeground="#5a6472")
        self._del_poster_sel_btn.pack(side="left", padx=4)
        self._del_poster2_sel_btn = tk.Button(self._meta_selected_frame,
                                              text="Remove Poster2 Selected",
                                              command=lambda: self._delete_selected_metadata("poster2"),
                                              state="disabled", **danger_btn_style,
                                              disabledforeground="#5a6472")
        self._del_poster2_sel_btn.pack(side="left", padx=4)
        self._del_about_sel_btn = tk.Button(self._meta_selected_frame,
                                             text="Remove about.txt Selected",
                                             command=lambda: self._delete_selected_metadata("about"),
                                             state="disabled", **danger_btn_style,
                                             disabledforeground="#5a6472")
        self._del_about_sel_btn.pack(side="left", padx=4)

        # Treeview for performant scrolling
        vsb = ttk.Scrollbar(inner, orient="vertical",
                             command=lambda *a: self._meta_tree.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.pack(side="right", fill="y")
        wrap = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(side="left", fill="both", expand=True)
        columns = ("title", "type", "badges")
        self._meta_tree = ttk.Treeview(wrap, columns=columns, show="headings",
                                       style="Settings.Treeview", height=20,
                                       selectmode="extended")
        self._meta_tree.heading("title", text="Title")
        self._meta_tree.heading("type", text="Type")
        self._meta_tree.heading("badges", text="Metadata")
        self._meta_tree.column("title", width=300, stretch=True)
        self._meta_tree.column("type", width=80, stretch=False)
        self._meta_tree.column("badges", width=500, stretch=True)
        self._meta_tree.tag_configure("odd", background="#151a24")
        self._meta_tree.tag_configure("duplicate", foreground="#e0a33e")
        self._meta_tree.configure(yscrollcommand=vsb.set)
        self._meta_tree.pack(side="left", fill="both", expand=True)
        self._meta_tree.bind("<<TreeviewSelect>>", self._on_meta_select)
        # drag selection: hold the mouse and drag over the rows
        self._meta_tree.bind("<Button-1>", self._on_tree_drag_start, add="+")
        self._meta_tree.bind("<B1-Motion>", self._on_tree_drag_select, add="+")

        # Status label
        self._meta_status = tk.Label(header, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                     anchor="w", justify="left")
        self._meta_status.pack(side="left", padx=(10, 0))

    def _refresh_meta_list(self) -> None:
        """Reload all items with metadata status."""
        self._meta_items = self._db.get_all_items()
        self._duplicates = {d["title"] for d in self._db.duplicates()}

        for iid in self._meta_tree.get_children():
            self._meta_tree.delete(iid)

        if not self._meta_items:
            self._meta_status.configure(text="No items in library.")
            return

        note = ""
        if self._duplicates:
            note = f" · {len(self._duplicates)} duplicate title(s)"
        self._meta_status.configure(text=f"Total: {len(self._meta_items)} items{note}")

        for idx, item in enumerate(self._meta_items):
            self._add_meta_row(item, idx)

    def _add_meta_row(self, item: dict, index: int = 0) -> None:
        """Add a row for one item in the tree."""
        media_id = item["id"]
        title = item.get("title") or "Unknown"
        year = f" ({item['year']})" if item.get("year") else ""
        mtype = "Series" if item.get("media_type") == "series" else "Movie"

        badges = []
        if item.get("imdb_id"):
            badges.append("IMDb")
        if item.get("plot"):
            badges.append("Plot")
        if item.get("genres"):
            badges.append("Genres")
        if item.get("rating") is not None:
            badges.append("Rating")
        if item.get("runtime"):
            badges.append("Runtime")
        if item.get("director"):
            badges.append("Director")
        if item.get("writer"):
            badges.append("Writer")
        if item.get("cast_list"):
            badges.append("Cast")
        if item.get("country"):
            badges.append("Country")
        if item.get("poster_path"):
            badges.append("Poster")
        if item.get("poster2_path"):
            badges.append("Poster2")
        if item.get("watched"):
            badges.append("Watched")
        if str(item.get("title") or "").lower() in getattr(self, "_duplicates", ()):
            badges.append("Duplicate")

        badge_text = "  ".join(badges) if badges else ""

        tags = ("odd",) if index % 2 else ()
        if str(item.get("title") or "").lower() in getattr(self, "_duplicates", ()):
            tags = tags + ("duplicate",)
        self._meta_tree.insert("", "end", iid=str(media_id),
                               values=(f"{title}{year}  [{mtype}]", mtype, badge_text),
                               tags=tags)

    def _on_meta_select(self, event) -> None:
        """Handle selection change in metadata editor tree."""
        has_selection = bool(self._meta_tree.selection())
        state = "normal" if has_selection else "disabled"
        bg = ACCENT if has_selection else "#2b2226"
        for btn in (self._del_meta_sel_btn, self._del_poster_sel_btn,
                    self._del_poster2_sel_btn, self._del_about_sel_btn):
            btn.configure(state=state, bg=bg)

    # --- multi selection ----------------------------------------------

    def _on_tree_drag_start(self, event) -> None:
        """Start a drag selection - select the row under the cursor.

        Ctrl and Shift are left to the Treeview itself, which already
        toggles a row (Ctrl) or extends the selection (Shift).
        """
        if event.state & 0x4 or event.state & 0x1:  # Ctrl or Shift
            return
        item = self._identify_row_from_event(event)
        if item:
            self._drag_select_anchor = item
            tree = event.widget
            tree.selection_set(item)

    def _on_tree_drag_select(self, event) -> None:
        """Extend the selection while dragging."""
        item = self._identify_row_from_event(event)
        if not item or not getattr(self, "_drag_select_anchor", None):
            return
        tree = event.widget
        children = tree.get_children()
        if not children:
            return
        try:
            start_idx = children.index(self._drag_select_anchor)
            end_idx = children.index(item)
        except ValueError:
            return
        lo, hi = sorted((start_idx, end_idx))
        tree.selection_set(children[lo:hi + 1])

    def _identify_row_from_event(self, event):
        """Identify the tree row under a mouse event."""
        tree = event.widget
        item = tree.identify_row(event.y)
        return item if item else None

    # --- deleting -----------------------------------------------------

    def _confirm(self, title: str, message: str, confirm_text: str = "Delete") -> bool:
        """Modal confirmation dialog. Returns True when confirmed."""
        return ConfirmDialog(self, title, message, confirm_text).ask()

    def _selected_meta_items(self) -> list[dict]:
        wanted = {int(iid) for iid in self._meta_tree.selection()}
        return [i for i in self._meta_items if i["id"] in wanted]

    def _delete_selected_metadata(self, which: str) -> None:
        """Delete metadata/poster/poster2/about.txt for all selected items."""
        items = self._selected_meta_items()
        if not items:
            return

        labels = {
            "meta": "metadata and about.txt files",
            "poster": "poster files",
            "poster2": "backdrop files",
            "about": "about.txt files",
        }
        if not self._confirm(
            "Confirm removal",
            f"This will remove the {labels[which]} for {len(items)} item(s) "
            "from your media folders.\n\nContinue?",
        ):
            return

        changed: list[int] = []
        for item in items:
            media_id = item["id"]
            if which == "meta":
                self._db.clear_metadata(media_id)
                media_ops.remove_about(item)
            elif which in ("poster", "poster2"):
                media_ops.remove_artwork(item, which)
                self._db.clear_poster(media_id, which)
            elif which == "about":
                media_ops.remove_about(item)
            changed.append(media_id)

        # one refresh for the whole selection, not one per item
        if changed and self._on_changed:
            self._on_changed(changed)

        self._meta_status.configure(
            text=f"Removed {labels[which]} for {len(items)} item(s).")
        self._refresh_meta_list()
        if which == "meta":
            self._refresh_bulk_list()

    # --- Dig Deeper tab ------------------------------------------------

    def _build_dig_deeper(self, parent: tk.Frame) -> None:
        """The movies and series whose story is based on a true story."""
        inner = tk.Frame(parent, bg=PANEL_BG, padx=16, pady=16)
        inner.pack(fill="both", expand=True)

        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", pady=(0, 12))

        tk.Label(header, text="Based on a true story", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        self._deeper_status = tk.Label(header, text="", bg=PANEL_BG, fg=MUTED,
                                       font=("Segoe UI", 9), anchor="w", justify="left")
        self._deeper_status.pack(side="left", padx=(10, 0))

        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=12, pady=5, font=("Segoe UI", 9), cursor="hand2",
        )
        tk.Button(header, text="Forget flags", command=self._reset_deeper,
                  **btn_style).pack(side="right", padx=(8, 0))
        self._deeper_cache_btn = tk.Button(
            header, text="Cache the stories", command=self._cache_stories,
            **btn_style)
        self._deeper_cache_btn.pack(side="right", padx=(8, 0))
        self._deeper_stop_btn = tk.Button(
            header, text="Stop", command=self._cancel_deeper, state="disabled",
            **btn_style, disabledforeground="#5a6472")
        self._deeper_stop_btn.pack(side="right", padx=(8, 0))
        self._deeper_scan_btn = tk.Button(
            header, text="Scan library", command=self._scan_deeper, **btn_style)
        self._deeper_scan_btn.pack(side="right")
        self._deeper_progress = ttk.Progressbar(
            header, mode="determinate", length=160,
            style="Dark.Horizontal.TProgressbar")

        body = tk.Frame(inner, bg=PANEL_BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=2, uniform="deeper")
        body.grid_columnconfigure(1, weight=3, uniform="deeper")
        body.grid_rowconfigure(0, weight=1)

        vsb = ttk.Scrollbar(body, orient="vertical",
                             command=lambda *a: self._deeper_tree.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.grid(row=0, column=0, sticky="ns")
        wrap = tk.Frame(body, bg=PANEL_BG, highlightthickness=1,
                        highlightbackground=BORDER)
        wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        columns = ("title", "year", "type")
        self._deeper_tree = ttk.Treeview(
            wrap, columns=columns, show="headings", style="Settings.Treeview",
            height=18, selectmode="browse")
        self._deeper_tree.heading("title", text="Title")
        self._deeper_tree.heading("year", text="Year")
        self._deeper_tree.heading("type", text="Type")
        self._deeper_tree.column("title", width=260, stretch=True)
        self._deeper_tree.column("year", width=60, stretch=False)
        self._deeper_tree.column("type", width=70, stretch=False)
        self._deeper_tree.tag_configure("odd", background="#151a24")
        self._deeper_tree.configure(yscrollcommand=vsb.set)
        self._deeper_tree.pack(side="left", fill="both", expand=True)
        self._deeper_tree.bind("<<TreeviewSelect>>", self._on_deeper_select)

        read = tk.Frame(body, bg=PANEL_BG, highlightthickness=1,
                        highlightbackground=BORDER)
        read.grid(row=0, column=1, sticky="nsew")
        read_buttons = tk.Frame(read, bg=PANEL_BG)
        read_buttons.pack(side="top", fill="x", padx=14, pady=(12, 0))
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            disabledforeground="#5a6472", bd=0, padx=12, pady=5,
            font=("Segoe UI", 9), cursor="hand2",
        )
        self._deeper_folder_btn = tk.Button(
            read_buttons, text="Open Folder", command=self._open_deeper_folder,
            state="disabled", **btn_style)
        self._deeper_folder_btn.pack(side="left", padx=(0, 8))
        self._deeper_details_btn = tk.Button(
            read_buttons, text="Open Details Page", command=self._open_deeper_details,
            state="disabled", **btn_style)
        self._deeper_details_btn.pack(side="left")
        self._deeper_text = tk.Text(
            read, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT, bd=0,
            highlightthickness=0, wrap="word", font=("Segoe UI", 10),
            padx=14, pady=12, state="disabled", spacing1=2, spacing3=4)
        self._deeper_text.tag_configure("title", font=("Segoe UI Semibold", 12),
                                        foreground=TEXT, spacing1=6, spacing3=8)
        self._deeper_text.tag_configure("head", font=("Segoe UI", 9, "bold"),
                                        foreground=ACCENT, spacing1=12)
        self._deeper_text.tag_configure("body", font=("Segoe UI", 10),
                                        foreground=TEXT)
        self._deeper_text.tag_configure("link", font=("Segoe UI", 8),
                                        foreground=MUTED)
        rvsb = ttk.Scrollbar(read, orient="vertical",
                              command=self._deeper_text.yview,
                              style="Dark.Vertical.TScrollbar")
        self._deeper_text.configure(yscrollcommand=rvsb.set)
        rvsb.pack(side="right", fill="y")
        self._deeper_text.pack(side="left", fill="both", expand=True)
        self._deeper_items: dict[int, dict] = {}
        self._deeper_running = False
        self._deeper_cancel = threading.Event()

    def _refresh_deeper_list(self) -> int:
        """Reload the flagged items from the database."""
        items = self._db.true_story_items()
        self._deeper_items = {int(i["id"]): i for i in items}
        for iid in self._deeper_tree.get_children():
            self._deeper_tree.delete(iid)
        for idx, item in enumerate(items):
            title = item.get("title") or "Unknown"
            year = item.get("year") or ""
            kind = "Series" if item.get("media_type") == "series" else "Movie"
            self._deeper_tree.insert(
                "", "end", iid=str(item["id"]), values=(title, year, kind),
                tags=("odd",) if idx % 2 else ())
        self._deeper_buttons(False)
        self._set_deeper_text(("body", "Pick a title to read about it."))
        return len(items)

    def _set_deeper_text(self, *blocks: tuple[str, str]) -> None:
        """Fill the reading pane - (style, text) blocks."""
        self._deeper_text.configure(state="normal")
        self._deeper_text.delete("1.0", "end")
        for style, text in blocks:
            if not text:
                continue
            self._deeper_text.insert("end", text, style)
        self._deeper_text.configure(state="disabled")

    def _deeper_head(self, item: dict) -> str:
        """The title and the kind of an item."""
        title = str(item.get("title") or "Untitled")
        year = item.get("year")
        kind = "Series" if item.get("media_type") == "series" else "Movie"
        head = f"{title} ({year})" if year else title
        return f"{head}  ·  {kind}\n"

    def _deeper_plot(self, item: dict) -> str:
        """The plot the metadata provider knows."""
        plot = str(item.get("plot") or "").strip()
        return ("\n" + plot + "\n") if plot else ""

    def _deeper_buttons(self, enabled: bool) -> None:
        """The two buttons of the reading pane follow the selection."""
        state = "normal" if enabled else "disabled"
        self._deeper_folder_btn.configure(state=state)
        self._deeper_details_btn.configure(state=state)

    def _deeper_selected(self) -> dict | None:
        """The full database row of the selected title."""
        selection = self._deeper_tree.selection()
        if not selection:
            return None
        item = self._deeper_items.get(int(selection[0]))
        if item is None:
            return None
        return self._db.get(item["id"]) or item

    def _open_deeper_folder(self) -> None:
        """Open the folder of the selected title."""
        item = self._deeper_selected()
        if item is None:
            return
        folder = media_ops.media_folder(item)
        if folder is not None:
            media_ops.reveal(folder)

    def _open_deeper_details(self) -> None:
        """Show the selected title in the details page of the app."""
        item = self._deeper_selected()
        if item is None or self._on_open_item is None:
            return
        self._on_open_item(item)

    def _on_deeper_select(self, event=None) -> None:
        """Show the story of the selected title - from the cache."""
        selection = self._deeper_tree.selection()
        if not selection:
            self._deeper_buttons(False)
            return
        item = self._deeper_items.get(int(selection[0]))
        if item is None:
            self._deeper_buttons(False)
            return
        self._deeper_buttons(True)
        story = self._db.story_cache(item["id"])
        self._show_deeper_story(item, story)
        if not story:
            # nothing cached yet - fetch it once, in the background
            self._fetch_story(item)

    def _show_deeper_story(self, item: dict, story: dict) -> None:
        """Fill the reading pane with what is known about the title."""
        blocks: list[tuple[str, str]] = [
            ("title", self._deeper_head(item)),
            ("body", self._deeper_plot(item)),
        ]
        if story.get("real"):
            blocks.append(("head", "WHAT REALLY HAPPENED\n"))
            blocks.append(("body", str(story["real"]).strip() + "\n"))
        places = story.get("places") or []
        if places:
            blocks.append(("head", "WHERE IT WAS FILMED\n"))
            blocks.append(("body", ", ".join(p[0] for p in places[:4]) + "\n"))
            link = next((p[1] for p in places if p[1]), None)
            if link:
                blocks.append(("link", (true_story.article_url(link) or "") + "\n"))
        people = story.get("people") or []
        if people:
            blocks.append(("head", "THE PEOPLE\n"))
            blocks.append(("body", ", ".join(p[0] for p in people[:10]) + "\n"))
        awards = story.get("awards") or []
        if awards:
            blocks.append(("head", "AWARDS\n"))
            blocks.append(("body", ", ".join(a[0] for a in awards[:6]) + "\n"))
        source = story.get("source")
        if source:
            blocks.append(("link", (true_story.article_url(source) or "") + "\n"))
        if len(blocks) == 2:
            blocks.append(("body", "\nThe story of this title has not been cached "
                                  "yet - press Cache the stories.\n"))
        self._set_deeper_text(*blocks)

    def _fetch_story(self, item: dict) -> None:
        """Fetch the story of one title in the background and cache it."""
        media_id = item["id"]

        def worker() -> None:
            try:
                found = true_story.stories([{
                    "id": media_id,
                    "true_story_article": item.get("true_story_article"),
                }])
            except Exception as exc:
                log.warning("the story lookup failed: %s", exc)
                return
            story = found.get(media_id)
            if not story:
                return
            self._db.set_story_cache(media_id, story)
            self._post(lambda: self._story_cached(media_id))

        threading.Thread(target=worker, daemon=True).start()

    def _story_cached(self, media_id: int) -> None:
        """The story arrived - show it, if it is still the selected one."""
        selection = self._deeper_tree.selection()
        if not selection or int(selection[0]) != media_id:
            return
        item = self._deeper_items.get(media_id)
        if item is None:
            return
        self._show_deeper_story(item, self._db.story_cache(media_id))

    def _cache_stories(self) -> None:
        """Fetch the story of every true-story title and keep it."""
        if self._deeper_running:
            return
        items = self._db.true_story_items()
        todo = [i for i in items
                if not self._db.story_cache(i["id"])
                and str(i.get("true_story_article") or "").strip()]
        if not todo:
            self._deeper_status.configure(
                text="Every story is cached - it can be read without a network.")
            return
        self._deeper_running = True
        self._deeper_cancel.clear()
        self._deeper_cache_btn.configure(state="disabled")
        self._deeper_stop_btn.configure(state="normal")
        self._deeper_progress.configure(maximum=len(todo), value=0)
        self._deeper_progress.pack(side="left", padx=(12, 0), pady=(2, 0))
        self._deeper_status.configure(
            text=f"Caching the story of {len(todo)} title(s)...")

        def worker() -> None:
            done = 0
            for start in range(0, len(todo), 20):
                if self._deeper_cancel.is_set():
                    break
                chunk = todo[start:start + 20]
                try:
                    stories = true_story.stories(chunk)
                except Exception as exc:
                    log.warning("the story lookup failed: %s", exc)
                    break
                for item in chunk:
                    story = stories.get(item["id"])
                    if story:
                        self._db.set_story_cache(item["id"], story)
                done += len(chunk)
                self._post(lambda d=done: self._deeper_progress.configure(value=d))
            self._post(lambda: self._stories_cached(done, len(todo)))

        threading.Thread(target=worker, daemon=True).start()

    def _stories_cached(self, done: int, total: int) -> None:
        self._deeper_running = False
        self._deeper_cache_btn.configure(state="normal")
        self._deeper_stop_btn.configure(state="disabled")
        self._deeper_progress.pack_forget()
        self._deeper_status.configure(
            text=f"Cached {done}/{total} story(ies) - readable without a network.")

    def _scan_deeper(self) -> None:
        """Ask Wikipedia which of the unchecked items are based on a true story."""
        if self._deeper_running:
            return
        items = self._db.unchecked_items()
        if not items:
            self._deeper_status.configure(
                text="Every item in the library has been checked.")
            return
        self._deeper_running = True
        self._deeper_cancel.clear()
        self._deeper_scan_btn.configure(state="disabled")
        self._deeper_stop_btn.configure(state="normal")
        self._deeper_progress.configure(maximum=len(items), value=0)
        self._deeper_progress.pack(side="left", padx=(12, 0), pady=(2, 0))
        self._deeper_status.configure(
            text=f"Checking {len(items)} item(s) on Wikipedia…")

        def worker() -> None:
            flagged: list[int] = []
            state = {"done": 0}

            def on_result(index: int, match) -> None:
                item = items[index]
                self._db.set_true_story(item["id"], match.is_true_story,
                                        match.article)
                if match.is_true_story:
                    flagged.append(item["id"])

            def progress(done: int, total: int, item: dict) -> None:
                state["done"] = done
                self._post(lambda d=done, t=total, n=item:
                           self._scan_progress_state(d, t, n))

            try:
                true_story.check(items, progress=progress, on_result=on_result,
                                 should_stop=self._deeper_cancel.is_set)
            except Exception as exc:
                log.warning("the true-story scan failed: %s", exc)
                self._post(lambda: self._deeper_finished(
                    state["done"], len(flagged), len(items), True, flagged))
                return
            self._post(lambda: self._deeper_finished(
                state["done"], len(flagged), len(items),
                self._deeper_cancel.is_set(), flagged))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_progress_state(self, done: int, total: int, item: dict) -> None:
        """Show what the scan is doing right now."""
        self._deeper_progress.configure(value=done)
        left = max(total - done, 0)
        title = str(item.get("title") or "")
        if len(title) > 30:
            title = title[:29] + "…"
        kind = "series" if item.get("media_type") == "series" else "movie"
        self._deeper_status.configure(
            text=f"Checking {done} of {total} — {title} ({kind}) · {left} to go")

    def _deeper_finished(self, done: int, found: int, total: int,
                         stopped: bool, flagged: list[int]) -> None:
        self._deeper_running = False
        self._deeper_scan_btn.configure(state="normal")
        self._deeper_stop_btn.configure(state="disabled")
        self._deeper_progress.pack_forget()
        self._refresh_deeper_list()
        if flagged and self._on_changed:
            # the library shows the flag in the details view too
            self._on_changed(flagged)
        if stopped:
            text = (f"Stopped after {done}/{total} item(s) — {found} based on a "
                    f"true story. The rest is checked by the next scan.")
        else:
            text = f"Checked {done} item(s) — {found} based on a true story."
        self._deeper_status.configure(text=text)

    def _cancel_deeper(self) -> None:
        self._deeper_cancel.set()
        self._deeper_status.configure(text="Stopping…")

    def _reset_deeper(self) -> None:
        """Forget every verdict, so the next scan checks the whole library."""
        if not self._confirm(
            "Forget the flags",
            "Every verdict is deleted and the next scan checks the whole library "
            "again.\n\nContinue?",
            "Forget",
        ):
            return
        self._db.reset_true_story()
        self._refresh_deeper_list()
        self._deeper_status.configure(text="Every flag was forgotten.")

    # --- Wikiquote tab --------------------------------------------------

    def _build_wikiquote(self, parent: tk.Frame) -> None:
        """The quotes of the films in the library."""
        inner = tk.Frame(parent, bg=PANEL_BG, padx=16, pady=16)
        inner.pack(fill="both", expand=True)

        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", pady=(0, 12))
        tk.Label(header, text="Quotes", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        self._quote_status = tk.Label(header, text="", bg=PANEL_BG, fg=MUTED,
                                      font=("Segoe UI", 9), anchor="w", justify="left")
        self._quote_status.pack(side="left", padx=(10, 0))
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            disabledforeground="#5a6472", bd=0, padx=12, pady=5,
            font=("Segoe UI", 9), cursor="hand2",
        )
        tk.Button(header, text="Random quote", command=self._random_quote,
                  **btn_style).pack(side="right", padx=(8, 0))
        tk.Button(header, text="Forget quotes", command=self._reset_quotes,
                  **btn_style).pack(side="right", padx=(8, 0))
        self._quote_stop_btn = tk.Button(
            header, text="Stop", command=self._cancel_quotes, state="disabled",
            **btn_style)
        self._quote_stop_btn.pack(side="right", padx=(8, 0))
        self._quote_scan_btn = tk.Button(
            header, text="Fetch quotes", command=self._scan_quotes, **btn_style)
        self._quote_scan_btn.pack(side="right", padx=(8, 0))
        self._quote_progress = ttk.Progressbar(
            header, mode="determinate", length=160,
            style="Dark.Horizontal.TProgressbar")

        body = tk.Frame(inner, bg=PANEL_BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=2, uniform="quotes")
        body.grid_columnconfigure(1, weight=3, uniform="quotes")
        body.grid_rowconfigure(0, weight=1)

        vsb = ttk.Scrollbar(body, orient="vertical",
                             command=lambda *a: self._quote_tree.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.grid(row=0, column=0, sticky="ns")
        wrap = tk.Frame(body, bg=PANEL_BG, highlightthickness=1,
                        highlightbackground=BORDER)
        wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        columns = ("title", "year", "count")
        self._quote_tree = ttk.Treeview(
            wrap, columns=columns, show="headings", style="Settings.Treeview",
            height=18, selectmode="browse")
        self._quote_tree.heading("title", text="Title")
        self._quote_tree.heading("year", text="Year")
        self._quote_tree.heading("count", text="Quotes")
        self._quote_tree.column("title", width=260, stretch=True)
        self._quote_tree.column("year", width=60, stretch=False)
        self._quote_tree.column("count", width=60, stretch=False, anchor="e")
        self._quote_tree.tag_configure("odd", background="#151a24")
        self._quote_tree.configure(yscrollcommand=vsb.set)
        self._quote_tree.pack(side="left", fill="both", expand=True)
        self._quote_tree.bind("<<TreeviewSelect>>", self._on_quote_select)

        read = tk.Frame(body, bg=PANEL_BG, highlightthickness=1,
                        highlightbackground=BORDER)
        read.grid(row=0, column=1, sticky="nsew")
        self._quote_text = tk.Text(
            read, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT, bd=0,
            highlightthickness=0, wrap="word", font=("Segoe UI", 10),
            padx=14, pady=12, state="disabled", spacing1=4, spacing3=8)
        self._quote_text.tag_configure("title", font=("Segoe UI Semibold", 12),
                                       foreground=TEXT, spacing1=6, spacing3=10)
        self._quote_text.tag_configure("quote", font=("Segoe UI", 10),
                                       foreground=TEXT)
        self._quote_text.tag_configure("speaker", font=("Segoe UI", 9),
                                       foreground=MUTED)
        self._quote_text.pack(side="left", fill="both", expand=True)
        self._quote_items: dict[int, dict] = {}
        self._quote_running = False
        self._quote_cancel = threading.Event()

    def _refresh_quote_list(self) -> int:
        """Reload the films that have quotes."""
        items = self._db.quote_items()
        self._quote_items = {int(i["id"]): i for i in items}
        for iid in self._quote_tree.get_children():
            self._quote_tree.delete(iid)
        total = 0
        for index, item in enumerate(items):
            total += len(item["quotes"])
            self._quote_tree.insert(
                "", "end", iid=str(item["id"]),
                values=(item.get("title") or "?", item.get("year") or "",
                        len(item["quotes"])),
                tags=("odd",) if index % 2 else ())
        self._set_quote_text(("body", "Pick a film to read its quotes."))
        self._quote_status.configure(
            text=f"{len(items)} film(s) with quotes · {total} quotes")
        return len(items)

    def _set_quote_text(self, *blocks: tuple[str, str]) -> None:
        self._quote_text.configure(state="normal")
        self._quote_text.delete("1.0", "end")
        for style, text in blocks:
            if not text:
                continue
            self._quote_text.insert("end", text, style)
        self._quote_text.configure(state="disabled")

    def _on_quote_select(self, event=None) -> None:
        """Show the quotes of the selected film."""
        selection = self._quote_tree.selection()
        if not selection:
            return
        item = self._quote_items.get(int(selection[0]))
        if item is None:
            return
        blocks: list[tuple[str, str]] = [("title", self._quote_head(item))]
        for quote in item["quotes"]:
            blocks.append(("quote", "“" + str(quote.get("text") or "") + "”\n"))
            speaker = str(quote.get("speaker") or "")
            if speaker:
                blocks.append(("speaker", "— " + speaker + "\n"))
        self._set_quote_text(*blocks)

    def _quote_head(self, item: dict) -> str:
        title = str(item.get("title") or "Untitled")
        year = item.get("year")
        kind = "Series" if item.get("media_type") == "series" else "Movie"
        return f"{title} ({year})  ·  {kind}\n" if year else f"{title}  ·  {kind}\n"

    def _random_quote(self) -> None:
        """Show a random quote of the library."""
        found = self._db.random_quote()
        if found is None:
            self._quote_status.configure(
                text="No quotes yet - fetch them first.")
            return
        item, quote = found
        blocks = [("title", self._quote_head(item)),
                  ("quote", "“" + str(quote.get("text") or "") + "”\n")]
        speaker = str(quote.get("speaker") or "")
        if speaker:
            blocks.append(("speaker", "— " + speaker + "\n"))
        self._set_quote_text(*blocks)

    def _scan_quotes(self) -> None:
        """Look the quotes of the whole library up on Wikiquote."""
        if self._quote_running:
            return
        items = self._db.unchecked_quote_items()
        if not items:
            self._quote_status.configure(
                text="Every film in the library has been looked up.")
            return
        self._quote_running = True
        self._quote_cancel.clear()
        self._quote_scan_btn.configure(state="disabled")
        self._quote_stop_btn.configure(state="normal")
        self._quote_progress.configure(maximum=len(items), value=0)
        self._quote_progress.pack(side="left", padx=(12, 0), pady=(2, 0))
        self._quote_status.configure(
            text=f"Looking up {len(items)} film(s) on Wikiquote — this takes a while…")

        def worker() -> None:
            done = 0
            found = 0
            for start in range(0, len(items), 6):
                if self._quote_cancel.is_set():
                    break
                chunk = items[start:start + 6]
                quotes = self._quote_batch(chunk)
                if quotes is None:
                    break
                for item in chunk:
                    self._db.set_quotes(
                        item["id"],
                        [{"text": t, "speaker": s}
                         for t, s in quotes.get(item["id"], [])])
                done += len(chunk)
                found += sum(1 for v in quotes.values() if v)
                self._post(lambda d=done: self._quote_progress.configure(value=d))
            self._post(lambda: self._quotes_finished(done, found, len(items)))

        threading.Thread(target=worker, daemon=True).start()

    def _quote_batch(self, chunk: list[dict]) -> dict | None:
        """The quotes of a batch of films - waiting out a throttled answer."""
        wait = 30.0
        while not self._quote_cancel.is_set():
            try:
                return true_story.quotes_for(chunk)
            except true_story.Throttled:
                self._post(lambda w=wait: self._quote_status.configure(
                    text=f"Wikiquote is throttling us - waiting {int(w)} s…"))
                # the wait is interruptible: "Stop" works right away
                deadline = time.monotonic() + wait
                while time.monotonic() < deadline:
                    if self._quote_cancel.is_set():
                        return None
                    time.sleep(0.25)
                wait = min(wait * 2, 300.0)
            except Exception as exc:
                log.warning("the quote lookup failed: %s", exc)
                return None
        return None

    def _quotes_finished(self, done: int, found: int, total: int) -> None:
        self._quote_running = False
        self._quote_scan_btn.configure(state="normal")
        self._quote_stop_btn.configure(state="disabled")
        self._quote_progress.pack_forget()
        self._refresh_quote_list()
        if self._quote_cancel.is_set():
            text = (f"Stopped after {done}/{total} film(s) — {found} with quotes. "
                    "The next run continues.")
        else:
            text = f"Looked up {done} film(s) — {found} have quotes."
        self._quote_status.configure(text=text)

    def _cancel_quotes(self) -> None:
        self._quote_cancel.set()
        self._quote_status.configure(text="Stopping…")

    def _reset_quotes(self) -> None:
        """Forget every quote, so the next run looks all of them up again."""
        if not self._confirm(
            "Forget the quotes",
            "Every quote is deleted and the next run looks the whole library up "
            "again.\n\nContinue?",
            "Forget",
        ):
            return
        self._db.reset_quotes()
        self._refresh_quote_list()
        self._quote_status.configure(text="Every quote was forgotten.")

    # --- Library Health tab ---------------------------------------------
    ISSUES = (
        ("duplicates", "Duplicate titles"),
        ("metadata", "No metadata"),
        ("poster", "No poster"),
        ("watched", "Never watched"),
        ("truestory", "No true-story verdict"),
        ("gaps", "Missing episodes"),
        ("future", "Upcoming seasons"),
        ("missing", "Missing from disk"),
    )

    def _build_library_health(self, parent: tk.Frame) -> None:
        """What is wrong with the library - and how big it is."""
        inner = tk.Frame(parent, bg=PANEL_BG, padx=16, pady=16)
        inner.pack(fill="both", expand=True)

        header = tk.Frame(inner, bg=PANEL_BG)
        header.pack(fill="x", pady=(0, 12))
        tk.Label(header, text="Library health", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        self._health_status = tk.Label(header, text="", bg=PANEL_BG, fg=MUTED,
                                       font=("Segoe UI", 9), anchor="w",
                                       justify="left")
        self._health_status.pack(side="left", padx=(10, 0))
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=12, pady=5, font=("Segoe UI", 9), cursor="hand2",
        )
        tk.Button(header, text="Refresh", command=self._refresh_health, **btn_style).pack(
            side="right", padx=(8, 0))
        self._health_details_btn = tk.Button(
            header, text="Open Details Page", command=self._open_health_details,
            state="disabled", **btn_style, disabledforeground="#5a6472")
        self._health_details_btn.pack(side="right", padx=(8, 0))
        self._health_relink_btn = tk.Button(
            header, text="Relink…", command=self._relink_item,
            state="disabled", **btn_style, disabledforeground="#5a6472")
        self._health_relink_btn.pack(side="right", padx=(8, 0))
        self._health_dupes_btn = tk.Button(
            header, text="Resolve duplicates", command=self._resolve_duplicates,
            state="disabled", **btn_style, disabledforeground="#5a6472")
        self._health_dupes_btn.pack(side="right", padx=(8, 0))
        self._health_missing: list[dict] = []
        self._health_size: int | None = None

        body = tk.Frame(inner, bg=PANEL_BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1, uniform="health")
        body.grid_columnconfigure(1, weight=2, uniform="health")
        body.grid_rowconfigure(0, weight=1)

        vsb = ttk.Scrollbar(body, orient="vertical",
                             command=lambda *a: self._health_issues.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.grid(row=0, column=0, sticky="ns")
        wrap = tk.Frame(body, bg=PANEL_BG, highlightthickness=1,
                        highlightbackground=BORDER)
        wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._health_issues = ttk.Treeview(
            wrap, columns=("issue", "count"), show="headings",
            style="Settings.Treeview", height=8, selectmode="browse")
        self._health_issues.heading("issue", text="Issue")
        self._health_issues.heading("count", text="Items")
        self._health_issues.column("issue", width=200, stretch=True)
        self._health_issues.column("count", width=60, stretch=False, anchor="e")
        self._health_issues.tag_configure("odd", background="#151a24")
        self._health_issues.configure(yscrollcommand=vsb.set)
        self._health_issues.pack(side="left", fill="both", expand=True)
        self._health_issues.bind("<<TreeviewSelect>>", self._on_health_issue)

        right = tk.Frame(body, bg=PANEL_BG)
        right.grid(row=0, column=1, sticky="nsew")
        ivsb = ttk.Scrollbar(right, orient="vertical",
                              command=lambda *a: self._health_items.yview(*a),
                              style="Dark.Vertical.TScrollbar")
        ivsb.pack(side="right", fill="y")
        iwrap = tk.Frame(right, bg=PANEL_BG, highlightthickness=1,
                         highlightbackground=BORDER)
        iwrap.pack(side="left", fill="both", expand=True)
        self._health_items = ttk.Treeview(
            iwrap, columns=("title", "year", "type"), show="headings",
            style="Settings.Treeview", height=16, selectmode="browse")
        self._health_items.heading("title", text="Title")
        self._health_items.heading("year", text="Year")
        self._health_items.heading("type", text="Type")
        self._health_items.column("title", width=300, stretch=True)
        self._health_items.column("year", width=60, stretch=False)
        self._health_items.column("type", width=70, stretch=False)
        self._health_items.tag_configure("odd", background="#151a24")
        self._health_items.configure(yscrollcommand=ivsb.set)
        self._health_items.pack(side="left", fill="both", expand=True)
        self._health_items.bind("<<TreeviewSelect>>", self._on_health_item_select)
        self._health_items.bind("<Double-1>", self._on_health_double_click)

        # --- the database itself ---
        maint = tk.Frame(inner, bg=PANEL_BG)
        maint.pack(fill="x", pady=(14, 0))
        tk.Label(maint, text="Maintenance", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(maint, text="The index of your library - it lives in data/library.db.",
                 bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left").pack(
            anchor="w", pady=(2, 6))
        row = tk.Frame(maint, bg=PANEL_BG)
        row.pack(anchor="w")
        tk.Button(row, text="Check the database", command=self._check_database,
                  **btn_style).pack(side="left", padx=(0, 8))
        tk.Button(row, text="Compact the database", command=self._compact_database,
                  **btn_style).pack(side="left", padx=(0, 8))
        tk.Button(row, text="Back up the database", command=self._backup_database,
                  **btn_style).pack(side="left", padx=(0, 8))
        tk.Button(row, text="Open the log folder", command=self._open_log_folder,
                  **btn_style).pack(side="left")
        self._maint_status = tk.Label(maint, text="", bg=PANEL_BG, fg=MUTED,
                                      font=("Segoe UI", 9), anchor="w", justify="left")
        self._maint_status.pack(anchor="w", pady=(6, 0))

    # --- maintenance --------------------------------------------------

    def _check_database(self) -> None:
        """Let SQLite look at its own database."""
        result = self._db.integrity_check()
        ok = str(result).lower() == "ok"
        self._maint_status.configure(
            text=f"The database is {self._db.file_size() / 1e6:.1f} MB - check: {result}",
            fg="#79c37d" if ok else ACCENT)

    def _compact_database(self) -> None:
        """Give the unused space of the database file back to the disk."""
        before = self._db.vacuum()
        after = self._db.file_size()
        self._maint_status.configure(
            text=f"The database is compacted: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB",
            fg="#79c37d")

    def _backup_database(self) -> None:
        """Write a copy of the library somewhere safe."""
        target = filedialog.asksaveasfilename(
            parent=self, title="Save a copy of the library",
            defaultextension=".db", initialfile="library.db",
            filetypes=[("Database", "*.db"), ("All files", "*")])
        if not target:
            return
        try:
            self._db.backup(Path(target))
        except Exception as exc:
            self._maint_status.configure(text=f"The backup failed: {exc}", fg=ACCENT)
            return
        self._maint_status.configure(
            text=f"A copy of your library was written to {target}", fg="#79c37d")

    def _open_log_folder(self) -> None:
        """Open the folder the log files live in."""
        media_ops.reveal(config.LOG_DIR)

    # --- duplicates and missing files ----------------------------------

    def _resolve_duplicates(self) -> None:
        """Take the copies of a duplicate title out of the library."""
        groups = self._db.duplicate_groups()
        if not groups:
            self._health_status.configure(text="There are no duplicate titles.")
            return
        dialog = DuplicateResolver(self, groups)
        dialog.grab_set()
        dialog.wait_window()
        if dialog.result is None:
            return
        count = len(dialog.result)
        if not self._confirm(
            "Resolve duplicates",
            f"{count} copy/copies are taken out of the library.\n\n"
            "The files stay where they are - only the app stops showing them.\n\n"
            "Continue?",
            confirm_text="Take them out",
        ):
            return
        for media_id in dialog.result:
            self._db.ignore(media_id)
        self._refresh_health()
        self._health_status.configure(
            text=f"{count} duplicate(s) were taken out of the library.")

    def _relink_item(self) -> None:
        """Point an item at the place it moved to."""
        selection = self._health_items.selection()
        if not selection:
            return
        item = self._db.get(int(selection[0]))
        if item is None:
            return
        start = self._health_start_dir(item)
        if item.get("media_type") == "series":
            chosen = filedialog.askdirectory(
                parent=self, title="Where is the series now?", initialdir=start)
            if not chosen:
                return
            new_path, new_video = chosen, None
        else:
            chosen = filedialog.askopenfilename(
                parent=self, title="Where is the movie now?", initialdir=start,
                filetypes=[("Videos", " ".join(f"*{e}" for e in config.VIDEO_EXTS)),
                           ("All files", "*")])
            if not chosen:
                return
            loose = item.get("path") == item.get("video_path")
            if loose:
                new_path, new_video = chosen, chosen
            else:
                new_path, new_video = str(Path(chosen).parent), chosen
        try:
            self._db.relink(item["id"], new_path, new_video)
        except ValueError as exc:
            self._health_status.configure(text=f"Could not relink: {exc}", fg=ACCENT)
            return
        except Exception as exc:
            self._health_status.configure(text=f"Could not relink: {exc}", fg=ACCENT)
            return
        self._refresh_health()
        self._health_status.configure(
            text=f"“{item.get('title')}” now points at {new_path}")

    @staticmethod
    def _health_start_dir(item: dict) -> str:
        """Where the file dialog should start."""
        raw = item.get("path") or ""
        if raw:
            path = Path(raw)
            start = str(path if path.is_dir() else path.parent)
            if Path(start).exists():
                return start
        return str(Path.home())

    def _refresh_health(self) -> None:
        """Fill the report: the numbers, the issues and the size."""
        counts = self._db.health_counts()
        for iid in self._health_issues.get_children():
            self._health_issues.delete(iid)
        for index, (key, label) in enumerate(self.ISSUES):
            self._health_issues.insert(
                "", "end", iid=key, values=(label, counts.get(key, 0)),
                tags=("odd",) if index % 2 else ())
        # the counts of the library come from one grouped query
        by_type = self._db.counts()
        movies = by_type.get("movie", 0)
        series = by_type.get("series", 0)
        total = movies + series
        runtime = self._db.total_runtime()
        rated = sum(self._db.rating_counts().values())
        parts = [f"{total} items", f"{movies} movies", f"{series} series"]
        if runtime:
            parts.append(f"{runtime // 60} h of runtime")
        if rated:
            parts.append(f"{rated} rated")
        if self._health_size is not None:
            parts.append(f"{self._health_size / 1e9:.0f} GB of video")
        else:
            parts.append("measuring the size…")
        self._health_status.configure(text=" · ".join(parts))

        def worker() -> None:
            size, missing = self._db.disk_report()
            self._post(lambda: self._health_details(missing, size))

        threading.Thread(target=worker, daemon=True).start()

    def _health_details(self, missing: list[dict], size: int) -> None:
        """The files that are gone and the size of the library."""
        self._health_missing = missing
        self._health_size = size
        text = self._health_status.cget("text").replace(
            "measuring the size…", f"{size / 1e9:.0f} GB of video")
        self._health_status.configure(text=text)
        if self._health_issues.exists("missing"):
            self._health_issues.item(
                "missing", values=("Missing from disk", len(missing)))

    def _on_health_issue(self, event=None) -> None:
        """Show the items of the selected issue."""
        selection = self._health_issues.selection()
        if not selection:
            for iid in self._health_items.get_children():
                self._health_items.delete(iid)
            self._update_health_buttons()
            return
        issue = selection[0]
        for iid in self._health_items.get_children():
            self._health_items.delete(iid)
        items = (self._health_missing if issue == "missing"
                 else self._db.health_items(issue))
        for index, item in enumerate(items):
            title = item.get("title") or "Unknown"
            if item.get("detail"):
                title += f"  —  {item['detail']}"
            year = item.get("year") or ""
            kind = "Series" if item.get("media_type") == "series" else "Movie"
            self._health_items.insert(
                "", "end", iid=str(item["id"]), values=(title, year, kind),
                tags=("odd",) if index % 2 else ())
        self._update_health_buttons()

    def _on_health_item_select(self, event=None) -> None:
        """The buttons of the item list follow the selection."""
        self._update_health_buttons()

    def _on_health_double_click(self, event=None) -> None:
        """Look again at the folder of one gap/row of the health report."""
        issues = self._health_issues.selection()
        if not issues or issues[0] not in ("gaps", "future"):
            return
        selection = self._health_items.selection()
        if not selection:
            return
        item = self._db.get(int(selection[0]))
        if item is None or item.get("media_type") != "series":
            return
        self._redetect_series_files(item)

    def _redetect_series_files(self, item: dict) -> None:
        """Scan the series folder again and fetch the files it holds now."""
        from scanner import library_scanner
        title = str(item.get("title") or "Unknown")
        before = self._db.missing_episodes_detail(item["id"])
        scan = library_scanner._collect(Path(item.get("path") or ""))
        if not scan.videos:
            self._health_status.configure(
                text=f"Re-checked “{title}”: no video files on the disk.",
                fg=ACCENT)
            return
        identified = [e for e in scan.episodes if e.episode is not None]
        stayed = [e for e in scan.episodes if e.episode is None]
        self._db.save_scanned_episodes(item["id"], identified)
        if stayed:
            names = "\n".join(sorted({Path(e.path).name for e in stayed})[:15])
            if self._confirm(
                "Unrecognised episode files",
                f"“{title}” has files that are playable, but their episode "
                "numbers cannot be read:\n\n" + names + "\n\n"
                "Add them to the app so it lists them as well?",
                confirm_text="Add them",
            ):
                self._db.save_scanned_episodes(item["id"], stayed)
        self._refresh_health()
        # the report stays on the issue it was on - "gaps" would steal it
        issue = self._health_issues.selection()
        self._health_issues.selection_set(issue[0] if issue else "gaps")
        self._on_health_issue()
        after = self._db.missing_episodes_detail(item["id"])
        upcoming = self._db.upcoming_seasons_detail(item["id"])
        text = (f"Re-checked “{title}”: {len(identified)} episode file(s) "
                "registered. ")
        if after:
            text += f"Still missing: {after}."
        else:
            text += ("All episodes found now." if before else "No missing episodes.")
        if upcoming:
            text += f" Not on the disk yet: {upcoming}."
        self._health_status.configure(text=text, fg=TEXT)

    def _update_health_buttons(self) -> None:
        """Which of the buttons can be used right now."""
        issue = self._health_issues.selection()
        kind = issue[0] if issue else ""
        selected = bool(self._health_items.selection())
        self._health_dupes_btn.configure(
            state="normal" if kind == "duplicates" else "disabled")
        self._health_relink_btn.configure(
            state="normal" if (selected and kind == "missing") else "disabled")
        self._health_details_btn.configure(
            state="normal" if selected else "disabled")

    def _open_health_details(self) -> None:
        """Show the selected item in the details page of the app."""
        selection = self._health_items.selection()
        if not selection or self._on_open_item is None:
            return
        item = self._db.get(int(selection[0]))
        if item is not None:
            self._on_open_item(item)
