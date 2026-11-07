from __future__ import annotations

import json
from pathlib import Path

from utils.path_utils import canonical_path

STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "scan_state.json"


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
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass


def root_key(root: str | Path | None) -> str:
    return canonical_path(root)


def root_signature(root: str | Path | None) -> str | None:
    if root is None:
        return None
    folder = Path(root)
    if not folder.exists() or not folder.is_dir():
        return None
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return None
    snapshot = []
    for child in children:
        try:
            st = child.stat()
        except OSError:
            continue
        snapshot.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
        })
    return json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
