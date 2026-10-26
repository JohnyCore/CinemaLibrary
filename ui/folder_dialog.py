from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.window_utils import setup_popup

PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
FIELD_BG = "#0a0c10"
BTN_BG = "#1a2130"
BTN_ACTIVE = "#243046"
ACCENT = "#e5484d"
ACCENT_HOVER = "#f05a60"
LIST_BG = "#0d1117"
SELECT_BG = "#293650"

NO_FOLDER = "— no folder —"


class FolderDialog(tk.Toplevel):
    """Create a folder in the app and file media into it.

    A folder is a category inside the app: nothing is written to disk and
    no media file is moved.
    """

    def __init__(self, master, collections, items, item=None, create=True,
                 add_to=None) -> None:
        super().__init__(master, bg=PANEL_BG)
        self.collections = list(collections)
        self.items = list(items)
        self.item = item
        self.create = create
        self.add_to = add_to
        self.name: str = ""
        self.collection_id: int | None = None
        self.item_ids: list[int] = []
        self.chosen = False
        self._rows: list[dict] = []
        self._visible_rows: list[dict] = []
        self._selected_item_ids: set[int] = set()

        if add_to is not None:
            self.title("Add Items")
        else:
            self.title("Add Folder" if create else "Move to Folder")
        self.transient(master)
        self.resizable(False, False)
        self.configure(bg=PANEL_BG)

        if add_to is not None:
            self._build_add()
        elif create:
            self._build_create()
        else:
            self._build_move()
        setup_popup(self, master)

    # --- layout -------------------------------------------------------

    def _build_create(self) -> None:
        body = tk.Frame(self, bg=PANEL_BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)

        tk.Frame(body, bg=ACCENT, height=3).pack(fill="x", pady=(0, 16))
        tk.Label(
            body, text="ORGANIZE YOUR LIBRARY", bg=PANEL_BG, fg=ACCENT,
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="w")
        tk.Label(
            body, text="Create a folder", bg=PANEL_BG, fg=TEXT,
            font=("Segoe UI Semibold", 19),
        ).pack(anchor="w", pady=(2, 2))
        tk.Label(
            body, text="Group movies and shows together. You can change this later.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 18))

        tk.Label(body, text="FOLDER NAME", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        self._name = tk.Entry(
            body, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
            bd=0, highlightthickness=1, highlightbackground=BORDER,
            highlightcolor=ACCENT, font=("Segoe UI", 10),
        )
        self._name.pack(fill="x", pady=(6, 16), ipady=7)
        self._name.insert(0, "New Folder")

        items_header = tk.Frame(body, bg=PANEL_BG)
        items_header.pack(fill="x", pady=(0, 7))
        tk.Label(
            items_header, text="ADD ITEMS", bg=PANEL_BG, fg=MUTED,
            font=("Segoe UI Semibold", 8),
        ).pack(side="left")
        self._selection_count = tk.Label(
            items_header, text="Optional", bg=PANEL_BG, fg=MUTED,
            font=("Segoe UI", 8),
        )
        self._selection_count.pack(side="right")

        tools = tk.Frame(body, bg=PANEL_BG)
        tools.pack(fill="x", pady=(0, 8))
        search_box = tk.Frame(
            tools, bg=FIELD_BG, highlightthickness=1,
            highlightbackground=BORDER,
        )
        search_box.pack(side="left", fill="x", expand=True)
        tk.Label(search_box, text="⌕", bg=FIELD_BG, fg=MUTED,
                 font=("Segoe UI", 13)).pack(side="left", padx=(9, 2))
        self._item_search = tk.Entry(
            search_box, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT, bd=0,
            font=("Segoe UI", 9),
        )
        self._item_search.pack(side="left", fill="x", expand=True, padx=(2, 8), ipady=6)
        self._item_search.insert(0, "Search your library")
        self._item_search.configure(fg=MUTED)
        self._item_search.bind("<FocusIn>", self._search_focus_in)
        self._item_search.bind("<FocusOut>", self._search_focus_out)
        self._item_search.bind("<KeyRelease>", self._filter_items)
        self._action_label(
            tools, "Select all", self._select_filtered_items,
            bg=PANEL_BG, fg="#9bb8ff", padx=8,
        ).pack(side="left", padx=(7, 0))

        list_frame = tk.Frame(
            body, bg=LIST_BG, highlightthickness=1, highlightbackground=BORDER,
        )
        list_frame.pack(fill="both", expand=True)
        self._list = tk.Listbox(
            list_frame, bg=LIST_BG, fg=TEXT, selectbackground=SELECT_BG,
            selectforeground=TEXT, activestyle="none", highlightthickness=0,
            bd=0, font=("Segoe UI", 9), height=9, selectmode="extended",
            exportselection=False,
        )
        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self._list.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self._list.configure(yscrollcommand=scrollbar.set)
        self._list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self._list.bind("<<ListboxSelect>>", self._selection_changed)
        self._rows = sorted(
            self.items, key=lambda i: str(i.get("title") or "").casefold(),
        )

        footer = tk.Frame(body, bg=PANEL_BG)
        footer.pack(fill="x", pady=(12, 0))
        self._status = tk.Label(
            footer, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
            anchor="w", justify="left", wraplength=300,
        )
        self._status.pack(side="left", fill="x", expand=True)
        self._buttons("Create folder", self._create_folder, parent=footer)
        self._render_item_list()
        self._name.focus_set()
        self._name.bind("<Return>", lambda _event: self._create_folder())
        self.bind("<Escape>", lambda _event: self.destroy())

    def _search_focus_in(self, _event=None) -> None:
        if self._item_search.get() == "Search your library":
            self._item_search.delete(0, "end")
            self._item_search.configure(fg=TEXT)

    def _search_focus_out(self, _event=None) -> None:
        if not self._item_search.get().strip():
            self._item_search.insert(0, "Search your library")
            self._item_search.configure(fg=MUTED)

    def _filter_items(self, _event=None) -> None:
        self._selection_changed()
        self._render_item_list()

    def _render_item_list(self) -> None:
        query = self._item_search.get().strip().casefold()
        if query == "search your library":
            query = ""
        self._visible_rows = [
            item for item in self._rows
            if not query or query in self._label(item).casefold()
        ]
        self._list.delete(0, "end")
        for index, item in enumerate(self._visible_rows):
            self._list.insert("end", self._label(item))
            if item["id"] in self._selected_item_ids:
                self._list.selection_set(index)
        self._update_selection_count()
        self._update_empty_list_status(query)

    def _selection_changed(self, _event=None) -> None:
        visible_ids = {item["id"] for item in self._visible_rows}
        selected_visible = {
            self._visible_rows[index]["id"] for index in self._list.curselection()
        }
        self._selected_item_ids.difference_update(visible_ids)
        self._selected_item_ids.update(selected_visible)
        self._update_selection_count()

    def _update_selection_count(self) -> None:
        count = len(self._selected_item_ids)
        self._selection_count.configure(
            text=f"{count} selected" if count else "Optional",
            fg=TEXT if count else MUTED,
        )

    def _update_empty_list_status(self, query: str) -> None:
        if not self._visible_rows:
            message = "No titles match your search." if query else "Your library is empty."
            self._status.configure(text=message)
        elif self._status.cget("text") in (
            "", "No titles match your search.", "Your library is empty.",
        ):
            self._status.configure(text="Select titles to add them now, or skip this step.")

    def _select_filtered_items(self) -> None:
        self._selected_item_ids.update(item["id"] for item in self._visible_rows)
        self._render_item_list()

    def _build_move(self) -> None:
        body = tk.Frame(self, bg=PANEL_BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)

        title = str((self.item or {}).get("title") or "item")
        tk.Frame(body, bg=ACCENT, height=3).pack(fill="x", pady=(0, 16))
        tk.Label(body, text="ORGANIZE YOUR LIBRARY", bg=PANEL_BG, fg=ACCENT,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w")
        tk.Label(body, text="Move to folder", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 19)).pack(anchor="w", pady=(2, 2))
        tk.Label(
            body, text=f"Choose where “{title}” belongs.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 16))
        tk.Label(body, text="DESTINATION", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI Semibold", 8)).pack(anchor="w", pady=(0, 6))

        list_frame = tk.Frame(
            body, bg=LIST_BG, highlightthickness=1, highlightbackground=BORDER,
        )
        list_frame.pack(fill="both", expand=True)
        self._list = tk.Listbox(
            list_frame, bg=LIST_BG, fg=TEXT, selectbackground=SELECT_BG,
            selectforeground=TEXT, activestyle="none", highlightthickness=0,
            bd=0, font=("Segoe UI", 10),
            height=min(7, max(2, len(self.collections) + 1)),
            selectmode="browse", exportselection=False,
        )
        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self._list.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self._list.configure(yscrollcommand=scrollbar.set)
        self._list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=6)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self._list.insert("end", "Library  ·  no folder")
        for collection in self.collections:
            self._list.insert("end", collection["name"])
        current = (self.item or {}).get("collection_id")
        if current is not None:
            for index, collection in enumerate(self.collections, start=1):
                if collection["id"] == current:
                    self._list.selection_set(index)
                    break

        footer = tk.Frame(body, bg=PANEL_BG)
        footer.pack(fill="x", pady=(12, 0))
        self._status = tk.Label(
            footer, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
            anchor="w", justify="left", wraplength=280,
        )
        self._status.pack(side="left", fill="x", expand=True)
        self._buttons("Move item", self._move_into, parent=footer)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._move_into())

    def _build_add(self) -> None:
        """Put items from the library into a folder that already exists."""
        body = tk.Frame(self, bg=PANEL_BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        name = next((c["name"] for c in self.collections
                     if c["id"] == self.add_to), "this folder")
        tk.Label(body, text=f"Add items to “{name}”:", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        self._list = tk.Listbox(body, bg="#0f1319", fg=TEXT, highlightthickness=1,
                                highlightbackground=BORDER, font=("Segoe UI", 9),
                                width=58, height=12, selectmode="extended",
                                exportselection=False)
        self._list.pack(fill="both", expand=True, pady=(6, 10))
        for item in sorted(self.items, key=lambda i: str(i.get("title") or "").lower()):
            self._rows.append(item)
            self._list.insert("end", self._label(item))
        self._status = tk.Label(body, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                anchor="w", justify="left", wraplength=440)
        self._status.pack(anchor="w", pady=(0, 8))
        self._buttons("Add", self._add_items)

    def _action_label(
        self, parent, text: str, command, *, bg: str, fg: str,
        hover_bg: str | None = None, padx: int = 16,
    ) -> tk.Label:
        widget = tk.Label(
            parent, text=text, bg=bg, fg=fg, padx=padx, pady=7,
            font=("Segoe UI Semibold", 9), cursor="hand2", takefocus=True,
        )

        def invoke(_event=None):
            command()
            return "break"

        widget.bind("<Button-1>", invoke)
        widget.bind("<Return>", invoke)
        widget.bind("<space>", invoke)
        if hover_bg:
            widget.bind("<Enter>", lambda _event: widget.configure(bg=hover_bg))
            widget.bind("<Leave>", lambda _event: widget.configure(bg=bg))
        return widget

    def _buttons(self, primary: str, command, parent=None) -> None:
        row = tk.Frame(parent or self, bg=PANEL_BG)
        row.pack(anchor="e")
        self._action_label(
            row, primary, command, bg=ACCENT, fg=TEXT, hover_bg=ACCENT_HOVER,
        ).pack(side="left", padx=(0, 8))
        self._action_label(
            row, "Close", self.destroy, bg=BTN_BG, fg=TEXT, hover_bg=BTN_ACTIVE,
        ).pack(side="left")

    @staticmethod
    def _label(item: dict) -> str:
        title = str(item.get("title") or "Untitled")
        year = item.get("year")
        kind = "Series" if item.get("media_type") == "series" else "Movie"
        return f"{title} ({year})  ·  {kind}" if year else f"{title}  ·  {kind}"

    # --- actions ------------------------------------------------------

    def _create_folder(self) -> None:
        name = self._name.get().strip()
        if not name:
            self._status.configure(text="Give the folder a name.")
            return
        if any(name.lower() == c["name"].lower() for c in self.collections):
            self._status.configure(text="A folder with that name already exists.")
            return
        self.name = name
        self.item_ids = [
            item["id"] for item in self._rows
            if item["id"] in self._selected_item_ids
        ]
        self.chosen = True
        self.destroy()

    def _add_items(self) -> None:
        """File the picked items into the folder."""
        self.item_ids = [self._rows[i]["id"] for i in self._list.curselection()]
        if not self.item_ids:
            self._status.configure(text="Pick at least one item.")
            return
        self.chosen = True
        self.destroy()

    def _move_into(self) -> None:
        sel = self._list.curselection()
        if not sel:
            self._status.configure(text="Pick a folder first.")
            return
        index = sel[0]
        self.collection_id = None if index == 0 else self.collections[index - 1]["id"]
        self.chosen = True
        self.destroy()
