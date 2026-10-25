from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
THUMB_CACHE_DIR = DATA_DIR / "thumbs"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "library.db"
PLACEHOLDER_PATH = APP_DIR / "assets" / "placeholder.png"
SETTINGS_PATH = APP_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "movies_dir": "",
    "series_dir": "",
    "organizer_dir": "",
    # the video players the user added: [{"name": ..., "path": ...}]
    "players": [],
    # the one that is used - "" means: the system default
    "player": "",
}

# a folder that is not set is None - Path("") would be the current folder
MOVIES_DIR: Path | None = None
SERIES_DIR: Path | None = None
ORGANIZER_DIR: Path | None = None
PLAYERS: list[dict] = []
PLAYER: str = ""

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".ts"}
ARTWORK_EXTS = (".jpg", ".png", ".webp", ".jpeg", ".jfif", ".bmp")


def as_dir(value) -> Path | None:
    """A setting that is a folder - None when it is not set."""
    text = str(value or "").strip()
    return Path(text) if text else None


def player_name(path: str = "") -> str:
    """The name of a player - the file name of the executable."""
    if not path:
        return ""
    return Path(path).name


def _apply(settings: dict) -> None:
    """Take the values the app runs with out of the settings."""
    global MOVIES_DIR, SERIES_DIR, ORGANIZER_DIR, PLAYERS, PLAYER
    MOVIES_DIR = as_dir(settings.get("movies_dir"))
    SERIES_DIR = as_dir(settings.get("series_dir"))
    ORGANIZER_DIR = as_dir(settings.get("organizer_dir"))
    players = settings.get("players") or []
    PLAYERS = [p for p in players if isinstance(p, dict) and p.get("path")]
    PLAYER = str(settings.get("player") or "")


def load_settings() -> dict:
    try:
        user = json.loads(SETTINGS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    if not isinstance(user, dict):
        return dict(DEFAULT_SETTINGS)
    return {**DEFAULT_SETTINGS, **user}


def save_settings(settings: dict) -> bool:
    """Write the settings file. Returns False when the write failed."""
    merged = {**load_settings(), **settings}
    try:
        SETTINGS_PATH.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.error("could not write %s: %s", SETTINGS_PATH, exc)
        return False
    _apply(merged)
    return True


def init() -> None:
    global MOVIES_DIR, SERIES_DIR, ORGANIZER_DIR
    for d in (DATA_DIR, THUMB_CACHE_DIR, LOG_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    settings = load_settings()
    _apply(settings)
    if not SETTINGS_PATH.exists():
        try:
            SETTINGS_PATH.write_text(
                json.dumps(DEFAULT_SETTINGS, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
