# Cinema — Agent Guide

Cinema is a local movie and TV library browser built with Python, Tkinter, SQLite, and Pillow. Use [README.md](README.md) for user-facing features and setup; keep this file focused on development guidance.

## Working in this repository

- Read the relevant code and follow its existing patterns before changing behavior. Keep changes focused and update related documentation when behavior or setup changes.
- Treat `data/`, `settings.json`, and configured media folders as user data. Do not overwrite, delete, or move their contents during development or validation unless the task explicitly requires it.
- For media file operations, use and inspect the existing helpers in `utils/media_ops.py` and `utils/organizer.py`; preserve confirmation and undo behavior.
- Report errors through the existing logging or UI patterns. Avoid silently swallowing failures or returning success-shaped fallbacks.

## Implementation and validation

- Trace the affected flow through its callers and helpers before editing. For behavior changes and bug fixes, add or update a focused check in the existing smoke-test harness when practical; do not claim a behavior is verified by lint alone.
- Keep validation isolated from personal data. `tools/smoke_test.py` creates a synthetic library in a temporary directory by default and removes it afterward. If using `--target`, choose a disposable path because the directory is retained. Never point `tools/make_test_library.py` at real media.
- Run `ruff check .` and `py -3 tools\smoke_test.py` for code changes. For Tkinter behavior changes, also run `py -3 tools\smoke_test.py --ui` when the environment supports it. If a relevant check cannot run, report that and why rather than implying it passed.
- For database changes, verify startup migration against both a fresh database and an existing database when practical; migrations must remain idempotent and preserve existing user data.
- In the final summary, state the behavior changed and the exact checks run, including any failures or unverified cases.

## Architecture and conventions

- `main.py` initializes configuration, logging, the database, and the Tkinter application.
- `config.py` owns local paths and settings. `settings.json` is machine-specific and ignored by Git; do not commit it.
- `database/db_manager.py` owns the SQLite schema, migrations, connections, and queries. Keep schema changes backward-compatible with an idempotent migration.
- `scanner/` discovers local media; `metadata/` contains the provider interface and optional integrations; `ui/` contains Tkinter views; `utils/` contains shared path, image, and file-operation helpers.
- SQLite connections are thread-local. Never pass a connection between threads; database workers should use `db.thread_target(fn)` so their connections are closed when finished.
- Keep Tkinter widget access on the UI thread. Run blocking work off-thread and schedule UI updates back through the existing `after`/posting patterns.
- Use `utils.path_utils.canonical_path()` for path comparisons.
- Rescans must preserve watched state (`watched` and `last_watched`). Episode rows are keyed by `(series_id, season, episode, path)`; chained episodes in one file share a path.
- The app should remain usable offline. Treat metadata/network integrations as optional and preserve existing cache behavior.

## Setup and validation

Requires Python 3.11+ and Pillow (`py -3 -m pip install -r requirements.txt`). Run the app with:

```powershell
py -3 main.py
```

There is no separate unit-test framework; the smoke-test script covers offline parser, scanner, database, organizer, metadata, and (optionally) UI checks. Ruff is configured for pyflakes and syntax/critical errors.

```powershell
ruff check .
py -3 tools\smoke_test.py
```

For Tkinter changes, run `py -3 tools\smoke_test.py --ui` and verify the affected flow in the app when practical. `tools/make_test_library.py` can create a dummy library for manual testing; use only a disposable target.
