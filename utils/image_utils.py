from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

LANCZOS = getattr(Image, "Resampling", Image).LANCZOS

_BACKDROP_STEMS = ("poster2", "fanart", "backdrop")
_ART_SUBDIRS = {"artwork", "extrafanart", "images", "posters", "fanart", "art"}
_LETTERBOX_BG = (16, 19, 25)


def _dir_map(folder: Path, cache: dict | None = None) -> dict[str, os.DirEntry]:
    key = str(folder)
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit
    try:
        names = {e.name.lower(): e for e in os.scandir(folder)}
    except OSError:
        names = {}
    if cache is not None:
        cache[key] = names
    return names


def _find(names: dict[str, os.DirEntry], stem: str) -> Path | None:
    for suffix in (".jpg", ".png", ".webp", ".jpeg", ".jfif", ".bmp"):
        entry = names.get(stem + suffix)
        if entry is not None and entry.is_file():
            return Path(entry.path)
    return None


def find_artwork(
    folder: Path, cache: dict | None = None
) -> tuple[Path | None, Path | None]:
    """Return (poster, backdrop) for a folder.

    `cache` is an optional per-scan dict of directory listings; pass the same
    dict to every call of one scan to avoid re-listing the same folder.
    """
    names = _dir_map(folder, cache)
    poster = _find(names, "poster")
    backdrop = _find(names, "poster2") or _find(names, "fanart") or _find(names, "backdrop")
    if poster is None or backdrop is None:
        for name in sorted(names):
            if poster is not None and backdrop is not None:
                break
            entry = names[name]
            if entry.name.lower() not in _ART_SUBDIRS:
                continue
            try:
                if not entry.is_dir():
                    continue
                sub = _dir_map(Path(entry.path), cache)
            except OSError:
                continue
            if poster is None:
                poster = _find(sub, "poster")
            if backdrop is None:
                backdrop = _find(sub, "poster2") or _find(sub, "fanart") or _find(sub, "backdrop")
    return poster, backdrop


def ensure_placeholder(path: Path, width: int = 320, height: int = 480) -> None:
    try:
        if path.exists():
            return
        img = Image.new("RGB", (width, height), (23, 26, 31))
        d = ImageDraw.Draw(img)
        d.rectangle((14, 14, width - 15, height - 15), outline=(58, 63, 72), width=2)
        cx, cy = width // 2, height // 2 - 10
        r = 52
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(84, 90, 102), width=3)
        d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(84, 90, 102))
        d.text((cx - 30, cy + r + 16), "No poster", fill=(140, 148, 160))
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, "PNG")
    except OSError:
        pass


def open_rgb(path: str, draft_size: tuple[int, int] | None = None) -> Image.Image | None:
    try:
        with Image.open(path) as opened:
            if draft_size is not None and opened.format == "JPEG":
                opened.draft("RGB", draft_size)
            # the transposed copy is a new image - the file that was opened
            # is closed by the "with" block above
            image = ImageOps.exif_transpose(opened) or opened
            return image.convert("RGB")
    except Exception:
        return None


def cover_fit(
    img: Image.Image, width: int, height: int, background: tuple = _LETTERBOX_BG,
    contain: bool = False,
) -> Image.Image:
    """Resize the image to the box.

    By default the image fills the box and the overflow is cropped. With
    `contain` the whole image is kept and the rest is letterboxed - a
    poster of an unusual shape must not lose half of itself.
    """
    sw, sh = img.size
    if sw < 1 or sh < 1:
        return img
    if contain:
        scale = min(width / sw, height / sh)
        nw = max(1, int(sw * scale + 0.5))
        nh = max(1, int(sh * scale + 0.5))
        smaller = img.resize((nw, nh), LANCZOS)
        out = Image.new("RGB", (width, height), background)
        out.paste(smaller, ((width - nw) // 2, (height - nh) // 2))
        return out
    scale = max(width / sw, height / sh)
    if scale >= 1.0:
        nw = max(width, int(sw * scale + 0.5))
        nh = max(height, int(sh * scale + 0.5))
        img = img.resize((nw, nh), LANCZOS)
        left = (nw - width) // 2
        top = (nh - height) // 2
        return img.crop((left, top, left + width, top + height))
    nw = max(1, int(sw * scale + 0.5))
    nh = max(1, int(sh * scale + 0.5))
    smaller = img.resize((nw, nh), LANCZOS)
    out = Image.new("RGB", (width, height), background)
    out.paste(smaller, ((width - nw) // 2, (height - nh) // 2))
    return out


def prepare_art(
    path: str, width: int, height: int, mode: str, contain: bool = False
) -> Image.Image | None:
    img = open_rgb(path, (width * 2, height * 2))
    if img is None:
        return None
    img = cover_fit(img, width, height, contain=contain)
    if mode == "backdrop":
        img = Image.blend(img, Image.new("RGB", img.size, (10, 12, 16)), 0.55)
    return img
