from __future__ import annotations

import http.client
import json
import logging
import re
import threading
import time
from urllib.parse import quote, urlsplit

from metadata.metadata_service import MetadataError, MetadataResult, MetadataService

log = logging.getLogger(__name__)

_BASE = "https://v3-cinemeta.strem.io"
_TIMEOUT = 20
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CinemaLibrary"
# how long to wait before the n-th retry of a 429 / 5xx answer
_BACKOFF = (0.8, 2.0, 4.0)

_TYPE_MAP = {"movie": "movie", "series": "series", "tv": "series"}

_YEAR_RE = re.compile(r"(19|20)\d{2}")

# one keep-alive connection per thread: a full bulk fetch is 800+ requests,
# and a fresh TLS handshake for every one of them dominates the run time
_local = threading.local()


def _connection(host: str) -> http.client.HTTPSConnection:
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    conn = conns.get(host)
    if conn is None:
        conn = http.client.HTTPSConnection(host, timeout=_TIMEOUT)
        conns[host] = conn
    return conn


def _drop_connection(host: str) -> None:
    conns = getattr(_local, "conns", None) or {}
    conn = conns.pop(host, None)
    if conn is not None:
        try:
            conn.close()
        except OSError:
            pass


def _get_json(url: str) -> dict:
    """GET a json document.

    The connection is reused, and a 429 or a 5xx answer is retried with a
    backoff - a busy CDN used to fail the whole item for good.
    """
    parts = urlsplit(url)
    host = parts.hostname or "v3-cinemeta.strem.io"
    path = parts.path + (("?" + parts.query) if parts.query else "")
    last: Exception | None = None
    for attempt in range(len(_BACKOFF) + 1):
        try:
            conn = _connection(host)
            conn.request("GET", path, headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/json",
                "Connection": "keep-alive",
            })
            response = conn.getresponse()
            body = response.read()
            status = response.status
        except (http.client.HTTPException, OSError) as exc:
            last = exc
            _drop_connection(host)
            if attempt >= len(_BACKOFF):
                break
            time.sleep(_BACKOFF[attempt])
            continue
        if status == 429 or status >= 500:
            last = RuntimeError(f"HTTP {status}")
            _drop_connection(host)
            if attempt >= len(_BACKOFF):
                break
            time.sleep(_BACKOFF[attempt])
            continue
        if status >= 400:
            raise MetadataError(f"HTTP {status} for {url}")
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise MetadataError(f"invalid json from {url}: {exc}") from exc
    raise MetadataError(f"request failed for {url}: {last}")


def _parse_year(meta: dict) -> int | None:
    year = meta.get("year")
    if isinstance(year, int) and year:
        return year
    text = str(meta.get("releaseInfo") or "")
    m = _YEAR_RE.search(text)
    return int(m.group(0)) if m else None


def _runtime_minutes(value) -> int | None:
    """The runtime in minutes - "2h 36min" is 156, not 36."""
    if isinstance(value, int):
        return value
    text = str(value or "")
    m = re.search(r"(\d+)\s*h(?:ours?|rs?)?\s*(\d+)?", text, re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2) or 0)
    m = re.search(r"(\d+)\s*min", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{2,4})", text)
    return int(m.group(1)) if m else None


def _names(value) -> list[str] | None:
    if isinstance(value, list):
        names = [str(v) for v in value if str(v).strip()]
    elif value:
        names = [str(value)]
    else:
        names = []
    return ", ".join(names) if names else None


def _score(query: str, name: str, year: int | None, want_year: int | None) -> int:
    q, n = query.strip().lower(), name.strip().lower()
    score = 0
    if q == n:
        score += 10
    elif q in n:
        score += 8
    tokens = [t for t in q.split() if len(t) > 1]
    if tokens:
        hits = sum(1 for t in tokens if t in n)
        if hits == 0:
            return 0
        score += hits
    elif not tokens and q not in n:
        return 0
    if want_year and year:
        if abs(year - want_year) <= 1:
            score += 2
        elif abs(year - want_year) > 2:
            score -= 2
    return score


