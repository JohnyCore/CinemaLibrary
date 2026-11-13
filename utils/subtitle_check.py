"""Subtitle checks for the Library Health report.

A video "has subtitles" when a sidecar file sits next to it, or when the
container itself carries subtitle tracks. Sidecar formats players accept
(.srt, .ass, .vtt, ...) are matched against the video file by name, and
container tracks are read out of the file itself: Matroska TrackType
elements and MP4 subtitle sample entries. When nothing can be proven -
neither a sidecar nor a readable container - the check says so instead
of guessing. Nothing here plays or rewrites a file.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# The sidecar formats players understand. .idx belongs to image-based
# VobSub (.sub); either file on its own proves a subtitle is there.
SUBTITLE_EXTS = frozenset({
    ".srt", ".ass", ".ssa", ".vtt", ".ttml", ".dfxp",
    ".smi", ".sami", ".sub", ".idx", ".sup", ".scc", ".stl", ".cap", ".lrc",
})

# tokens a subtitle file adds to a video name that say nothing about
# which episode the subtitle belongs to
_LANG_TAGS = frozenset({
    "en", "eng", "english", "es", "spa", "spanish", "fr", "fre", "fra",
    "french", "de", "ger", "german", "it", "ita", "italian", "pt", "por",
    "portuguese", "br", "ru", "rus", "nl", "dut", "dutch", "pl", "pol",
    "tr", "tur", "ar", "ara", "arabic", "he", "heb", "hebrew", "hi", "hin",
    "ja", "jpn", "jp", "ko", "kor", "zh", "chi", "zho", "und", "multi",
    "sub", "subs", "subtitle", "subtitles", "forced", "cc", "sdh", "sdhh",
    "hi", "vost", "vostfr", "vo", "original", "big", "small", "normal",
    "full", "default", "sdh-sub", "hin",
})

# release tags a video file name carries that a sidecar usually drops
_VIDEO_NOISE = frozenset({
    "2160p", "1080p", "720p", "480p", "4k", "8bit", "10bit", "hdr", "hdr10",
    "web", "webdl", "webrip", "web-dl", "web-drip", "bluray", "blu", "ray",
    "bdrip", "brrip", "dvdrip", "dvd", "hdrip", "hdtv", "cam", "x264",
    "x265", "h264", "h265", "hevc", "avc", "aac", "aac2", "ac3", "eac3",
    "dts", "dtshd", "truehd", "atmos", "ddp", "dd", "dd2", "dd5", "5",
    "1", "0", "2", "0-5", "dolby", "vision", "remastered", "extended",
    "unrated", "proper", "repack", "internal", "dual", "amzn", "nf",
    "dsnp", "atvp", "hmax", "max", "hulu", "ma", "uhd", "hd", "sd",
    "dvdscr", "bd", "remux", "hybrid", "complete", "dvdr",
})

_MP4_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl"})
_MP4_SUBTITLE_FORMATS = frozenset({
    b"tx3g", b"sbtl", b"subt", b"text", b"sami", b"stpp", b"wvtt"})
_MOOV_READ_LIMIT = 128 * 1024 * 1024

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_FFPROBE_TIMEOUT = 15.0

# EBML element ids
_MKV_SEGMENT = 0x18538067
_MKV_TRACKS = 0x1654AE6B
_MKV_TRACK_ENTRY = 0xAE
_MKV_TRACK_TYPE = 0x83
_TRACK_TYPE_SUBTITLE = 0x11
_TRACK_TYPE_COMPLEX = 0x10


def _norm(text: str) -> str:
    """A comparable stem: lower case words separated by a single space."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return " ".join(cleaned.split())


def stems_match(video_stem: str, subtitle_stem: str) -> bool:
    """Does a subtitle file belong to a video file?

    The names are compared after release tags are dropped from the video
    side and language tags from the subtitle side: "Movie 2020 1080p" is
    covered by "Movie 2020.en", and "Series S01E02" by
    "Series.S01E02.English". A different year or episode never matches.
    """
    video = _norm(video_stem)
    sub = _norm(subtitle_stem)
    if not video or not sub:
        return False
    if video == sub:
        return True
    # one name is the start of the other: "Series S01E02" and
    # "Series S01E02 English"
    if sub.startswith(video + " ") or video.startswith(sub + " "):
        return True
    video_words = video.split(" ")
    sub_words = sub.split(" ")
    shared = 0
    for a, b in zip(video_words, sub_words):
        if a != b:
            break
        shared += 1
    if shared == 0:
        return False
    video_rest = video_words[shared:]
    sub_rest = sub_words[shared:]
    return (all(word in _VIDEO_NOISE for word in video_rest)
            and all(word in _LANG_TAGS for word in sub_rest))


