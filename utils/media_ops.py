from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import config

log = logging.getLogger(__name__)


def media_folder(item: dict) -> Path | None:
    """The folder that holds a media item's files."""
    raw = item.get("video_path") or item.get("path")
    if not raw:
        return None
    p = Path(raw)
    try:
        if p.is_file():
            return p.parent
    except OSError:
        return None
    return p


def media_file(item: dict) -> Path | None:
    """The video file of a loose movie item - None for a folder item."""
    raw = item.get("video_path") or item.get("path")
    if not raw:
        return None
    p = Path(raw)
    try:
        if p.is_file() and p.suffix:
            return p
    except OSError:
        return None
    return None


def shares_folder_with_videos(video: Path) -> bool:
    """Is there another video file in the same folder as this one?"""
    try:
        for entry in os.scandir(video.parent):
            if not entry.is_file():
                continue
            if entry.name.lower() == video.name.lower():
                continue
            if Path(entry.name).suffix.lower() in config.VIDEO_EXTS:
                return True
    except OSError:
        return False
    return False


def artwork_prefix(item: dict) -> str:
    """The artwork file-name prefix of a media item.

    A bare video that shares its folder with other videos must not write
    poster.jpg / about.txt for the whole folder: its artwork is named
    after the file ("Casablanca-poster.jpg", "Casablanca-about.txt").
    """
    video = media_file(item)
    if video is not None and shares_folder_with_videos(video):
        return f"{video.stem}-"
    return ""


def reveal(path) -> None:
    """Open a file or folder with the operating system's default handler."""
    if not path:
        return
    p = Path(path)
    try:
        if not p.exists():
            return
        if sys.platform == "win32":
            os.startfile(str(p))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except OSError:
        pass


def play(path) -> None:
    """Play a video file.

    The player the user chose in the settings is used when there is one;
    otherwise the file is opened with the system default.
    """
    if not path:
        return
    player = config.PLAYER
    if player:
        try:
            subprocess.Popen([player, str(path)])
            return
        except OSError as exc:
            log.warning("could not start %s: %s", player, exc)
    reveal(path)


def about_file(item: dict) -> Path | None:
    folder = media_folder(item)
    if folder is None:
        return None
    return folder / f"{artwork_prefix(item)}about.txt"


def remove_about(item: dict) -> None:
    target = about_file(item)
    if target is None:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def remove_artwork(item: dict, which: str) -> None:
    """Delete a poster file from the media folder ('poster' or 'poster2')."""
    path = item.get("poster_path" if which == "poster" else "poster2_path")
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass
