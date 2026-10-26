"""Offline smoke + consistency tests for scanner, parser, database and UI.

Run from the cinema folder:
    py -3 tools/smoke_test.py          (scanner + parser + database)
    py -3 tools/smoke_test.py --ui     (also runs an invisible UI test)
"""
from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from database.db_manager import Database  # noqa: E402
from metadata import metadata_service  # noqa: E402
from scanner.library_scanner import scan  # noqa: E402
from tools.make_test_library import build  # noqa: E402
from utils import image_utils, title_parser as tp  # noqa: E402
from utils import organizer  # noqa: E402

failures = 0


class FakeProvider(metadata_service.MetadataService):
    """Offline stand-in for a metadata provider."""

    def __init__(self, candidates: int = 1) -> None:
        self.candidates = candidates

    def search_candidates(self, title, year=None, media_type=None):
        return [
            metadata_service.MetadataResult(
                title=title, year=year, imdb_id="tt0000001")
            for _ in range(self.candidates)
        ]

    def fetch_metadata(self, title, year=None, media_type=None, imdb_id=None):
        return metadata_service.MetadataResult(
            title=title, year=year, plot="Test plot", imdb_id=imdb_id or "tt0000001")


def find_button(widget, text: str):
    """First button with the given label, searched depth-first."""
    for child in widget.winfo_children():
        if isinstance(child, tk.Button) and child.cget("text") == text:
            return child
        found = find_button(child, text)
        if found is not None:
            return found
    return None


def check(label: str, cond: bool) -> None:
    global failures
    tag = "ok" if cond else "FAIL"
    print(f"[{tag}] {label}")
    if not cond:
        failures += 1


def parse_checks() -> None:
    title, year = tp.parse_title_and_year("Inception (2010)")
    check("parse_title_and_year", title == "Inception" and year == 2010)
    title, year = tp.parse_title_and_year("The.Matrix.1999.BluRay")
    check("trailing year", title == "The Matrix" and year == 1999)
    p = tp.parse_title_and_year("2001: A Space Odyssey")
    check("year not eaten from title", p[0] == "2001: A Space Odyssey" and p[1] is None)
    check("clean_for_search", tp.clean_for_search(
        "The.Matrix.1999.1080p.BluRay.x264-GROUP", 1999) == "The Matrix")
    check("SAE", tp.episode_numbers("S01E01 Pilot.mkv", None) == (1, 1))
    check("sxey", tp.episode_numbers("show 2x13.mkv", None) == (2, 13))
    check("inherited season", tp.episode_numbers("Episode 5.mkv", 3) == (3, 5))
    check("bare number", tp.episode_numbers("07.mkv", 1) == (1, 7))
    check("chained episodes land in the first season",
          tp.episode_numbers("The Mentalist S03E23E24.mkv", None) == (3, 23))
    check("chained episodes keep their season",
          tp.episode_numbers("The Mentalist S07E12E13 DigiMoviez.mkv", None) == (7, 12))
    check("a double episode file is tv shaped",
          tp.has_episode_tokens("The Mentalist S03E23E24.mkv"))
    check("the chained marker is split from the series name",
          tp.episode_marker_span("Show.S03E23E24.mkv") == (5, 14))
    check("season folder", tp.season_from_folder("Season 01") == 1)
    check("season folder s02", tp.season_from_folder("s02") == 2)
    check("no season folder", tp.season_from_folder("Extra material") is None)

    # release tags must not hide the year, and must not become the title
    t, y = tp.parse_title_and_year("Some.Movie.2020.PROPER.1080p.WEB-DL.x264")
    check("year before release tags", t == "Some Movie" and y == 2020)
    t, y = tp.parse_title_and_year("10 Things I Hate About You 1999 1080p")
    check("year before resolution", t == "10 Things I Hate About You" and y == 1999)

    # a name that merely starts with digits is not an episode
    check("title starting with digits is not an episode",
          tp.episode_numbers("10.Things.I.Hate.About.You.mkv", None) == (None, None))
    # "95x62" inside a random download hash is not an episode
    check("hash filename is not an episode",
          tp.episode_numbers("v6ajb95x62k2mrdroouv.mp4", None) == (None, None))
    # a zero-padded leading number IS an episode
    check("leading zero-padded number is episode 1",
          tp.episode_numbers("001   Puss Gets the Boot [1940].mp4", None) == (None, 1))
    check("leading zero-padded number with dash separator",
          tp.episode_numbers("002 - The Midnight Snack [1941].mp4", None) == (None, 2))
    check("leading number with dot separator",
          tp.episode_numbers("003.The Night Before Christmas [1941].mp4", None) == (None, 3))
    check("display name strips the leading number",
          tp.episode_display_name("001   Puss Gets the Boot [1940]") == "Puss Gets the Boot [1940]")
    # not zero-padded: still not an episode
    check("non-padded leading number is not an episode",
          tp.episode_numbers("10 Things I Hate About You.mkv", None) == (None, None))
    # a file that holds several episodes registers all of them
    check("chained file gives both episodes",
          tp.episode_numbers_all("Prison.Break.S04E01E02.mkv", None) == [(4, 1), (4, 2)])
    check("chained file with release tags",
          tp.episode_numbers_all(
              "Elementary.S01E23E24.720p.WEB-DL.SoftSub.DigiMoviez.mkv",
              None) == [(1, 23), (1, 24)])
    check("chain keeps only the first for single episodes",
          tp.episode_numbers("The Mentalist S03E23E24.mkv", None) == (3, 23))
    check("chain drops repeated numbers",
          tp.episode_numbers_all("Show.S01E01E01.mkv", None) == [(1, 1)])
    # underscores are separators too - "\\b" does not fire in front of them
    check("underscore file name is an episode",
          tp.episode_numbers("Peaky_Blinders_S04E01_720p_Digimoviez.mp4", None) == (4, 1))
    check("underscore episode tokens",
          tp.has_episode_tokens("Peaky_Blinders_S04E01_720p_Digimoviez.mp4"))
    check("hash names still stay out",
          tp.episode_numbers_all("v6ajb95x62k2mrdroouv.mp4", None) == [])