def sidecar_subtitles(video: str | Path) -> list[Path]:
    """The subtitle files in the video's folder that belong to it."""
    video = Path(video)
    try:
        names = os.listdir(video.parent)
    except OSError:
        return []
    stem = _norm(video.stem)
    if not stem:
        return []
    return [
        video.parent / name for name in names
        if Path(name).suffix.lower() in SUBTITLE_EXTS
        and stems_match(stem, Path(name).stem)
    ]


def has_embedded_subtitles(video: str | Path, cache: "dict | None" = None,
                           ) -> bool | None:
    """Does the video file itself carry subtitle tracks?

    True or False when the container could be read, None when it could
    not be decided - no ffprobe and an unsupported container. `cache`
    keeps one answer per file for a whole report run. The built-in
    parsers answer for Matroska and MP4 first; ffprobe is only spawned
    for the containers they cannot walk.
    """
    key = str(video)
    if cache is not None and key in cache:
        return cache[key]
    suffix = Path(key).suffix.lower()
    state: bool | None = None
    if suffix == ".mkv":
        state = _mkv_has_subtitle_track(key)
    elif suffix in (".mp4", ".m4v", ".mov"):
        state = _mp4_has_subtitle_track(key)
    if state is None:
        state = _ffprobe_subtitles(key)
    if cache is not None:
        cache[key] = state
    return state


def video_subtitle_state(video: str | Path,
                         cache: "dict | None" = None) -> tuple[bool, str]:
    """(missing, note) for one video file.

    missing is False when a sidecar sits next to the file or the
    container carries subtitle tracks. The note explains what could not
    be proven when the file stays reported.
    """
    video = Path(video)
    if sidecar_subtitles(video):
        return False, ""
    embedded = has_embedded_subtitles(video, cache)
    if embedded:
        return False, ""
    if embedded is None:
        return True, "no external subtitles; embedded could not be checked"
    return True, "no external or embedded subtitles"


def _ffprobe_subtitles(path: str) -> bool | None:
    """Subtitle streams through ffprobe, when it is installed."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    cmd = [exe, "-v", "error", "-select_streams", "s", "-show_entries",
           "stream=index", "-of", "json", path]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=_FFPROBE_TIMEOUT,
            creationflags=_NO_WINDOW, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        streams = json.loads(
            result.stdout.decode("utf-8", "replace")).get("streams") or []
    except ValueError:
        return None
    return bool(streams)


# --- the Matroska side: read the Tracks element straight from the file ---

def _ebml_vint(fh, pos: int, keep_marker: bool) -> "tuple[int, int] | None":
    """One EBML variable-size integer at pos: (value, byte length).

    Element ids keep their marker bit; sizes drop it. A size whose bits
    are all ones means "unknown size" and is returned as None value.
    """
    fh.seek(pos)
    buf = fh.read(8)
    if not buf or buf[0] == 0:
        return None
    length = 1
    mask = 0x80
    while not (buf[0] & mask):
        mask >>= 1
        length += 1
        if length > 8:
            return None
    if len(buf) < length:
        return None
    if keep_marker:
        return int.from_bytes(buf[:length], "big"), length
    value = buf[0] & (mask - 1)
    if length > 1:
        value = (value << (8 * (length - 1))) | int.from_bytes(
            buf[1:length], "big")
    if value == (1 << (7 * length)) - 1:
        return None, length  # unknown size
    return value, length


def _mkv_has_subtitle_track(path: str) -> bool | None:
    """A subtitle TrackType inside the file's Tracks element."""
    try:
        with open(path, "rb") as fh:
            return _mkv_children(fh, 0, os.fstat(fh.fileno()).st_size)
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return None


def _mkv_children(fh, start: int, limit: int) -> bool | None:
    """Scan EBML siblings from start to limit for subtitle tracks."""
    pos = start
    while pos + 2 <= limit:
        idr = _ebml_vint(fh, pos, True)
        if idr is None:
            return None
        eid, id_len = idr
        szr = _ebml_vint(fh, pos + id_len, False)
        if szr is None:
            return None
        size, size_len = szr
        body = pos + id_len + size_len
        if eid == _MKV_TRACKS:
            if size is None:
                return None
            return _mkv_tracks(fh, body, min(body + size, limit))
        if eid == _MKV_SEGMENT:
            seg_end = limit if size is None else min(body + size, limit)
            inside = _mkv_children(fh, body, seg_end)
            if inside is not None:
                return inside
            if size is None:
                return None
        if size is None or body + size > limit:
            return None
        pos = body + size
    return None


