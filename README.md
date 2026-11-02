# Cinema — Personal Movie & TV Series Library

<div align='center'>
<p>
  <img width="720" src="https://i.imgur.com/Oeae7ZO.jpeg" />
</p>

<p>
  <img width="480" src="https://i.imgur.com/acWJCVj.png" /> <img width="480" src="https://i.imgur.com/CzeDl06.png" />
</p>
</div>

A dark, media-centre-style desktop app that scans your local folders and shows
your movies and series in a poster grid, with detailed series/episode views.
It runs fully offline; an optional metadata provider (Cinemeta) can be
connected to fill in titles, plots, cast and artwork.

## Requirements

- Python 3.11 or newer (developed against 3.13)
- Pillow (`py -3 -m pip install pillow`)

## Install & run (Windows)

```bat
py -3 -m pip install -r requirements.txt
launch.bat
```

Or manually:

```bat
py -3 main.py
```

`launch.bat` sits next to `main.py` and just runs the app. If something fails
it tells you to install Pillow.

## Configure your media folders

Click **Settings** at the bottom of the sidebar — it opens a full page inside
the app with these tabs:

* **General** — card-based controls for library folders, backup and restore,
  CSV export, the local data folder, artwork cache maintenance, video player,
  and metadata provider.
* **Bulk Fetch** — separate *Movies & Series* and optional *People Profiles*
  lists, with clear fetch controls and progress. People profiles come from
  Wikipedia and are cached with their biographies and photos for offline
  reading.
* **Metadata Editor** — a searchable-at-a-glance collection table with
  metadata badges; remove metadata, poster, backdrop or `about.txt` for
  selected rows (each removal asks for confirmation first).
* **Organizer** — sorts a messy folder of loose video files into folders
  named after the files.
* **EP Renamer** — gives the episode files of a series the names of the
  fetched episode titles.
* **Dig Deeper** — the movies and series whose story is based on a true
  story, with a two-pane story reader.
* **Wikiquote** — browse films and read their quotes side by side.
* **Library Health** — review missing files, duplicates, and index issues,
  with tools to inspect and repair the library.

<div align='center'>
<p>
  <img width="480" src="https://i.imgur.com/FV0moWo.png" />
</p>
</div>

Everything is stored in `settings.json`, written on first start:

```json
{
  "movies_dir": "F:\\Cinema\\Movies",
  "series_dir": "F:\\Cinema\\Series"
}
```

You can also edit that file directly — then hit **Rescan Library**. This file
is machine-local; it is listed in `.gitignore` and can also hold API keys
later (never keys in source code).

Use **Export Backup** in Settings to save a ZIP containing the library
database and settings. **Restore Backup** validates the archive, database and
settings before replacing anything, and saves a safety copy of the current
database and settings alongside the originals. **Export CSV** writes the
current movie and series list. **Open Data Folder** opens Cinema's local data
directory in the system file explorer.

## What it detects

- **Movies**: one item per subfolder of the movies root (or a bare video file
  directly inside the root). Title/year parsed from `Name (2010)` and similar,
  including release names such as `Movie 2020 PROPER 1080p WEB-DL x264`.
- **Series**: one item per subfolder of the series root; seasons/episodes are
  detected from `S01E01`, `1x13`, `Episode 5`, `07` file names, `Season 01`
  folders, or a flat list of numbered files (`001 Title.mp4`, `002 Title.mp4`
  … `161 Title.mp4`). Episodes are not shown as cards — open a series to browse
  seasons and episodes.
- Video formats: `.mkv .mp4 .avi .mov .wmv .m4v .ts`
- Artwork: `poster.*` and `poster2.*` (also `fanart`/`backdrop` for the
  background), case-insensitive, optionally inside an `artwork`/`posters`
  subfolder.
- Folders without media, inaccessible folders and malformed files are skipped
  with a log entry, not a crash.

## The interface

- Sidebar: All Library / Movies / TV Series, a separator, then **+ Add Folder**
  and one entry per folder you created
- Top bar: search with a placeholder, an active-filter chip, the **sort**
  dropdown next to **Rescan Library** and **Surprise me**
- The status bar says what the scan did: *Scan finished in 0.2s - 378
  movies, 49 series - 3 new, 1 gone, 5 changed*
- Several cards at once: **ctrl+click** marks one, **shift+click** marks
  the whole range, **Ctrl+A** marks everything. The right-click menu then
  works on the whole selection - mark them watched, file them into a
  folder or throw their metadata away
- Poster grid with lazy rendering — only visible cards exist, so scrolling
  stays smooth with 400+ items. Click a card to open it, right-click for a
  context menu (open, open folder, move to folder, mark watched, remove
  metadata)