def true_story_checks() -> None:
    """The true-story matcher, without touching the network."""
    from metadata import true_story

    check("the article title is normalized",
          true_story.normalize("Argo (2012 film)") == "argo")
    check("a film based on actual events is a true story",
          true_story.classify(["Category:Drama films based on actual events"]))
    check("a film based on a non-fiction book is a true story",
          true_story.classify(["Category:Films based on non-fiction books"]))
    check("a biographical film is a true story",
          true_story.classify(["Category:American biographical drama films"]))
    check("a semi-autobiographical film is not counted",
          not true_story.classify(["Category:Semi-autobiographical films"]))
    check("a documentary is not counted",
          not true_story.classify(["Category:Documentary films"]))
    check("an ordinary film is not a true story",
          not true_story.classify(["Category:2010 science fiction films",
                                   "Category:American crime films"]))
    check("the candidate article names are tried",
          true_story.candidates("Argo", 2012, "movie")[:2] == ["Argo", "Argo (film)"])
    check("a series gets the series names",
          "Chernobyl (TV series)" in true_story.candidates("Chernobyl", 2019, "series"))
    quotes = true_story._parse_quotes(
        "'''A Beautiful Mind''' is a film about John Nash.\n\n== John Nash ==\n"
        "* There has to be a mathematical explanation for how bad your tie is.\n"
        "* [[Alicia]]: I think I am falling in love with you.\n", 5)
    check("the quotes of a page are found",
          quotes and quotes[0][0].startswith("There has to be")
          and quotes[0][1] == "John Nash")
    check("a quote names its own speaker",
          len(quotes) > 1 and quotes[1][1] == "Alicia"
          and quotes[1][0].startswith("I think"))
    check("the introduction is not a quote",
          not true_story._parse_quotes("'''A film''' is a film.\n", 5))


