from __future__ import annotations

import re

_PAREN_YEAR_RE = re.compile(r"[\(\[](19\d{2}|20\d{2})[\)\]]")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)")

# Release / quality tags that may follow the year in a release folder name.
_TAG_WORDS = (
    r"2160p|1080p|720p|480p|[1-9]\d{2,3}[pi]"
    r"|blu-?ray|bdremux|remux|bdrip|brrip|dvdrip|web-?dl|webrip|hdtv|hdrip"
    r"|camrip|hdcam|telesync"
    r"|hevc|x26[45]|h\.?26[45]|xvid|avc|vp9|10bit|8bit"
    r"|hdr10?\+?|dolby.?vision|dv|hl[gs]"
    r"|aac(?:\s?\d(?:\.\d)?)?|ac3|eac3|dts(?:-?hd)?(?:-?ma)?|truehd|atmos|opus|flac"
    r"|proper|repack|extended|unrated|remastered|internal|complete"
)

# A year that ends the name, or that is followed by a release tag.
_TRAILING_YEAR_RE = re.compile(
    r"[\s._-]+(?:[\(\[])?((?:19|20)\d{2})(?:[\)\]])?"
    r"(?=[\s._-]*(?:" + _TAG_WORDS + r")\b|$)",
    re.IGNORECASE,
)

_SAE_RE = re.compile(r"(?i)\bs(\d{1,2})\s?e(\d{1,3})(?:[\s._\-]?e\d{1,3})*\b")
# "2x13" must be a standalone token: without the word boundary it also
# matches random download hashes such as "v6ajb95x62k2mrdroouv.mp4".
_XYX_RE = re.compile(r"(?<![\w])(\d{1,2})x(\d{1,3})(?![\w])")
_EPNUM_RE = re.compile(
    r"(?i)(?:^|[\s._\-])(?:e|ep|episode)[\s._\-]?(\d{1,3})(?:$|[\s._\-])"
)
# Only a stem that is *just* a number counts ("07.mkv"). Anything longer is a
# title that happens to start with digits ("10.Things.I.Hate.About.You").
_BARE_NUM_RE = re.compile(r"^\s*(\d{1,3})\s*$")
# A zero padded number in front of the title counts as the episode number:
# "001   Puss Gets the Boot [1940].mp4" is the first episode. The padding is
# what tells it apart from a title that starts with digits ("10 Things I
# Hate About You"). A plain three-digit number also counts ("100   Title")
# to handle long series where the padding runs out.
_LEAD_NUM_RE = re.compile(r"^\s*(0\d{1,3}|\d{3})(?=[\s._\-])")
_SEASON_FOLDER_RE = re.compile(
    r"(?i)^\s*(?:season|saison|temporada|staffel|s)[\s._\-]*(\d{1,2})\b"
)
_TV_TOKEN_RE = re.compile(
    r"(?i)\bs\d{1,2}\s?e\d{1,3}(?:[\s._\-]?e\d{1,3})*\b"
    r"|\bs\d{1,2}\s?e?\d{0,3}\b|\b\d{1,2}x\d{1,3}\b")
_EP_TAG_RE = re.compile(
    r"(?i)(?:^|[\s._\-])(?:e|ep|episode)[\s._\-]?\d{1,3}(?:$|[\s._\-])"
)
# a quality tag at the end of a folder name: "Six Feet Under 720p"
_QUALITY_SUFFIX = re.compile(r"(?i)\s(?:2160p|1080p|720p|480p|4k)$")
# The marker that separates the series name from the episode: "S01E02",
# "1x02" or "Episode 5". Used to cut a name in two.
_EP_MARKER_RE = re.compile(
    r"(?i)(?:^|(?<=[\s._\-]))"
    r"(?:s\d{1,2}\s?e\d{1,3}(?:[\s._\-]?e\d{1,3})*|\d{1,2}x\d{1,3}"
    r"|episodes?[\s._\-]?\d{1,3}|ep?[\s._\-]?\d{1,3})"
    r"(?=$|[\s._\-])"
)

_TAG_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:2160p|1080p|720p|480p|[1-9]\d{2,3}[pi])\b",
        r"\b(?:blu-?ray|bdremux|remux|bdrip|brrip|dvdrip|web-?dl|webrip|hdtv|hdrip"
        r"|camrip|hdcam|telesync)\b",
        r"\b(?:hevc|x26[45]|h\.?26[45]|xvid|avc|vp9|10bit|8bit)\b",
        r"\b(?:hdr10?\+?|dolby.?vision|dv|hl[gs])\b",
        r"\b(?:aac(?:\s?\d(?:\.\d)?)?|ac3|eac3|dts(?:-?hd)?(?:-?ma)?|truehd|atmos"
        r"|opus|flac)\b",
        r"\b(?:proper|repack|extended|unrated|remastered|internal|complete)\b",
        # the source of a release: "Dexter 720p SoftSub DigiMovie"
        r"\b(?:soft-?sub|hard-?sub|subbed|multi-?sub|dual-?audio|"
        r"digi-?moviez?|yify|rarbg|amzn|web-?rip)\b",
    )
]


