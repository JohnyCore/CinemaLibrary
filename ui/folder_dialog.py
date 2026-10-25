from __future__ import annotations

import tkinter as tk

from ui.window_utils import setup_popup

PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
FIELD_BG = "#0a0c10"
BTN_BG = "#1a2130"
BTN_ACTIVE = "#243046"
ACCENT = "#e5484d"

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
        body = tk.Frame(self, bg=PANEL_BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Folder name", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        self._name = tk.Entry(body, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT,
                              bd=0, highlightthickness=1, highlightbackground=BORDER,
                              font=("Segoe UI", 10))
        self._name.pack(fill="x", pady=(4, 12), ipady=4)
        self._name.insert(0, "New Folder")

        tk.Label(body, text="Items to put in it (optional)", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")
        self._list = tk.Listbox(body, bg="#0f1319", fg=TEXT, highlightthickness=1,
                                highlightbackground=BORDER, font=("Segoe UI", 9),
                                width=58, height=12, selectmode="extended",
                                exportselection=False)
        self._list.pack(fill="both", expand=True, pady=(2, 10))
        for item in sorted(self.items, key=lambda i: str(i.get("title") or "").lower()):
            self._rows.append(item)
            self._list.insert("end", self._label(item))

        self._status = tk.Label(body, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                anchor="w", justify="left", wraplength=440)
        self._status.pack(anchor="w", pady=(0, 8))
        self._buttons("Create Folder", self._create_folder)

    def _build_move(self) -> None:
        body = tk.Frame(self, bg=PANEL_BG, padx=18, pady=14)
        body.pack(fill="both", expand=True)

        title = str((self.item or {}).get("title") or "item")
        tk.Label(body, text=f"Move “{title}” into:", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        self._list = tk.Listbox(body, bg="#0f1319", fg=TEXT, highlightthickness=1,
                                highlightbackground=BORDER, font=("Segoe UI", 10),
                                width=52, height=min(9, max(1, len(self.collections) + 1)),
                                exportselection=False)
        self._list.pack(fill="both", expand=True, pady=(6, 10))
        self._list.insert("end", NO_FOLDER)
        for collection in self.collections:
            self._list.insert("end", collection["name"])
        current = (self.item or {}).get("collection_id")
        if current is not None:
            for index, collection in enumerate(self.collections, start=1):
                if collection["id"] == current:
                    self._list.selection_set(index)
                    break

        self._status = tk.Label(body, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                anchor="w", justify="left", wraplength=440)
        self._status.pack(anchor="w", pady=(0, 8))
        self._buttons("Move", self._move_into)

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

    def _buttons(self, primary: str, command) -> None:
        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(anchor="e")
        tk.Button(row, text=primary, command=command, bg=ACCENT, fg=TEXT, bd=0,
                  padx=16, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))
        tk.Button(row, text="Close", command=self.destroy, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=16, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left")

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
        self.item_ids = [self._rows[i]["id"] for i in self._list.curselection()]
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
