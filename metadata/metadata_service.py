from __future__ import annotations

import ipaddress
import logging
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen)


from utils import media_ops
from utils import title_parser

log = logging.getLogger(__name__)

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 CinemaLibrary")

CINEMETA_ART_HOSTS = frozenset({
    "images.metahub.space", "images.strem.io", "upload.wikimedia.org",
})
MAX_ART_BYTES = 12 * 1024 * 1024
MAX_ART_PIXELS = 25_000_000
_ART_TIMEOUT = 20


def validated_art_url(url: str | None) -> str | None:
    """Only allowlisted artwork hosts may be downloaded from."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname not in CINEMETA_ART_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    return url


def are_public_art_addresses(addresses: list[str]) -> bool:
    """Artwork must come from a public address - no private or reserved IPs."""
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError:
        return False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_ART_OPENER = build_opener(ProxyHandler({}), NoRedirect())


class DaemonPool:
    """A pool of daemon threads - an idle fetch must not hold the app open."""

    def __init__(self, size: int = 4) -> None:
        self._tasks: "queue.SimpleQueue[Callable[[], None]]" = queue.SimpleQueue()
        self._threads = [
            threading.Thread(
                target=self._run, daemon=True, name=f"metadata-fetch-{index}",
            )
            for index in range(max(1, size))
        ]
        for thread in self._threads:
            thread.start()

    def _run(self) -> None:
        while True:
            task = self._tasks.get()
            try:
                task()
            except Exception:
                log.exception("metadata fetch worker failed")

    def submit(self, task: Callable[[], None]) -> None:
        self._tasks.put(task)


class MetadataError(RuntimeError):
    pass


@dataclass
class MetadataResult:
    title: str | None = None
    year: int | None = None
    plot: str | None = None
    genres: list[str] = field(default_factory=list)
    rating: float | None = None
    runtime: int | None = None
    director: str | None = None
    writer: str | None = None
    cast: list[str] = field(default_factory=list)
    country: str | None = None
    imdb_id: str | None = None
    release_date: str | None = None
    seasons: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    poster_url: str | None = None
    backdrop_url: str | None = None
    provider: str | None = None
    provider_id: str | None = None
    media_type: str | None = None


class MetadataService:
    """Provider-agnostic metadata interface.

    The application runs fully offline until a provider is connected, so the
    UI keeps working with un-fetched metadata. To connect one later, subclass
    and implement `fetch_metadata` and `search_candidates`, then call
    `set_service(YourService())` at startup. Good candidates: Cinemagoer
    (offline IMDb data dumps, pip install Cinemagoer) or OMDb / TMDB / Trakt
    via their REST APIs. Keep API keys in environment variables or in
    settings.json (which is not committed to version control).

    Suggested workflow when fetching for one media item:
      1. clean_query(media.title, media.year) -> provider search text
      2. search_candidates(...) -> list of matches; ambiguous titles are
         confirmed by the user
      3. fetch_metadata(..., imdb_id=chosen.imdb_id) -> MetadataResult
         (an imdb id lets the provider skip a second, identical search)
      4. save_metadata(db, media_id, result) -> merged into the local DB
      5. optionally download_artwork(media, ...), strictly on explicit request
    """

    def fetch_metadata(
        self, title: str, year: int | None = None, media_type: str | None = None,
        imdb_id: str | None = None,
    ) -> MetadataResult | None:
        raise NotImplementedError("Connect a metadata provider first (see README).")

    def search_candidates(
        self, title: str, year: int | None = None, media_type: str | None = None
    ) -> list[MetadataResult]:
        raise NotImplementedError("Connect a metadata provider first (see README).")

    @staticmethod
    def clean_query(title: str, year: int | None = None) -> str:
        return title_parser.clean_for_search(title, year)


_service: MetadataService | None = None
_FETCH_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="metadata")
_FETCH_CANCELS: dict[str, threading.Event] = {}


def set_service(service: MetadataService) -> None:
    global _service
    _service = service


def get_service() -> MetadataService:
    if _service is None:
        raise MetadataError(
            "No metadata provider connected. Implement MetadataService and "
            "register it with metadata_service.set_service()."
        )
    return _service


def fetch_metadata(
    title: str, year: int | None = None, media_type: str | None = None,
    imdb_id: str | None = None,
) -> MetadataResult | None:
    return get_service().fetch_metadata(title, year, media_type, imdb_id)


def fetch_metadata_async(
    title: str, year: int | None = None, media_type: str | None = None,
    imdb_id: str | None = None,
) -> tuple[str, threading.Event]:
    """Queue a metadata fetch on a dedicated worker pool and return a cancel token."""
    token = uuid.uuid4().hex
    cancel = threading.Event()
    _FETCH_CANCELS[token] = cancel

    def run() -> None:
        try:
            if cancel.is_set():
                return
            get_service().fetch_metadata(title, year, media_type, imdb_id)
        finally:
            _FETCH_CANCELS.pop(token, None)

    _FETCH_POOL.submit(run)
    return token, cancel


def cancel_fetch(token: str) -> bool:
    cancel = _FETCH_CANCELS.get(token)
    if cancel is None:
        return False
    cancel.set()
    return True


def search_candidates(
    title: str, year: int | None = None, media_type: str | None = None
) -> list[MetadataResult]:
    return get_service().search_candidates(title, year, media_type)


def save_metadata(db: Any, media_id: int, result: MetadataResult) -> None:
    db.save_metadata(
        media_id,
        {
            "title": result.title,
            "year": result.year,
            "plot": result.plot,
            "genres": result.genres,
            "rating": result.rating,
            "runtime": result.runtime,
            "director": result.director,
            "writer": result.writer,
            "cast": result.cast,
            "country": result.country,
            "imdb_id": result.imdb_id,
            "release_date": result.release_date,
        },
    )
    if result.seasons:
        db.set_episode_names(media_id, result.seasons)
        db.set_expected_episodes(media_id, result.seasons)


def write_about(media: Mapping[str, Any], result: MetadataResult) -> str | None:
    """Write a human-readable about.txt next to the movie/series files."""
    base = Path(str(media.get("video_path") or media.get("path")))
    folder = base.parent if base.is_file() and base.suffix else base
    if not folder.is_dir():
        return None
    def line(label: str, value) -> str:
        text = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value or "")
        return f"{label}: {text.strip()}\n" if text.strip() else ""

    runtime = result.runtime
    body = "".join([
        f"{result.title or ''} ({result.year or '?'})\n",
        f"Type: {'Series' if media.get('media_type') == 'series' else 'Movie'}\n",
        f"Source: {result.provider or 'unknown'}\n\n",
        line("Plot", result.plot),
        line("Genres", result.genres),
        line("Rating", f"{result.rating:.1f} / 10" if result.rating is not None else None),
        line("Runtime", f"{runtime // 60}h {runtime % 60:02d}m" if runtime else None),
        line("Director", result.director),
        line("Writer", result.writer),
        line("Cast", result.cast),
        line("Country", result.country),
        line("Release date", result.release_date),
        line("IMDb", result.imdb_id),
    ])
    target = folder / f"{media_ops.artwork_prefix(media)}about.txt"
    try:
        target.write_text(body, encoding="utf-8")
    except OSError as exc:
        log.warning("could not write about.txt in %s: %s", folder, exc)
        return None
    return str(target)


def download_artwork(
    media: Mapping[str, Any],
    poster_url: str | None,
    backdrop_url: str | None,
    overwrite: bool = False,
) -> dict[str, str]:
    """Download artwork into the media folder, strictly on explicit request.

    Existing artwork is never replaced unless overwrite=True was passed by an
    explicitly user-triggered action. Files are always stored as .jpg so the
    artwork lookup finds them whatever the source format was.
    """
    base = Path(str(media.get("video_path") or media.get("path")))
    folder = base.parent if base.is_file() and base.suffix else base
    # a loose video that shares its folder names its artwork after the
    # file, so two bare movies in one folder do not overwrite each other
    prefix = media_ops.artwork_prefix(media)
    downloaded: dict[str, str] = {}
    for kind, url in (("poster", poster_url), ("poster2", backdrop_url)):
        if not url:
            continue
        if not overwrite and any(folder.glob(f"{prefix}{kind}.*")):
            continue
        target = folder / f"{prefix}{kind}.jpg"
        # every download gets its own temp file: two items in one folder
        # must not write into the same file
        tmp = folder / f".{prefix}{kind}.{uuid.uuid4().hex[:8]}.part"
        try:
            req = Request(url, headers={"User-Agent": _USER_AGENT})
            with urlopen(req, timeout=30) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
            _to_jpeg(tmp, target)
            tmp.unlink(missing_ok=True)
            downloaded[kind] = str(target)
        except OSError as exc:
            log.warning("artwork download failed for %s: %s", url, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return downloaded


def _to_jpeg(src: Path, dst: Path) -> None:
    """Convert a downloaded image to JPEG, so artwork is always poster.jpg."""
    try:
        from PIL import Image

        with Image.open(src) as im:
            im = im.convert("RGB")
            im.save(dst, "JPEG", quality=90)
    except Exception:
        # not an image we can read - keep the raw bytes
        src.replace(dst)