def _to_result(meta: dict, media_type: str) -> MetadataResult:
    def art(kind: str) -> str | None:
        url = meta.get(kind)
        return url.replace("/small/", "/medium/") if url else None

    seasons: dict[int, list[dict]] = {}
    for video in meta.get("videos") or []:
        s, e = video.get("season"), video.get("episode")
        name = video.get("name") or video.get("title")
        if not (isinstance(s, int) and isinstance(e, int) and name):
            continue
        released = str(video.get("released") or "")[:10]
        summary = str(video.get("overview") or video.get("description") or "").strip()
        seasons.setdefault(s, []).append({
            "episode": e, "name": str(name),
            "released": released or None, "summary": summary or None,
        })

    release_date = None
    released = str(meta.get("released") or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", released)
    if m:
        release_date = m.group(0)

    return MetadataResult(
        title=meta.get("name"),
        year=_parse_year(meta),
        plot=meta.get("description"),
        genres=meta.get("genres") or ([meta["genre"]] if meta.get("genre") else []),
        rating=(lambda v: float(v) if isinstance(v, (int, float, str)) and re.match(r"^\d+(\.\d+)?$", str(v)) else None)(
            meta.get("imdbRating")
        ),
        runtime=_runtime_minutes(meta.get("runtime")),
        director=_names(meta.get("director")),
        writer=_names(meta.get("writer")),
        cast=[str(c) for c in meta.get("cast") or []],
        imdb_id=meta.get("imdb_id") or (meta.get("id") if str(meta.get("id") or "").startswith("tt") else None),
        country=_names(meta.get("country")),
        release_date=release_date,
        poster_url=art("poster"),
        backdrop_url=art("background"),
        seasons=seasons,
        provider="Cinemeta",
    )


class CinemetaService(MetadataService):
    """Metadata via Stremio's public Cinemeta API -- no key, no signup.

    Endpoints (path extras, not query strings):
      /catalog/{type}/top/search={query}.json   -> list of {id, name, releaseInfo}
      /meta/{type}/{imdb_id}.json               -> full metadata
    Requires an IMDb id; ties results back to the library item after saving.
    """

    def search_candidates(
        self, title: str, year: int | None = None, media_type: str | None = None
    ) -> list[MetadataResult]:
        query = self.clean_query(title, year)
        types = [_TYPE_MAP.get(media_type or "", "movie")]
        if not media_type:
            types = ["movie", "series"]
        hits: list[MetadataResult] = []
        seen: set[str] = set()
        for t in types:
            url = f"{_BASE}/catalog/{t}/top/search={quote(query)}.json"
            try:
                data = _get_json(url)
            except Exception as exc:
                log.warning("cinemeta search failed (%s): %s", t, exc)
                continue
            for meta in data.get("metas", []):
                cid = meta.get("id")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                hits.append(_to_result(meta, t))
        # the catalog answers with a hundred titles - only the ones that
        # really match the title are worth showing
        hits = [h for h in hits
                if _score(query, h.title or "", h.year, year) > 0]
        hits.sort(key=lambda h: -_score(query, h.title or "", h.year, year))
        return hits[:20]

    def fetch_metadata(
        self, title: str, year: int | None = None, media_type: str | None = None,
        imdb_id: str | None = None,
    ) -> MetadataResult | None:
        if imdb_id and media_type:
            # the id is known already - skip the catalog search entirely
            t = _TYPE_MAP.get(media_type, "movie")
            try:
                data = _get_json(f"{_BASE}/meta/{t}/{imdb_id}.json")["meta"]
            except Exception as exc:
                log.warning("cinemeta meta fetch failed for %s: %s", imdb_id, exc)
            else:
                return _to_result(data, t)
        query = self.clean_query(title, year)
        if media_type:
            types = [_TYPE_MAP.get(media_type, "movie")]
        else:
            types = ["movie", "series"]  # fall back to series when no movie matches

        best: tuple[int, str, str] | None = None  # (score, type, id)
        for t in types:
            url = f"{_BASE}/catalog/{t}/top/search={quote(query)}.json"
            try:
                data = _get_json(url)
            except Exception as exc:
                raise MetadataError(f"search failed: {exc}") from exc
            for meta in data.get("metas", []):
                s = _score(query, meta.get("name") or "", _parse_year(meta), year)
                if s > 0 and (best is None or s > best[0]):
                    best = (s, t, meta["id"])
        if best is None:
            return None
        _, t, cid = best
        try:
            data = _get_json(f"{_BASE}/meta/{t}/{cid}.json")["meta"]
        except Exception as exc:
            raise MetadataError(f"meta fetch failed for {cid}: {exc}") from exc
        return _to_result(data, t)
