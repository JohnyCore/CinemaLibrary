from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import config
from utils import title_parser as tp
from utils.image_utils import find_artwork
from utils.path_utils import canonical_path

log = logging.getLogger(__name__)

_SKIP_DIRS = {
    "sample", "samples", "extras", "featurettes", "proof", "screens",
    "subs", "subtitles", "bonus", "trailers",
}

MAX_DEPTH = 4


@dataclass
class Episode:
    season: int | None
    episode: int | None
    path: str
    name: str


@dataclass
class MediaItem:
    path: str
    title: str
    media_type: str
    year: int | None
    root: str
    video_path: str | None
    poster_path: str | None
    poster2_path: str | None
    mtime: float | None
    episodes: list[Episode] = field(default_factory=list)


@dataclass
class FolderScan:
    """Everything one walk of a media folder yields."""

    videos: list[tuple[Path, int, float]] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    mtime: float | None = None
    has_season_dir: bool = False


def scan(
    movies_dir: Path | None,
    series_dir: Path | None,
    progress: "Callable[[int, int], None] | None" = None,
    walked: "set[str] | None" = None,
    unreadable: "set[str] | None" = None,
) -> list[MediaItem]:
    """Index both roots.

    Each top-level folder is walked exactly once; the walk decides whether
    the folder is a series (TV-shaped episodes) or a movie.

    `walked` is filled with the roots that could actually be read. A root
    that is missing or unreadable is left out, so the caller can keep the
    rows of an unplugged drive instead of deleting them.

    `unreadable` is filled with the folders that could not be listed. Their
    rows have to be kept as well - a folder that cannot be read right now
    is not an empty folder.
    """
    items: list[MediaItem] = []
    seen: set[str] = set()
    art_cache: dict = {}
    roots = []
    for root in (movies_dir, series_dir):
        entries, listed = _root_entries(root)
        roots.append((root, entries, listed))
    total = sum(len(entries) for _root, entries, _listed in roots)
    done = 0
    for root, entries, listed in roots:
        root_str = str(root)
        if listed and walked is not None:
            walked.add(canonical_path(root_str))
        for entry in entries:
            try:
                item = _classify(entry, root_str, art_cache, unreadable)
            except OSError:
                # the folder could not be read - its rows have to be kept
                if unreadable is not None:
                    unreadable.add(canonical_path(str(entry)))
                item = None
            if item is not None:
                key = canonical_path(item.path)
                if key not in seen:
                    seen.add(key)
                    items.append(item)
            done += 1
            if progress is not None:
                progress(done, total)
    return items


def _is_video(name: str) -> bool:
    return Path(name).suffix.lower() in config.VIDEO_EXTS


def _wanted(name: str) -> bool:
    n = name.lower()
    return not n.startswith(".") and "sample" not in n


def _root_entries(root: Path | None) -> "tuple[list[Path], bool]":
    """Return (entries, listed).

    `listed` is False when the root is missing or could not be read, which
    is the difference between "the folder is empty" and "the drive is gone".
    """
    if not root or not root.is_dir():
        return [], False
    try:
        return sorted(root.iterdir(), key=lambda p: p.name.lower()), True
    except OSError as exc:
        log.warning("cannot list %s: %s", root, exc)
        return [], False


def _collect(
    folder: Path, season_default: int | None = None, depth: int = 0,
    cache: dict | None = None, unreadable: "set[str] | None" = None,
) -> FolderScan:
    """Walk a media folder once, collecting video files and episode metadata."""
    out = FolderScan()
    if depth > MAX_DEPTH:
        return out
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError as exc:
        log.warning("cannot list %s: %s", folder, exc)
        if unreadable is not None:
            unreadable.add(str(folder))
        return out
    if cache is not None and depth == 0:
        # the artwork lookup needs the same listing - save the second walk
        cache[str(folder)] = {e.name.lower(): e for e in entries}
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                if e.name.lower() in _SKIP_DIRS or e.name.startswith("."):
                    continue
                season = tp.season_from_folder(e.name)
                if season is not None:
                    out.has_season_dir = True
                child = season if season is not None else season_default
                sub = _collect(Path(e.path), child, depth + 1,
                               unreadable=unreadable)
                out.videos.extend(sub.videos)
                out.episodes.extend(sub.episodes)
                if sub.mtime is not None and (out.mtime is None or sub.mtime > out.mtime):
                    out.mtime = sub.mtime
                out.has_season_dir = out.has_season_dir or sub.has_season_dir
            elif e.is_file() and _wanted(e.name) and _is_video(e.name):
                try:
                    st = e.stat()
                except OSError:
                    if unreadable is not None:
                        unreadable.add(e.path)
                    continue
                path = Path(e.path)
                out.videos.append((path, st.st_size, st.st_mtime))
                if out.mtime is None or st.st_mtime > out.mtime:
                    out.mtime = st.st_mtime
                pairs = tp.episode_numbers_all(e.name, season_default)
                stem = Path(e.name).stem
                display = tp.episode_display_name(stem) or stem
                if pairs:
                    # a chained file ("S04E01E02") is one entry per episode
                    out.episodes.extend(
                        Episode(season, number, str(path), display)
                        for season, number in pairs)
                else:
                    out.episodes.append(
                        Episode(None, None, str(path), display))
        except OSError:
            continue
    return out


def _has_tv_shape(scan: FolderScan) -> bool:
    """True when the folder looks like a TV series rather than a movie."""
    if any(e.season is not None for e in scan.episodes):
        return True
    if scan.has_season_dir:
        return True
    if any(tp.has_episode_tokens(v[0].name) for v in scan.videos[:50]):
        return True
    # a folder of bare numbers ("01.mkv", "02.mkv") is a series - but a
    # single file called "300.mkv" is a movie
    bare = [e for e in scan.episodes if e.episode is not None and e.season is None]
    if len(bare) < 2 or len(bare) != len(scan.episodes):
        return False
    # the numbers of an episode list are unique - two files with the same
    # number are two versions of one file, not two episodes
    numbers = [e.episode for e in bare]
    return len(set(numbers)) == len(numbers)


def _children(folder: Path, cache: dict | None = None) -> list[Path]:
    """The children of a folder - the cached listing when there is one."""
    if cache is not None:
        names = cache.get(str(folder))
        if names is not None:
            return [Path(e.path) for e in names.values()]
    return list(folder.iterdir())


def _descend(folder: Path, cache: dict | None = None) -> Path:
    """Follow a single-subfolder chain down to the folder holding the media."""
    cur = folder
    for _ in range(3):
        try:
            children = _children(cur, cache)
        except OSError:
            break
        videos = [p for p in children if p.is_file() and _is_video(p.name) and _wanted(p.name)]
        subdirs = [
            p for p in children
            if p.is_dir() and p.name.lower() not in _SKIP_DIRS and not p.name.startswith(".")
        ]
        if videos or len(subdirs) != 1:
            break
        if tp.has_season_folder(subdirs[0].name):
            break  # a season folder belongs to a series, not to a movie
        cur = subdirs[0]
    return cur


def _artwork_for(folder: Path, cache: dict) -> tuple[Path | None, Path | None]:
    poster, backdrop = find_artwork(folder, cache)
    if poster is None and backdrop is None:
        deeper = _descend(folder, cache)
        if deeper != folder:
            poster, backdrop = find_artwork(deeper, cache)
    return poster, backdrop


def _named_title(name: str) -> tuple[str, int | None]:
    """The title and year of a folder or file name.

    A folder called "Memento 2000 1080p" is the movie "Memento" from 2000,
    a folder called "Six Feet Under 720p" is the series "Six Feet Under".
    """
    title, year = tp.parse_title_and_year(name)
    return tp.strip_quality_suffix(title) or title, year


