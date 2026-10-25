from __future__ import annotations

import os
import re
import sys
import tkinter as tk
from tkinter import filedialog

from ui.window_utils import ScrollArea, setup_popup

PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
FIELD_BG = "#0a0c10"
BTN_BG = "#1a2130"
BTN_ACTIVE = "#243046"
ACCENT = "#e5484d"


class ConfirmDialog(tk.Toplevel):
    """Dark modal confirmation, centred on the app window."""

    def __init__(self, master, title: str, message: str,
                 confirm_text: str = "Delete") -> None:
        super().__init__(master, bg=PANEL_BG)
        self.title(title)
        self.configure(bg=PANEL_BG)
        self.transient(master)
        self.result = False

        tk.Label(self, text=message, bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 10),
                 justify="left", wraplength=460).pack(padx=20, pady=16)
        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(pady=(0, 16))

        def yes() -> None:
            self.result = True
            self.destroy()

        tk.Button(row, text=confirm_text, command=yes, bg=ACCENT, fg=TEXT,
                  bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(row, text="Cancel", command=self.destroy, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)
        setup_popup(self, master)

    def ask(self) -> bool:
        self.grab_set()
        self.wait_window()
        return self.result


class PromptDialog(tk.Toplevel):
    """Dark single-line input, centred on the app window."""

    def __init__(self, master, title: str, message: str, value: str = "") -> None:
        super().__init__(master, bg=PANEL_BG)
        self.title(title)
        self.configure(bg=PANEL_BG)
        self.transient(master)
        self.result: str | None = None

        tk.Label(self, text=message, bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 10),
                 justify="left").pack(padx=20, pady=(16, 8))
        entry = tk.Entry(self, bg="#0a0c10", fg=TEXT, insertbackground=TEXT, bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         font=("Segoe UI", 10), width=44)
        entry.pack(padx=20, fill="x", ipady=5)
        entry.insert(0, value)
        entry.select_range(0, "end")
        entry.focus_set()
        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(pady=(12, 16))

        def ok() -> None:
            self.result = entry.get().strip()
            self.destroy()

        tk.Button(row, text="Save", command=ok, bg=ACCENT, fg=TEXT, bd=0, padx=14,
                  pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(row, text="Cancel", command=self.destroy, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)
        entry.bind("<Return>", lambda _e: ok())
        setup_popup(self, master)

    def ask(self) -> str | None:
        self.grab_set()
        self.wait_window()
        return self.result


class ImdbIdDialog(tk.Toplevel):
    """Ask for an IMDb ID after a failed fetch.

    The user may paste either the bare id (tt0944947) or an imdb.com link;
    whatever the id is inside gets used.
    """

    def __init__(self, master, message: str) -> None:
        super().__init__(master, bg=PANEL_BG)
        self.title("Fetch using IMDb ID")
        self.configure(bg=PANEL_BG)
        self.transient(master)
        self.result: str | None = None

        tk.Label(self, text=message, bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 10),
                 justify="left", wraplength=460).pack(padx=20, pady=(16, 8))
        entry = tk.Entry(self, bg="#0a0c10", fg=TEXT, insertbackground=TEXT, bd=0,
                         highlightthickness=1, highlightbackground=BORDER,
                         font=("Segoe UI", 10), width=44)
        entry.pack(padx=20, fill="x", ipady=5)
        entry.insert(0, "e.g. tt0944947")
        entry.selection_range(0, "end")
        entry.focus_set()
        self._entry = entry
        self._error = tk.Label(self, bg=PANEL_BG, fg=ACCENT, font=("Segoe UI", 9),
                               anchor="w", justify="left", wraplength=460)
        self._error.pack(anchor="w", padx=20, pady=(6, 0))
        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(pady=(12, 16))
        tk.Button(row, text="Fetch", command=self._ok, bg=ACCENT, fg=TEXT,
                  bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(row, text="Cancel", command=self.destroy, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=14, pady=6, cursor="hand2").pack(side="left", padx=6)
        entry.bind("<Return>", lambda _e: self._ok())
        setup_popup(self, master)

    def _ok(self) -> None:
        match = re.search(r"tt\d{5,}", self._entry.get().strip())
        if not match:
            self._error.configure(text="That does not look like an IMDb ID "
                                       "(e.g. tt0944947).")
            return
        self.result = match.group(0)
        self.destroy()

    def ask(self) -> str | None:
        self.grab_set()
        self.wait_window()
        return self.result


class PlayerDialog(tk.Toplevel):
    """Name a video player and point at its executable."""

    def __init__(self, master, name: str = "", path: str = "") -> None:
        super().__init__(master, bg=PANEL_BG)
        self.title("Add a video player")
        self.configure(bg=PANEL_BG)
        self.transient(master)
        self.result: dict | None = None

        tk.Label(self, text="Name", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(16, 2))
        self._name = tk.Entry(self, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT, bd=0,
                              highlightthickness=1, highlightbackground=BORDER,
                              font=("Segoe UI", 10), width=44)
        self._name.pack(padx=20, fill="x", ipady=5)
        self._name.insert(0, name)

        tk.Label(self, text="Program", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(12, 2))
        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(padx=20, fill="x")
        self._path = tk.Entry(row, bg=FIELD_BG, fg=TEXT, insertbackground=TEXT, bd=0,
                              highlightthickness=1, highlightbackground=BORDER,
                              font=("Segoe UI", 10))
        self._path.pack(side="left", fill="x", expand=True, ipady=5)
        self._path.insert(0, path)
        tk.Button(row, text="Browseâ€¦", command=self._browse, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=14, pady=5, font=("Segoe UI", 9), cursor="hand2").pack(
            side="left", padx=(8, 0))

        self._status = tk.Label(self, bg=PANEL_BG, fg=ACCENT, font=("Segoe UI", 9),
                                anchor="w", justify="left")
        self._status.pack(anchor="w", padx=20, pady=(8, 0))

        row2 = tk.Frame(self, bg=PANEL_BG)
        row2.pack(pady=(12, 16))
        tk.Button(row2, text="Save", command=self._save, bg=ACCENT, fg=TEXT, bd=0,
                  padx=16, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left", padx=6)
        tk.Button(row2, text="Cancel", command=self.destroy, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=16, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left", padx=6)
        setup_popup(self, master)

    def _browse(self) -> None:
        types = [("Programs", "*.exe")] if sys.platform == "win32" else []
        types.append(("All files", "*"))
        chosen = filedialog.askopenfilename(
            parent=self, title="Select the player", filetypes=types)
        if chosen:
            self._path.delete(0, "end")
            self._path.insert(0, chosen)
            if not self._name.get().strip():
                from pathlib import Path
                self._name.insert(0, Path(chosen).stem)

    def _save(self) -> None:
        name = self._name.get().strip()
        path = self._path.get().strip()
        if not path:
            self._status.configure(text="Point at the program of the player.")
            return
        if not os.path.exists(path):
            self._status.configure(text=f"There is no file at {path}.")
            return
        self.result = {"name": name or os.path.basename(path), "path": path}
        self.destroy()

    def ask(self) -> dict | None:
        self.grab_set()
        self.wait_window()
        return self.result


def _richness(item: dict) -> tuple:
    """How much a copy of a title knows - the richer one is worth keeping."""
    fields = ("imdb_id", "plot", "genres", "rating", "runtime", "director",
              "writer", "cast_list", "country", "poster_path", "poster2_path")
    known = sum(1 for f in fields if item.get(f))
    return (known, 1 if item.get("poster_path") else 0, len(item.get("path") or ""))


class DuplicateResolver(tk.Toplevel):
    """Choose which copy of a duplicate title stays in the library.

    The copies that are not chosen are taken out of the library: the files
    stay where they are, but the app does not show them any more.
    """

    def __init__(self, master, groups: list[dict]) -> None:
        super().__init__(master, bg=PANEL_BG)
        self.title("Resolve duplicates")
        self.configure(bg=PANEL_BG)
        self.transient(master)
        self.groups = groups
        self.result: list[int] | None = None
        self._keep: dict[str, str] = {}
        self._buttons: dict[str, list[tuple[str, tk.Radiobutton]]] = {}

        tk.Label(self, text="One copy of every title stays - pick which one.",
                 bg=PANEL_BG, fg=TEXT, font=("Segoe UI", 10)).pack(
            anchor="w", padx=18, pady=(14, 8))

        area = ScrollArea(self, PANEL_BG, padding=(18, 0))
        area.pack(fill="both", expand=True)
        inner = area.inner
        for group in groups:
            title = group.get("title") or "?"
            tk.Label(inner, text=title, bg=PANEL_BG, fg=ACCENT,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(8, 2))
            buttons = []
            for item in group["items"]:
                known = _richness(item)
                text = f"{item.get('path')}   ({known[0]} fields)"
                # a plain label, not a radio button: the native one cannot
                # be seen in a dark window
                label = tk.Label(inner, text="â—‹  " + text, bg=PANEL_BG, fg=MUTED,
                                 font=("Segoe UI", 9), anchor="w", cursor="hand2")
                label.pack(anchor="w", fill="x")
                label.bind("<Button-1>",
                           lambda _e, g=group, p=item["path"]: self._choose(g, p))
                buttons.append((item["path"], label))
            self._buttons[str(group["title"])] = buttons
            self._keep[str(group["title"])] = self._best(group)["path"]
        self._keep_best()

        row = tk.Frame(self, bg=PANEL_BG)
        row.pack(pady=(12, 16))
        tk.Button(row, text="Keep the richest", command=self._keep_best, bg=BTN_BG,
                  fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=14, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left", padx=6)
        tk.Button(row, text="Resolve", command=self._resolve, bg=ACCENT, fg=TEXT,
                  bd=0, padx=16, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left", padx=6)
        tk.Button(row, text="Cancel", command=self.destroy, bg=BTN_BG, fg=TEXT,
                  activebackground=BTN_ACTIVE, activeforeground=TEXT,
                  bd=0, padx=16, pady=6, cursor="hand2",
                  font=("Segoe UI", 10)).pack(side="left", padx=6)
        setup_popup(self, master)

    @staticmethod
    def _best(group: dict) -> dict:
        """The copy that knows the most about its title."""
        return max(group["items"], key=_richness)

    def _choose(self, group: dict, path: str) -> None:
        """The copy that stays in the library."""
        key = str(group.get("title") or "?")
        self._keep[key] = path
        for other, label in self._buttons.get(key, []):
            chosen = other == path
            label.configure(text=("â—  " if chosen else "â—‹  ") + label.cget("text")[2:],
                            fg=TEXT if chosen else MUTED)

    def _keep_best(self) -> None:
        for group in self.groups:
            self._choose(group, self._best(group)["path"])

    def _resolve(self) -> None:
        drop: list[int] = []
        for group in self.groups:
            key = str(group.get("title") or "?")
            keep = self._keep.get(key)
            for item in group["items"]:
                if item["path"] != keep:
                    drop.append(item["id"])
        self.result = drop
        self.destroy()

    def ask(self) -> list[int] | None:
        self.grab_set()
        self.wait_window()
        return self.result


class PopupMenu(tk.Toplevel):
    """Dark context menu.

    The native Windows menu draws a light border and a light separator, so
    the menu is drawn with plain widgets instead.
    """

    BG = "#12161d"
    BORDER = "#232b36"
    TEXT = "#e8eaed"
    ACTIVE = "#243041"

    def __init__(self, master, x: int, y: int, entries) -> None:
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.configure(bg=self.BORDER)
        self._done = False

        inner = tk.Frame(self, bg=self.BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        for label, command in entries:
            if label is None:
                tk.Frame(inner, bg=self.BORDER, height=1).pack(
                    fill="x", padx=6, pady=4)
                continue
            item = tk.Label(inner, text=label, bg=self.BG, fg=self.TEXT,
                            font=("Segoe UI", 9), anchor="w", padx=14, pady=5,
                            cursor="hand2")
            item.pack(fill="x")
            item.bind("<Button-1>", lambda _e, c=command: self._run(c))
            item.bind("<Enter>", lambda _e, w=item: w.configure(bg=self.ACTIVE))
            item.bind("<Leave>", lambda _e, w=item: w.configure(bg=self.BG))

        self.bind("<Button-1>", self._dismiss)
        self.bind("<Escape>", self._dismiss)
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = min(x, master.winfo_rootx() + master.winfo_width() - width)
        y = min(y, master.winfo_rooty() + master.winfo_height() - height)
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.deiconify()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _run(self, command) -> None:
        # the menu has to go before the command runs: it holds a grab, and
        # a grab that is not released freezes the whole app
        self._dismiss()
        if command is not None:
            command()

    def _dismiss(self, _event=None) -> None:
        if self._done:
            return
        self._done = True
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass
