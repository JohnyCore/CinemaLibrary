from __future__ import annotations

import hashlib
import logging
import threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path
from typing import Callable

import config
from database.db_manager import Database
from metadata import metadata_service
from ui.dialogs import ConfirmDialog
from ui.icons import photo as icon_photo

log = logging.getLogger(__name__)

BG = "#0d0e12"
PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
ACCENT = "#e5484d"
BTN = dict(
    bg="#1a2130", fg=TEXT, activebackground="#243046",
    activeforeground=TEXT, bd=0, padx=14, pady=6,
    font=("Segoe UI", 10), cursor="hand2",
)
PANEL_INSET = 14
MAX_SEARCH_LENGTH = 200
SEARCH_PLACEHOLDER = "Search by movie or series title"


class WatchedElsewhereView(tk.Frame):
    def __init__(
        self,
        master,
        db: Database,
        on_open_candidate: Callable[[metadata_service.MetadataResult], None],
        on_open_saved: Callable[[int], None],
        post: Callable[[Callable[[], None]], None],
    ) -> None:
        super().__init__(master, bg=BG)
        self._db = db
        self._on_open_candidate = on_open_candidate
        self._on_open_saved = on_open_saved
        self._post = post
        self._generation = 0
        self._candidates: dict[str, metadata_service.MetadataResult] = {}
        self._saved_items: dict[int, dict] = {}
        self._saved_watch_icons = {
            False: icon_photo("eye", size=16, color=TEXT),
            True: icon_photo("eye-slash", size=16, color=TEXT),
        }
        self._saved_watch_disabled_icons = {
            False: icon_photo("eye", size=16, color=MUTED),
            True: icon_photo("eye-slash", size=16, color=MUTED),
        }
        self._saved_watch_watched = False
        self._saved_remove_icon = icon_photo("trash", size=16, color=TEXT)
        self._saved_remove_disabled_icon = icon_photo("trash", size=16, color=MUTED)
        self._tooltip_job: str | None = None
        self._tooltip_window: tk.Toplevel | None = None
        self._build()
        self.refresh_saved()

    def _build(self) -> None:
        header = tk.Frame(self, bg=BG, padx=28)
        header.pack(fill="x", pady=(24, 16))
        tk.Label(
            header, text="Watched Elsewhere", bg=BG, fg=TEXT,
            font=("Segoe UI Semibold", 24),
        ).pack(anchor="w")
        tk.Label(
            header,
            text="Keep a separate, offline record of titles you watched without a local file.",
            bg=BG, fg=MUTED, font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(5, 16))

        search_row = tk.Frame(
            header, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER,
        )
        search_row.pack(fill="x")
        self._query = tk.Entry(
            search_row, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            bd=0, highlightthickness=0, font=("Segoe UI", 10),
        )
        self._query.pack(
            side="left", fill="x", expand=True, ipady=10, padx=(14, 8),
        )
        self._query.bind("<Return>", lambda _event: self.search())
        self._query.bind("<FocusIn>", self._search_focus_in)
        self._query.bind("<FocusOut>", self._search_focus_out)
        self._query.insert(0, SEARCH_PLACEHOLDER)
        self._query.configure(fg=MUTED)
        self._search_btn = tk.Button(
            search_row, text="Search provider", command=self.search,
            bg="#263348", activebackground="#32435c",
            fg=TEXT, activeforeground=TEXT, disabledforeground="#788394",
            bd=0, padx=18, pady=10, font=("Segoe UI Semibold", 10), cursor="hand2",
        )
        self._search_btn.pack(side="right", padx=5, pady=5)

        self._status = tk.Label(
            header, text="Search the provider or open a title saved offline.",
            bg=BG, fg=MUTED, anchor="w", font=("Segoe UI", 9),
        )
        self._status.pack(fill="x", pady=(8, 0))

        content = tk.Frame(self, bg=BG, padx=28)
        content.pack(fill="both", expand=True, pady=(0, 24))
        content.grid_columnconfigure(0, weight=1, uniform="catalog")
        content.grid_columnconfigure(1, weight=1, uniform="catalog")
        content.grid_rowconfigure(0, weight=1)
        self._results_panel = tk.Frame(
            content, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER,
        )
        self._saved_panel = tk.Frame(
            content, bg=PANEL_BG, highlightthickness=1, highlightbackground=BORDER,
        )
        self._results_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._saved_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self._build_tree(
            self._results_panel, "Search results", "Choose a result to review its details.",
            ("title", "year", "type"), ("Title", "Year", "Type"),
            "Search for a movie or series to see provider results.",
            "_results",
        )
        self._build_tree(
            self._saved_panel, "Saved titles", "Your collection, available offline.",
            ("title", "year", "watched"), ("Title", "Year", "Watched"),
            "Saved titles appear here after you add them.",
            "_saved",
        )
        self._results.bind("<Double-Button-1>", lambda _event: self._open_result())
        self._results.bind("<Return>", lambda _event: self._open_result())
        self._saved.bind("<Double-Button-1>", lambda _event: self._open_saved())
        self._saved.bind("<Return>", lambda _event: self._open_saved())
        self._results.bind("<<TreeviewSelect>>", self._selection_changed)
        self._saved.bind("<<TreeviewSelect>>", self._selection_changed)

    def _build_tree(
        self, parent: tk.Frame, title: str, description: str,
        columns: tuple[str, ...], headings: tuple[str, ...],
        empty_text: str, attribute: str,
    ) -> None:
        heading = tk.Frame(parent, bg=PANEL_BG, padx=PANEL_INSET, pady=12)
        heading.pack(fill="x")
        heading.grid_columnconfigure(0, weight=1)
        title_label = tk.Label(
            heading, text=f"{title} · 0", bg=PANEL_BG, fg=TEXT,
            font=("Segoe UI Semibold", 11), anchor="w",
        )
        title_label.grid(row=0, column=0, sticky="w")
        tk.Label(
            heading, text=description, bg=PANEL_BG, fg=MUTED,
            font=("Segoe UI", 9), anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))

        list_area = tk.Frame(parent, bg="#12161d")
        list_area.pack(fill="both", expand=True, padx=PANEL_INSET)
        list_area.grid_columnconfigure(0, weight=1)
        list_area.grid_rowconfigure(0, weight=1)
        tree = ttk.Treeview(
            list_area, columns=columns, show="headings", style="Dark.Treeview",
            selectmode="browse", height=12,
        )
        for column, column_heading in zip(columns, headings):
            tree.heading(column, text=column_heading)
            tree.column(
                column,
                width=74 if column == "year" else 105 if column in ("type", "watched")
                else 280,
                minwidth=60 if column != "title" else 120,
                stretch=column == "title",
            )
        scrollbar = ttk.Scrollbar(
            list_area, orient="vertical", command=tree.yview,
            style="Dark.Vertical.TScrollbar",
        )
        tree.configure(yscrollcommand=scrollbar.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        empty = tk.Label(
            list_area, text=empty_text, bg="#12161d", fg=MUTED,
            font=("Segoe UI", 10), justify="center", wraplength=250,
        )
        empty.place(relx=0.5, rely=0.5, anchor="center")

        footer = tk.Frame(parent, bg=PANEL_BG, padx=PANEL_INSET, pady=12)
        footer.pack(fill="x")
        footer_actions = tk.Frame(footer, bg=PANEL_BG)
        footer_actions.pack(fill="x")
        tk.Label(
            footer_actions, text="Double-click a title to open", bg=PANEL_BG,
            fg=MUTED, font=("Segoe UI", 9),
        ).pack(side="left")
        button = tk.Button(
            footer_actions, text="Open details", command=(
                self._open_result if attribute == "_results" else self._open_saved),
            state="disabled", disabledforeground="#687384", **BTN,
        )
        button.pack(side="right")
        if attribute == "_saved":
            self._saved_remove = tk.Button(
                footer_actions, image=self._saved_remove_disabled_icon,
                command=self._remove_saved,
                state="disabled", bd=0, padx=14, pady=6, cursor="hand2",
                bg="#3a2028", activebackground="#512a34",
                highlightthickness=1, highlightbackground="#512a34",
                highlightcolor="#512a34",
            )
            self._saved_remove_help = "Remove this title and its saved artwork"
            self._bind_tooltip(self._saved_remove, self._saved_remove_help)
            self._saved_remove.pack(side="right", padx=(0, 8))
            self._saved_watch = tk.Button(
                footer_actions, image=self._saved_watch_disabled_icons[False],
                command=self._toggle_saved_watched,
                state="disabled",
                bd=0, padx=14, pady=6, cursor="hand2",
                bg="#1a2130", activebackground="#243046",
                highlightthickness=1, highlightbackground=BORDER,
                highlightcolor=BORDER,
            )
            self._saved_watch_help = "Select a saved title to change its watched status"
            self._bind_tooltip(
                self._saved_watch, lambda: self._saved_watch_help,
            )
            self._saved_watch.pack(side="right", padx=(0, 8))

        setattr(self, attribute, tree)
        setattr(self, f"{attribute}_empty", empty)
        setattr(self, f"{attribute}_heading", title_label)
        setattr(self, f"{attribute}_open", button)

    def _search_focus_in(self, _event=None) -> None:
        if self._query.get() == SEARCH_PLACEHOLDER:
            self._query.delete(0, "end")
            self._query.configure(fg=TEXT)

    def _search_focus_out(self, _event=None) -> None:
        if not self._query.get().strip():
            self._query.insert(0, SEARCH_PLACEHOLDER)
            self._query.configure(fg=MUTED)

    def _selection_changed(self, _event=None) -> None:
        self._results_open.configure(
            state="normal" if self._results.selection() else "disabled")
        self._saved_open.configure(
            state="normal" if self._saved.selection() else "disabled")
        selected = self._selected_saved_item()
        if selected is None:
            watched = self._saved_watch_watched
            self._saved_watch.configure(
                image=self._saved_watch_disabled_icons[watched],
                state="disabled",
            )
            self._saved_remove.configure(
                image=self._saved_remove_disabled_icon,
                state="disabled",
            )
            self._saved_watch_help = "Select a saved title to change its watched status"
        else:
            watched = bool(selected.get("watched"))
            self._saved_watch_watched = watched
            self._saved_watch_help = (
                "Mark this title as unwatched" if watched
                else "Mark this title as watched"
            )
            self._saved_watch.configure(
                image=self._saved_watch_icons[watched],
                state="normal",
            )
            self._saved_remove.configure(
                image=self._saved_remove_icon, state="normal",
            )

    def _selected_saved_item(self) -> dict | None:
        selected = self._saved.selection()
        if not selected:
            return None
        try:
            return self._saved_items.get(int(selected[0]))
        except ValueError:
            log.error("invalid watched-elsewhere row id: %r", selected[0])
            return None

    def _bind_tooltip(
        self, widget: tk.Widget, text: str | Callable[[], str],
    ) -> None:
        widget.bind(
            "<Enter>", lambda _event: self._schedule_tooltip(
                text() if callable(text) else text,
            ),
            add="+",
        )
        widget.bind("<Leave>", lambda _event: self._hide_tooltip(), add="+")
        widget.bind(
            "<FocusIn>", lambda _event: self._schedule_tooltip(
                text() if callable(text) else text,
            ),
            add="+",
        )
        widget.bind("<FocusOut>", lambda _event: self._hide_tooltip(), add="+")

    def _schedule_tooltip(self, text: str) -> None:
        self._hide_tooltip()
        self._tooltip_job = self.after(
            500, lambda: self._show_tooltip(text),
        )

    def _show_tooltip(self, text: str) -> None:
        self._tooltip_job = None
        window = tk.Toplevel(self)
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        tk.Label(
            window, text=text, bg="#202833", fg=TEXT,
            font=("Segoe UI", 9), padx=8, pady=4,
        ).pack()
        pointer_x = self.winfo_pointerx()
        pointer_y = self.winfo_pointery()
        window.wm_geometry(f"+{pointer_x + 12}+{pointer_y + 18}")
        self._tooltip_window = window

    def _hide_tooltip(self) -> None:
        if self._tooltip_job is not None:
            self.after_cancel(self._tooltip_job)
            self._tooltip_job = None
        if self._tooltip_window is not None:
            self._tooltip_window.destroy()
            self._tooltip_window = None

    def _toggle_saved_watched(self) -> None:
        item = self._selected_saved_item()
        if item is None:
            self._status.configure(text="Select a saved title first.")
            return
        watched = not bool(item.get("watched"))
        try:
            if not self._db.set_watched_elsewhere(item["id"], watched):
                raise RuntimeError("the saved title no longer exists")
        except Exception as exc:
            log.exception("could not update watched status for saved title %s",
                          item.get("title"))
            self._status.configure(text=f"Could not update watched status: {exc}")
            self.refresh_saved()
            return
        title = item.get("title") or "Untitled"
        self.refresh_saved(selected_id=item["id"])
        self._status.configure(
            text=f"Marked {title} as {'watched' if watched else 'unwatched'}.")

    def _remove_saved(self) -> None:
        item = self._selected_saved_item()
        if item is None:
            self._status.configure(text="Select a saved title first.")
            return
        title = item.get("title") or "Untitled"
        if not ConfirmDialog(
            self, "Remove saved title",
            f"Remove {title} from Watched Elsewhere?\n\n"
            "Its saved metadata and cached artwork will be deleted.",
            "Remove",
        ).ask():
            return
        try:
            self._remove_saved_artwork(item)
            if not self._db.delete_watched_elsewhere(item["id"]):
                raise RuntimeError("the saved title no longer exists")
        except (OSError, RuntimeError, ValueError) as exc:
            log.exception("could not remove watched-elsewhere title %s", title)
            self._status.configure(text=f"Could not remove {title}: {exc}")
            return
        self.refresh_saved()
        self._status.configure(text=f"Removed {title} and its saved artwork.")

    @staticmethod
    def _remove_saved_artwork(item: dict) -> None:
        paths = [
            (item.get("poster_path"), "poster.jpg"),
            (item.get("poster2_path"), "poster2.jpg"),
        ]
        paths = [(raw, name) for raw, name in paths if raw]
        if not paths:
            return

        root = config.WATCHED_ELSEWHERE_DIR.resolve()
        cache_key = (
            f"{str(item.get('provider') or '').casefold()}|"
            f"{item.get('media_type')}|"
            f"{str(item.get('provider_id') or '').casefold()}"
        )
        directory = root / hashlib.sha256(
            cache_key.encode("utf-8")).hexdigest()[:24]
        if directory.is_symlink():
            raise ValueError("the artwork directory is not a trusted cache folder")
        resolved_directory = directory.resolve()
        if resolved_directory.parent != root:
            raise ValueError("the artwork directory is outside the cache")

        targets = []
        for raw, expected_name in paths:
            target = Path(raw)
            if target.name != expected_name or target.is_symlink():
                raise ValueError("the saved artwork path is not a trusted cache file")
            if target.resolve().parent != resolved_directory:
                raise ValueError("the saved artwork path is outside this title's cache")
            targets.append(target)
        for target in targets:
            if target.exists():
                target.unlink()
        if directory.exists():
            try:
                directory.rmdir()
            except OSError:
                log.warning("leaving non-empty watched-elsewhere cache directory %s",
                            directory)

    def _update_list_state(self, tree, empty, heading, title: str) -> None:
        count = len(tree.get_children())
        heading.configure(text=f"{title} · {count}")
        if count:
            empty.place_forget()
        else:
            empty.place(relx=0.5, rely=0.5, anchor="center")

    def search(self) -> None:
        query = self._query.get().strip()
        if query == SEARCH_PLACEHOLDER:
            query = ""
        if not query:
            self._status.configure(text="Enter a movie or series title to search.")
            self._query.focus_set()
            return
        if len(query) > MAX_SEARCH_LENGTH:
            self._status.configure(
                text=f"Search text must be {MAX_SEARCH_LENGTH} characters or fewer.")
            self._query.focus_set()
            return
        self._generation += 1
        generation = self._generation
        self._search_btn.configure(state="disabled", text="Searching…")
        self._status.configure(text="Searching the connected metadata provider…")

        def worker() -> None:
            candidates = None
            error = None
            try:
                candidates = metadata_service.search_candidates(query)
            except Exception as exc:
                error = exc
                log.warning("watched-elsewhere search failed for %r: %s", query, exc)
            self._post(lambda: self._search_finished(
                generation, candidates, error))

        threading.Thread(target=worker, daemon=True).start()

    def _search_finished(self, generation, candidates, error) -> None:
        if generation != self._generation or not self.winfo_exists():
            return
        self._search_btn.configure(state="normal", text="Search provider")
        self._results.delete(*self._results.get_children())
        self._candidates.clear()
        if error is not None:
            self._results_empty.configure(text="Search failed. Try again.")
            self._update_list_state(
                self._results, self._results_empty, self._results_heading,
                "Search results",
            )
            self._selection_changed()
            self._status.configure(text=f"Search failed: {error}")
            return
        for index, candidate in enumerate(candidates or []):
            iid = f"candidate-{index}"
            self._candidates[iid] = candidate
            self._results.insert(
                "", "end", iid=iid,
                values=(
                    candidate.title or "Untitled", candidate.year or "",
                    "Series" if candidate.media_type == "series" else "Movie",
                ),
            )
        count = len(candidates or [])
        self._results_empty.configure(
            text="No matching titles. Try a different search."
            if not count else
            "Search for a movie or series to see provider results."
        )
        self._update_list_state(
            self._results, self._results_empty, self._results_heading,
            "Search results",
        )
        self._selection_changed()
        self._status.configure(
            text=f"{count} provider result{'s' if count != 1 else ''}. "
                 "Open a result to review its details before saving."
            if count else "No matching titles were found.",
        )

    def _open_result(self) -> None:
        selected = self._results.selection()
        if not selected:
            self._status.configure(text="Select a provider result first.")
            return
        candidate = self._candidates.get(selected[0])
        if candidate is not None:
            self._on_open_candidate(candidate)

    def _open_saved(self) -> None:
        selected = self._saved.selection()
        if not selected:
            self._status.configure(text="Select a saved title first.")
            return
        try:
            item_id = int(selected[0])
        except ValueError:
            log.error("invalid watched-elsewhere row id: %r", selected[0])
            self._status.configure(text="The selected saved title is invalid.")
            return
        self._on_open_saved(item_id)

    def refresh_saved(self, selected_id: int | None = None) -> None:
        if selected_id is None:
            selected = self._saved.selection()
            if selected:
                try:
                    selected_id = int(selected[0])
                except ValueError:
                    selected_id = None
        self._saved.delete(*self._saved.get_children())
        items = self._db.list_watched_elsewhere()
        self._saved_items = {int(item["id"]): item for item in items}
        for item in items:
            self._saved.insert(
                "", "end", iid=str(item["id"]),
                values=(
                    item.get("title") or "Untitled", item.get("year") or "",
                    "Yes" if item.get("watched") else "No",
                ),
            )
        if selected_id is not None and str(selected_id) in self._saved.get_children():
            self._saved.selection_set(str(selected_id))
        self._update_list_state(
            self._saved, self._saved_empty, self._saved_heading, "Saved titles")
        self._selection_changed()

    def set_status(self, text: str) -> None:
        self._status.configure(text=text)
