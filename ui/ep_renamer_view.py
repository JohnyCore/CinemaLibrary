from __future__ import annotations

import logging
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import ttk

from metadata import metadata_service
from utils import organizer

log = logging.getLogger(__name__)

PANEL_BG = "#12161d"
BORDER = "#232b36"
TEXT = "#e8eaed"
MUTED = "#9aa4b2"
BTN_BG = "#1a2130"
BTN_ACTIVE = "#243046"
ACCENT = "#e5484d"


def _short(text: str) -> str:
    return text if len(text) <= 70 else text[:67] + "…"


class EpRenamerView(tk.Frame):
    """Tab that renames the episode files of a series.

    Every row shows the name a file has now and the name it would get from
    the titles a provider fetched ("Show S02E11 - Episode Name.mkv").
    Nothing on the disk changes until "Apply all" is pressed, and the run
    can be taken back with "Undo".
    """

    def __init__(self, master, db, post=None) -> None:
        super().__init__(master, bg=PANEL_BG, highlightthickness=0, bd=0)
        self._db = db
        self._post = post or (lambda fn: self.after(0, fn))
        self._series: list[dict] = []
        self._entries: list[organizer.EpisodeRename] = []
        self._running = False

        inner = tk.Frame(self, bg=PANEL_BG, padx=22, pady=20)
        inner.pack(fill="both", expand=True)

        tk.Label(inner, text="EP Renamer", bg=PANEL_BG, fg=TEXT,
                 font=("Segoe UI Semibold", 17)).pack(anchor="w")
        tk.Label(
            inner,
            text="The files of a series get the names of the fetched episode "
                 "titles: “The.Mentalist.S02E11.Age.of.Reduction.mkv” becomes "
                 "“The Mentalist S02E11 - Age of Reduction.mkv”. Files stay in "
                 "place; only names change. The "
                 "run can be taken back with Undo.",
            bg=PANEL_BG, fg=MUTED, font=("Segoe UI", 9), justify="left",
        ).pack(anchor="w", pady=(3, 14))

        controls = tk.Frame(
            inner, bg="#0f1319", highlightthickness=1,
            highlightbackground=BORDER, padx=14, pady=12,
        )
        controls.pack(fill="x", pady=(0, 12))
        row = tk.Frame(controls, bg="#0f1319")
        row.pack(fill="x", pady=(0, 10))
        tk.Label(row, text="Choose a series", bg="#0f1319", fg=MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 5))
        self._combo = ttk.Combobox(row, state="readonly", width=52,
                                   style="Dark.TCombobox")
        self._combo.pack(fill="x")
        self._combo.bind("<<ComboboxSelected>>", lambda _e: self._plan())

        buttons = tk.Frame(controls, bg="#0f1319")
        buttons.pack(fill="x")
        btn_style = dict(
            bg=BTN_BG, fg=TEXT, activebackground=BTN_ACTIVE, activeforeground=TEXT,
            bd=0, padx=16, pady=6, font=("Segoe UI", 10), cursor="hand2",
        )
        self._apply_btn = tk.Button(
            buttons, text="Apply all", command=self._apply,
            state="disabled", disabledforeground="#5a6472",
            bg=ACCENT, fg=TEXT, activebackground="#f05a60",
            activeforeground=TEXT, bd=0, padx=16, pady=6,
            font=("Segoe UI", 10, "bold"), cursor="hand2")
        self._apply_btn.pack(side="left")
        self._undo_btn = tk.Button(
            buttons, text="Undo", command=self._undo,
            state="disabled", disabledforeground="#5a6472", **btn_style)
        self._undo_btn.pack(side="left", padx=(10, 0))
        self._check_btn = tk.Button(
            buttons, text="Check All name", command=self._check_all_names,
            **btn_style)
        self._check_btn.pack(side="left", padx=(10, 0))

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
        self._tree = ttk.Treeview(
            wrap, columns=("now", "new", "result"), show="headings",
            style="Settings.Treeview", height=16, selectmode="browse")
        self._tree.heading("now", text="Current name")
        self._tree.heading("new", text="New name")
        self._tree.heading("result", text="Result")
        self._tree.column("now", width=330, stretch=True)
        self._tree.column("new", width=330, stretch=True)
        self._tree.column("result", width=190, stretch=False)
        self._tree.tag_configure("odd", background="#151a24")
        self._tree.tag_configure("skip", foreground="#8a93a2")
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)

    # --- the series ----------------------------------------------------

    def refresh(self) -> None:
        """Read the series of the library when the tab is opened."""
        previous = self._combo.current()
        self._series = [i for i in self._db.get_all_items()
                        if i["media_type"] == "series"]
        titles = [str(i.get("title") or "?") for i in self._series]
        self._combo.configure(values=titles)
        if organizer.load_undo():
            self._undo_btn.configure(state="normal")
        if not titles:
            self._entries = []
            self._fill_rows()
            self._apply_btn.configure(state="disabled")
            self._status.configure(text="The library holds no series.")
            return
        index = previous if 0 <= previous < len(titles) else 0
        self._combo.current(index)
        self._plan()

    def _plan(self) -> None:
        """Show what the files of the chosen series would be called."""
        index = self._combo.current()
        if index < 0 or index >= len(self._series):
            self._entries = []
            self._fill_rows()
            self._apply_btn.configure(state="disabled")
            self._status.configure(text="Choose a series.")
            return
        item = self._series[index]
        self._entries = organizer.episode_rename_plan(
            item, self._db.episodes(item["id"]))
        self._fill_rows()
        todo = [e for e in self._entries if not e.result]
        text = f"{item.get('title') or '?'} - {len(self._entries)} episode file(s)"
        text += f" - {len(todo)} to rename" if todo else " - nothing to rename"
        left = len(self._entries) - len(todo)
        if left:
            text += f" - {left} left alone"
        self._status.configure(text=text)
        self._apply_btn.configure(state="normal" if todo else "disabled")

    def _fill_rows(self) -> None:
        for iid in self._tree.get_children():
            self._tree.delete(iid)
        for index, entry in enumerate(self._entries):
            tags = ("odd",) if index % 2 else ()
            if entry.result and entry.result != "renamed":
                tags = tags + ("skip",)
            self._tree.insert("", "end", values=(
                _short(entry.source.name), _short(entry.target.name),
                entry.result), tags=tags)

    # --- renaming ------------------------------------------------------

    def _apply(self) -> None:
        """Rename what the plan can rename and remember it for the undo."""
        if self._running:
            return
        todo = [e for e in self._entries if not e.result]
        if not todo:
            return
        self._running = True
        self._apply_btn.configure(state="disabled", text="Renaming…")
        self._status.configure(text=f"Renaming {len(todo)} file(s)…")

        def worker() -> None:
            try:
                organizer.rename_episode_files(todo)
                moves = [(e.source, e.target) for e in todo if e.result == "renamed"]
                for old, new in moves:
                    self._db.rename_episode(str(old), str(new))
                if moves:
                    organizer.save_undo([(str(old), str(new)) for old, new in moves])
            except Exception:
                log.exception("the renames could not be saved")
                self._post(self._failed)
                return
            self._post(self._applied)

        threading.Thread(target=worker, daemon=True).start()

    def _applied(self) -> None:
        self._running = False
        done = sum(1 for e in self._entries if e.result == "renamed")
        failed = sum(1 for e in self._entries
                     if e.result.startswith("failed"))
        self._apply_btn.configure(state="disabled", text="Apply all")
        self._undo_btn.configure(state="normal" if done else "disabled")
        self._plan()
        text = f"{done} file(s) renamed."
        if failed:
            text += f" {failed} failed - the run can be taken back with Undo."
        self._status.configure(text=text)

    def _failed(self) -> None:
        self._running = False
        self._apply_btn.configure(state="normal", text="Apply all")
        self._status.configure(text="The renames could not be saved.")

    def _undo(self) -> None:
        """Put the names of the last run back."""
        if self._running:
            return
        moves = organizer.load_undo()
        if not moves:
            self._undo_btn.configure(state="disabled")
            return
        self._running = True
        self._undo_btn.configure(state="disabled")
        self._status.configure(text=f"Taking back {len(moves)} name(s)…")

        def worker() -> None:
            try:
                back = organizer.undo_episode_renames(moves)
                for old, new in moves:
                    self._db.rename_episode(str(new), str(old))
                organizer.clear_undo()
            except Exception:
                log.exception("the undo could not be saved")
                self._post(self._failed)
                return
            self._post(lambda: self._undone(back, len(moves)))

        threading.Thread(target=worker, daemon=True).start()

    def _undone(self, back: int, total: int) -> None:
        self._running = False
        self._plan()
        self._status.configure(text=f"{back} of {total} name(s) taken back.")

    # --- the episode titles of every series -----------------------------

    def _name_stats(self, series_id: int) -> dict[tuple, str]:
        """The titles each episode row carries right now."""
        return {
            (e["season"], e["episode"]): str(e.get("name") or "")
            for e in self._db.episodes(series_id)}

    def _check_all_names(self) -> None:
        """Fetch the titles of all series and fill in what is new.

        Series that were fetched before a file was recognised ("the second
        episode of a chained file") have rows without a title - a new fetch
        of the provider list fills those in.
        """
        if self._running:
            return
        series = list(self._series)
        if not series:
            return
        self._running = True
        for btn in (self._check_btn, self._apply_btn, self._undo_btn):
            btn.configure(state="disabled")
        self._check_btn.configure(text="Checking…")
        self._status.configure(text=f"Checking {len(series)} series…")

        def worker(item: dict):
            sid = item["id"]
            title = str(item.get("title") or "?")
            before = self._name_stats(sid)
            try:
                query = metadata_service.MetadataService.clean_query(
                    str(item.get("title") or ""), item.get("year"))
                result = metadata_service.fetch_metadata(
                    query, item.get("year"), item.get("media_type"),
                    item.get("imdb_id"))
            except Exception as exc:
                log.warning("the titles of %s could not be fetched: %s",
                            title, exc)
                return (title, False, 0)
            if result is None:
                return (title, False, 0)
            metadata_service.save_metadata(self._db, sid, result)
            after = self._name_stats(sid)
            fresh = sum(
                1 for key, name in after.items()
                if name and name != before.get(key))
            return (title, True, fresh)

        def run() -> None:
            done = 0
            failed = []
            fresh: list[str] = []
            titles_in = 0
            pool = ThreadPoolExecutor(max_workers=4)
            try:
                futures = {pool.submit(worker, item): item for item in series}
                for future in list(futures):
                    title, ok, came_in = future.result()
                    done += 1
                    if ok and came_in:
                        titles_in += came_in
                        fresh.append(f"{title}: {came_in}")
                    elif not ok:
                        failed.append(title)
                    self._post(lambda d=done, t=len(series),
                               fr=titles_in, se=len(fresh), fa=len(failed):
                               self._checking(d, t, fr, se, fa))
            finally:
                pool.shutdown(wait=True)
            self._post(lambda d=len(series), fr=titles_in,
                       se=fresh, fa=failed: self._checked(d, fr, se, fa))

        threading.Thread(target=run, daemon=True).start()

    def _checking(self, done: int, total: int, titles_in: int,
                  sold: int, failed: int) -> None:
        text = (f"Checking series {done}/{total} - {titles_in} new title(s) "
                f"in {sold} series")
        if failed:
            text += f" - {failed} failed"
        self._status.configure(text=text)

    def _checked(self, total: int, titles_in: int,
                 fresh: list[str], failed: list[str]) -> None:
        self._running = False
        self._check_btn.configure(text="Check All name")
        for btn, state in ((self._apply_btn, "normal"), (self._undo_btn, None)):
            if state is None:
                state = "normal" if organizer.load_undo() else "disabled"
            btn.configure(state=state)
        if not self._series:
            return
        self._plan()
        if fresh:
            names = ", ".join(fresh[:4])
            more = f" (+{len(fresh) - 4} more)" if len(fresh) > 4 else ""
            text = (f"Checked {total} series - {titles_in} new episode "
                    f"title(s) in {len(fresh)} series ({names}{more}).")
        else:
            text = f"Checked {total} series - nothing new."
        if failed:
            text += f" {len(failed)} series could not be fetched."
        self._status.configure(text=text)