- Keyboard: click a card once, then use the arrow keys to move, `Enter` to
  open
- Details view: `poster2` (or `poster`) as darkened backdrop, click the
  poster to enlarge it, full metadata display, and a season/episode browser
  where every episode has its own **play** and **watched** button (the
  seasons collapse, the rows are only built while a season is open), plus
  *Open Folder* (Esc = back)
- Click a cast, director or writer name to see its Wikipedia profile in a
  reading pane beside the filtered library results; fetched profiles remain
  available offline
- Every popup (the metadata chooser, the confirmation dialogs, the poster
  viewer, the context menu) opens in the middle of the app window and is
  drawn in the dark theme, including the Windows title bar
- Status bar: last scan time and live thumbnail counters; a progress bar
  appears while a scan is running

### Folders

A folder is a **category inside the app** — nothing is created on disk and no
media file is ever moved:

- **+ Add Folder** in the sidebar opens a dialog: give the folder a name and
  tick the movies/series you want in it. The folder shows up in the sidebar
  with the number of items in it.
- click a folder in the sidebar to see only what is inside it
- an empty folder says **Folder is empty** and offers an **Add Item**
  button, which files items from your library into it
- right-click a card and pick **Move to folder…** to file it into a folder (or
  take it out of one) later
- right-click a folder in the sidebar to **rename** or **delete** it — deleting
  a folder only removes the category, the items stay in your library
- the details view shows the folder an item is filed in

Folders live in the app database (`collections` table), so a rescan of your
media folders never touches them.

## Searching

The search box understands a small query language:

| Query | Meaning |
|-------|---------|
| `batman` | free text over title, cast, director, writer, genres, country, year, IMDb id |
| `1989` | free text, but also matches the year column |
| `1990-2000` | year range (`-2000` = before, `2000-` = after) |
| `genre:Drama` | only items in that genre |
| `director:Nolan` | only items by that director |
| `actor:Pfiffer` | only items with that actor |
| `writer:Kaufman` | only items by that writer |
| `watched:yes` / `unwatched:` | only what you watched (or did not) |
| `rating:7+` | the rating of the provider - `7`, `7+`, `>7`, `<7` |
| `myrating:4+` | your own rating (1 to 5) |
| `type:movie` / `type:series` | only movies or only series |
| `missing:poster` | only the items without a poster (also `metadata`, `imdb`, `rating`, `runtime`, `cast`) |

The fields above cover the same ground as the filter row the app had
before: clicking a genre, person or country in the details view adds a
removable chip to the search bar.

- SQLite index in `data/library.db`; startup loads records first, the
  filesystem scan runs on a background thread and only writes the diff
  (removed/changed folders are updated, fetched metadata **and the watched
  flags are kept**). The scan walks the whole tree every time; only the
  write is a diff. A folder that cannot be read - a locked drive, a
  network hiccup - keeps its rows instead of losing them
- One directory walk per media folder — the walk decides whether the folder
  is a movie or a series, so nothing is visited twice
- Thumbnail disk cache in `data/thumbs` (resized ~82-quality JPEGs), keyed by
  file path + mtime + size — regenerated only when a source image changes.
  The cache itself never writes into your media folders; a *metadata fetch*
  does (`about.txt`, `poster.jpg`, `poster2.jpg`). The backdrop is fetched
  in a few sizes (640 → 2560 wide), so it always covers the window, also
  when the app is maximised
- Bounded LRU memory cache (256 thumbnails, 3 backdrops), Pillow `draft()`
  decoding for JPEG posters, a 3-worker priority pool with cancellation
- The grid re-draws only the cards whose content actually changed, so typing
  in the search box does not restart every thumbnail request
- Live counters in the status bar; per-run statistics go to `data/logs/app.log`
  (rotated at 1 MB), which also records scan duration and thumbnail
  cache hits/misses/generation times

## Project layout

