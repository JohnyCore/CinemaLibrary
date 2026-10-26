from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk


def unbind_bindall(widget: tk.Misc, sequence: str, funcid) -> None:
    """Remove ONE global binding, keeping the others.

    The old unbind_all(sequence, funcid) form stopped working in Python 3.13
    (TypeError); the same edit is done here against the Tcl side so that
    views can drop their own binding without stripping the rest of the app.
    """
    if not funcid:
        return
    lines = str(widget.tk.call("bind", "all", sequence)).split("\n")
    prefix = f'if {{"[{funcid} '
    keep = "\n".join(line for line in lines if not line.startswith(prefix))
    widget.tk.call("bind", "all", sequence, keep)
    try:
        widget.deletecommand(funcid)
    except tk.TclError:
        pass


class ScrollArea(tk.Frame):
    """A scrollable region: a canvas with a frame inside it.

    The content keeps its natural size. When it does not fit, the region
    scrolls instead of clipping the content away - a details panel that
    grows with the metadata must never push what is below it out of view.
    """

    def __init__(self, master, bg: str, padding: tuple = (0, 0)) -> None:
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                 style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner = tk.Frame(self.canvas, bg=bg, padx=padding[0], pady=padding[1])
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        # the wheel handler has to be global - the pointer is often over the
        # content, not over the canvas - but it belongs to this region only
        # and is removed again when the region goes away
        self._wheel_id = self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.bind("<Destroy>", self._unbind_wheel)
        # the wheel does not jump: it eases toward where the ticks put it
        self._smooth_job: str | None = None
        self._smooth_target: float | None = None

    def _unbind_wheel(self, event=None) -> None:
        """Drop the global wheel binding when the region goes away."""
        if self._smooth_job is not None:
            try:
                self.after_cancel(self._smooth_job)
            except tk.TclError:
                pass
            self._smooth_job = None
        try:
            unbind_bindall(self, "<MouseWheel>", self._wheel_id)
        except Exception:
            pass

    def _scrollregion(self) -> tuple:
        """The region to scroll, never smaller than the window.

        A canvas whose region is smaller than the window lets its content
        float inside it: scrolling up then pushes the content down and
        leaves a gap above it. The region is at least as tall as the
        canvas, so short content simply stays at the top.
        """
        bbox = self.canvas.bbox("all")
        if not bbox:
            return (0, 0, 0, 0)
        x, y, width, height = bbox
        return (x, y, width, max(height, self.canvas.winfo_height()))

    def _on_inner(self, _event=None) -> None:
        self.canvas.itemconfigure(self._window,
                                  height=self.inner.winfo_reqheight())
        self.canvas.configure(scrollregion=self._scrollregion())
        self._update_scrollbar()

    def _on_canvas(self, event=None) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        needed = (bbox[3] - bbox[1]) > self.canvas.winfo_height()
        if needed and not self.vsb.winfo_ismapped():
            self.vsb.grid()
        elif not needed and self.vsb.winfo_ismapped():
            self.vsb.grid_remove()

    def update_scrollregion(self) -> None:
        """The content grew or shrank - the canvas item and the bar follow.

        A canvas window item keeps the size it was given, so a frame
        that just got shorter would stay as tall as it was and leave an
        empty area below the content. The item is given the size the
        content asks for.
        """
        self.inner.update_idletasks()
        self.canvas.itemconfigure(self._window,
                                  height=self.inner.winfo_reqheight())
        self.canvas.configure(scrollregion=self._scrollregion())
        self._update_scrollbar()

    def scroll_to_top(self) -> None:
        """Cancel pending smooth scrolling and return to the start."""
        if self._smooth_job is not None:
            self.after_cancel(self._smooth_job)
            self._smooth_job = None
        self._smooth_target = None
        self.canvas.yview_moveto(0)

    def _on_wheel(self, event) -> None:
        """Scroll this region when the pointer is over it."""
        widget = event.widget
        over = False
        while widget is not None:
            if widget is self:
                over = True
                break
            widget = getattr(widget, "master", None)
        if not over or isinstance(event.widget, ttk.Treeview):
            return
        delta = -event.delta
        if not delta:
            return
        units = int(delta / 40) or (-1 if delta > 0 else 1)
        # a tick moves the same distance it always did - one tenth of the
        # window per canvas unit - but lands smoothly instead of jumping
        self._nudge(units * self._unit_fraction())
        if self._smooth_job is None:
            self._smooth_job = self.after(16, self._smooth_step)

    def _unit_fraction(self) -> float:
        """One canvas unit as a yview fraction."""
        height = self.canvas.winfo_height()
        if height < 2:
            return 0.0
        region = self._scrollregion()[3]
        if region <= 0:
            return 0.0
        return 0.1 * height / region

    def _nudge(self, fraction: float) -> None:
        """Fold one wheel tick into the target the view eases toward."""
        if self._smooth_target is None:
            self._smooth_target = self.canvas.yview()[0]
        self._smooth_target = min(1.0, max(0.0, self._smooth_target + fraction))

    def _smooth_step(self) -> None:
        """Move part of the way to the target, then repeat until arrived."""
        self._smooth_job = None
        if self._smooth_target is None or not self.winfo_exists():
            return
        before = self.canvas.yview()[0]
        gap = self._smooth_target - before
        if abs(gap) < 0.005:
            self.canvas.yview_moveto(self._smooth_target)
            self._smooth_target = None
            return
        self.canvas.yview_moveto(before + gap * 0.35)
        if abs(self.canvas.yview()[0] - before) < 1e-6:
            # the canvas cannot move here - short content, clamped target
            self._smooth_target = None
            return
        self._smooth_job = self.after(16, self._smooth_step)


def set_dark_title_bar(widget: tk.Misc) -> None:
    """Force the Windows title bar of a window into the dark theme."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        dwm = ctypes.windll.dwmapi
        widget.update_idletasks()
        hwnd = user32.GetParent(widget.winfo_id())
        if not hwnd:
            return
        one = ctypes.c_int(1)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (19 on older Win10 builds)
        for attribute in (20, 19):
            if dwm.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(one), ctypes.sizeof(one)
            ) == 0:
                break
        # Windows 11 keeps a light caption when the system theme is light,
        # so force caption (35) and title text (36) colours as fallback.
        # COLORREF is 0x00BBGGRR; #101319 -> 0x00191310.
        caption = ctypes.c_uint(0x00191310)
        text = ctypes.c_uint(0x00EAEAE8)
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4)
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)
    except Exception:
        pass


def center_window(win: tk.Toplevel, parent: tk.Misc | None = None) -> None:
    """Place a popup in the middle of the window it belongs to."""
    win.update_idletasks()
    top = parent.winfo_toplevel() if parent is not None else win.winfo_toplevel()
    if top is win:
        return
    w = win.winfo_width() if win.winfo_width() > 1 else win.winfo_reqwidth()
    h = win.winfo_height() if win.winfo_height() > 1 else win.winfo_reqheight()
    x = top.winfo_rootx() + (top.winfo_width() - w) // 2
    y = top.winfo_rooty() + (top.winfo_height() - h) // 2
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def setup_popup(win: tk.Toplevel, parent: tk.Misc | None = None) -> None:
    """Centre a popup and give it the dark title bar of the main window."""
    set_dark_title_bar(win)
    try:
        win.after(250, lambda: set_dark_title_bar(win))
    except tk.TclError:
        pass
    center_window(win, parent)
