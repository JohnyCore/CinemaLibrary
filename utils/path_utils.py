from __future__ import annotations

import os
from pathlib import Path


def canonical_path(value: str | Path | None) -> str:
    """Stable, case-insensitive path key for comparisons across roots."""
    if value is None:
        return ""
    text = os.path.normpath(str(value)).strip()
    if not text:
        return ""
    return os.path.normcase(text).replace("\\", os.sep).replace("/", os.sep)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write a text file so a crash can never leave a torn file behind.

    The data lands in a sibling temp file that is then moved over the
    target in one step - a half-written settings file used to reset the
    user's library folders to their defaults.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