```
cinema/
├── main.py                     # entry point
├── config.py                   # paths & settings (settings.json)
├── database/db_manager.py      # SQLite index library + episodes
├── scanner/library_scanner.py  # filesystem scanning (UI-independent)
├── metadata/
│   ├── metadata_service.py     # provider-agnostic metadata API
│   ├── cinemeta_service.py     # Cinemeta (Stremio) provider
│   └── true_story.py           # "based on a true story" lookup (Wikipedia)
├── ui/
│   ├── main_window.py          # shell, sidebar, search, filters, scanning
│   ├── library_view.py         # canvas poster grid
│   ├── details_view.py         # detail panel + episode browser
│   ├── dialogs.py              # dark confirm / prompt / context menu
│   ├── folder_dialog.py        # add a folder / file media into it
│   ├── settings_view.py        # general / bulk fetch / metadata editor
│   ├── organizer_view.py       # folder organizer tab
│   ├── ep_renamer_view.py      # episode renamer tab
│   └── window_utils.py         # centre popups, dark title bar
├── utils/
│   ├── title_parser.py         # title / year / episode parsing
│   ├── organizer.py            # file name -> folder plan, moving files
│   ├── image_cache.py          # async thumbnail cache
│   ├── image_utils.py          # artwork lookup, resizing
│   └── media_ops.py            # open folder / play file / move helpers
├── tools/
│   ├── make_test_library.py    # generates a 400+ item test library
│   └── smoke_test.py           # offline + (optional, --ui) smoke tests
├── assets/placeholder.png
└── data/                       # created at runtime: db, thumb cache, logs
```

## Testing

```bat
py -3 tools\smoke_test.py --ui
```

Builds a synthetic 410-item library (mixed small/high-res posters) in a temp
folder, verifies scanner/database/parser behaviour — including that the
watched flags survive a rescan and that a removed library root is cleaned up
— then runs the UI invisibly (grid rendering, details view, back navigation,
sidebar counts, search). With `--target X` the temp library is kept, e.g. to
test against a persistent copy.

## Organizer

**Settings → Organizer** cleans up a folder where the video files were dumped
without a folder structure. Nothing is downloaded and no URL is asked for —
the file name alone decides where a file goes:

* **Scan folder** lists every loose video file in the folder with the folder
  it would end up in. The folder is remembered in `settings.json`
  (`organizer_dir`) and the tab scans it as soon as you open it.
* A movie becomes `Title Year Quality` — `Inception.2010.1080p.BluRay.x264.mkv`
  goes into `Inception 2010 1080p`.
* A series becomes `Title Quality` with a `Season N` sub folder —
  `Breaking.Bad.S01E02.720p.mkv` goes into `Breaking Bad 720p/Season 1`.
* A file that carries no name (only `07.mkv` or `S01E02.Pilot.mkv`) is left
  where it is and marked in the list.
* Double click a row to change the folder name before you move anything.
* **Organize files** moves the files (after a confirmation) and then
  rescans the library. The files are only moved, never renamed, and existing
  artwork in the target folder is left alone.
* **Undo** takes the last run back - the files go where they were.

## EP Renamer

**Settings → EP Renamer** renames the episode files of a series after the
episode titles a metadata fetch brought in. Pick a series and the tab lists
every episode file with the name it has now and the name it would get:

```
The.Mentalist.S02E11.Age.of.Reduction.mkv  ->  The Mentalist S02E11 - Age of Reduction.mkv
```

* The names come from the provider, so **fetch the series first** - a file
  without an episode title is listed with the reason and left alone.
* A file that holds two episodes, a file without a season number and a file
  that is not on the disk are never renamed.
* **Apply all** renames the files; they keep their place and the library
  follows the new paths.
* **Undo** puts the names of the last run back.

## Metadata