def _mkv_tracks(fh, start: int, limit: int) -> bool | None:
    """True/False when TrackEntries could be read, None when not."""
    pos = start
    entries = 0
    while pos + 2 <= limit:
        idr = _ebml_vint(fh, pos, True)
        if idr is None:
            return None
        eid, id_len = idr
        szr = _ebml_vint(fh, pos + id_len, False)
        if szr is None:
            return None
        size, size_len = szr
        body = pos + id_len + size_len
        if eid == _MKV_TRACK_ENTRY:
            if size is None:
                return None
            kind = _mkv_track_type(fh, body, min(body + size, limit))
            if kind is None:
                return None
            entries += 1
            if kind in (_TRACK_TYPE_SUBTITLE, _TRACK_TYPE_COMPLEX):
                return True
        if size is None or body + size > limit:
            return None
        pos = body + size
    return False if entries else None


def _mkv_track_type(fh, start: int, limit: int) -> "int | None":
    """The TrackType value of one TrackEntry, None when it is missing."""
    pos = start
    while pos + 2 <= limit:
        idr = _ebml_vint(fh, pos, True)
        if idr is None:
            return None
        eid, id_len = idr
        szr = _ebml_vint(fh, pos + id_len, False)
        if szr is None:
            return None
        size, size_len = szr
        body = pos + id_len + size_len
        if eid == _MKV_TRACK_TYPE:
            if size is None or size < 1:
                return None
            fh.seek(body)
            data = fh.read(min(size, 2))
            return int.from_bytes(data, "big") if data else None
        if size is None or body + size > limit:
            return None
        pos = body + size
    return None


# --- the ISO base media side: walk the boxes down to each stsd ---------

def _mp4_has_subtitle_track(path: str) -> bool | None:
    """A subtitle sample entry inside the file's moov box."""
    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as fh:
            pos = 0
            while pos + 8 <= file_size:
                fh.seek(pos)
                head = fh.read(16)
                if len(head) < 8:
                    return None
                box_size = int.from_bytes(head[:4], "big")
                box_type = head[4:8]
                header = 8
                if box_size == 1:
                    if len(head) < 16:
                        return None
                    box_size = int.from_bytes(head[8:16], "big")
                    header = 16
                elif box_size == 0:
                    box_size = file_size - pos
                if box_size < header:
                    return None
                if box_type == b"moov":
                    fh.seek(pos + header)
                    body = fh.read(min(box_size - header, _MOOV_READ_LIMIT))
                    return _mp4_find_stsd(body, 0, len(body))
                pos += box_size
    except OSError as exc:
        log.debug("cannot read %s: %s", path, exc)
        return None
    return None


def _mp4_find_stsd(data: bytes, start: int, end: int,
                   depth: int = 0) -> bool | None:
    """Walk moov/trak/mdia/... looking at every track's stsd entries.

    True when a subtitle sample entry was found, False when the walk
    completed without one, None when a box could not be walked and the
    caller should fall back to ffprobe.
    """
    broken = False
    pos = start
    while pos + 8 <= end:
        box_size = int.from_bytes(data[pos:pos + 4], "big")
        box_type = data[pos + 4:pos + 8]
        if box_size < 8 or pos + box_size > end:
            broken = True
            break
        body = pos + 8
        if box_type == b"stsd":
            if _mp4_stsd_formats(data, body, body + box_size - 8):
                return True
        elif box_type in _MP4_CONTAINERS and depth < 6:
            found = _mp4_find_stsd(data, body, body + box_size - 8, depth + 1)
            if found:
                return True
            if found is None:
                broken = True
        pos += box_size
    return None if broken else False


def _mp4_stsd_formats(data: bytes, start: int, end: int) -> bool | None:
    """True/False when sample entries could be read, None when not."""
    if end - start < 8:
        return None
    pos = start + 8  # version/flags and the entry count
    entries = 0
    while pos + 8 <= end:
        entry_size = int.from_bytes(data[pos:pos + 4], "big")
        fmt = data[pos + 4:pos + 8]
        if entry_size < 8 or pos + entry_size > end:
            return None
        entries += 1
        if fmt in _MP4_SUBTITLE_FORMATS:
            return True
        pos += entry_size
    return False if entries else None
