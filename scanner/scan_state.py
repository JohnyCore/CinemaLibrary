from __future__ import annotations

import json
import os
from pathlib import Path

from utils.path_utils import atomic_write_text, canonical_path

STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "scan_state.json"

# folders that are not part of a title - kept in sync with the scanner
_SKIP_DIRS = {
    "sample", "samples", "extras", "featurettes", "proof", "screens",
    "subs", "subtitles", "bonus", "trailers",
}
_MAX_DEPTH = 8


def load_scan_state() -> dict[str, str]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    cleaned: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            cleaned[canonical_path(key)] = value
    return cleaned


def save_scan_state(state: dict[str, str]) -> None:
    try:
        atomic_write_text(
            STATE_PATH,
            json.dumps(state, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def invalidate() -> None:
    """Drop the whole signature - the next rescan has to walk everything."""
    save_scan_state({})


def root_key(root: str | Path | None) -> str:
    return canonical_path(root)


def root_signature(root: str | Path | None) -> str | None:
    """A snapshot of everything under a root, nested folders included.

    Only the top-level children would miss a new episode inside
    "Season 2" - its folder mtime changes, the series folder's does not.
    The signature therefore records every directory's mtime (it changes
    when a child is added, removed or renamed) and every file's mtime and
    size (it changes when a file is rewritten in place).
    """
    if root is None:
        return None
    folder = Path(root)
    try:
        if not folder.is_dir():
            return None
    except OSError:
        return None
    snapshot: list[dict] = []
    for base, dirs, files in os.walk(folder):
        rel_base = Path(base).relative_to(folder)
        depth = 0 if str(rel_base) == "." else len(rel_base.parts)
        keep: list[str] = []
        for name in dirs:
            if name.lower() in _SKIP_DIRS or name.startswith("."):
                continue
            if depth < _MAX_DEPTH:
                keep.append(name)
            try:
                st = os.stat(os.path.join(base, name))
            except OSError:
                continue
            rel = name if depth == 0 else os.path.join(str(rel_base), name)
            snapshot.append({"d": rel, "mtime_ns": st.st_mtime_ns})
        # prune the walk: skipped folders are not descended into
        dirs[:] = keep
        for name in files:
            if name.lower() in _SKIP_DIRS or name.startswith("."):
                continue
            try:
                st = os.stat(os.path.join(base, name))
            except OSError:
                continue
            rel = name if depth == 0 else os.path.join(str(rel_base), name)
            snapshot.append({"f": rel, "mtime_ns": st.st_mtime_ns,
                             "size": st.st_size})
    snapshot.sort(key=lambda entry: entry.get("d", entry.get("f", "")))
    return json.dumps(snapshot, separators=(",", ":"))
