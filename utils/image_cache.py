from __future__ import annotations

import hashlib
import logging
import os
import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image, ImageTk

from . import image_utils

log = logging.getLogger(__name__)

# The details backdrop is rendered at a few fixed sizes. Without this every
# window size would produce its own cached file, and the cache would grow
# with every resize.
BACKDROP_SIZES = ((640, 360), (960, 540), (1280, 720), (1600, 900),
                  (1920, 1080), (2560, 1440))


def bucket_size(width: int, height: int) -> tuple[int, int]:
    """The smallest backdrop bucket that covers the requested size."""
    for w, h in BACKDROP_SIZES:
        if w >= width and h >= height:
            return (w, h)
    return BACKDROP_SIZES[-1]


@dataclass
class ImageJob:
    source: str
    width: int
    height: int
    mode: str
    priority: int
    order: int
    callback: Callable[[ImageTk.PhotoImage], None]
    name: str | None = None
    disk: Path | None = None
    contain: bool = False
    cancelled: bool = False
    done: bool = False


class ImageCache:
    """Bounded async image loader.

    - disk cache of resized movie posters / backgrounds (persists between runs)
    - bounded in-memory LRU of PhotoImage objects
    - small priority worker pool; visible cards request with lower priority
    - results delivered to the main thread through `post_to_main`
    """

    def __init__(
        self,
        cache_dir: Path,
        post_to_main: Callable[[Callable[[], None]], None],
        placeholder_png: Path,
        workers: int = 3,
        max_thumbs: int = 256,
        max_backdrops: int = 3,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._post = post_to_main
        self._placeholder_png = Path(placeholder_png)
        self._max_thumbs = max_thumbs
        self._max_backdrops = max_backdrops
        self._thumbs: OrderedDict[str, ImageTk.PhotoImage] = OrderedDict()
        self._backdrops: OrderedDict[str, ImageTk.PhotoImage] = OrderedDict()
        self._placeholders: dict[tuple[int, int], ImageTk.PhotoImage] = {}
        self._flats: dict[tuple[int, int], ImageTk.PhotoImage] = {}
        self._jobs: "queue.PriorityQueue[tuple[int, int, ImageJob | None]]" = queue.PriorityQueue()
        self._order = 0
        self._alive = True
        # the counters are written by the workers and by the main thread
        self._stats_lock = threading.Lock()
        self.stats: dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "generated": 0,
            "disk_hits": 0,
            "errors": 0,
            "gen_ms": 0,
            "active": 0,
            "queued": 0,
        }
        self._workers = max(1, workers)
        for _ in range(self._workers):
            threading.Thread(target=self._worker, daemon=True, name="image-cache").start()

    def _bump(self, key: str, amount: int = 1) -> None:
        """Change a counter - the workers and the UI thread share them."""
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def _key(
        self, source: str | None, width: int, height: int, mode: str,
        contain: bool = False,
    ) -> tuple[str | None, Path | None]:
        if not source:
            return None, None
        try:
            st = os.stat(source)
            sig = f"{source}|{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            sig = f"{source}|missing"
        digest = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:18]
        # the fit is part of the name: a letterboxed poster and a cropped
        # one are different pictures
        fit = "_fit" if contain else ""
        name = f"{digest}_{mode}_{width}x{height}{fit}.jpg"
        return name, self._cache_dir / name

    def placeholder(self, width: int, height: int) -> ImageTk.PhotoImage:
        ph = self._placeholders.get((width, height))
        if ph is None:
            img = image_utils.open_rgb(str(self._placeholder_png), (width * 2, height * 2))
            if img is None:
                img = Image.new("RGB", (width, height), (23, 26, 31))
            ph = ImageTk.PhotoImage(image_utils.cover_fit(img, width, height))
            self._placeholders[(width, height)] = ph
        return ph

    def _flat(self, width: int, height: int) -> ImageTk.PhotoImage:
        ph = self._flats.get((width, height))
        if ph is None:
            ph = ImageTk.PhotoImage(Image.new("RGB", (width, height), (16, 19, 25)))
            self._flats[(width, height)] = ph
        return ph

    def get_thumbnail(
        self,
        source: str | None,
        size: tuple[int, int],
        on_ready: Callable[[ImageTk.PhotoImage], None],
        *,
        priority: int = 5,
        contain: bool = True,
    ) -> ImageJob | None:
        """A poster thumbnail. By default the whole poster is kept."""
        return self._request(source, size, "thumb", on_ready, priority, contain=contain)

    def get_backdrop(
        self,
        source: str | None,
        size: tuple[int, int],
        on_ready: Callable[[ImageTk.PhotoImage], None],
        *,
        priority: int = 5,
        disk_cache: bool = True,
    ) -> ImageJob | None:
        """Request a backdrop, optionally avoiding a disk entry per size."""
        return self._request(
            source, size, "backdrop", on_ready, priority,
            disk_cache=disk_cache,
        )

    def _request(
        self,
        source: str | None,
        size: tuple[int, int],
        mode: str,
        on_ready: Callable[[ImageTk.PhotoImage], None],
        priority: int,
        contain: bool = False,
        disk_cache: bool = True,
    ) -> ImageJob | None:
        width, height = size
        name, disk = self._key(source, width, height, mode, contain)
        if not disk_cache:
            disk = None
        store = self._thumbs if mode == "thumb" else self._backdrops
        if name is not None:
            cached = store.get(name)
            if cached is not None:
                self._bump("hits")
                store.move_to_end(name)
                on_ready(cached)
                return None
        # A backdrop with a source keeps the picture that is on screen until
        # the real one arrives. A thumbnail shows the "No poster" artwork
        # while it is loading - and stays on it when there is no poster.
        if mode == "backdrop" and source is not None:
            pass  # skip immediate delivery
        elif mode == "thumb":
            on_ready(self.placeholder(width, height))
        else:
            on_ready(self._flat(width, height))
        if source is None:
            return None
        self._order += 1
        job = ImageJob(
            source=str(source),
            width=width,
            height=height,
            mode=mode,
            priority=priority,
            order=self._order,
            callback=on_ready,
            name=name,
            disk=disk,
            contain=contain,
        )
        self._bump("queued")
        self._bump("misses")
        self._jobs.put((job.priority, job.order, job))
        return job

    def cancel(self, job: ImageJob | None) -> None:
        if job is not None and not job.done:
            job.cancelled = True

    def _worker(self) -> None:
        while True:
            _, _, job = self._jobs.get()
            if job is None:  # shutdown sentinel
                break
            self._bump("queued", -1)
            if job.cancelled:
                continue
            self._bump("active")
            name, disk_path = job.name, job.disk
            img: Image.Image | None = None
            if Path(job.source).is_file():
                if disk_path is not None and disk_path.is_file():
                    img = image_utils.open_rgb(str(disk_path))
                    if img is not None:
                        self._bump("disk_hits")
                if img is None:
                    t0 = time.perf_counter()
                    img = image_utils.prepare_art(job.source, job.width, job.height,
                                                  job.mode, contain=job.contain)
                    ms = int((time.perf_counter() - t0) * 1000)
                    if img is not None:
                        self._bump("generated")
                        self._bump("gen_ms", ms)
                        if disk_path is not None:
                            try:
                                img.save(disk_path, "JPEG", quality=82)
                            except (OSError, ValueError):
                                pass
            if job.cancelled:
                self._bump("active", -1)
                continue
            self._post(lambda j=job, n=name, im=img: self._finish(j, n, im))

    def _finish(self, job: ImageJob, name: str | None, img: Image.Image | None) -> None:
        self._bump("active", -1)
        job.done = True
        if not self._alive or job.cancelled:
            return
        try:
            if img is None:
                self._bump("errors")
                image = self._flat(job.width, job.height)
            else:
                image = ImageTk.PhotoImage(img)
        except Exception:
            return
        store = self._thumbs if job.mode == "thumb" else self._backdrops
        cap = self._max_thumbs if job.mode == "thumb" else self._max_backdrops
        if name is not None:
            store[name] = image
            store.move_to_end(name)
            while len(store) > cap:
                store.popitem(last=False)
        try:
            job.callback(image)
        except Exception:
            log.exception("image callback failed")

    def snapshot_stats(self) -> dict[str, int]:
        return dict(self.stats)

    # --- disk cache maintenance ---------------------------------------

    def clear_disk_cache(self) -> int:
        """Delete every cached image file."""
        return self._remove_files(lambda name: True)

    def prune_disk_cache(self, keep_parts: set[str]) -> int:
        """Delete cached files whose name contains none of `keep_parts`."""
        return self._remove_files(lambda name: not any(p in name for p in keep_parts))

    def _remove_files(self, should_remove) -> int:
        removed = 0
        try:
            for f in self._cache_dir.iterdir():
                if not f.is_file():
                    continue
                if not should_remove(f.name):
                    continue
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        return removed

    def clear_memory(self) -> None:
        """Drop the in-memory thumbnail LRU.

        The widgets keep their own reference to the picture they show, so
        the images on screen stay where they are - only the cache is gone
        and the next request reads them from the disk again.
        """
        self._thumbs.clear()
        self._backdrops.clear()

    def close(self) -> None:
        self._alive = False
        for _ in range(self._workers):
            self._jobs.put((0, -1, None))
