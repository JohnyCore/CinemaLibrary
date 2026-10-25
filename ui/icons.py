"""Offline icon service.

Bundles the Font Awesome Free webfonts in assets/fonts, registers them
privately for the process with AddFontResourceExW (no admin install,
nothing fetched at runtime), and hands icons out in two forms:

- `label(...)`   a tk.Label showing the glyph as text - the cheap path
                 for anything that accepts a plain widget
- `photo(...)`   the glyph rendered into a transparent PhotoImage
                 (Pillow), for widgets that need an image (ttk.Button,
                 ttk.Label image=, ...)
- `glyph(...)` / `font(...)` for putting a glyph inside existing text

Everything works fully offline; the font files ship in the repo and the
license for redistribution is assets/fonts/FA-LICENSE.txt (SIL OFL 1.1
/ CC BY 4.0). Non-Windows hosts must have the "Font Awesome 6 Free"
family installed system-wide; the app is Windows-targeted.

Adding an icon: look the codepoint up in the Font Awesome 6 cheat sheet,
e.g. "magnifying-glass-chart f522", add one line to _CODE (or to
_BRAND_CODE for brand marks like imdb).
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageTk

_FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_SOLID = "fa-solid-900.ttf"
_REGULAR = "fa-regular-400.ttf"
_BRANDS = "fa-brands-400.ttf"
SOLID_FAMILY = "Font Awesome 6 Free Solid"
REGULAR_FAMILY = "Font Awesome 6 Free Regular"
BRANDS_FAMILY = "Font Awesome 6 Brands"

# name -> codepoint from the Font Awesome 6 Free cheat sheet
_CODE = {
    "back": "f060",       # arrow-left
    "play": "f04b",
    "check": "f00c",
    "circle": "f111",     # the outlined ring (regular family)
    "film": "f008",
    "folder": "f07b",
    "star": "f005",
    "heart": "f004",
    "bookmark": "f02e",
    "search": "f002",
    "gear": "f013",
    "refresh": "f021",
    "download": "f019",
    "eye": "f06e",
    "calendar": "f133",
    "clock": "f017",
    "trash": "f1f8",      # trash-can
    "backdrop": "f65e",   # images
    "link": "f0c1",
    "pen": "f304",        # writer fact
    "users": "f0c0",      # cast fact
    "globe": "f0ac",      # country fact
}
# brand glyphs live in their own family, not in Free
_BRAND_CODE = {
    "imdb": "f1ac",
}

_installed = False
_ui_fonts: dict[tuple[int, bool], tkfont.Font] = {}
_pil_fonts: dict[tuple[Path, int], ImageFont.FreeTypeFont] = {}
_photos: dict[tuple[str, int, str, bool], ImageTk.PhotoImage] = {}


def font(size: int = 14, *, outline: bool = False) -> tkfont.Font:
    """A ready-made icon-font Font for labels (solid unless outline=True)."""
    install()
    key = (size, outline)
    f = _ui_fonts.get(key)
    if f is None:
        family = REGULAR_FAMILY if outline else SOLID_FAMILY
        f = tkfont.Font(family=family, size=size)
        _ui_fonts[key] = f
    return f


def _code(name: str) -> str:
    return _BRAND_CODE.get(name) or _CODE[name]


def _family(name: str, *, outline: bool = False) -> str:
    if name in _BRAND_CODE:
        return BRANDS_FAMILY
    return REGULAR_FAMILY if outline else SOLID_FAMILY


def install() -> None:
    """Register the bundled fonts with Windows for this process only."""
    global _installed
    if _installed or sys.platform != "win32":
        _installed = True
        return
    for name in (_SOLID, _REGULAR, _BRANDS):
        try:
            ctypes.windll.gdi32.AddFontResourceExW(
                str(_FONTS / name), 0x10, 0)  # FR_PRIVATE
        except OSError:
            pass
    _installed = True


def glyph(name: str) -> str:
    """The unicode character for an icon name."""
    install()
    return chr(int(_code(name), 16))


def label(parent: tk.Misc, name: str, *, size: int = 14,
          color: str = "#d3ddec", outline: bool = False) -> tk.Label:
    """A small icon widget, drawn by Tk itself."""
    try:
        bg = parent.cget("bg") or None
    except tk.TclError:  # ttk parents: caller sets bg themselves
        bg = None
    family = _family(name, outline=outline)
    return tk.Label(
        parent, text=glyph(name),
        font=tkfont.Font(family=family, size=size), bg=bg, fg=color,
    )


def photo(name: str, *, size: int = 16, color: str = "#ffffff",
          outline: bool = False) -> ImageTk.PhotoImage:
    """The icon as a PhotoImage, e.g. for ttk.Button(image=...)."""
    install()
    key = (name, size, color, outline)
    ph = _photos.get(key)
    if ph is None:
        path = _FONTS / (_BRANDS if name in _BRAND_CODE
                         else _REGULAR if outline else _SOLID)
        fkey = (path, size)
        f = _pil_fonts.get(fkey)
        if f is None:
            f = ImageFont.truetype(str(path), size)
            _pil_fonts[fkey] = f
        g = glyph(name)
        # render generously, then crop to the ink - glyph boxes vary
        probe = Image.new("RGBA", (size * 3, size * 3), (0, 0, 0, 0))
        draw = ImageDraw.Draw(probe)
        box = draw.textbbox((0, 0), g, font=f)
        img = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]),
                        (0, 0, 0, 0))
        ImageDraw.Draw(img).text((box[0], box[1]), g, font=f, fill=color)
        ph = ImageTk.PhotoImage(img)
        _photos[key] = ph
    return ph