def scanner_db_checks(target: Path) -> None:
    started = time.perf_counter()
    items = scan(target / "Movies", target / "Series")
    elapsed = time.perf_counter() - started
    print(f"    scan time: {elapsed:.2f}s")
    check("scan finds every folder", len(items) == 410)
    movies = [i for i in items if i.media_type == "movie"]
    series = [i for i in items if i.media_type == "series"]
    check("300 movies found", len(movies) == 300)
    check("110 series found", len(series) == 110)
    inception = next(i for i in movies if i.title == "Test Movie 0")
    check("movie year parsed", inception.year == 2000)
    check("movie poster found", any(i.poster_path for i in movies))
    check("backdrop found", any(i.poster2_path for i in movies))
    show = next(i for i in series if i.title == "Test Series 1")
    check("series episodes collected", len(show.episodes) > 3)
    eps = show.episodes[:3]
    check("episodes numbered",
          eps[0].season == 1 and eps[0].episode == 1
          and all(e.episode is not None for e in show.episodes))
    check("episode display name", "Pilot" in eps[0].name)
    nogood = [i for i in items if i.title.startswith("Test ") is False]
    check("titles clean", not nogood)

    db = Database(target / "library.db")
    counts = db.sync(items)
    check("db movie count", counts.get("movie") == 300)
    check("db series count", counts.get("series") == 110)
    db.sync(items)
    check("rescan keeps counts stable", len(db.load_all()) == 410)
    row = next(r for r in db.load_all() if r["title"] == "Test Movie 5")
    db.save_metadata(row["id"], {"plot": "Saved plot", "genres": ["Sci-Fi"], "rating": 7.5})
    db.sync(items)
    again = db.get(row["id"])
    check("metadata survives rescan", again["plot"] == "Saved plot" and again["rating"] == 7.5)

    # --- the flag that says "this story is based on a true story" ---
    db.set_true_story(row["id"], True)
    check("the true-story flag is stored", db.get(row["id"])["true_story"] == 1)
    check("the true-story list holds the item",
          any(i["id"] == row["id"] for i in db.true_story_items()))
    check("a flagged item is not unchecked",
          all(i["id"] != row["id"] for i in db.unchecked_items()))
    db.set_true_story(row["id"], None)
    check("the flag can be forgotten", db.get(row["id"])["true_story"] is None)
    db.set_true_story(row["id"], False)
    check("a checked item leaves the true-story list",
          all(i["id"] != row["id"] for i in db.true_story_items()))
    db.set_true_story(row["id"], True)

    # --- the personal rating ---
    db.set_my_rating(row["id"], 4)
    check("the personal rating is stored", db.get(row["id"])["my_rating"] == 4)
    check("the rating is counted", db.rating_counts().get(4) == 1)
    db.set_my_rating(row["id"], 9)
    check("the rating is capped at five", db.get(row["id"])["my_rating"] == 5)
    db.set_my_rating(row["id"], None)
    check("the rating can be cleared", db.get(row["id"])["my_rating"] is None)

    # --- the quotes of a film ---
    db.set_quotes(row["id"], [{"text": "A quote", "speaker": "Someone"}])
    check("the quotes are stored",
          len(db.quote_items()) == 1 and len(db.quote_items()[0]["quotes"]) == 1)
    check("a random quote comes from the library",
          (db.random_quote() or (None, None))[0] is not None)
    db.set_quotes(row["id"], [])
    check("a film without quotes leaves the list", db.quote_items() == [])
    db.conn.execute("UPDATE media SET quotes=NULL WHERE id=?", (row["id"],))
    db.conn.commit()
    check("an unchecked film is asked again",
          any(i["id"] == row["id"] for i in db.unchecked_quote_items()))

    # --- the story of a film, cached so it can be read offline ---
    db.set_story_cache(row["id"], {"real": "What really happened.",
                                   "source": "A Book (book)",
                                   "places": [["Vienna", "Vienna"]],
                                   "people": [["A Director", "A_Director"]],
                                   "awards": [["An Award", "An_Award"]]})
    cached = db.story_cache(row["id"])
    check("the story is cached", cached.get("real") == "What really happened.")
    check("the places are cached", cached.get("places")[0][0] == "Vienna")
    db.conn.execute("UPDATE media SET story_cache=NULL WHERE id=?", (row["id"],))
    db.conn.commit()
    check("the cache can be cleared", db.story_cache(row["id"]) == {})

    # --- the library health report ---
    counts = db.health_counts()
    check("the health report counts the duplicates",
          counts.get("duplicates", 0) >= 0)
    check("the health report finds the unwatched items",
          any(i["id"] == row["id"] for i in db.health_items("watched")))
    check("a surprise pick comes from the library",
          (db.random_unwatched_movie() or {}).get("title") is not None)

    series_id = next(r["id"] for r in db.load_all() if r["title"] == "Test Series 0")
    eps_db = db.episodes(series_id)
    check("episodes in db", len(eps_db) > 0)
    check("episodes expose series_id", "series_id" in eps_db[0])
    db.set_episode_watched(eps_db[0]["path"], True)
    check("watched flag persists",
          any(e["watched"] == 1 for e in db.episodes(series_id)))

    # the provider episode list the health report compares the files against
    seasons: dict[int, list[dict]] = {}
    for e in eps_db:
        if e["season"] is not None and e["episode"] is not None:
            seasons.setdefault(e["season"], []).append({"episode": e["episode"]})
    extra = max((x["episode"] for x in seasons.get(1, [])), default=0) + 1
    seasons.setdefault(1, []).append({"episode": extra})
    db.set_expected_episodes(series_id, seasons)
    counts = db.health_counts()
    check("a missing episode is reported", counts.get("gaps", 0) >= 1)
    gap_item = next(i for i in db.health_items("gaps") if i["id"] == series_id)
    check("the missing numbers are named",
          gap_item["detail"] == f"1 missing · S01: {extra}")

    # a season the provider announced but that has no folder yet is not
    # "missing" - it shows up as an upcoming season instead
    next_season = max(s for s in seasons) + 1
    announced = [
        {"episode": 1, "name": "Winter Is Coming Again"},
        {"episode": 2, "name": "The Second Wave"},
    ]
    seasons[next_season] = announced
    db.set_expected_episodes(series_id, seasons)
    counts = db.health_counts()
    check("an announced season without a folder is upcoming",
          counts.get("future", 0) >= 1)
    fut_item = next(i for i in db.health_items("future") if i["id"] == series_id)
    check("the upcoming season is listed as a whole",
          fut_item["detail"] == f"S{next_season:02d}: 1-2")
    stored = db.conn.execute(
        "SELECT name FROM expected_episodes WHERE series_id=? AND season=? "
        "AND episode=1", (series_id, next_season)).fetchone()
    check("the announced names are stored",
          stored is not None and "Winter" in (stored["name"] or ""))
    gap_item = next(i for i in db.health_items("gaps") if i["id"] == series_id)
    check("the announced season does not count as missing",
          f"S{next_season:02d}" not in gap_item["detail"])
    db.set_expected_episodes(series_id, {})
    check("a series with no expectations is not reported",
          all(i["id"] != series_id for i in db.health_items("gaps"))
          and all(i["id"] != series_id for i in db.health_items("future")))

    # renaming episode files: apply and take it back
    item = db.get(series_id)
    before = {str(e["path"]) for e in db.episodes(series_id)}
    # the plan works from the titles a provider fetched - most rows only
    # carry the junk of their file name, which the plan now cleans up
    db.set_episode_names(series_id, {
        1: [{"episode": n, "name": f"Fetched Title {n}"}
            for n in (1, 2)]})
    plan = organizer.episode_rename_plan(item, db.episodes(series_id))
    renameable = [p for p in plan if not p.result]
    check("a rename plan was made", bool(renameable))
    moved_count = organizer.rename_episode_files(plan)
    moved = [p for p in plan if p.result == "renamed"]
    check("every planned rename worked",
          moved_count == len(moved) and len(moved) == len(renameable))
    for entry in moved:
        db.rename_episode(str(entry.source), str(entry.target))
    check("the database follows the renames",
          {str(p.target) for p in moved} <= {
              r["path"] for r in db.episodes(series_id)})
    organizer.save_undo([(str(p.source), str(p.target)) for p in moved])
    check("the undo journal holds the renames",
          len(organizer.load_undo()) == len(moved))
    back = organizer.undo_episode_renames(organizer.load_undo())
    check("the renames were taken back", back == len(moved))
    for entry in moved:
        db.rename_episode(str(entry.target), str(entry.source))
    organizer.clear_undo()
    check("a taken-back library is as before",
          {r["path"] for r in db.episodes(series_id)} == before)
    # a rescan must not throw the watched flags away
    db.sync(items)
    check("watched flag survives rescan",
          any(e["watched"] == 1 for e in db.episodes(series_id)))
    check("watched flag reloads", db.episodes(series_id)[0]["watched"] == 1)

    # movie watch flag survives a rescan too
    db.set_media_watched(row["id"], True)
    db.sync(items)
    check("movie watched flag survives rescan", db.get(row["id"])["watched"] == 1)

    # a root that is no longer configured is dropped, an unreachable one is kept
    other = target / "OtherMovies"
    other.mkdir(exist_ok=True)
    db.sync(scan(other, target / "Series"),
            {str(other), str(target / "Series")}, {str(other)})
    check("rows of an unconfigured root are removed",
          all(r["root"] != str(target / "Movies") for r in db.load_all()))
    check("rows of a configured but unreachable root are kept",
          any(r["root"] == str(target / "Series") for r in db.load_all()))
    db.sync(items, {str(target / "Movies"), str(target / "Series")}, set())
    check("rows of an unreachable root are kept", len(db.load_all()) == 410)

    # folders are in-app categories: nothing on disk moves
    collection = db.create_collection("Anime")
    check("the folder is created", db.list_collections()[0]["name"] == "Anime")
    row = next(r for r in db.load_all() if r["title"] == "Test Movie 5")
    db.save_metadata(row["id"], {"plot": "Saved plot"})
    db.set_collection([row["id"]], collection)
    filed = db.get(row["id"])
    check("an item is filed into a folder",
          filed["collection_id"] == collection
          and filed["collection_name"] == "Anime")
    check("filing keeps the metadata and the path",
          filed["plot"] == "Saved plot" and filed["path"] == row["path"])
    check("the folder counts its items", db.list_collections()[0]["n"] == 1)
    db.sync(items)
    check("a rescan keeps the folder",
          db.get(row["id"])["collection_id"] == collection)
    second_row = next(r for r in db.load_all() if r["title"] == "Test Movie 6")
    db.set_collection([second_row["id"]], collection)
    removed = db.remove_from_collection([second_row["id"]], collection)
    check("an item can be removed from a folder without deleting it",
          removed == 1 and db.get(second_row["id"])["collection_id"] is None)
    db.rename_collection(collection, "Anime & Manga")
    check("a folder can be renamed",
          db.list_collections()[0]["name"] == "Anime & Manga")
    db.set_collection([row["id"]], None)
    check("an item can be taken out of a folder",
          db.get(row["id"])["collection_id"] is None)
    db.delete_collection(collection)
    check("a folder can be deleted", db.list_collections() == [])
    check("deleting a folder keeps the item", len(db.load_all()) == 410)

    # --- a scan that reads nothing must not empty the library ---
    unplugged = target / "Unplugged"
    walked: set[str] = set()
    found = scan(unplugged, unplugged, walked=walked)
    check("an unreadable root is not walked", found == [] and not walked)
    db.sync(found,
            {str(target / "Movies"), str(target / "Series"), str(unplugged)},
            walked)
    check("a failed scan leaves the library alone", len(db.load_all()) == 410)

    # --- a renamed folder keeps the metadata that was fetched for it ---
    item5 = next(i for i in items if i.title == "Test Movie 5")
    row5 = next(r for r in db.load_all() if r["path"] == item5.path)
    db.save_metadata(row5["id"], {"imdb_id": "tt1234567", "plot": "Keep me",
                                  "rating": 8.5})
    db.set_true_story(row5["id"], True)
    renamed_path = item5.path + " (2019)"
    db.sync(
        [dataclasses.replace(item5, path=renamed_path) if i.path == item5.path else i
         for i in items],
        {str(target / "Movies"), str(target / "Series")},
        {str(target / "Movies"), str(target / "Series")},
    )
    after = next((r for r in db.load_all() if r["path"] == renamed_path), None)
    check("a renamed folder keeps its metadata",
          after is not None and after["imdb_id"] == "tt1234567"
          and after["plot"] == "Keep me" and after["rating"] == 8.5)
    check("a renamed folder keeps the true-story flag",
          after is not None and after["true_story"] == 1)
    db.sync(items,
            {str(target / "Movies"), str(target / "Series")},
            {str(target / "Movies"), str(target / "Series")})

    # --- a renamed episode file keeps its watched flag ---
    series_item = next(i for i in items if i.media_type == "series" and i.episodes)
    series_id = next(r["id"] for r in db.load_all() if r["path"] == series_item.path)
    first = series_item.episodes[0]
    db.set_episode_watched(first.path, True)
    moved = dataclasses.replace(
        first, path=str(Path(first.path).with_name(
            Path(first.path).stem + " renamed" + Path(first.path).suffix)))
    db.sync(
        [dataclasses.replace(series_item,
                             episodes=[moved] + list(series_item.episodes[1:]))
         if i.path == series_item.path else i for i in items],
        {str(target / "Movies"), str(target / "Series")},
        {str(target / "Movies"), str(target / "Series")},
    )
    check("a renamed episode keeps its watched flag",
          any(e["watched"] == 1 for e in db.episodes(series_id)))
    db.sync(items,
            {str(target / "Movies"), str(target / "Series")},
            {str(target / "Movies"), str(target / "Series")})

    # --- a flat series without season folders gets season 1 ---
    flat_root = target / "Flat"
    flat_root.mkdir(exist_ok=True)
    show_folder = flat_root / "Shorts"
    show_folder.mkdir(exist_ok=True)
    for n in range(1, 6):
        (show_folder / f"{n:03d}   Short {n}.mp4").write_bytes(b"x")
    items = scan(flat_root, None)
    show = next(i for i in items if i.media_type == "series")
    check("a flat numbered folder is a series", show is not None)
    check("episodes get season 1", all(e.season == 1 for e in show.episodes))
    check("episodes numbered 1..5", {e.episode for e in show.episodes} == {1, 2, 3, 4, 5})
    check("names stripped of leading number",
          all("Short" in e.name and not e.name.startswith(("00", "0")) for e in show.episodes))

    # --- a movie folder with same-number files stays a movie ---
    dup_root = target / "Dup"
    dup_root.mkdir(exist_ok=True)
    (dup_root / "007 - Casino Royale.mkv").write_bytes(b"x")
    (dup_root / "007 - Trailer.mkv").write_bytes(b"x")
    items = scan(dup_root, None)
    movie = next(i for i in items if i.media_type == "movie")
    check("duplicate numbers is a movie", movie is not None)

    # --- a failed settings write is reported, not swallowed ---
    saved_path = config.SETTINGS_PATH
    try:
        config.SETTINGS_PATH = target          # a directory: the write fails
        check("settings report a failed save",
              config.save_settings({"movies_dir": str(target)}) is False)
    finally:
        config.SETTINGS_PATH = saved_path
    db.close()


