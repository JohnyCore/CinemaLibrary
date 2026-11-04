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
        self._undo_source: Path | None = None
        self._last_folders: list[tuple[str, Path]] = []
        self._last_transferred: list[tuple[Path, Path]] = []
        self._transfer_roots: set[Path] = set()

        inner = tk.Frame(self, bg=PANEL_BG, padx=22, pady=20)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="Organizer", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(
            inner,
            text="Every loose video file is moved into a folder named after the "
                 "file: a movie into “Title Year Quality”, a series into "
                 "“Title Quality / Season N”. Files are moved, never renamed. "
                 "Double click a row to "
                 "change the folder name. The episode files of a series are "
                 "renamed in the EP Renamer tab.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(3, 14))

        self._path_var = tk.StringVar(value=self._saved_path())
        controls = tk.Frame(
            inner, bg="#0f1319", highlightthickness=1,
            highlightbackground=BORDER, padx=14, pady=12,
        )
        controls.pack(fill="x", pady=(0, 12))
        tk.Label(controls, text="Source folder", bg="#0f1319", fg=TEXT,
                 font=("Segoe UI Semibold", 10)).pack(anchor="w")
        row = tk.Frame(controls, bg="#0f1319")
        row.pack(fill="x", pady=(8, 10))
        self._path_entry = tk.Entry(
            row, textvariable=self._path_var, bg=FIELD_BG, fg=TEXT,
            insertbackground=TEXT, bd=0, highlightthickness=1,
            highlightbackground=BORDER, font=("Segoe UI", 10),
        )
        self._path_entry.pack(side="left", fill="x", expand=True, ipady=6)
        self._path_entry.bind("<Return>", lambda _e: self._scan(remember=True))
        tk.Button(
            row, text="Browse…", command=self._browse, bg=BTN_BG, fg=TEXT,
            activebackground=BTN_ACTIVE, activeforeground=TEXT, bd=0,
            padx=14, pady=5, font=("Segoe UI", 9), cursor="hand2",
        ).pack(side="left", padx=(8, 0))

        buttons = tk.Frame(controls, bg="#0f1319")
        buttons.pack(fill="x")
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=16, pady=6, font=("Segoe UI", 10), cursor="hand2",
        )
        self._scan_btn = tk.Button(
            buttons, text="Scan folder", command=lambda: self._scan(remember=True),
            bg=ACCENT, fg=TEXT, activebackground="#f05a60",
            activeforeground=TEXT, bd=0, padx=16, pady=6,
            font=("Segoe UI", 10, "bold"), cursor="hand2",
        )
        self._scan_btn.pack(side="left")
        self._organize_btn = tk.Button(
            buttons, text="Organize files", command=self._organize,
            state="disabled", disabledforeground="#5a6472", **btn_style)
        self._organize_btn.pack(side="left", padx=(10, 0))
        self._undo_btn = tk.Button(
            buttons, text="Undo", command=self._undo,
            state="disabled", disabledforeground="#5a6472", **btn_style)
        self._undo_btn.pack(side="left", padx=(10, 0))
        self._move_to_library_btn = tk.Button(
            buttons, text="Move to Movie/TV Shows folder",
            command=self._move_to_library, state="disabled",
            disabledforeground="#5a6472", **btn_style)
        self._move_to_library_btn.pack(side="left", padx=(10, 0))

        self._status = tk.Label(
            controls, bg="#0f1319", fg=MUTED, font=("Segoe UI", 9),
            anchor="w", justify="left",
        )
        self._status.pack(anchor="w", pady=(10, 0))

        vsb = ttk.Scrollbar(inner, orient="vertical",
                             command=lambda *a: self._tree.yview(*a),
                             style="Dark.Vertical.TScrollbar")
        vsb.pack(side="right", fill="y")
        wrap = tk.Frame(inner, bg="#0f1319", highlightthickness=1,
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
            self._scan(remember=True)

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

    def _scan(self, remember: bool = False) -> None:
        if self._running:
            return
        source = self._source()
        if source is None:
            return
        self._running = True
        self._scan_btn.configure(state="disabled")
        self._organize_btn.configure(state="disabled")
        self._undo_btn.configure(state="disabled")
        self._move_to_library_btn.configure(state="disabled")
        self._status.configure(text=f"Scanning {source}…")

        def worker() -> None:
            try:
                entries = organizer.plan(source)
            except Exception:
                log.exception("could not scan organizer folder %s", source)
                self._post(lambda: self._scan_failed(source))
                return
            self._post(lambda: self._show_scan(source, entries, remember))

        threading.Thread(target=worker, daemon=True).start()

    def _scan_failed(self, source: Path) -> None:
        self._running = False
        self._scan_btn.configure(state="normal")
        has_todo = any(entry.kind != "skip" for entry in self._entries)
        self._organize_btn.configure(
            state="normal" if source == self._scanned and has_todo else "disabled")
        self._undo_btn.configure(
            state="normal" if self._last_moved or self._last_transferred else "disabled")
        self._move_to_library_btn.configure(
            state="normal" if self._last_folders else "disabled")
        self._status.configure(text=f"Could not scan {source}. See the application log.")

    def _show_scan(
        self, source: Path, entries: list[organizer.PlanEntry], remember: bool = False,
    ) -> None:
        self._running = False
        self._scan_btn.configure(state="normal")
        self._undo_btn.configure(
            state="normal" if self._last_moved or self._last_transferred else "disabled")
        self._move_to_library_btn.configure(
            state="normal" if self._last_folders else "disabled")
        self._scanned = source
        self._entries = entries
        if remember:
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
            try:
                for index, entry in enumerate(todo, 1):
                    try:
                        organizer.move_entry(entry, source)
                    except Exception as exc:
                        entry.result = f"failed: {exc}"
                        log.exception("could not organize %s", entry.source)
                    self._post(lambda i=index: self._status.configure(
                        text=f"Moving {i}/{len(todo)}…"))
            except Exception:
                log.exception("organizer run failed")
            finally:
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
        self._undo_source = self._scanned
        self._last_transferred = []
        self._transfer_roots.clear()
        self._last_folders = list(dict.fromkeys(
            (e.kind, self._scanned / e.folder)
            for e in self._entries
            if e.result == "moved" and e.kind in ("movie", "series")
        ))
        self._undo_btn.configure(
            state="normal" if self._last_moved else "disabled")
        self._move_to_library_btn.configure(
            state="normal" if self._last_folders else "disabled")
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

    def _move_to_library(self) -> None:
        if self._running or not self._last_folders:
            return
        folders = list(self._last_folders)
        movie_root = config.MOVIES_DIR
        series_root = config.SERIES_DIR
        destinations = {
            "movie": movie_root,
            "series": series_root,
        }
        configured = [
            f"{path.name} → {destinations[kind]}"
            for kind, path in folders
            if destinations[kind] is not None
        ]
        if not configured:
            self._status.configure(
                text="Set the Movies and TV series folders in General settings first.")
            return
        destination_text = "\n".join(configured[:4])
        if len(configured) > 4:
            destination_text += f"\n… and {len(configured) - 4} more"
        if not ConfirmDialog(
            self, "Move to Movie/TV Shows folder",
            "Move the organized folders to their configured library locations?\n\n"
            f"{destination_text}\n\n"
            "Existing folders will not be overwritten. Undo can reverse these moves.",
            "Move folders",
        ).ask():
            return

        self._running = True
        self._organize_btn.configure(state="disabled")
        self._undo_btn.configure(state="disabled")
        self._move_to_library_btn.configure(state="disabled")
        self._status.configure(text=f"Moving {len(folders)} folder(s) to the library…")

        def worker() -> None:
            try:
                moved, problems = organizer.transfer_organized_folders(
                    folders, movie_root, series_root,
                )
            except Exception:
                log.exception("organizer library transfer failed")
                moved, problems = [], ["The transfer failed. See the application log."]
            self._post(lambda: self._library_transfer_finished(moved, problems))

        threading.Thread(target=worker, daemon=True).start()

    def _library_transfer_finished(
        self, moved: list[tuple[Path, Path]], problems: list[str],
    ) -> None:
        self._running = False
        self._last_transferred.extend(moved)
        for source_folder, destination_folder in moved:
            self._transfer_roots.add(destination_folder.parent)
            self._last_moved = [
                (origin, destination_folder / target.relative_to(source_folder))
                if target == source_folder or source_folder in target.parents
                else (origin, target)
                for origin, target in self._last_moved
            ]
            for entry in self._entries:
                if (entry.result == "moved" and self._scanned is not None
                        and self._scanned / entry.folder == source_folder):
                    entry.result = "in library"

        self._last_folders = [
            (kind, source)
            for kind, source in self._last_folders
            if source.exists()
            and (config.MOVIES_DIR if kind == "movie" else config.SERIES_DIR) is not None
            and source.resolve() != (
                (config.MOVIES_DIR if kind == "movie" else config.SERIES_DIR)
                / source.name
            ).resolve()
        ]
        self._undo_btn.configure(
            state="normal" if self._last_moved else "disabled")
        self._move_to_library_btn.configure(
            state="normal" if self._last_folders else "disabled")
        moved_count = len(moved)
        text = f"Moved {moved_count} folder(s) to the library."
        if problems:
            text += "\n" + "\n".join(problems[:3])
            if len(problems) > 3:
                text += f"\n(+{len(problems) - 3} more)"
        self._status.configure(text=text)
        for index, entry in enumerate(self._entries):
            if self._tree.exists(str(index)):
                self._tree.set(str(index), "result", entry.result)
        if moved and self._on_rescan is not None:
            self._on_rescan()

    def _undo(self) -> None:
        """Move transferred folders and organized files back to their sources."""
        if self._running or not (self._last_moved or self._last_transferred):
            return
        moves = list(self._last_moved)
        transferred = list(self._last_transferred)
        source = self._undo_source or self._scanned
        if source is None:
            return
        self._running = True
        self._undo_btn.configure(state="disabled")
        self._move_to_library_btn.configure(state="disabled")
        self._status.configure(text=f"Moving {len(moves)} file(s) back…")

        def worker() -> None:
            back = 0
            problems: list[str] = []
            try:
                undo_moves = moves
                restored, folder_problems = organizer.restore_transferred_folders(
                    transferred,
                )
                problems.extend(folder_problems)
                for source_folder, destination_folder in restored:
                    undo_moves = [
                        (origin, source_folder / target.relative_to(destination_folder))
                        if target == destination_folder or destination_folder in target.parents
                        else (origin, target)
                        for origin, target in undo_moves
                    ]
                for origin, target in undo_moves:
                    try:
                        if target.exists() and not origin.exists():
                            origin.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(target), str(origin))
                            keep = next(
                                (root for root in self._transfer_roots
                                 if target == root or root in target.parents),
                                source,
                            )
                            organizer.drop_empty(target.parent, keep)
                            back += 1
                        elif origin.exists():
                            problems.append(f"{origin.name}: original file already exists")
                        else:
                            problems.append(f"{target.name}: organized file is missing")
                    except OSError as exc:
                        log.exception("could not undo organizer move %s", target)
                        problems.append(f"{target.name}: {exc}")
            except Exception:
                log.exception("organizer undo failed")
                problems.append("Undo failed. See the application log.")
            remaining_files = [
                (origin, target) for origin, target in undo_moves
                if target.exists() and not origin.exists()
            ]
            remaining_folders = [
                (original, destination) for original, destination in transferred
                if destination.exists() and not original.exists()
            ]
            self._post(lambda: self._undone(
                back, len(moves), remaining_files,
                remaining_folders, problems,
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _undone(
        self,
        back: int,
        total: int,
        remaining_files: list[tuple[Path, Path]],
        remaining_folders: list[tuple[Path, Path]],
        problems: list[str],
    ) -> None:
        """The last run was taken back."""
        self._running = False
        self._last_moved = remaining_files
        self._last_transferred = remaining_folders
        self._last_folders = []
        self._undo_btn.configure(
            state="normal" if remaining_files or remaining_folders else "disabled")
        self._move_to_library_btn.configure(state="disabled")
        text = f"{back} of {total} file(s) moved back."
        if problems:
            text += "\n" + "\n".join(problems[:3])
            if len(problems) > 3:
                text += f"\n(+{len(problems) - 3} more)"
        self._status.configure(text=text)
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