def _classify(entry: Path, root: str, cache: dict,
              unreadable: "set[str] | None" = None) -> MediaItem | None:
    if entry.is_file():
        if _is_video(entry.name) and _wanted(entry.name):
            return _movie_from_file(entry, root, cache)
        return None
    if not entry.is_dir():
        return None
    scan = _collect(entry, cache=cache, unreadable=unreadable)
    if not scan.videos:
        return None
    if _has_tv_shape(scan):
        if not scan.episodes:
            return None
        return _series_item(entry, root, scan, cache)
    return _movie_item(entry, root, scan, cache)


def _movie_item(folder: Path, root: str, scan: FolderScan, cache: dict) -> MediaItem:
    main, _size, mtime = _pick_main_video(folder, scan.videos)
    title, year = _named_title(folder.name)
    if not title:
        title, year = _named_title(main.stem)
    poster, backdrop = _artwork_for(folder, cache)
    return MediaItem(
        path=str(folder),
        title=title or folder.name,
        media_type="movie",
        year=year,
        root=root,
        video_path=str(main),
        poster_path=str(poster) if poster else None,
        poster2_path=str(backdrop) if backdrop else None,
        mtime=mtime,
    )


def _series_item(folder: Path, root: str, scan: FolderScan, cache: dict) -> MediaItem:
    title, year = _named_title(folder.name)
    poster, backdrop = _artwork_for(folder, cache)
    episodes = scan.episodes
    if not scan.has_season_dir:
        # a series without season folders ("Tom and Jerry") - the files sit
        # in one flat list, which is the first season
        episodes = [replace(e, season=1) if e.season is None else e
                    for e in episodes]
    return MediaItem(
        path=str(folder),
        title=title or folder.name,
        media_type="series",
        year=year,
        root=root,
        video_path=None,
        poster_path=str(poster) if poster else None,
        poster2_path=str(backdrop) if backdrop else None,
        mtime=scan.mtime,
        episodes=episodes,
    )


# files that live next to a movie but are not the movie
_EXTRA_WORDS = (
    "trailer", "extras", "extra", "featurette", "proof", "promo", "teaser",
    "sample", "behind the scenes", "deleted scene", "interview", "subs",
)


def _norm_name(text: str) -> str:
    """A comparable name: lower case words separated by a single space."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return " ".join(cleaned.split())


def _pick_main_video(folder: Path, videos: list) -> tuple:
    """The folder's actual movie, not the biggest file next to it.

    A trailer or a featurette is often bigger than the real video, and the
    Play button would then open the trailer. The file whose name matches the
    folder wins; a file that looks like an extra never does.

    A folder that holds nothing but extras still has a main file - the
    biggest one - so the folder is indexed instead of breaking the scan.
    """
    if not videos:
        raise ValueError("no videos")
    folder_key = _norm_name(folder.name)
    best = None
    best_score = -2
    for path, size, mtime in videos:
        stem = path.stem.lower()
        if any(word in stem for word in _EXTRA_WORDS):
            score = -1
        else:
            key = _norm_name(path.stem)
            if (key == folder_key
                    or key.startswith(folder_key + " ")
                    or folder_key.startswith(key + " ")):
                score = 2
            else:
                score = 0
        if score > best_score or (score == best_score and best is not None and size > best[1]):
            best = (path, size, mtime)
            best_score = score
    return best


def _movie_from_file(file: Path, root: str, cache: dict) -> MediaItem | None:
    title, year = _named_title(file.stem)
    poster, backdrop = find_artwork(file.parent, cache)
    return MediaItem(
        path=str(file),
        title=title or file.stem,
        media_type="movie",
        year=year,
        root=root,
        video_path=str(file),
        poster_path=str(poster) if poster else None,
        poster2_path=str(backdrop) if backdrop else None,
        mtime=file.stat().st_mtime if file.exists() else None,
    )
