"""Sort a folder of loose video files into a tidy library layout.

The file name decides the folder: a movie becomes "Title (Year)", a series
becomes "Title / Season N". Files are only moved - never renamed - and
nothing is fetched from the web.

The episode files of a series are a different job: they are renamed after
the titles a provider fetched (`episode_rename_plan`), and the run can be
taken back.
"""
from __future__ import annotations

import logging
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import config
from utils import title_parser as tp

log = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')
_TRAILING = re.compile(r"[\s.]+$")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# the quality of a release: "Memento 2000 1080p"
_QUALITY = re.compile(r"\b(2160p|1080p|720p|480p)\b", re.IGNORECASE)
# the release group at the end of a scene name: "-GROUP"
_GROUP_SUFFIX = re.compile(r"\s-\s?[^-]{1,40}$")
# cutting a name at its year leaves the opening bracket behind
_OPEN_BRACKET = re.compile(r"[\(\[\{]+\s*$")


@dataclass
class PlanEntry:
    """One file and the folder it should end up in."""

    source: Path
    kind: str = "skip"           # "movie", "series" or "skip"
    folder: str = ""             # target folder name
    season: int | None = None
    title: str = ""
    year: int | None = None
    reason: str = ""             # why a file is skipped
    result: str = ""             # what the move did

    @property
    def target_dir(self) -> str:
        """The target folder, relative to the source folder."""
        if self.kind == "series":
            return f"{self.folder} / Season {self.season}"
        return self.folder

    @property
    def target(self) -> Path:
        """Where the file ends up, relative to the source folder."""
        parts = [self.folder]
        if self.kind == "series":
            parts.append(f"Season {self.season}")
        return Path(*parts) / self.source.name


def sanitize(name: str) -> str:
    """A folder name that is legal on Windows."""
    name = _INVALID_CHARS.sub("", name)
    name = _TRAILING.sub("", name)
    return name.strip() or "Untitled"


def _clean(text: str) -> str:
    """A name without release tags, group suffix or dots."""
    text = tp.strip_release_tags(text)
    text = _GROUP_SUFFIX.sub(" ", text)
    text = _OPEN_BRACKET.sub("", text)
    return tp.tidy_title(text)


def _split_year(text: str) -> tuple[str, int | None]:
    """The name before its year and the year itself.

    A year at the very start belongs to the title ("2001: A Space Odyssey").
    """
    found = [m for m in _YEAR.finditer(text) if m.start() > 0]
    if not found:
        return text, None
    match = found[-1]
    return text[: match.start()], int(match.group(1))


def _folder_name(title: str, year: int | None, quality: str) -> str:
    """The folder a file goes into: "Memento 2000 1080p"."""
    parts = [title]
    if year:
        parts.append(str(year))
    if quality:
        parts.append(quality)
    return sanitize(" ".join(parts))


def _quality_of(stem: str) -> str:
    match = _QUALITY.search(stem)
    return match.group(1) if match else ""


def _classify(path: Path) -> PlanEntry:
    """Work out the folder a file belongs in."""
    stem = path.stem
    quality = _quality_of(stem)
    span = tp.episode_marker_span(stem)

    if span is not None:
        head = stem[: span[0]]
        raw, year = _split_year(head)
        title = _clean(raw)
        if not title:
            return PlanEntry(path, reason="no series name in the file name")
        season, _number = tp.episode_numbers(stem, None)
        return PlanEntry(path, "series",
                         folder=_folder_name(title, year, quality),
                         season=season or 1, title=title, year=year)

    if tp.episode_numbers(stem, None)[1] is not None:
        # "07.mkv" - a bare episode number without a series name
        return PlanEntry(path, reason="no series name in the file name")

    raw, year = _split_year(stem)
    title = _clean(raw)
    if not title:
        return PlanEntry(path, reason="no title in the file name")
    return PlanEntry(path, "movie", folder=_folder_name(title, year, quality),
                     title=title, year=year)


def plan(source_dir: Path) -> list[PlanEntry]:
    """Classify every loose video file in a folder."""
    entries: list[PlanEntry] = []
    try:
        items = sorted(source_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        log.warning("cannot list %s: %s", source_dir, exc)
        return entries
    for item in items:
        if not item.is_file():
            continue
        if item.suffix.lower() not in config.VIDEO_EXTS:
            continue
        name = item.name.lower()
        if name.startswith(".") or "sample" in name:
            continue
        entries.append(_classify(item))
    return entries


def move_entry(entry: PlanEntry, root: Path) -> str:
    """Move one file into its folder. Returns what happened."""
    if entry.kind == "skip":
        entry.result = "skipped"
        return entry.result
    target = root / entry.target
    try:
        if target.exists():
            entry.result = "already there"
            return entry.result
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry.source), str(target))
        entry.result = "moved"
    except OSError as exc:
        log.warning("could not move %s: %s", entry.source, exc)
        entry.result = f"failed: {exc}"
        # an empty folder that the failed move left behind is cleaned up
        drop_empty(target.parent, root)
    return entry.result


def drop_empty(folder: Path, keep: Path) -> None:
    """Remove a folder that holds nothing - the source folder stays."""
    try:
        if folder == keep or not folder.is_dir() or any(folder.iterdir()):
            return
        folder.rmdir()
        drop_empty(folder.parent, keep)
    except OSError:
        pass