def tidy_title(raw: str) -> str:
    t = raw.replace("_", " ").replace(".", " ")
    t = re.sub(r"\s{2,}", " ", t).strip(" -_\t")
    return t


def strip_tv_tokens(text: str) -> str:
    # "_" would defeat the \b of the tokens, so it is spaced out first
    return _TV_TOKEN_RE.sub(" ", text.replace("_", " "))


def strip_release_tags(text: str) -> str:
    t = text
    for pat in _TAG_PATTERNS:
        t = pat.sub(" ", t)
    return t


def parse_title_and_year(text: str) -> tuple[str, int | None]:
    """Split a release name into (title, year).

    Handles "Inception (2010)", "The.Matrix.1999.BluRay" and
    "Some.Movie.2020.PROPER.1080p.WEB-DL.x264" alike. A year that only
    starts the name ("2001: A Space Odyssey") is left alone.
    """
    t = text.strip()
    year = None
    m = _PAREN_YEAR_RE.search(t)
    if m:
        year = int(m.group(1))
        t = t[: m.start()] + " " + t[m.end():]
    else:
        m = _TRAILING_YEAR_RE.search(t)
        if m:
            year = int(m.group(1))
            t = t[: m.start()]
    return tidy_title(t), year


def clean_for_search(title: str, year: int | None = None) -> str:
    original = tidy_title(title) or title
    t = title.replace("_", " ")
    t = re.sub(r"[\[\(\{][^\)\]\}]*[\)\]\}]", " ", t)
    t = t.replace(".", " ")
    for pat in _TAG_PATTERNS:
        t = pat.sub(" ", t)
    m = _YEAR_RE.search(t)
    if m and (year is None or int(m.group(1)) == year):
        t = t[: m.start()]
    t = re.sub(r"\s-\s?[^-]{1,40}$", " ", t)
    return tidy_title(t) or original


def has_episode_tokens(name: str) -> bool:
    clean = name.replace("_", ".")
    return bool(_SAE_RE.search(clean) or _XYX_RE.search(clean))


def has_season_folder(name: str) -> bool:
    return _SEASON_FOLDER_RE.match(name) is not None


def season_from_folder(name: str) -> int | None:
    m = _SEASON_FOLDER_RE.match(name)
    return int(m.group(1)) if m else None


def episode_numbers_all(name: str, inherited_season: int | None
                        ) -> "list[tuple[int | None, int | None]]":
    """Every episode number a file name carries.

    A file often holds several episodes: "Prison.Break.S04E01E02.mkv" is
    episodes 1 and 2 of season 4. Underscores count as separators, so
    "Peaky_Blinders_S04E01" reads the same as the spaced and dotted names.
    """
    # "_" sits next to words, so "\\b" would not fire in front of "S04"
    clean = name.replace("_", ".")
    m = _SAE_RE.search(clean)
    if m:
        season = int(m.group(1))
        numbers = [int(m.group(2))]
        tail = m.group(0)[m.end(2) - m.start():]
        numbers.extend(int(n) for n in re.findall(r"(?i)e(\d{1,3})", tail))
        # "S04E01E01" doubles are dropped - one file is one episode each
        return [(season, n) for n in dict.fromkeys(numbers)]
    m = _XYX_RE.search(clean)
    if m:
        return [(int(m.group(1)), int(m.group(2)))]
    stem = clean.rsplit(".", 1)[0]
    ep = None
    m = _EPNUM_RE.search(stem)
    if m:
        ep = int(m.group(1))
    else:
        m = _BARE_NUM_RE.match(stem)
        if m:
            ep = int(m.group(1))
        else:
            m = _LEAD_NUM_RE.match(stem)
            if m:
                ep = int(m.group(1))
    if ep is not None:
        return [(inherited_season, ep)]
    return []


def episode_numbers(
    name: str, inherited_season: int | None
) -> tuple[int | None, int | None]:
    pairs = episode_numbers_all(name, inherited_season)
    return pairs[0] if pairs else (None, None)


def episode_display_name(stem: str) -> str:
    t = strip_tv_tokens(stem)
    t = _EP_TAG_RE.sub(" ", t)
    # the number in front of the title is the episode number - it is shown
    # on its own, so it does not belong to the name
    t = _LEAD_NUM_RE.sub(" ", t)
    t = strip_release_tags(t)
    return tidy_title(t)


def episode_marker_span(name: str) -> tuple[int, int] | None:
    """Where the episode marker starts and ends in a name.

    "Show.S01E02.mkv" -> (5, 11), "Show 1x02.mkv" -> (5, 9),
    "Show.mkv" -> None. The part in front of the marker is the series name.
    """
    m = _EP_MARKER_RE.search(name)
    return (m.start(), m.end()) if m else None


def strip_quality_suffix(text: str) -> str:
    """Drop a trailing quality tag.

    A folder called "Six Feet Under 720p" is the series "Six Feet Under".
    """
    return _QUALITY_SUFFIX.sub("", text).strip()
