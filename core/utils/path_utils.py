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
