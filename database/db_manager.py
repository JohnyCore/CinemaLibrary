from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    media_type TEXT NOT NULL,
    year INTEGER,
    root TEXT,
    video_path TEXT,
    poster_path TEXT,
    poster2_path TEXT,
    mtime REAL,
    imdb_id TEXT,
    plot TEXT,
    genres TEXT,
    rating REAL,
    runtime INTEGER,
    director TEXT,
    writer TEXT,
    cast_list TEXT,
    country TEXT,
    release_date TEXT,
    watched INTEGER NOT NULL DEFAULT 0,
    last_watched REAL,
    added_at REAL,
    last_scanned REAL,
    collection_id INTEGER,
    true_story INTEGER,
    true_story_article TEXT,
    my_rating INTEGER,
    quotes TEXT,
    story_cache TEXT,
    from_provider INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type);
CREATE INDEX IF NOT EXISTS idx_media_title ON media(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_media_root ON media(root);
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    series_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    season INTEGER,
    episode INTEGER,
    name TEXT,
    released TEXT,
    summary TEXT,
    name_from_provider INTEGER NOT NULL DEFAULT 0,
    path TEXT NOT NULL,
    watched INTEGER NOT NULL DEFAULT 0,
    last_watched REAL
);
CREATE TABLE IF NOT EXISTS ignored (
    path TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS expected_episodes (
    series_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    episode INTEGER NOT NULL,
    name TEXT,
    PRIMARY KEY (series_id, season, episode)
);
CREATE TABLE IF NOT EXISTS person_profiles (
    name_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    extract TEXT,
    image_path TEXT,
    url TEXT,
    not_found INTEGER NOT NULL DEFAULT 0,
    fetched_at REAL NOT NULL
);
"""

# created after the migration, so it can rely on the newer columns
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_episodes_series ON episodes(series_id, season, episode);
CREATE INDEX IF NOT EXISTS idx_episodes_watched ON episodes(watched, last_watched);
CREATE INDEX IF NOT EXISTS idx_media_collection ON media(collection_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_row
    ON episodes(series_id, season, episode, path);
"""

# the folder an item is filed in comes along with the item
_SELECT_MEDIA = """
SELECT m.*, c.name AS collection_name FROM media m
LEFT JOIN collections c ON c.id = m.collection_id
"""

_UPSERT_MEDIA = """
INSERT INTO media (
    path, title, media_type, year, root, video_path, poster_path, poster2_path,
    mtime, last_scanned, added_at
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(path) DO UPDATE SET
    title = CASE WHEN media.from_provider = 1
                 THEN media.title ELSE excluded.title END,
    media_type = excluded.media_type,
    year = CASE WHEN media.from_provider = 1
                THEN COALESCE(media.year, excluded.year) ELSE excluded.year END,
    root = excluded.root,
    video_path = excluded.video_path,
    poster_path = COALESCE(excluded.poster_path, media.poster_path),
    poster2_path = COALESCE(excluded.poster2_path, media.poster2_path),
    mtime = excluded.mtime,
    last_scanned = excluded.last_scanned,
    added_at = COALESCE(media.added_at, excluded.added_at)
"""

# watched / last_watched are deliberately absent from the DO UPDATE set:
# a rescan must never reset what the user already marked as seen.
# The conflict key covers chained files ("S04E01E02.mkv") that hold more
# than one episode - their rows share the path.
_UPSERT_EPISODE = """
INSERT INTO episodes(series_id, season, episode, name, released, summary,
                     name_from_provider, path, watched, last_watched)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(series_id, season, episode, path) DO UPDATE SET
    name = CASE WHEN episodes.name_from_provider = 1
                THEN episodes.name ELSE excluded.name END
"""

# for rows without an episode number ("files the app could not read"),
# which the sync handles as delete-and-insert instead
_INSERT_EPISODE = """
INSERT INTO episodes(series_id, season, episode, name, released, summary,
                     name_from_provider, path, watched, last_watched)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# a renamed item keeps the metadata that was fetched for it
_CARRY_MEDIA = """
UPDATE media SET
    title=?, year=?, from_provider=?,
    imdb_id=?, plot=?, genres=?, rating=?, runtime=?, director=?, writer=?,
    cast_list=?, country=?, watched=?, last_watched=?, collection_id=?,
    poster_path=COALESCE(?, poster_path), poster2_path=COALESCE(?, poster2_path),
    true_story=?, true_story_article=?, my_rating=?, quotes=?
WHERE path=?
"""

_SELECT_EXISTING = """
SELECT id, path, title, year, root, video_path, imdb_id, plot, genres, rating,
       runtime, director, writer, cast_list, country, poster_path, poster2_path,
       release_date,
       watched, last_watched, collection_id, true_story, true_story_article,
       my_rating, quotes, from_provider, media_type, mtime
FROM media
"""

_SELECT_EPISODES = """
SELECT path, season, episode, watched, last_watched,
       name, released, summary, name_from_provider
FROM episodes WHERE series_id=?
"""


class Database:
    def __init__(self, path: Path) -> None:
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._path = path
        # One connection per thread. The scan worker, the bulk-fetch workers
        # and the UI thread all use the database at the same time; a shared
        # connection would interleave their transactions (and sqlite3
        # connections are not safe for concurrent use).
        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        conn = self._connection()
        conn.executescript(_SCHEMA)
        self._migrate()
        conn.executescript(_INDEXES)
        conn.commit()

    @property
    def conn(self) -> sqlite3.Connection:
        """The calling thread's connection, created on first use."""
        return self._connection()

    def _connection(self) -> sqlite3.Connection:
        """The calling thread's connection, created on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # the timeout comes first: the pragmas below need a lock too
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
            with self._conn_lock:
                self._all_conns.append(conn)
        return conn

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(media)")}
        for name, decl in (
            ("country", "TEXT"),
            ("watched", "INTEGER NOT NULL DEFAULT 0"),
            ("last_watched", "REAL"),
            ("added_at", "REAL"),
            ("collection_id", "INTEGER"),
            ("true_story", "INTEGER"),
            ("true_story_article", "TEXT"),
            ("my_rating", "INTEGER"),
            ("quotes", "TEXT"),
            ("story_cache", "TEXT"),
            ("from_provider", "INTEGER NOT NULL DEFAULT 0"),
            ("release_date", "TEXT"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE media ADD COLUMN {name} {decl}")
                if name == "added_at":
                    # an existing library gets its "added" date from the file
                    # timestamps, so "recently added" is useful straight away
                    self.conn.execute(
                        "UPDATE media SET added_at = mtime WHERE added_at IS NULL"
                    )
                if name == "from_provider":
                    # a library that was enriched before this column existed
                    # keeps the title of the provider
                    self.conn.execute(
                        "UPDATE media SET from_provider = 1 "
                        "WHERE imdb_id IS NOT NULL AND imdb_id != ''"
                    )
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(episodes)")}
        for name, decl in (
            ("watched", "INTEGER NOT NULL DEFAULT 0"),
            ("last_watched", "REAL"),
            ("released", "TEXT"),
            ("summary", "TEXT"),
            ("name_from_provider", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE episodes ADD COLUMN {name} {decl}")
        self._rebuild_episodes_table()
        if "name" not in {
            r["name"] for r in
            self.conn.execute("PRAGMA table_info(expected_episodes)")
        }:
            self.conn.execute("ALTER TABLE expected_episodes ADD COLUMN name TEXT")

    def _rebuild_episodes_table(self) -> None:
        """Drop the old UNIQUE(path) of the episodes table.

        A chained file ("S04E01E02.mkv") holds several episodes, so its
        rows share a path - the rows are keyed by season, episode and path
        together now.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='episodes'"
        ).fetchone()
        # the fresh schema has a unique *index* instead, not "UNIQUE" in the
        # column, so this only runs for the older tables
        if row is None or "UNIQUE" not in str(row["sql"]).upper():
            return
        with self.conn:
            self.conn.execute("ALTER TABLE episodes RENAME TO episodes_old")
            self.conn.execute("""
                CREATE TABLE episodes (
                    id INTEGER PRIMARY KEY,
                    series_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
                    season INTEGER,
                    episode INTEGER,
                    name TEXT,
                    released TEXT,
                    summary TEXT,
                    name_from_provider INTEGER NOT NULL DEFAULT 0,
                    path TEXT NOT NULL,
                    watched INTEGER NOT NULL DEFAULT 0,
                    last_watched REAL
                )
            """)
            self.conn.execute(
                "INSERT INTO episodes(id, series_id, season, episode, name, "
                "released, summary, name_from_provider, path, watched, last_watched) "
                "SELECT id, series_id, season, episode, name, released, summary, "
                "name_from_provider, path, watched, last_watched FROM episodes_old"
            )
            self.conn.execute("DROP TABLE episodes_old")

    def load_all(self) -> list[dict]:
        rows = self.conn.execute(_SELECT_MEDIA).fetchall()
        items = [dict(r) for r in rows]
        stats = self.episode_stats()
        for item in items:
            self._add_counts(item, stats.get(item["id"]))
        return items

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT media_type, COUNT(*) AS n FROM media GROUP BY media_type"
        ).fetchall()
        return {r["media_type"]: r["n"] for r in rows}

    def episode_stats(self, media_id: int | None = None) -> dict[int, tuple]:
        """series_id -> (watched, total, last watched)."""
        sql = ("SELECT series_id, COUNT(*) AS total, COALESCE(SUM(watched),0) AS seen, "
               "MAX(last_watched) AS last FROM episodes")
        if media_id is None:
            rows = self.conn.execute(sql + " GROUP BY series_id").fetchall()
        else:
            rows = self.conn.execute(
                sql + " WHERE series_id=? GROUP BY series_id", (media_id,)).fetchall()
        return {r["series_id"]: (r["seen"], r["total"], r["last"]) for r in rows}

    @staticmethod
    def _add_counts(item: dict, stats: tuple | None) -> None:
        """The episode counts are counted, not stored - add them to the item.

        Every item that comes out of the database gets them, so a refreshed
        item still knows how many of its episodes are watched.
        """
        if item.get("media_type") != "series":
            item["episodes_watched"] = 0
            item["episodes_total"] = 0
            item["seen"] = 1 if item.get("watched") else 0
            item["fully_watched"] = item["seen"]
            return
        seen, total, last = stats or (0, 0, None)
        item["episodes_watched"] = seen
        item["episodes_total"] = total
        item["seen"] = 1 if seen else 0
        item["fully_watched"] = 1 if (total and seen >= total) else 0
        item["last_watched"] = last or item.get("last_watched")

    def get(self, media_id: int) -> dict | None:
        row = self.conn.execute(
            _SELECT_MEDIA + " WHERE m.id=?", (media_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        self._add_counts(item, self.episode_stats(media_id).get(media_id))
        return item

    def get_many(self, media_ids: list[int]) -> dict[int, dict]:
        """Several items at once - two queries instead of two per item."""
        if not media_ids:
            return {}
        marks = ",".join("?" * len(media_ids))
        rows = self.conn.execute(
            _SELECT_MEDIA + f" WHERE m.id IN ({marks})", media_ids).fetchall()
        stats = {
            r["series_id"]: (r["seen"], r["total"], r["last"])
            for r in self.conn.execute(
                "SELECT series_id, COUNT(*) AS total, COALESCE(SUM(watched),0) AS seen, "
                f"MAX(last_watched) AS last FROM episodes WHERE series_id IN ({marks}) "
                "GROUP BY series_id",
                media_ids,
            )
        }
        out: dict[int, dict] = {}
        for row in rows:
            item = dict(row)
            self._add_counts(item, stats.get(item["id"]))
            out[item["id"]] = item
        return out

    # --- items that are taken out of the library ---------------------

    def ignored_paths(self) -> set[str]:
        """The folders that were taken out of the library."""
        return {r["path"] for r in self.conn.execute("SELECT path FROM ignored")}

    def is_ignored(self, path: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM ignored WHERE path=?", (path,)).fetchone())

    def ignore(self, media_id: int) -> bool:
        """Take an item out of the library.

        The files stay where they are - the app just does not show the item
        any more, and the next scan does not bring it back.
        """
        row = self.conn.execute(
            "SELECT path FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None:
            return False
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO ignored(path) VALUES (?)", (row["path"],))
            self.conn.execute("DELETE FROM media WHERE id=?", (media_id,))
        return True

    def unignore(self, path: str) -> None:
        """Put an item back into the library."""
        with self.conn:
            self.conn.execute("DELETE FROM ignored WHERE path=?", (path,))

    def ignored_items(self) -> list[dict]:
        """The items that were taken out of the library."""
        rows = self.conn.execute(
            "SELECT path FROM ignored ORDER BY path").fetchall()
        return [{"path": r["path"]} for r in rows]

    # --- duplicates ---------------------------------------------------

    def duplicate_groups(self) -> list[dict]:
        """The titles that more than one item carries."""
        rows = self.conn.execute(
            "SELECT id, path, title, year, media_type, poster_path, poster2_path, "
            "imdb_id, plot, genres, rating, runtime, director, writer, cast_list, "
            "country, watched FROM media ORDER BY title COLLATE NOCASE, path"
        ).fetchall()
        groups: dict[str, list[dict]] = {}
        for row in rows:
            groups.setdefault(str(row["title"] or "").lower(), []).append(dict(row))
        return [{"title": items[0].get("title") or "", "items": items}
                for items in groups.values() if len(items) > 1]

    # --- a new place on the disk --------------------------------------

    @staticmethod
    def _rebase(old: str, new: str, value: str | None) -> str | None:
        """The new path of something that lived in the old folder."""
        if not value:
            return value
        if value == old:
            return new
        if value.startswith(old + os.sep):
            return new + value[len(old):]
        return value

    def relink(self, media_id: int, new_path: str,
               new_video: str | None = None) -> bool:
        """Point an item at a new place on the disk.

        `new_path` is the new path of the item (the folder of a movie or a
        series, the file of a loose movie), `new_video` the video file
        inside it. Everything that lived in the old folder - the episodes,
        the artwork - moves along.
        """
        row = self.conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
        if row is None:
            return False
        old = row["path"]
        if new_path == old:
            return True
        if self.conn.execute(
                "SELECT 1 FROM media WHERE path=?", (new_path,)).fetchone():
            raise ValueError("that location is already in the library")

        video = new_video if new_video is not None else self._rebase(
            old, new_path, row["video_path"])
        poster = self._rebase(old, new_path, row["poster_path"])
        poster2 = self._rebase(old, new_path, row["poster2_path"])
        for art in (poster, poster2):
            if art and not Path(art).exists():
                art = None
        episodes = self.episodes(media_id)
        with self.conn:
            self.conn.execute(
                "UPDATE media SET path=?, video_path=?, poster_path=?, poster2_path=? "
                "WHERE id=?",
                (new_path, video, poster, poster2, media_id),
            )
            for episode in episodes:
                moved = self._rebase(old, new_path, episode["path"])
                if moved and moved != episode["path"]:
                    self.conn.execute(
                        "UPDATE episodes SET path=? WHERE path=?",
                        (moved, episode["path"]),
                    )
            # an item that was taken out stays out
            if self.is_ignored(old):
                self.conn.execute(
                    "UPDATE ignored SET path=? WHERE path=?", (new_path, old))
        return True

    # --- the database itself ------------------------------------------

    def file_size(self) -> int:
        """The size of the database file."""
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def integrity_check(self) -> str:
        """What SQLite's own check says about the database."""
        try:
            row = self.conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return f"the check failed: {exc}"
        return str(row[0]) if row else "unknown"

    def vacuum(self) -> int:
        """Compact the database file. Returns the size it had before."""
        before = self.file_size()
        self.conn.commit()
        self.conn.execute("VACUUM")
        return before

    def backup(self, target: Path) -> Path:
        """Write a copy of the library into another file."""
        self.conn.commit()
        dest = sqlite3.connect(str(target))
        try:
            self.conn.backup(dest)
        finally:
            dest.close()
        return Path(target)

    # --- folders (in-app categories) -----------------------------------

    def list_collections(self) -> list[dict]:
        """Every folder with the number of items in it."""
        rows = self.conn.execute(
            "SELECT c.id, c.name, COUNT(m.id) AS n FROM collections c "
            "LEFT JOIN media m ON m.collection_id = c.id "
            "GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def create_collection(self, name: str) -> int:
        """Create a folder and return its id."""
        name = name.strip()
        if not name:
            raise ValueError("a folder needs a name")
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO collections(name) VALUES (?)", (name,))
        row = self.conn.execute(
            "SELECT id FROM collections WHERE name=?", (name,)).fetchone()
        return row["id"]

    def delete_collection(self, collection_id: int) -> None:
        """Delete a folder; the items stay in the library."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET collection_id=NULL WHERE collection_id=?",
                (collection_id,))
            self.conn.execute(
                "DELETE FROM collections WHERE id=?", (collection_id,))

    def rename_collection(self, collection_id: int, name: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE collections SET name=? WHERE id=?",
                (name.strip(), collection_id))

    def set_collection(self, media_ids: list[int], collection_id: int | None) -> None:
        """File items into a folder; None takes them out of their folder."""
        if not media_ids:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE media SET collection_id=? WHERE id=?",
                [(collection_id, mid) for mid in media_ids],
            )

    def get_unfetched(self) -> list[dict]:
        """Return items missing metadata (no imdb_id and no plot)."""
        rows = self.conn.execute(
            "SELECT id, title, year, media_type, path, video_path, imdb_id, plot, "
            "genres, rating, runtime, director, writer, cast_list, country "
            "FROM media "
            "WHERE (imdb_id IS NULL OR imdb_id = '') "
            "  AND (plot IS NULL OR plot = '') "
            "ORDER BY media_type, title"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_items(self) -> list[dict]:
        """Return all items with their metadata status."""
        rows = self.conn.execute(
            "SELECT id, title, year, media_type, path, video_path, imdb_id, plot, "
            "genres, rating, runtime, director, writer, cast_list, country, "
            "poster_path, poster2_path, watched "
            "FROM media "
            "ORDER BY media_type, title"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_person_profile(self, name_key: str) -> dict | None:
        """Return a cached people profile, if one has been fetched."""
        row = self.conn.execute(
            "SELECT name_key, name, extract, image_path, url, not_found, fetched_at "
            "FROM person_profiles WHERE name_key=?",
            (name_key,),
        ).fetchone()
        return dict(row) if row else None

    def get_person_profiles(self) -> dict[str, dict]:
        """Return cached people profiles indexed by normalized name."""
        rows = self.conn.execute(
            "SELECT name_key, name, extract, image_path, url, not_found, fetched_at "
            "FROM person_profiles"
        ).fetchall()
        return {row["name_key"]: dict(row) for row in rows}

    def save_person_profile(self, profile: dict) -> None:
        """Persist a biography and its local image path for offline reading."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO person_profiles "
                "(name_key, name, extract, image_path, url, not_found, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name_key) DO UPDATE SET "
                "name=excluded.name, extract=excluded.extract, "
                "image_path=excluded.image_path, url=excluded.url, "
                "not_found=excluded.not_found, fetched_at=excluded.fetched_at",
                (
                    profile["name_key"], profile["name"], profile.get("extract"),
                    profile.get("image_path"), profile.get("url"),
                    int(bool(profile.get("not_found"))), time.time(),
                ),
            )

    def duplicates(self) -> list[dict]:
        """Return groups of items that share the same title (case-insensitive)."""
        rows = self.conn.execute(
            "SELECT LOWER(title) AS title, COUNT(*) AS n, "
            "GROUP_CONCAT(path, ' | ') AS paths "
            "FROM media GROUP BY LOWER(title) HAVING n > 1"
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_metadata(self, media_id: int) -> None:
        """Clear fetched metadata fields for an item."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET imdb_id=NULL, plot=NULL, genres=NULL, rating=NULL, "
                "runtime=NULL, director=NULL, writer=NULL, cast_list=NULL, country=NULL, "
                # the title belongs to the folder again
                "from_provider=0 "
                "WHERE id=?",
                (media_id,),
            )
            # the expected episode list came from the provider too
            self.conn.execute(
                "DELETE FROM expected_episodes WHERE series_id=?", (media_id,))

    def clear_poster(self, media_id: int, which: str) -> None:
        """Clear poster_path or poster2_path for an item."""
        col = "poster_path" if which == "poster" else "poster2_path"
        with self.conn:
            self.conn.execute(f"UPDATE media SET {col}=NULL WHERE id=?", (media_id,))

    def episodes(self, series_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, series_id, season, episode, name, released, summary, "
            "path, watched, last_watched "
            "FROM episodes WHERE series_id=? "
            "ORDER BY (season IS NULL) ASC, season, (episode IS NULL) ASC, episode",
            (series_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def rename_episode(self, old_path: str, new_path: str) -> None:
        """A renamed episode file is stored under its new path."""
        with self.conn:
            self.conn.execute(
                "UPDATE episodes SET path=? WHERE path=?", (new_path, old_path))

    def set_episode_watched(self, path: str, watched: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE episodes SET watched=?, last_watched=? WHERE path=?",
                (1 if watched else 0, time.time() if watched else None, path),
            )

    def set_episodes_watched(self, paths: list[str], watched: bool) -> None:
        """Mark a batch of episodes (e.g. a whole season) watched/unwatched."""
        if not paths:
            return
        stamp = time.time() if watched else None
        with self.conn:
            self.conn.executemany(
                "UPDATE episodes SET watched=?, last_watched=? WHERE path=?",
                [(1 if watched else 0, stamp, p) for p in paths],
            )

    def set_media_watched(self, media_id: int, watched: bool) -> None:
        """Mark a movie watched/unwatched."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET watched=?, last_watched=? WHERE id=?",
                (1 if watched else 0, time.time() if watched else None, media_id),
            )

    # --- based on a true story ----------------------------------------

    def set_true_story(self, media_id: int, value: bool | None,
                       article: str | None = None) -> None:
        """Flag an item as based on a true story (None = not known yet).

        `article` is the Wikipedia article the verdict came from.
        """
        with self.conn:
            self.conn.execute(
                "UPDATE media SET true_story=?, true_story_article=? WHERE id=?",
                (None if value is None else (1 if value else 0), article, media_id),
            )

    def true_story_items(self) -> list[dict]:
        """Every item whose story is based on a true story."""
        rows = self.conn.execute(
            "SELECT id, title, year, media_type, imdb_id, plot, genres, "
            "true_story, true_story_article, story_cache FROM media "
            "WHERE true_story=1 ORDER BY title COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_story_cache(self, media_id: int, story: dict) -> None:
        """Store the whole story of a film, so it can be read offline."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET story_cache=? WHERE id=?",
                (json.dumps(story, ensure_ascii=False), media_id))

    def story_cache(self, media_id: int) -> dict:
        """The cached story of a film (empty when there is none)."""
        row = self.conn.execute(
            "SELECT story_cache FROM media WHERE id=?", (media_id,)).fetchone()
        if not row or not row["story_cache"]:
            return {}
        try:
            data = json.loads(row["story_cache"])
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def unchecked_items(self) -> list[dict]:
        """The items that were never checked for a true story."""
        rows = self.conn.execute(
            "SELECT id, title, year, media_type FROM media "
            "WHERE true_story IS NULL ORDER BY media_type, title COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_true_story(self) -> None:
        """Forget every verdict, so the next scan checks everything again."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET true_story=NULL, true_story_article=NULL")

    # --- personal rating -----------------------------------------------

    def set_my_rating(self, media_id: int, rating: int | None) -> None:
        """Rate an item from 1 to 5 stars (None = no rating)."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET my_rating=? WHERE id=?",
                (None if rating is None else max(1, min(5, rating)), media_id))

    # --- quotes (Wikiquote) ---------------------------------------------

    def set_quotes(self, media_id: int, quotes: list[dict]) -> None:
        """Store the quotes of a film (an empty list means: none found)."""
        with self.conn:
            self.conn.execute(
                "UPDATE media SET quotes=? WHERE id=?",
                (json.dumps(quotes, ensure_ascii=False), media_id))

    def quote_items(self) -> list[dict]:
        """The films that have quotes."""
        rows = self.conn.execute(
            "SELECT id, title, year, media_type, quotes FROM media "
            "WHERE quotes IS NOT NULL AND quotes != '[]' "
            "ORDER BY title COLLATE NOCASE"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["quotes"] = json.loads(item["quotes"] or "[]")
            except ValueError:
                item["quotes"] = []
            out.append(item)
        return out

    def unchecked_quote_items(self) -> list[dict]:
        """The films whose quotes were never looked up."""
        rows = self.conn.execute(
            "SELECT id, title, year, media_type, true_story_article FROM media "
            "WHERE quotes IS NULL ORDER BY media_type, title COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def random_quote(self) -> tuple[dict, dict] | None:
        """A random quote of the library, with the film it is from."""
        items = self.quote_items()
        if not items:
            return None
        item = random.choice(items)
        if not item["quotes"]:
            return None
        return item, random.choice(item["quotes"])

    def reset_quotes(self) -> None:
        """Forget every quote, so the next run looks all of them up again."""
        with self.conn:
            self.conn.execute("UPDATE media SET quotes=NULL")

    def total_runtime(self) -> int | None:
        """The summed runtime of the items that know theirs, in minutes."""
        row = self.conn.execute(
            "SELECT SUM(runtime) AS t FROM media WHERE runtime IS NOT NULL"
        ).fetchone()
        return int(row["t"]) if row and row["t"] else None

    def rating_counts(self) -> dict[int, int]:
        """How many items carry each personal rating."""
        rows = self.conn.execute(
            "SELECT my_rating, COUNT(*) AS n FROM media "
            "WHERE my_rating IS NOT NULL GROUP BY my_rating"
        ).fetchall()
        return {int(r["my_rating"]): int(r["n"]) for r in rows}

    # --- surprise me ---------------------------------------------------

    def random_unwatched_movie(self) -> dict | None:
        """A random movie that was never watched."""
        row = self.conn.execute(
            _SELECT_MEDIA + " WHERE m.media_type='movie' "
            "AND COALESCE(m.watched,0)=0 ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # --- library health ------------------------------------------------

    def health_counts(self) -> dict[str, int]:
        """How many items have which problem."""
        counts = {
            "metadata": self._count(
                "WHERE (imdb_id IS NULL OR imdb_id='') "
                "AND (plot IS NULL OR plot='')"),
            "poster": self._count("WHERE poster_path IS NULL OR poster_path=''"),
            "watched": self._count("WHERE COALESCE(watched,0)=0"),
            "truestory": self._count("WHERE true_story IS NULL"),
            "gaps": self._count(
                "WHERE media_type='series' AND EXISTS ("
                "  SELECT 1 FROM expected_episodes x WHERE x.series_id=media.id"
                # a season higher than everything on the disk is not
                # "missing" - it is the next season, not downloaded yet
                "  AND x.season <= COALESCE(("
                "    SELECT MAX(ep.season) FROM episodes ep"
                "    WHERE ep.series_id=media.id), 0)"
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM episodes ep WHERE ep.series_id=x.series_id"
                "    AND ep.season=x.season AND ep.episode=x.episode))"),
            "future": self._count(
                "WHERE media_type='series' AND EXISTS ("
                "  SELECT 1 FROM expected_episodes x WHERE x.series_id=media.id"
                "  AND x.season > COALESCE(("
                "    SELECT MAX(ep.season) FROM episodes ep"
                "    WHERE ep.series_id=media.id), 0)"
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM episodes ep WHERE ep.series_id=x.series_id"
                "    AND ep.season=x.season AND ep.episode=x.episode))"),
        }
        rows = self.conn.execute(
            "SELECT LOWER(title) AS t, COUNT(*) AS n FROM media "
            "GROUP BY LOWER(title) HAVING n > 1"
        ).fetchall()
        counts["duplicates"] = sum(r["n"] - 1 for r in rows)
        return counts

    def health_items(self, issue: str) -> list[dict]:
        """The items behind one problem of the health report."""
        if issue == "duplicates":
            rows = self.conn.execute(
                "SELECT m.*, c.name AS collection_name FROM media m "
                "LEFT JOIN collections c ON c.id = m.collection_id "
                "WHERE LOWER(m.title) IN ("
                "  SELECT LOWER(title) FROM media GROUP BY LOWER(title) HAVING COUNT(*)>1"
                ") ORDER BY m.title COLLATE NOCASE, m.path"
            ).fetchall()
        elif issue == "gaps":
            rows = self.conn.execute(
                _SELECT_MEDIA + " WHERE m.media_type='series' AND EXISTS ("
                "  SELECT 1 FROM expected_episodes x WHERE x.series_id=m.id"
                "  AND x.season <= COALESCE(("
                "    SELECT MAX(ep.season) FROM episodes ep"
                "    WHERE ep.series_id=m.id), 0)"
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM episodes ep WHERE ep.series_id=x.series_id"
                "    AND ep.season=x.season AND ep.episode=x.episode))"
            ).fetchall()
            items = [dict(r) for r in rows]
            for item in items:
                item["detail"] = self._missing_episodes_detail(item["id"])
            return items
        elif issue == "future":
            rows = self.conn.execute(
                _SELECT_MEDIA + " WHERE m.media_type='series' AND EXISTS ("
                "  SELECT 1 FROM expected_episodes x WHERE x.series_id=m.id"
                "  AND x.season > COALESCE(("
                "    SELECT MAX(ep.season) FROM episodes ep"
                "    WHERE ep.series_id=m.id), 0)"
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM episodes ep WHERE ep.series_id=x.series_id"
                "    AND ep.season=x.season AND ep.episode=x.episode))"
            ).fetchall()
            items = [dict(r) for r in rows]
            for item in items:
                item["detail"] = self._upcoming_seasons_detail(item["id"])
            return items
        elif issue == "metadata":
            rows = self.conn.execute(
                _SELECT_MEDIA + " WHERE (m.imdb_id IS NULL OR m.imdb_id='') "
                "AND (m.plot IS NULL OR m.plot='') ORDER BY m.title COLLATE NOCASE"
            ).fetchall()
        elif issue == "poster":
            rows = self.conn.execute(
                _SELECT_MEDIA + " WHERE m.poster_path IS NULL OR m.poster_path='' "
                "ORDER BY m.title COLLATE NOCASE"
            ).fetchall()
        elif issue == "watched":
            rows = self.conn.execute(
                _SELECT_MEDIA + " WHERE COALESCE(m.watched,0)=0 "
                "ORDER BY m.title COLLATE NOCASE"
            ).fetchall()
        elif issue == "truestory":
            rows = self.conn.execute(
                _SELECT_MEDIA + " WHERE m.true_story IS NULL "
                "ORDER BY m.title COLLATE NOCASE"
            ).fetchall()
        else:
            rows = []
        return [dict(r) for r in rows]

    def library_size(self) -> int:
        """The size of the video files of the library, in bytes."""
        return self.disk_report()[0]

    def missing_items(self) -> list[dict]:
        """The items whose files are not on the disk any more."""
        return self.disk_report()[1]

    def disk_report(self) -> tuple[int, list[dict]]:
        """The size of the library and the items that are gone.

        One pass over the files answers both questions - the size and the
        existence of a file are the same stat.
        """
        sizes: dict[str, int] = {}
        rows = self.conn.execute(_SELECT_MEDIA).fetchall()
        for row in rows:
            target = row["video_path"] or row["path"]
            if not target or target in sizes:
                continue
            try:
                sizes[target] = Path(target).stat().st_size
            except OSError:
                sizes[target] = -1
        total = sum(size for size in sizes.values() if size > 0)
        missing = []
        for row in rows:
            item = dict(row)
            target = item.get("video_path") or item.get("path")
            if not target:
                continue
            if sizes.get(target, -1) < 0:
                missing.append(item)
        return total, missing

    def _count(self, where: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM media {where}").fetchone()
        return int(row["n"])

    def sync(
        self,
        items: list,
        configured_roots: set[str] | None = None,
        scanned_roots: set[str] | None = None,
        unreadable: set[str] | None = None,
    ) -> dict[str, int]:
        """Replace the indexed library with the scan result.

        Filesystem-derived fields are refreshed; already fetched metadata
        columns (imdb_id, plot, ...) and the watched flags are preserved.

        `configured_roots` are the roots the user has set up, `scanned_roots`
        the subset that was actually walked during the scan. Rows of a root
        that is no longer configured are dropped; rows of a configured but
        currently unreachable root are kept, so an unplugged drive never
        wipes the library.

        `unreadable` are the folders the scan could not read. Their rows are
        kept as well - a folder that cannot be listed right now must not
        throw its items away.

        A renamed item keeps the metadata that was fetched for it: the new
        path is matched against the old rows by video file name and by title.

        The return value holds the number of items per kind **and** the
        difference to the scan before: `counts["diff"]` says how many items
        are new, how many are gone and how many changed.
        """
        now = time.time()
        # the items that were taken out of the library stay out
        ignored = self.ignored_paths()
        if ignored:
            items = [i for i in items if i.path not in ignored]
        new_paths = {i.path for i in items}
        if scanned_roots is None:
            scanned_roots = {i.root for i in items}
        conn = self.conn
        with conn:
            existing = {
                r["path"]: dict(r) for r in conn.execute(_SELECT_EXISTING)
            }
            # the episode rows are read before the stale media rows are
            # deleted - deleting a series cascades its episodes away
            old_episodes: dict[int, list[dict]] = {}
            for r in conn.execute(
                "SELECT id, series_id, season, episode, path, watched, last_watched "
                "FROM episodes"
            ):
                old_episodes.setdefault(r["series_id"], []).append(dict(r))

            renamed = self._match_renames(items, existing)

            # the difference to the scan before - the status line shows it
            added = sum(1 for i in items if i.path not in existing)
            changed = sum(1 for i in items
                          if i.path in existing and self._changed(existing[i.path], i))

            stale = [
                (row["id"],)
                for row in existing.values()
                if row["path"] not in new_paths
                and not self._protected(row["path"], unreadable)
                and (
                    row["root"] in scanned_roots
                    or (configured_roots is not None and row["root"] not in configured_roots)
                )
            ]
            if stale:
                conn.executemany("DELETE FROM media WHERE id=?", stale)

            conn.executemany(
                _UPSERT_MEDIA,
                [
                    (i.path, i.title, i.media_type, i.year, i.root, i.video_path,
                     i.poster_path, i.poster2_path, i.mtime, now, i.mtime or now)
                    for i in items
                ],
            )

            # a renamed item inherits the metadata of the row it replaces
            carries = []
            for item in items:
                old = renamed.get(item.path)
                if old is None:
                    continue
                # an item that was enriched keeps the title and the year of
                # the provider - the folder name is only a fallback
                enriched = bool(old["from_provider"])
                carries.append((
                    old["title"] if enriched else item.title,
                    old["year"] if enriched else item.year,
                    old["from_provider"],
                    old["imdb_id"], old["plot"], old["genres"], old["rating"],
                    old["runtime"], old["director"], old["writer"],
                    old["cast_list"], old["country"], old["watched"],
                    old["last_watched"], old["collection_id"],
                    item.poster_path, item.poster2_path, old["true_story"],
                    old["true_story_article"], old["my_rating"],
                    old["quotes"], item.path,
                ))
            if carries:
                conn.executemany(_CARRY_MEDIA, carries)

            ids = {
                row["path"]: row["id"]
                for row in conn.execute("SELECT id, path FROM media WHERE media_type='series'")
            }
            for i in items:
                if i.media_type != "series":
                    continue
                sid = ids.get(i.path)
                if sid is None:
                    continue
                old = renamed.get(i.path)
                carry = old_episodes.get(old["id"], []) if old is not None else []
                self._sync_episodes(conn, sid, i.episodes, carry)
            # a folder that stopped being a series loses its episode rows
            conn.execute(
                "DELETE FROM episodes WHERE series_id IN "
                "(SELECT id FROM media WHERE media_type != 'series')"
            )
        counts = self.counts()
        counts["diff"] = {"new": added, "gone": len(stale), "changed": changed}
        return counts

    @staticmethod
    def _changed(old: dict, item) -> bool:
        """Did the files of an item change since the last scan?"""
        if old["media_type"] != item.media_type:
            return True
        if old["video_path"] != item.video_path:
            return True
        if old["poster_path"] != item.poster_path:
            return True
        if old["poster2_path"] != item.poster2_path:
            return True
        return (old["mtime"] or 0) != (item.mtime or 0)

    @staticmethod
    def _protected(path: str, folders: set[str] | None) -> bool:
        """Is the row inside a folder the scan could not read?

        Such a folder is not "empty" - it is unreadable. Its rows have to
        stay, or a locked drive would wipe the library.
        """
        if not folders:
            return False
        for folder in folders:
            base = folder.rstrip("\\/")
            if path == base or path.startswith(base + os.sep):
                return True
        return False

    @staticmethod
    def _match_renames(items: list, existing: dict) -> dict:
        """Match new items against the old rows they replace (a rename).

        A renamed folder keeps its video file, so the file name is the
        strongest signal; the title is the fallback for a series, which has
        no single video file.
        """
        by_video: dict[tuple, dict] = {}
        by_title: dict[tuple, dict] = {}
        for row in existing.values():
            video_path = row["video_path"]
            if video_path:
                by_video.setdefault(
                    (row["root"], Path(video_path).name.lower()), row)
            by_title.setdefault(
                (row["root"], str(row["title"] or "").strip().lower()), row)
        out: dict = {}
        for i in items:
            if i.path in existing:
                continue
            row = None
            if i.video_path:
                row = by_video.get((i.root, Path(i.video_path).name.lower()))
            if row is None:
                row = by_title.get(
                    (i.root, str(i.title or "").strip().lower()))
            if row is not None:
                out[i.path] = row
        return out

    @staticmethod
    def _sync_episodes(conn, sid: int, episodes: list, carry: list = ()) -> None:
        """Refresh a series' episodes without losing the watched flags.

        `carry` holds the episode rows of a series whose folder was renamed:
        they keep their watched flag even though the path changed.
        """
        old = [dict(r) for r in conn.execute(_SELECT_EPISODES, (sid,))]
        old.extend(carry)
        old_by_path = {r["path"]: r for r in old}
        new_paths = {e.path for e in episodes}

        # an episode whose file was renamed keeps its watched flag
        by_number: dict[tuple, dict] = {}
        by_name: dict[str, dict] = {}
        for r in old:
            if r["path"] in new_paths:
                continue
            if r["season"] is not None and r["episode"] is not None:
                by_number.setdefault((r["season"], r["episode"]), r)
            by_name.setdefault(Path(r["path"]).name.lower(), r)

        gone = [r["path"] for r in old if r["path"] not in new_paths]
        if gone:
            conn.executemany("DELETE FROM episodes WHERE path=?", [(p,) for p in gone])
        if not episodes:
            return
        # the rows without an episode number ("files the app could not
        # read") cannot be told apart by (season, episode, path) - a scan
        # always writes the complete set, so they are replaced wholesale
        upserts, inserts = [], []
        for e in episodes:
            watched, last_watched = 0, None
            released, summary, name_from_provider = None, None, 0
            if e.path not in old_by_path:
                match = None
                if e.season is not None and e.episode is not None:
                    match = by_number.get((e.season, e.episode))
                if match is None:
                    match = by_name.get(Path(e.path).name.lower())
                if match is not None:
                    watched = match["watched"] or 0
                    last_watched = match["last_watched"]
                    # a renamed episode file keeps the titles that were
                    # fetched for it
                    released = match["released"]
                    summary = match["summary"]
                    name_from_provider = match["name_from_provider"]
            elif e.episode is None:
                # without the upsert merge the watched flag has to be
                # carried over by hand
                old_row = old_by_path[e.path]
                watched = old_row["watched"] or 0
                last_watched = old_row["last_watched"]
            row = (
                (sid, e.season, e.episode, e.name, released, summary,
                 name_from_provider, e.path, watched, last_watched))
            (inserts if e.episode is None else upserts).append(row)
        if upserts:
            conn.executemany(_UPSERT_EPISODE, upserts)
        # the delete has to run before the fresh rows are written, and it
        # also clears the rows the old parser left behind
        conn.execute(
            "DELETE FROM episodes WHERE series_id=? AND episode IS NULL",
            (sid,))
        if inserts:
            conn.executemany(_INSERT_EPISODE, inserts)

    def save_metadata(self, media_id: int, data: dict) -> None:
        def enc(value) -> object:
            if isinstance(value, (list, tuple)):
                return json.dumps(list(value), ensure_ascii=False)
            return value

        title = data.get("title")
        params = (
            enc(title), data.get("year"), data.get("imdb_id"),
            data.get("plot"), enc(data.get("genres")), data.get("rating"),
            data.get("runtime"), data.get("director"), data.get("writer"),
            enc(data.get("cast")), data.get("country"),
            data.get("release_date"),
            enc(title), media_id,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE media SET "
                "title=COALESCE(?, title), year=COALESCE(?, year), "
                "imdb_id=COALESCE(?, imdb_id), plot=COALESCE(?, plot), "
                "genres=COALESCE(?, genres), rating=COALESCE(?, rating), "
                "runtime=COALESCE(?, runtime), director=COALESCE(?, director), "
                "writer=COALESCE(?, writer), cast_list=COALESCE(?, cast_list), "
                "country=COALESCE(?, country), "
                "release_date=COALESCE(?, release_date), "
                # a title that came from the provider survives a rescan
                "from_provider = CASE WHEN ? IS NOT NULL THEN 1 ELSE from_provider END "
                "WHERE id=?",
                params,
            )

    def set_episode_names(self, media_id: int,
                          seasons: dict[int, list[dict]]) -> None:
        """Fill in the episode titles, air dates and summaries a provider gave.

        Rows are matched by season and episode number, so a title can never
        land on the wrong file, even when a season folder is unsorted.
        """
        rows = []
        for season, eps in seasons.items():
            for ep in eps:
                name = str(ep.get("name") or "").strip()
                if not name:
                    continue
                released = str(ep.get("released") or "").strip() or None
                summary = str(ep.get("summary") or "").strip() or None
                rows.append((name, released, summary, 1, media_id,
                             season, ep["episode"]))
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE episodes SET name=?, released=?, summary=?, "
                "name_from_provider=? "
                "WHERE series_id=? AND season=? AND episode=?", rows)

    def set_expected_episodes(self, media_id: int,
                              seasons: dict[int, list[dict]]) -> None:
        """Store the episode numbers the provider says exist (season >= 1).

        The library-health report compares these against the episode files
        that are actually on the disk. Season 0 (specials) is skipped - the
        files almost never carry those.
        """
        pairs = []
        for season, eps in seasons.items():
            if not season:
                continue
            for ep in eps:
                number = ep.get("episode")
                if isinstance(number, int):
                    name = str(ep.get("name") or "").strip() or None
                    pairs.append((media_id, int(season), number, name))
        with self.conn:
            self.conn.execute(
                "DELETE FROM expected_episodes WHERE series_id=?", (media_id,))
            self.conn.executemany(
                "INSERT OR IGNORE INTO expected_episodes"
                "(series_id, season, episode, name) VALUES (?,?,?,?)", pairs)

    @staticmethod
    def _compress_runs(numbers: list[int]) -> str:
        """Missing episode numbers, readable: "1, 2, 5-8, 12"."""
        out, base, prev = [], None, None
        for n in sorted(numbers):
            if base is None:
                base = prev = n
            elif n == prev + 1:
                prev = n
            else:
                out.append(str(base) if base == prev else f"{base}-{prev}")
                base = prev = n
        if base is not None:
            out.append(str(base) if base == prev else f"{base}-{prev}")
        return ", ".join(out)

    def _missing_episodes(self, series_id: int) -> dict[int, list[tuple[int, str]]]:
        """The provider episodes the library has no file for, per season.

        Seasons higher than what is on the disk are left out - the next
        season of a series is not "missing", it simply has no folder yet
        (they are listed as "upcoming seasons" instead). Names come along
        when the provider already announced them.
        """
        have = {
            (r["season"], r["episode"])
            for r in self.conn.execute(
                "SELECT season, episode FROM episodes WHERE series_id=?",
                (series_id,),
            )
            if isinstance(r["season"], int) and isinstance(r["episode"], int)
        }
        last = max((s for s, _ep in have), default=0)
        out: dict[int, list[tuple[int, str]]] = {}
        for r in self.conn.execute(
            "SELECT season, episode, name FROM expected_episodes WHERE series_id=?",
            (series_id,),
        ):
            if r["season"] > last:
                continue
            key = (int(r["season"]), int(r["episode"]))
            if key not in have:
                out.setdefault(key[0], []).append((key[1], r["name"] or ""))
        return out

    def _upcoming_seasons(self, series_id: int) -> dict[int, list[int]]:
        """Expected seasons that sit higher than anything on the disk."""
        rows = self.conn.execute(
            "SELECT DISTINCT season FROM episodes WHERE series_id=? "
            "AND season IS NOT NULL",
            (series_id,),
        ).fetchall()
        last = max((r["season"] for r in rows), default=0)
        out: dict[int, list[int]] = {}
        for r in self.conn.execute(
            "SELECT season, episode FROM expected_episodes WHERE series_id=?",
            (series_id,),
        ):
            if r["season"] > last:
                out.setdefault(int(r["season"]), []).append(int(r["episode"]))
        return out

    def _missing_episodes_detail(self, media_id: int) -> str:
        """Where a series is missing episodes: "2 missing · S02: 5-7 · S03: 1".

        Episode names the provider announced come along for the first few
        missing ones, so the row tells what to download: "17 “The End”".
        """
        gaps = self._missing_episodes(media_id)
        if not gaps:
            return ""
        total = sum(len(missing) for missing in gaps.values())
        parts = []
        for season, missing in sorted(gaps.items()):
            runs = self._compress_runs([number for number, _name in missing])
            label = f"S{int(season):02d}: {runs}"
            named = [(n, name) for n, name in missing if name.strip()]
            if named:
                shown = named[:3]
                joiner = ", ".join(f"{int(n)} “{name.strip()}”" for n, name in shown)
                more = "…" if len(named) > len(shown) else ""
                label += f"  ({joiner}{more})"
            parts.append(label)
        count = f"{total} missing" if total != 1 else "1 missing"
        return count + " · " + " · ".join(parts)

    def _upcoming_seasons_detail(self, media_id: int) -> str:
        """The announced seasons that have no folder on the disk: "S07: 1-8"."""
        upcoming = self._upcoming_seasons(media_id)
        return " · ".join(
            f"S{int(s):02d}: {self._compress_runs(eps)}"
            for s, eps in sorted(upcoming.items()))

    def missing_episodes_detail(self, media_id: int) -> str:
        """The gaps of one series, read by the health re-check."""
        return self._missing_episodes_detail(media_id)

    def upcoming_seasons_detail(self, media_id: int) -> str:
        """The announced seasons without any file on the disk."""
        return self._upcoming_seasons_detail(media_id)

    def save_scanned_episodes(self, series_id: int, episodes: list) -> None:
        """Register the episode files a re-check found on the disk."""
        if not episodes:
            return
        with self.conn:
            self._sync_episodes(self.conn, series_id, episodes)

    def set_artwork_paths(self, media_id: int, poster: str | None, poster2: str | None) -> None:
        if not poster and not poster2:
            return
        with self.conn:
            if poster:
                self.conn.execute("UPDATE media SET poster_path=? WHERE id=?", (poster, media_id))
            if poster2:
                self.conn.execute("UPDATE media SET poster2_path=? WHERE id=?", (poster2, media_id))

    def close(self) -> None:
        with self._conn_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            try:
                conn.commit()
                conn.close()
            except sqlite3.Error:
                log.exception("error closing database")
        # the thread-local still points at a closed connection
        self._local.conn = None
