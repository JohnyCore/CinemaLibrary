from __future__ import annotations

import hashlib
import io
import logging
import unicodedata
import urllib.request
from pathlib import Path

from PIL import Image

from metadata import true_story

log = logging.getLogger(__name__)


def person_key(name: str) -> str:
    """Return a stable case-insensitive key for a person name."""
    normalized = unicodedata.normalize("NFKC", name)
    return " ".join(normalized.split()).casefold()


def fetch_person_profile(
    name: str,
    role: str,
    image_dir: Path,
) -> dict[str, str | bool | None]:
    """Fetch a Wikipedia profile and cache its thumbnail on disk."""
    profile = true_story.person_profile(name, role)
    if profile is None:
        return {
            "name": name,
            "name_key": person_key(name),
            "extract": None,
            "image_path": None,
            "url": None,
            "not_found": True,
        }

    image_path = None
    image_error = None
    thumbnail = profile.get("thumbnail")
    if thumbnail:
        digest = hashlib.sha256(person_key(name).encode("utf-8")).hexdigest()[:24]
        target = image_dir / f"{digest}.jpg"
        try:
            request = urllib.request.Request(
                thumbnail, headers={"User-Agent": "CinemaLibrary/1.0 (desktop app)"})
            with urllib.request.urlopen(request, timeout=20) as response:
                image = Image.open(io.BytesIO(response.read())).convert("RGB")
            image.thumbnail((320, 420), getattr(Image, "Resampling", Image).LANCZOS)
            image_dir.mkdir(parents=True, exist_ok=True)
            image.save(target, "JPEG", quality=88, optimize=True)
            image_path = str(target)
        except (OSError, ValueError) as exc:
            image_error = str(exc)
            log.warning("could not cache Wikipedia photo for %s: %s", name, exc)

    return {
        "name": name,
        "name_key": person_key(name),
        "extract": profile.get("extract"),
        "image_path": image_path,
        "url": profile.get("url"),
        "not_found": False,
        "image_error": image_error,
    }