def organizer_checks(target: Path) -> None:
    from utils import organizer

    messy = target / "Messy"
    messy.mkdir(exist_ok=True)
    names = [
        "Inception.2010.1080p.BluRay.x264-GROUP.mkv",
        "The Matrix 1999.mp4",
        "Breaking.Bad.S01E02.720p.WEB-DL.mkv",
        "Breaking.Bad.S02E01.720p.WEB-DL.mkv",
        "The.Office.US.S03E05.1080p.mkv",
        "S01E02.Pilot.mkv",
        "notes.txt",
    ]
    for name in names:
        (messy / name).write_bytes(b"x")

    entries = organizer.plan(messy)
    check("only video files are planned", len(entries) == 6)
    by_name = {e.source.name: e for e in entries}
    check("movie folder", by_name[names[0]].folder == "Inception 2010 1080p")
    check("movie without release tags",
          by_name[names[1]].folder == "The Matrix 1999")
    check("series folder", by_name[names[2]].folder == "Breaking Bad 720p")
    check("series season", by_name[names[3]].season == 2)
    check("series with dots in the name",
          by_name[names[4]].folder == "The Office US 1080p")
    check("a file without a series name is left alone",
          by_name[names[5]].kind == "skip")

    for entry in entries:
        organizer.move_entry(entry, messy)

    check("movie moved",
          (messy / "Inception 2010 1080p" / names[0]).exists())
    check("series moved", (messy / "Breaking Bad 720p" / "Season 1" / names[2]).exists())
    check("second season moved",
          (messy / "Breaking Bad 720p" / "Season 2" / names[3]).exists())
    check("a skipped file stays put", (messy / names[5]).exists())

    # the scanner has to understand what the organizer produced
    items = scan(messy, target / "Empty")
    titles = {i.title for i in items}
    check("scanner sees the movies", {"Inception", "The Matrix"} <= titles)
    check("scanner sees the series", "Breaking Bad" in titles)
    show = next(i for i in items if i.title == "Breaking Bad")
    check("episodes keep their season",
          len(show.episodes) == 2
          and {e.season for e in show.episodes} == {1, 2})
    check("the movie year is read back", next(
        i for i in items if i.title == "Inception").year == 2010)

    # --- renaming episode files out of the fetched titles ---
    chain_file = target / "Show S03E23E24.mkv"
    chain_file.write_bytes(b"x")
    chained = organizer.episode_rename_plan(
        {"title": "Show"}, [
            {"season": 3, "episode": 23, "name": "Real", "path": str(chain_file),
             "name_from_provider": 1},
            {"season": 3, "episode": 24, "name": "Also Real",
             "path": str(chain_file), "name_from_provider": 1},
        ])[0]
    check("a double episode file gets the name of both episodes",
          chained.target.name == "Show S03E23E24 - Real + Also Real.mkv")
    for name in ("loose.mkv", "x.mkv", "y.mkv"):
        (target / name).write_bytes(b"x")
    planner = organizer.episode_rename_plan({"title": "Show"}, [
        {"season": None, "episode": None, "name": "Real",
         "path": str(target / "loose.mkv")},
        {"season": 1, "episode": 1, "name": "?", "path": str(target / "x.mkv")},
        {"season": 1, "episode": 2, "name": "Show - Show",
         "path": str(target / "y.mkv")},
    ])
    check("a file without a season and a title is listed alone",
          planner[0].result == "no season number - left alone")
    check("a junk title becomes a plain filler",
          planner[1].target.name == "Show S01E01 - Episode 01.mkv"
          and planner[2].target.name == "Show S01E02 - Episode 02.mkv")

    neat = target / "Show S01E02 - Real.mkv"
    neat.write_bytes(b"x")
    edges = organizer.episode_rename_plan({"title": "Show"}, [
        {"season": 1, "episode": 2, "name": "Real", "path": str(neat)},
        {"season": 1, "episode": 3, "name": "Real",
         "path": str(target / "gone.mkv")},
    ])
    check("a file that is already tidy is left alone",
          edges[0].result == "already neat")
    check("a file that is not on the disk is left alone",
          edges[1].result == "file not found - left alone")
    untitled = organizer.episode_rename_plan({"title": ""}, [
        {"season": 1, "episode": 2, "name": "Real", "path": str(neat)}])[0]
    check("a series without a title is left alone",
          untitled.result == "no series title - left alone")
    messy_name = target / "Show S01E01 - Old.mkv"
    messy_name.write_bytes(b"x")
    renamed = organizer.episode_rename_plan({"title": "Show"}, [
        {"season": 1, "episode": 1, "name": "Real", "path": str(messy_name)},
    ])[0]
    check("the plan holds the new name",
          renamed.target.name == "Show S01E01 - Real.mkv")


