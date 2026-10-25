"""Generate a synthetic media library for performance testing.

Usage:
    py -3 tools/make_test_library.py --target C:\\temp\\cinema_perf --movies 300 --series 110
"""
from __future__ import annotations

import argparse
import random
from pathlib import Path

from PIL import Image, ImageDraw

_POSTER_SIZES = [(300, 450), (800, 1200), (1400, 2100)]


def _art(folder: Path, stem: str, rng: random.Random) -> None:
    w, h = rng.choice(_POSTER_SIZES)
    color = (rng.randint(30, 220), rng.randint(30, 220), rng.randint(30, 220))
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    for _ in range(40):
        c = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
        x0, y0 = rng.randint(0, w), rng.randint(0, h)
        d.rectangle((x0, y0, x0 + rng.randint(10, 200), y0 + rng.randint(10, 200)), fill=c)
    img.save(folder / f"{stem}.jpg", "JPEG", quality=90)


def build(target: Path, movies: int = 300, series: int = 110) -> None:
    rng = random.Random(7)
    movies_root = target / "Movies"
    series_root = target / "Series"
    for i in range(movies):
        folder = movies_root / f"Test Movie {i} ({2000 + i % 25})"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"Test Movie {i}.mkv").write_bytes(b"")
        if i % 7:
            _art(folder, "poster", rng)
        if i % 3 == 0:
            _art(folder, "poster2", rng)
    for i in range(series):
        folder = series_root / f"Test Series {i} ({2000 + i % 26})"
        folder.mkdir(parents=True, exist_ok=True)
        for s in range(1, rng.randint(1, 6) + 1):
            season_dir = folder / f"Season {s:02d}"
            season_dir.mkdir(parents=True, exist_ok=True)
            for e in range(1, rng.randint(2, 9) + 1):
                (season_dir / f"Test Series {i} S{s:02d}E{e:02d} - Pilot.mkv").write_bytes(b"")
        if i % 5:
            _art(folder, "poster", rng)
        if i % 4 == 0:
            _art(folder, "poster2", rng)
    print(f"built {movies} movie folders, {series} series folders in {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--movies", type=int, default=300)
    parser.add_argument("--series", type=int, default=110)
    args = parser.parse_args()
    build(Path(args.target), movies=args.movies, series=args.series)


if __name__ == "__main__":
    main()
