from __future__ import annotations

import logging
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import config
from ui.dialogs import ConfirmDialog, PromptDialog
from utils import organizer

log = logging.getLogger(__name__)

PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
FIELD_BG = "#0a0c10"
BTN_BG = "#1a2130"
BTN_ACTIVE = "#243046"
ACCENT = "#e5484d"


class OrganizerView(tk.Frame):
    """Tab that sorts loose video files into folders named after the file."""

    def __init__(self, master, on_rescan=None, post=None) -> None:
        super().__init__(master, bg=PANEL_BG, highlightthickness=0, bd=0)
        self._on_rescan = on_rescan
        self._post = post or (lambda fn: self.after(0, fn))
        self._entries: list[organizer.PlanEntry] = []
        self._scanned: Path | None = None
        self._running = False
        self._last_moved: list[tuple[Path, Path]] = []

        inner = tk.Frame(self, bg=PANEL_BG, padx=22, pady=20)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="Sort out a messy folder", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 12)).pack(anchor="w")
        tk.Label(
            inner,
            text="Every loose video file is moved into a folder named after the "
                 "file: a movie into “Title Year Quality”, a series into "
                 "“Title Quality / Season N”.\n"
                 "The files are only moved, never renamed. Double click a row to "
                 "change the folder name. The episode files of a series are "
                 "renamed in the EP Renamer tab.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(2, 14))

        self._path_var = tk.StringVar(value=self._saved_path())
        row = tk.Frame(inner, bg=PANEL_BG)
        row.pack(anchor="w", pady=(0, 10))
        tk.Label(row, text="Folder", bg=PANEL_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(side="left")
        self._path_entry = tk.Entry(
            row, textvariable=self._path_var, bg=FIELD_BG, fg=TEXT,
            insertbackground=TEXT, bd=0, highlightthickness=1,
            highlightbackground=BORDER, font=("Segoe UI", 10), width=52,
        )
        self._path_entry.pack(side="left", ipady=5, padx=(8, 0))
        self._path_entry.bind("<Return>", lambda _e: self._scan())
        tk.Button(
            row, text="Browse…", command=self._browse, bg=BTN_BG, fg=TEXT,
            activebackground=BTN_ACTIVE, activeforeground=TEXT, bd=0,
            padx=14, pady=5, font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        buttons = tk.Frame(inner, bg=PANEL_BG)
        buttons.pack(anchor="w", pady=(0, 10))
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=16, pady=6, font=("Segoe UI", 10), cursor="hand2",
        )
        tk.Button(buttons, text="Scan folder", command=self._scan,
                  **btn_style).pack(side="left")
        self._organize_btn = tk.Button(
            buttons, text="Organize files", command=self._organize,
            state="disabled", disabledforeground="#5a6472", **btn_style)
        self._organize_btn.pack(side="left", padx=(10, 0))
        self._undo_btn = tk.Button(
            buttons, text="Undo", command=self._undo,
            state="disabled", disabledforeground="#5a6472", **btn_style)
        self._undo_btn.pack(side="left", padx=(10, 0))

        self._status = tk.Label(inner, bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9),
                                anchor="w", justify="left")
        self._status.pack(anchor="w", pady=(0, 12))

        vsb = ttk.Scrollbar(inner, orient="vertical",
                             command=lambda *a: self._tree.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.pack(side="right", fill="y")
        wrap = tk.Frame(inner, bg=PANEL_BG, highlightthickness=1,
                        highlightbackground=BORDER)
        wrap.pack(side="left", fill="both", expand=True)
        columns = ("file", "kind", "target", "result")
        self._tree = ttk.Treeview(wrap, columns=columns, show="headings",
                                  style="Settings.Treeview", height=16,
                                  selectmode="browse")
        self._tree.heading("file", text="File")
        self._tree.heading("kind", text="Kind")
        self._tree.heading("target", text="Target folder")
        self._tree.heading("result", text="Result")
        self._tree.column("file", width=330, stretch=True)
        self._tree.column("kind", width=70, stretch=False)
        self._tree.column("target", width=280, stretch=True)
        self._tree.column("result", width=110, stretch=False)
        self._tree.tag_configure("odd", background="#151a24")
        self._tree.tag_configure("skip", foreground="#8a93a2")
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        self._tree.bind("<Double-1>", self._on_double_click)

    # --- folder -------------------------------------------------------

    def _saved_path(self) -> str:
        """The folder the organizer worked on last."""
        settings = config.load_settings()
        return str(settings.get("organizer_dir") or config.MOVIES_DIR or "")

    def _remember_path(self, path: Path) -> None:
        """Keep the folder for the next start."""
        config.save_settings({"organizer_dir": str(path)})

    def refresh(self) -> None:
        """Show the files of the folder when the tab is opened."""
        if self._scanned is None and not self._running:
            self._scan()

    def _browse(self) -> None:
        start = self._path_var.get()
        if not start or not Path(start).exists():
            start = str(Path.home())
        chosen = filedialog.askdirectory(parent=self, initialdir=start,
                                         title="Select folder")
        if chosen:
            self._path_var.set(chosen)
            self._scan()

    def _source(self) -> Path | None:
        raw = self._path_var.get().strip()
        if not raw:
            self._status.configure(text="Choose the folder to sort out.")
            return None
        path = Path(raw)
        if not path.is_dir():
            self._status.configure(text=f"Not a folder: {path}")
            return None
        return path

    # --- scanning -----------------------------------------------------

    def _scan(self) -> None:
        if self._running:
            return
        source = self._source()
        if source is None:
            return
        self._scanned = source
        self._entries = organizer.plan(source)
        self._remember_path(source)

        for iid in self._tree.get_children():
            self._tree.delete(iid)
        for index, entry in enumerate(self._entries):
            kind = {"movie": "Movie", "series": "Series"}.get(entry.kind, "")
            target = entry.target_dir if entry.kind != "skip" else entry.reason
            tags = ("odd",) if index % 2 else ()
            if entry.kind == "skip":
                tags = tags + ("skip",)
            self._tree.insert("", "end", iid=str(index),
                              values=(entry.source.name, kind, target, entry.result),
                              tags=tags)

        todo = sum(1 for e in self._entries if e.kind != "skip")
        skipped = len(self._entries) - todo
        text = f"{len(self._entries)} file(s) in {source}"
        if todo:
            text += f" · {todo} to move"
        if skipped:
            text += f" · {skipped} left alone"
        self._status.configure(text=text)
        self._organize_btn.configure(state="normal" if todo else "disabled")

    # --- moving -------------------------------------------------------

    def _organize(self) -> None:
        if self._running:
            return
        source = self._source()
        if source is None:
            return
        if self._scanned is None or source != self._scanned:
            self._status.configure(text="The folder changed - scan it again.")
            return
        todo = [e for e in self._entries if e.kind != "skip"]
        if not todo:
            return
        if not ConfirmDialog(
            self, "Organize files",
            f"This moves {len(todo)} file(s) into new folders under:\n{source}\n\n"
            "The files are only moved, never renamed.\n\nContinue?",
            "Move files",
        ).ask():
            return

        self._running = True
        self._organize_btn.configure(state="disabled", text="Moving…")
        self._status.configure(text=f"Moving 0/{len(todo)}…")

        def worker() -> None:
            for index, entry in enumerate(todo, 1):
                organizer.move_entry(entry, source)
                self._post(lambda i=index: self._status.configure(
                    text=f"Moving {i}/{len(todo)}…"))
            self._post(self._finished)

        threading.Thread(target=worker, daemon=True).start()

    def _finished(self) -> None:
        self._running = False
        left = [e for e in self._entries
                if e.kind != "skip" and e.source.exists()]
        self._organize_btn.configure(
            state="normal" if left else "disabled", text="Organize files")
        moved = sum(1 for e in self._entries if e.result == "moved")
        # the run can be taken back: the files are put where they were
        self._last_moved = [(e.source, self._scanned / e.target)
                            for e in self._entries if e.result == "moved"]
        self._undo_btn.configure(
            state="normal" if self._last_moved else "disabled")
        problems = [e for e in self._entries
                    if e.result.startswith("failed") or e.result == "already there"]
        text = f"Moved {moved} file(s)."
        if problems:
            names = ", ".join(e.source.name for e in problems[:3])
            more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
            text += f" {len(problems)} not moved: {names}{more}"
        self._status.configure(text=text)
        rows = set(self._tree.get_children())
        for index, entry in enumerate(self._entries):
            if str(index) in rows:
                self._tree.set(str(index), "result", entry.result)
        if self._on_rescan is not None:
            self._on_rescan()

    def _undo(self) -> None:
        """Move the files of the last run back where they were."""
        if self._running or not self._last_moved:
            return
        moves = list(self._last_moved)
        source = self._scanned
        if source is None:
            return
        self._running = True
        self._undo_btn.configure(state="disabled")
        self._status.configure(text=f"Moving {len(moves)} file(s) back…")

        def worker() -> None:
            back = 0
            for origin, target in moves:
                try:
                    if target.exists() and not origin.exists():
                        origin.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(target), str(origin))
                        # the folder the file went into is empty now
                        organizer.drop_empty(target.parent, source)
                        back += 1
                except OSError as exc:
                    log.warning("could not move %s back: %s", target, exc)
            self._post(lambda: self._undone(back, len(moves)))

        threading.Thread(target=worker, daemon=True).start()

    def _undone(self, back: int, total: int) -> None:
        """The last run was taken back."""
        self._running = False
        self._last_moved = []
        self._undo_btn.configure(state="disabled")
        self._status.configure(text=f"{back} of {total} file(s) moved back.")
        self._scan()

    # --- renaming -----------------------------------------------------

    def _on_double_click(self, event) -> None:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return
        entry = self._entries[int(iid)]
        if entry.kind == "skip":
            return
        name = PromptDialog(self, "Rename folder", "Folder name:",
                            entry.folder).ask()
        if not name:
            return
        entry.folder = organizer.sanitize(name)
        self._tree.set(iid, "target", entry.target_dir)
