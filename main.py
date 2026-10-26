from __future__ import annotations

import logging
import logging.handlers
import time
import tkinter as tk

import config
from database.db_manager import Database
from metadata import metadata_service
from ui.main_window import MainWindow
from ui.window_utils import set_dark_title_bar
from utils import image_utils


def main() -> None:
    config.init()
    image_utils.ensure_placeholder(config.PLACEHOLDER_PATH)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )
    log = logging.getLogger("cinema")
    started = time.perf_counter()

    root = tk.Tk()
    root.title("Cinema")
    root.geometry("1240x780")
    root.minsize(980, 620)
    root.configure(bg="#0d0e12")
    set_dark_title_bar(root)
    root.after(300, lambda: set_dark_title_bar(root))

    db = Database(config.DB_PATH)
    try:
        from metadata.cinemeta_service import CinemetaService
        metadata_service.set_service(CinemetaService())
    except Exception:
        logging.getLogger("cinema").exception("metadata service unavailable")
    app = MainWindow(root, db, movies_dir=config.MOVIES_DIR, series_dir=config.SERIES_DIR)
    log.info("UI ready in %.2fs", time.perf_counter() - started)

    def on_close() -> None:
        try:
            app.close()
        except Exception:
            log.exception("error during shutdown")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    # exceptions inside Tk callbacks are printed to a console that
    # pythonw users never see - put them in the log instead
    def _log_callback_error(exc, val, tb) -> None:
        log.exception("uncaught Tk callback error", exc_info=(exc, val, tb))

    root.report_callback_exception = _log_callback_error
    root.mainloop()
    try:
        db.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()