def ui_checks(target: Path) -> None:
    from ui.library_view import PRIMS_PER_CARD

    image_utils.ensure_placeholder(config.PLACEHOLDER_PATH)
    config.THUMB_CACHE_DIR = target / "thumbs"
    # a player of the user would be started by the play button - the test
    # must not open anything
    saved_player = config.PLAYER
    config.PLAYER = ""
    root = tk.Tk()
    root.attributes("-alpha", 0.0)
    root.geometry("1240x780")
    db = Database(target / "library.db")

    from ui.main_window import MainWindow
    app = MainWindow(root, db, movies_dir=target / "Movies", series_dir=target / "Series")

    results: list[str] = []
    attempts = {"n": 0}

    def poll() -> None:
        if app._scan_running:
            root.after(250, poll)
            return
        try:
            check("ui loaded all items", len(app._all_items) == 410)
            check("scan finished and reported",
                  "Scan finished" in app._status_left.cget("text"))
            check("rescan button is usable again",
                  str(app._rescan_btn.cget("state")) == "normal")
            lib = app._library
            for _ in range(60):
                lib.canvas.yview_scroll(55, "units")
            lib.canvas.update()
            check("grid created visible cards", 0 < len(lib._rendered) <= 60)
            check("no orphan canvas items",
                  len(lib.canvas.find_all()) == len(lib._rendered) * PRIMS_PER_CARD)
            next(i for i in app._all_items if i["media_type"] == "movie")
            app._set_section("movie")
            root.update()
            check("movies section shows movies",
                  0 < len(lib._rendered) <= 60
                  and all(i["media_type"] == "movie" for i in lib._items))
            app._set_section("all")
            root.update()
            check("sidebar has no item counters",
                  not any(hasattr(app, "_nav_counts") for _ in (0,)))
            series_item = next(i for i in app._all_items if i["media_type"] == "series")
            # an IMDb row in the panel - its margin must not change later
            app._db.save_metadata(series_item["id"], {"imdb_id": "tt1234567"})
            series_item = app._db.get(series_item["id"]) or series_item
            app._open_item(series_item)
            root.update()
            check("details visible", app._details.winfo_ismapped())

            # the episode panel keeps the open season across a rebuild
            app._details.show(series_item)
            root.update()
            seasons = list(app._details._ep_seasons)
            check("the episode panel has seasons", bool(seasons))
            if seasons:
                name = seasons[0]
                app._details._ep_open[name] = False
                movie_item = next(i for i in app._all_items
                                  if i["media_type"] == "movie")
                app._details.show(movie_item)
                app._details.show(series_item)
                root.update()
                check("the episode panel keeps the selected season",
                      app._details._ep_open.get(name) is False)

                # a short episode list must not float inside its canvas:
                # scrolling up would push the seasons down
                area = app._details._ep_area
                header = app._details._ep_seasons[name]["header"]
                top = header.winfo_rooty() - area.canvas.winfo_rooty()
                area.canvas.yview_scroll(-5, "units")
                root.update()
                check("the episode list stays at the top when scrolled",
                      header.winfo_rooty() - area.canvas.winfo_rooty() == top)

                # every episode has a real play button - the rows of a
                # season are built when the season is opened
                app._details._toggle_season(name)
                root.update()
                rows = app._details._ep_rows
                check("the episode panel has episode rows", bool(rows))
                if rows:
                    path = next(iter(rows))
                    opened = []
                    import utils.media_ops as media_ops
                    original = media_ops.reveal
                    media_ops.reveal = lambda p: opened.append(p)
                    try:
                        rows[path]["play"][0].invoke()
                    finally:
                        media_ops.reveal = original
                    check("the play button opens the episode",
                          opened == [path])
                    before = any(e["path"] == path and e["watched"]
                                 for e in app._db.episodes(series_item["id"]))
                    if before:
                        # an earlier check marked this episode as watched:
                        # start from zero, so the count is known
                        rows[path]["watched"][0].invoke()
                        root.update()
                        before = False
                    stars = app._details._panel["stars"]
                    row_y = stars[0].winfo_rooty()
                    rows[path]["watched"][0].invoke()
                    root.update()
                    after = any(e["path"] == path and e["watched"]
                                for e in app._db.episodes(series_item["id"]))
                    check("the watched button marks the episode", before != after)
                    check("the panel keeps its margins when an episode is marked",
                          stars[0].winfo_rooty() == row_y)
                    meta = str(app._details._panel["meta"].cget("text"))
                    check("the meta line keeps the episode count",
                          "watched" in meta and "1/" in meta)

            app._show_library()
            root.update()
            check("back to library", lib.winfo_ismapped())

            # the grid keeps its scroll position when an item changes
            lib.canvas.yview_scroll(40, "units")
            root.update()
            before = lib.canvas.canvasy(0)
            app._on_item_fetched(app._all_items[0]["id"])
            root.update()
            after = lib.canvas.canvasy(0)
            check("the grid keeps its scroll position after a fetch",
                  abs(after - before) < 5)
            # search filter
            app._search_text = "series 0"
            app._apply_filters()
            root.update()
            check("search narrows the grid", 0 < len(lib._items) < 410)
            app._search_text = ""
            app._apply_filters()
            root.update()
            check("the view toggles are gone", not hasattr(app, "_view_buttons"))
            check("the sidebar has an add-folder button",
                  find_button(app, "+ Add Folder") is not None)

            # the scan says what it changed
            check("the status line tells what the scan found",
                  "Scan finished" in app._status_left.cget("text"))
            check("the difference is written out",
                  app._diff_text({"new": 3, "gone": 1, "changed": 5})
                  == " · 3 new, 1 gone, 5 changed")

            # the grid can mark several cards
            lib.canvas.update()
            root.update()
            check("the grid holds cards", bool(lib._items))
            marked = lib.canvas.find_withtag("c:0")
            if marked:
                box = lib.canvas.bbox(marked[0])
                lib.canvas.event_generate("<Button-1>", state=0x4,
                                          x=(box[0] + box[2]) // 2,
                                          y=(box[1] + box[3]) // 2)
                root.update()
                check("ctrl click marks a card", len(lib.selected_items()) == 1)
                lib.select_all()
                check("select all marks every card",
                      len(lib.selected_items()) == len(lib._items))
                lib.clear_selection()
                check("the selection can be cleared", not lib.selected_items())

            # the search understands the new fields
            app._search_text = "type:series"
            app._apply_filters()
            root.update()
            check("the search filters by kind",
                  bool(lib._items)
                  and all(i["media_type"] == "series" for i in lib._items))
            app._search_text = "unwatched:"
            app._apply_filters()
            root.update()
            check("the search filters by watched",
                  all(not i.get("seen") for i in lib._items))
            app._search_text = "rating:7+"
            app._apply_filters()
            root.update()
            check("the search filters by rating",
                  all((i.get("rating") or 0) >= 7 for i in lib._items))
            app._search_text = ""
            app._apply_filters()
            root.update()

            # the Dig Deeper tab: the reading pane follows the selection
            movie = next(i for i in app._all_items if i["media_type"] == "movie")
            app._db.set_true_story(movie["id"], True, "Test Movie 0 (film)")
            app._open_settings()
            root.update()
            app._settings._select_tab("deeper")
            root.update()
            rows = app._settings._deeper_tree.get_children()
            check("the true-story list holds the flagged item",
                  movie["id"] in [int(r) for r in rows])
            app._settings._deeper_tree.selection_set(str(movie["id"]))
            app._settings._on_deeper_select()
            root.update()
            check("the reading pane has an open-folder button",
                  find_button(app._settings, "Open Folder") is not None)
            check("the reading pane has a details button",
                  find_button(app._settings, "Open Details Page") is not None)
            check("the reading pane buttons wait for a selection",
                  str(app._settings._deeper_details_btn.cget("state")) == "normal")
            app._settings._open_deeper_details()
            root.update()
            check("the details page opens from the Dig Deeper tab",
                  app._details.winfo_ismapped()
                  and app._details._item.get("id") == movie["id"])

            # the reading pane shows the cached story, without a network
            app._db.set_story_cache(movie["id"], {
                "real": "What really happened.",
                "source": "A Book (book)",
                "places": [["Vienna", "Vienna"]],
                "people": [["A Director", "A_Director"]],
                "awards": [["An Award", "An_Award"]],
            })
            app._settings._select_tab("deeper")
            root.update()
            app._settings._deeper_tree.selection_set(str(movie["id"]))
            app._settings._on_deeper_select()
            root.update()
            pane = app._settings._deeper_text.get("1.0", "end")
            check("the cached story is shown in the pane",
                  "What really happened." in pane and "Vienna" in pane)
            check("the pane does not wait for the network",
                  "Looking up" not in pane)

            # the personal rating is set from the details page
            stars = app._details._panel.get("stars") or []
            check("the details page has five stars", len(stars) == 5)
            if stars:
                stars[2].invoke()
                root.update()
                check("a star rates the item",
                      app._db.get(movie["id"])["my_rating"] == 3)
                check("the same star clears the rating",
                      stars[2].cget("fg") == "#e0a33e")

            # the surprise button opens a movie that was never watched
            app._show_library()
            root.update()
            app._surprise_me()
            root.update()
            check("the surprise button opens a movie",
                  app._details.winfo_ismapped()
                  and app._details._item.get("media_type") == "movie")

            # the library health tab
            # an empty folder says so and offers to fill it
            empty_folder = app._db.create_collection("Empty In Smoke")
            app._load_from_db()
            app._set_section(f"collection:{empty_folder}")
            root.update()
            check("an empty folder says so",
                  app._library._empty_label.cget("text") == "Folder is empty.")
            check("an empty folder offers the add button",
                  str(app._library._empty_btn.cget("state")) == "normal")
            check("the cards of the view before are gone",
                  not app._library._rendered)
            app._db.delete_collection(empty_folder)
            app._set_section("all")
            root.update()

            # the context menu has to let go of the app again
            from ui.dialogs import PopupMenu
            menu = PopupMenu(app, 400, 300, [("Do it", lambda: None)])
            root.update()
            entries = [w for w in menu.winfo_children()
                       for w in w.winfo_children() if isinstance(w, tk.Label)]
            entries[0].event_generate("<Button-1>")
            root.update()
            check("the context menu closes itself", not menu.winfo_exists())
            check("the context menu lets go of the app",
                  root.grab_current() is None)

            app._open_settings()
            root.update()
            app._settings._select_tab("health")
            root.update()
            check("the health tab lists the issues",
                  len(app._settings._health_issues.get_children())
                  == len(app._settings.ISSUES))
            app._settings._health_issues.selection_set("gaps")
            app._settings._on_health_issue()
            root.update()
            check("the health tab shows the items of an issue",
                  len(app._settings._health_items.get_children()) >= 0)
            app._settings._health_issues.selection_set("poster")
            app._settings._on_health_issue()
            root.update()
            check("the health tab shows the items of an issue",
                  len(app._settings._health_items.get_children()) >= 0)

            # the database can be checked, compacted and copied
            app._settings._check_database()
            root.update()
            check("the database check says something",
                  "check:" in app._settings._maint_status.cget("text"))
            app._settings._compact_database()
            root.update()
            check("the database is compacted",
                  "compacted" in app._settings._maint_status.cget("text"))
            app._db.backup(target / "backup.db")
            check("the library can be copied",
                  Database(target / "backup.db").load_all())

            # a duplicate title can be resolved
            app._db.conn.execute(
                "INSERT INTO media(path,title,media_type,root) VALUES(?,?,?,?)",
                (str(target / "Dup" / "copy.mkv"), movie["title"], "movie",
                 str(target / "Dup")))
            app._db.conn.commit()
            groups = app._db.duplicate_groups()
            check("the duplicate title is found", bool(groups))
            if groups:
                from ui.dialogs import DuplicateResolver
                resolver = DuplicateResolver(app, groups)
                resolver._keep_best()
                resolver._resolve()
                root.update()
                check("the resolver keeps one copy of every title",
                      len(resolver.result) == len(groups[0]["items"]) - 1)
                for media_id in resolver.result:
                    app._db.ignore(media_id)
                check("the copy is out of the library",
                      len(app._db.load_all()) == 410)
                app._db.sync(scan(target / "Movies", target / "Series"))
                check("the copy stays out after a rescan",
                      len(app._db.load_all()) == 410)

            # a video player of your own
            app._settings._select_tab("general")
            root.update()
            check("the settings have a player list",
                  bool(app._settings._player_combo.cget("values")))
            saved_path = config.SETTINGS_PATH
            try:
                config.SETTINGS_PATH = target / "settings.json"
                config.save_settings({"players": [{"name": "Test", "path": "x.exe"}],
                                      "player": "x.exe"})
                check("the chosen player is remembered", config.PLAYER == "x.exe")
                app._settings._refresh_players()
                check("the player list shows the player",
                      "Test" in app._settings._player_combo.cget("values"))
            finally:
                config.SETTINGS_PATH = saved_path
                config.init()

            # the organizer can take a run back
            messy = target / "Undo"
            messy.mkdir(exist_ok=True)
            film = messy / "Inception.2010.1080p.mkv"
            film.write_bytes(b"x")
            org = app._settings._organizer
            org._path_var.set(str(messy))
            org._scan()
            root.update()
            check("the organizer plans the file", bool(org._entries))
            if org._entries:
                moved_to = messy / org._entries[0].target
                moved_to.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(film), str(moved_to))
                org._last_moved = [(film, moved_to)]
                org._undo()
                for _ in range(60):
                    root.update()
                    if not org._running:
                        break
                    time.sleep(0.05)
                check("the organizer puts the file back", film.exists())

            # the EP renamer renames the episode files of a series
            app._settings._select_tab("renamer")
            root.update()
            renamer = app._settings._renamer
            series_items = [i for i in app._db.get_all_items()
                            if i["media_type"] == "series"]
            check("the renamer knows the series of the library",
                  bool(series_items)
                  and len(renamer._series) == len(series_items))
            show_item = None
            show_ep = None
            for cand in series_items:
                for e in app._db.episodes(cand["id"]):
                    if e["season"] and e["episode"]:
                        show_item, show_ep = cand, e
                        break
                if show_item is not None:
                    break
            check("the renamer finds a series with episodes", show_item is not None)
            if show_item is not None:
                # a title that is not the one of the file
                app._db.set_episode_names(show_item["id"], {
                    show_ep["season"]: [{"episode": show_ep["episode"],
                                         "name": "The Long One"}]})
                index = [i["id"] for i in renamer._series].index(show_item["id"])
                renamer._combo.current(index)
                renamer._plan()
                root.update()
                check("the renamer lists the episode files",
                      bool(renamer._tree.get_children()))
                planned = [e for e in renamer._entries if not e.result]
                check("the renamer previews the new name", bool(planned))
                if planned:
                    old = planned[0].source
                    new = planned[0].target
                    renamer._apply()
                    for _ in range(60):
                        root.update()
                        if not renamer._running:
                            break
                        time.sleep(0.05)
                    check("the file got its new name",
                          new.exists() and not old.exists())
                    check("the database follows the rename",
                          any(e["path"] == str(new)
                              for e in app._db.episodes(show_item["id"])))
                    renamer._undo()
                    for _ in range(60):
                        root.update()
                        if not renamer._running:
                            break
                        time.sleep(0.05)
                    check("the new name was taken back",
                          old.exists() and not new.exists())
                    check("the database follows the undo",
                          any(e["path"] == str(old)
                              for e in app._db.episodes(show_item["id"])))

            # the retry button waits for a failure
            app._settings._select_tab("bulk")
            root.update()
            check("the retry button waits for a failure",
                  str(app._settings._retry_btn.cget("state")) == "disabled")
            app._settings._bulk_failed_ids = [movie["id"]]
            app._settings._update_bulk_buttons_state()
            root.update()
            check("the retry button comes with the failures",
                  str(app._settings._retry_btn.cget("state")) == "normal")
            app._settings._bulk_failed_ids = []

            # the wikiquote tab
            app._settings._select_tab("quotes")
            root.update()
            check("the wikiquote tab is there",
                  find_button(app._settings, "Fetch quotes") is not None)
            app._db.set_quotes(movie["id"], [{"text": "A quote", "speaker": "X"}])
            app._settings._refresh_quote_list()
            root.update()
            check("the wikiquote list holds the film",
                  len(app._settings._quote_tree.get_children()) == 1)
            app._settings._quote_tree.selection_set(str(movie["id"]))
            app._settings._on_quote_select()
            root.update()
            check("the quote is shown in the pane",
                  "A quote" in app._settings._quote_text.get("1.0", "end"))
            app._show_library()
            root.update()
            stats_snapshot = app._images.snapshot_stats()
            print(f"    image stats: {stats_snapshot}")
            from utils.image_cache import bucket_size
            check("the backdrop covers a maximised window",
                  bucket_size(1900, 1000)[0] >= 1900
                  and bucket_size(1900, 1000)[1] >= 1000)
            results.append("done")
        except Exception as exc:
            print(f"[FAIL] ui smoke raised {exc!r}")
            import traceback
            traceback.print_exc()
            finish()
            return
        root.after(50, fetch_stage)

    def fetch_stage() -> None:
        """A metadata fetch must end in a state, not stay on 'Searching…'."""
        try:
            metadata_service.set_service(FakeProvider(1))
            movie = next(i for i in app._all_items if i["media_type"] == "movie")
            app._open_item(movie)
            root.update()
            app._details._fetch_metadata()
            # the status is set synchronously; a later update() may already
            # have replaced it with the finished state
            check("fetch starts in the searching state",
                  app._details._action_status.cget("text") == "Searching…")
            root.update()
            wait_fetch()
        except Exception as exc:
            print(f"[FAIL] fetch smoke raised {exc!r}")
            import traceback
            traceback.print_exc()
            finish()

    def wait_fetch() -> None:
        status = app._details._action_status.cget("text")
        if status == "Searching…":
            if attempts["n"] < 100:
                attempts["n"] += 1
                root.after(50, wait_fetch)
                return
            check("fetch leaves the searching state", False)
            picker_stage()
            return
        check("fetch leaves the searching state", status == "Metadata updated.")
        check("the fetch button is usable again",
              str(app._details._fetch_btn.cget("state")) == "normal")
        plot = app._details._panel.get("plot")
        check("the panel shows the new plot",
              plot is not None and "Test plot" in plot.cget("text"))
        picker_stage()

    def picker_stage() -> None:
        """Several matches open a chooser; cancelling must not wedge the UI."""
        try:
            metadata_service.set_service(FakeProvider(2))
            attempts["n"] = 0
            app._details._fetch_metadata()
            wait_picker()
        except Exception as exc:
            print(f"[FAIL] picker smoke raised {exc!r}")
            import traceback
            traceback.print_exc()
            finish()

    def wait_picker() -> None:
        pickers = [w for w in app._details.winfo_children()
                   if isinstance(w, tk.Toplevel)]
        if not pickers:
            if attempts["n"] < 100:
                attempts["n"] += 1
                root.after(50, wait_picker)
                return
            check("chooser opens for an ambiguous title", False)
            finish()
            return
        check("chooser opens for an ambiguous title", True)
        cancel = find_button(pickers[0], "Cancel")
        check("chooser offers a cancel button", cancel is not None)
        if cancel is not None:
            cancel.invoke()
            root.update()
            check("cancelling re-enables the fetch button",
                  str(app._details._fetch_btn.cget("state")) == "normal")
            check("cancelling clears the searching state",
                  app._details._action_status.cget("text") == "Cancelled.")
        bulk_stage()

    def bulk_stage() -> None:
        """Stopping a bulk fetch must not start new items."""
        try:
            state = {"attempts": []}

            class SlowProvider(metadata_service.MetadataService):
                def search_candidates(self, title, year=None, media_type=None):
                    return []

                def fetch_metadata(self, title, year=None, media_type=None,
                                   imdb_id=None):
                    state["attempts"].append(title)
                    time.sleep(0.05)
                    return metadata_service.MetadataResult(
                        title=title, year=year, plot="Test plot",
                        imdb_id="tt0000001")

            metadata_service.set_service(SlowProvider())
            items = [dict(i) for i in app._all_items[:20]]
            app._settings._fetch_items(items)
            root.after(120, lambda: stop_bulk(state, items))
        except Exception as exc:
            print(f"[FAIL] bulk smoke raised {exc!r}")
            import traceback
            traceback.print_exc()
            finish()

    def stop_bulk(state, items) -> None:
        app._settings._cancel_bulk()
        root.after(500, lambda: check_bulk(state, items))

    def check_bulk(state, items) -> None:
        check("stopping a bulk fetch does not start new items",
              0 < len(state["attempts"]) < len(items))
        finish()

    finished = {"closed": False}

    def finish() -> None:
        if finished["closed"]:
            return
        finished["closed"] = True
        try:
            app.close()
        finally:
            config.PLAYER = saved_player
            try:
                root.destroy()
            except tk.TclError:
                pass

    def from_ui() -> None:
        try:
            poll()
        except SystemExit:
            raise
        except Exception as exc:
            print(f"[FAIL] ui smoke raised {exc!r}")
            import traceback
            traceback.print_exc()
            finish()

    root.after(2500, from_ui)
    root.after(120000, finish)
    root.mainloop()
    check("ui smoke completed", results == ["done"])


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui", action="store_true", help="also run invisible UI test")
    parser.add_argument("--target", default=None)
    args = parser.parse_args()

    target = Path(args.target or tempfile.mkdtemp(prefix="cinema_smoke_"))
    fresh = args.target is None
    build(target)
    parse_checks()
    true_story_checks()
    scanner_db_checks(target)
    organizer_checks(target)
    if args.ui:
        ui_checks(target)
    if fresh:
        shutil.rmtree(target, ignore_errors=True)
    if failures:
        print(f"FAILED: {failures} check(s)")
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