# --- renaming episode files from the fetched titles ------------------

@dataclass
class EpisodeRename:
    """One episode file and the name it is headed for."""

    source: Path
    target: Path
    result: str = ""


_CHAINED = re.compile(r"(?i)\bs\d{1,2}\s?e\d{1,3}[\s._\-]?e\d{1,3}\b")

_UNDO_PATH = config.DATA_DIR / "rename_undo.json"


def _strip_title_repeats(name: str, title: str) -> str:
    """Cut the scan junk out of an episode name.

    Files that were renamed from their own file name carry names like
    "Lost - Lost" or "Twin Peaks - Twin Peaks", and every plan run would
    glue another copy on. The series title alone carries no information,
    so the repeats are dropped.
    """
    t = title.strip().lower()
    if not t:
        return name.strip()
    parts = [p.strip() for p in name.split(" - ") if p.strip()]
    kept = [p for p in parts if p.lower() != t]
    return " - ".join(kept)


def _episode_title(ep: dict, title: str, number) -> str:
    """The name an episode file earns, or a plain "Episode NN" folder-filler."""
    name = _strip_title_repeats(str(ep.get("name") or ""), title)
    if name and sanitize(name) not in ("", "Untitled"):
        return sanitize(name)
    if ep.get("name_from_provider") and str(ep.get("name") or "").strip().lower() == title.strip().lower():
        # the episode really is called like the series - keep it once
        return sanitize(str(ep["name"]).strip())
    return f"Episode {int(number):02d}"


def episode_rename_plan(item: dict, episodes: list[dict]) -> list[EpisodeRename]:
    """A tidy name per episode file: "Title S01E02 - Episode name.mkv".

    A chained file ("Title S04E23E24.mkv") is named after both episodes:
    "Title S04E23E24 - Funland + Sweet Caroline.mkv". Names come from what
    a provider fetched; files a plan cannot clean up honestly (no series
    title, no season number, not on the disk) are listed with the reason
    instead of guessed at.
    """
    title = sanitize(str(item.get("title") or ""))
    out: list[EpisodeRename] = []
    # the rows of one file share the path - a chained file is renamed once
    grouped: dict[str, list[dict]] = {}
    for ep in episodes:
        key = str(Path(str(ep["path"]))).lower()
        grouped.setdefault(key, []).append(ep)
    for rows in grouped.values():
        src = Path(str(rows[0]["path"]))
        first = rows[0]
        season, number = first.get("season"), first.get("episode")
        if title == "Untitled":
            out.append(EpisodeRename(src, src, "no series title - left alone"))
        elif season is None or number is None:
            out.append(EpisodeRename(src, src, "no season number - left alone"))
        elif not src.exists():
            out.append(EpisodeRename(src, src, "file not found - left alone"))
        else:
            names = [_episode_title(row, title, row.get("episode"))
                     for row in rows]
            label = f"S{int(season):02d}E{int(number):02d}"
            combined = " + ".join(dict.fromkeys(names))
            if len(rows) > 1 or _CHAINED.search(src.stem):
                last = rows[-1].get("episode")
                if last is not None and last != number:
                    label = f"S{int(season):02d}E{int(number):02d}E{int(last):02d}"
            target = src.with_name(f"{title} {label} - {combined}{src.suffix}")
            if src.name.lower() == target.name.lower():
                out.append(EpisodeRename(src, src, "already neat"))
            elif target.exists():
                out.append(EpisodeRename(src, src, "name taken - left alone"))
            else:
                out.append(EpisodeRename(src, target))
    return out


def rename_episode_files(entries: list[EpisodeRename]) -> int:
    """Apply the planned renames. Returns how many succeeded."""
    done = 0
    for entry in entries:
        if entry.result:
            continue
        try:
            entry.source.rename(entry.target)
            entry.result = "renamed"
            done += 1
        except OSError as exc:
            log.warning("could not rename %s: %s", entry.source, exc)
            entry.result = f"failed: {exc}"
    return done


def save_undo(moves: list[tuple[str, str]]) -> None:
    """Remember the last applied rename so it can be taken back."""
    try:
        _UNDO_PATH.write_text(json.dumps(
            {"moves": [{"old": old, "new": new} for old, new in moves]}),
            encoding="utf-8")
    except OSError as exc:
        log.warning("could not write %s: %s", _UNDO_PATH, exc)


def load_undo() -> list[tuple[str, str]]:
    try:
        data = json.loads(_UNDO_PATH.read_text(encoding="utf-8"))
        moves = data.get("moves") or []
        return [(m["old"], m["new"]) for m in moves
                if isinstance(m, dict) and m.get("old") and m.get("new")]
    except (OSError, ValueError):
        return []


def clear_undo() -> None:
    try:
        _UNDO_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def undo_episode_renames(moves: list[tuple[str, str]]) -> int:
    """Put the renamed files back to their old names. Returns how many."""
    back = 0
    for old, new in moves:
        try:
            new_path, old_path = Path(new), Path(old)
            if new_path.exists() and not old_path.exists():
                new_path.rename(old_path)
                back += 1
        except OSError as exc:
            log.warning("could not undo the rename %s: %s", new, exc)
    return back
