from __future__ import annotations

from contextlib import closing
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
THUMB_CACHE_DIR = DATA_DIR / "thumbs"
PEOPLE_CACHE_DIR = DATA_DIR / "people"
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


def backup_library(destination: str | Path) -> tuple[bool, str]:
    """Create a zip backup containing the database, settings and caches."""
    target = Path(destination)
    if not target.name.lower().endswith(".zip"):
        target = target.with_suffix(".zip")
    if not DB_PATH.exists():
        return False, f"Library database is missing: {DB_PATH}"
    if not SETTINGS_PATH.exists():
        return False, f"Settings file is missing: {SETTINGS_PATH}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cinema-backup-") as tmp_dir:
            stage = Path(tmp_dir)
            db_copy = stage / "library.db"
            source = sqlite3.connect(str(DB_PATH))
            destination = sqlite3.connect(str(db_copy))
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            shutil.copy2(SETTINGS_PATH, stage / "settings.json")
            for name, directory in (("thumbs", THUMB_CACHE_DIR), ("people", PEOPLE_CACHE_DIR)):
                if directory.exists():
                    shutil.copytree(directory, stage / name, dirs_exist_ok=True)

            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(stage.iterdir()):
                    archive.write(path, arcname=path.name)
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        log.exception("could not create library backup")
        return False, f"The backup could not be created: {exc}"

    return True, f"Backup saved to {target}"


def _stage_backup(archive: zipfile.ZipFile, stage: Path) -> tuple[Path, Path, dict]:
    """Extract only supported backup members, rejecting unsafe archive paths."""
    members: dict[str, Path] = {}
    for info in archive.infolist():
        name = info.filename
        member = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or member.is_absolute()
            or any(part in ("", ".", "..") for part in member.parts)
            or (member.parts and ":" in member.parts[0])
        ):
            raise ValueError(f"Unsafe path in backup: {name!r}")
        if name in members:
            raise ValueError(f"Duplicate backup member: {name}")
        if info.is_dir():
            continue
        if member.parts[0] in ("library.db", "settings.json"):
            if len(member.parts) != 1 or name in members:
                raise ValueError(f"Unexpected or duplicate backup member: {name}")
            target = stage / member.parts[0]
            members[name] = target
        elif member.parts[0] in ("thumbs", "people") and len(member.parts) > 1:
            target = stage.joinpath(*member.parts)
            members[name] = target
        else:
            raise ValueError(f"Unexpected file in backup: {name}")

        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination)

    if "library.db" not in members or "settings.json" not in members:
        raise ValueError("This backup is missing library.db or settings.json.")

    database_path = members["library.db"]
    with closing(sqlite3.connect(str(database_path))) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError("The backup database failed its integrity check.")
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "media" not in tables:
            raise ValueError("The backup database does not contain the library.")
        media_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(media)")
        }
        if not {"id", "path", "title", "media_type"}.issubset(media_columns):
            raise ValueError("The backup database has an invalid media table.")

    settings_path = members["settings.json"]
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise ValueError("The backup settings must contain a JSON object.")
    for key in ("movies_dir", "series_dir", "organizer_dir", "player"):
        if key in settings and not isinstance(settings[key], str):
            raise ValueError(f"The backup setting {key!r} must be a string.")
    if "players" in settings:
        if not isinstance(settings["players"], list):
            raise ValueError("The backup setting 'players' must be a list.")
        for player in settings["players"]:
            if (
                not isinstance(player, dict)
                or not isinstance(player.get("path"), str)
                or not isinstance(player.get("name", ""), str)
            ):
                raise ValueError("The backup contains an invalid video player setting.")
    return database_path, settings_path, settings


def _sqlite_backup(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(str(source_path))
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(str(destination_path))
        source.backup(destination)
    finally:
        if destination is not None:
            destination.close()
        source.close()


def validate_library_backup(path: str | Path) -> tuple[bool, str]:
    """Check a backup archive and its database/settings without changing app data."""
    archive_path = Path(path)
    if not archive_path.is_file():
        return False, f"Backup file was not found: {archive_path}"

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            with tempfile.TemporaryDirectory(prefix="cinema-validate-") as tmp_dir:
                _stage_backup(archive, Path(tmp_dir))
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, json.JSONDecodeError) as exc:
        return False, f"The backup is invalid: {exc}"
    return True, "Backup validation passed."


def _backup_filename(path: Path, stamp: str) -> Path:
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    return candidate


def restore_library_backup(path: str | Path) -> tuple[bool, str]:
    """Validate and restore a zip backup created by backup_library()."""
    archive_path = Path(path)
    if not archive_path.is_file():
        return False, f"Backup file was not found: {archive_path}"

    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            with tempfile.TemporaryDirectory(prefix="cinema-restore-") as tmp_dir:
                staged_db, staged_settings, settings_values = _stage_backup(
                    archive, Path(tmp_dir)
                )

                DATA_DIR.mkdir(parents=True, exist_ok=True)
                APP_DIR.mkdir(parents=True, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")

                db_safety_copy = _backup_filename(DB_PATH, stamp)
                settings_safety_copy = _backup_filename(SETTINGS_PATH, stamp)
                if DB_PATH.exists():
                    _sqlite_backup(DB_PATH, db_safety_copy)
                if SETTINGS_PATH.exists():
                    shutil.copy2(SETTINGS_PATH, settings_safety_copy)

                staged_settings_target = SETTINGS_PATH.with_name(
                    f".{SETTINGS_PATH.name}.restore-{stamp}"
                )
                shutil.copy2(staged_settings, staged_settings_target)
                try:
                    _sqlite_backup(staged_db, DB_PATH)
                    os.replace(staged_settings_target, SETTINGS_PATH)
                except (OSError, sqlite3.Error):
                    staged_settings_target.unlink(missing_ok=True)
                    if db_safety_copy.exists():
                        _sqlite_backup(db_safety_copy, DB_PATH)
                    raise

                cache_warning = ""
                for name, directory in (("thumbs", THUMB_CACHE_DIR), ("people", PEOPLE_CACHE_DIR)):
                    source = Path(tmp_dir) / name
                    if source.exists():
                        try:
                            if directory.exists():
                                shutil.rmtree(directory)
                            shutil.copytree(source, directory)
                        except OSError as exc:
                            log.exception("could not restore %s cache", name)
                            cache_warning = f" The {name} cache could not be restored: {exc}"

                _apply(settings_values)
    except (OSError, sqlite3.Error, zipfile.BadZipFile, ValueError, json.JSONDecodeError) as exc:
        log.exception("could not restore library backup")
        return False, f"The backup could not be restored: {exc}"

    return True, "Backup restored successfully." + cache_warning


def init() -> None:
    global MOVIES_DIR, SERIES_DIR, ORGANIZER_DIR
    for d in (DATA_DIR, THUMB_CACHE_DIR, PEOPLE_CACHE_DIR, LOG_DIR):
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