`Cinemeta` (Stremio's public catalogue) is connected by default and needs no
key. It is only used when you ask for it — the **Fetch this movie/series**
button in the details view, or the Bulk Fetch tab.

* When several titles match, a chooser lets you pick the right one.
* The items that fail can be tried again: **Retry failed** in the Bulk
  Fetch tab.
* Fetching writes `about.txt` next to the media and downloads `poster.jpg` /
  `poster2.jpg`.
* **Existing artwork is never overwritten.** Remove the poster first in
  *Settings → Metadata Editor* if you want the downloaded one to replace it.

To use a different provider, implement `MetadataService.fetch_metadata()` and
`search_candidates()` in `metadata/` and call
`metadata_service.set_service(YourService())` in `main.py`. API keys belong in
`settings.json` or an environment variable — never in source code.

## Dig Deeper

**Settings → Dig Deeper** lists the movies and series in your library whose
story is based on a true story, and lets you read about each of them.

* **Scan library** asks Wikipedia whether the title belongs to one of its
  "based on a true story" categories (`Films based on actual events`,
  `Biographical films`, `Films based on non-fiction books`, …). Fifty
  titles are asked for in one request, so a whole library takes a couple of
  minutes. The status line shows what it is doing — *Checking 250 of 427 —
  Some Movie (2019) (movie) · 177 to go* — and **Stop** ends the run
  without losing the verdicts already found.
* The list holds the flagged titles — pick one to read it. The pane shows
  **What really happened** (the lead of the book, the article or the person
  the film was made from, with the link to it), **Where it was filmed**,
  **The people** (director, writers and cast) and **Awards**. **Open
  Folder** opens the folder of the title, **Open Details Page** jumps to
  the item's page in the app. The details view of a flagged title also says
  *Based on a true story*.
* **Cache the stories** fetches the story of every flagged title and keeps
  it in the library — twenty titles per request, so the whole list takes
  well under a minute. After that the pane is filled from the database and
  the app needs **no network at all** to read the stories. A title that was
  not cached yet is fetched once in the background when you pick it, and
  kept for the next time.
* The categories are Wikipedia's own, so a film nobody tagged that way is
  not on the list, and a film Wikipedia tags loosely (a historical epic with
  a fictional story, say) is. Only titles that were checked are skipped by
  the next scan.
* **Forget flags** clears every verdict, so the next scan checks the whole
  library again.

The scan needs the network; without it the tab says so and the items stay
unchecked for the next try. Once the stories are cached, the tab reads
without a network.

## Wikiquote

**Settings → Wikiquote** brings the quotes of your own films into the app.
**Fetch quotes** walks the library and looks every title up on Wikiquote —
six films per request, so the whole library takes about seven minutes.
**Stop** ends the run and keeps what it found. The list on the left holds
the films that have quotes, the pane on the right shows them with who said
them, and **Random quote** picks one from the whole library.

Wikimedia throttles apps that ask too much, too fast. When that happens
the run does not die: it waits (the status says *Wikiquote is throttling us
- waiting 30 s…*) and continues with the same batch afterwards. The quotes
are stored in the library, so the tab works offline afterwards and the next
run only asks about the films it has not checked yet.

Not every film has a page on Wikiquote, and a title that is written
differently in your folder than on Wikipedia is not found — those films
just do not show up. In a library of 427 films about 280 of them have
quotes.

What is shown is real dialogue with the name of the character who said
it — the lists of characters and episodes on the Wikiquote pages are left
alone. **Forget quotes** deletes everything that was fetched, so the next
run looks the whole library up again.

## Your own rating

Every item has a rating of its own next to the one from the metadata
provider: the five stars under the facts in the details view. Click a star
to rate it, click it again to clear it. The rating shows on the poster
card (`2010  ·  Movie  ·  ★4`) and **My rating high→low** in the sort list
brings your favourites to the front.

## Insights

The Insights page summarizes your library and includes a **Continue watching**
row for series with episodes still unwatched. Open a series directly from the
row or choose **View all** to browse every in-progress series.

## Surprise me

The button next to **Rescan Library** in the top bar picks a random title from
the currently filtered library and opens its page. Active section, search,
genre, watched state, and rating filters are respected; when no watched filter
is active, Cinema prefers titles you have not watched.

## Library Health

**Settings → Library Health** shows what the library looks like from the
outside: how many items, movies and series it holds, how many hours of
runtime are known, how many of them you rated and how much video is on
disk. The list on the left names the problems — duplicate titles, items
without metadata, items without a poster, movies you never watched, titles
that were never checked for a true story, and files that are not on the
disk any more (an unplugged drive, a moved folder) — and the list on the
right shows the items behind the selected problem. **Open Details Page**
jumps to one of them.

* **Resolve duplicates** (on the duplicate problem) asks which copy of every
  title stays — the richest one is offered first. The copies that are
  taken out are *ignored*: their files stay where they are, but the app does
  not show them any more and the next scan does not bring them back.
* **Relink…** (on the missing problem) points an item at the place it moved
  to. Pick the new file (or the new folder of a series) and the item, its
  episodes and its artwork move along; the metadata is kept.
* **Maintenance** looks after the database itself: **Check the database**
  runs SQLite's own integrity check, **Compact the database** gives the
  unused space back to the disk, **Back up the database** writes a copy
  somewhere else, and **Open the log folder** shows `data/logs`.

## Video player

**Settings → General → Video player** decides what opens your movies.
**Add player…** asks for a name and points at the program of any player you
have installed (VLC, mpv, your favourite thing); the list is kept in
`settings.json` and the player you pick is used from then on. Without a
player of your own the file is opened with the system default, as before.

## Notes

- App data (DB, thumbnails, logs) lives in `data\` — delete that folder to
  hard-reset the app; your media files are never touched.
- Remove any `settings.json` to regenerate the defaults with the paths above.
- Not implemented: a file-watcher for auto-rescan, and a virtualized canvas
  with widget recycling (the incremental visible-row approach already caps the
  widget count).
