"""Recognise a movie or series whose story is based on a true story.

Wikipedia keeps that answer in its categories: a film that dramatises real
events sits in "Films based on actual events" (and its sub-categories), a
film about a real person in "Biographical films". The MediaWiki API returns
the categories of fifty titles in one request, so a whole library is checked
in a few dozen requests - no API key, no search, nothing that gets
throttled the way a search does.

The metadata provider (Cinemeta) carries no such field: it has genres, a
plot and an IMDb id, but nothing that says "this was a true story".
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_API = "https://en.wikipedia.org/w/api.php"
_RAW = "https://en.wikipedia.org/w/index.php"
_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_SPARQL = "https://query.wikidata.org/sparql"
_UA = "CinemaLibrary/1.0 (personal film library; local desktop app)"

_MAX_TITLES = 50  # the API takes 50 titles per request
_SLICE = 25       # items per request, so the progress moves every few seconds
_QUOTE_BATCH = 20  # films per request when the quotes are looked up
_TIMEOUT = 30
_GAP = 3.0        # seconds between two api.php calls - Wikipedia throttles bursts
_QUOTE_GAP = 1.2  # wikiquote is a different wiki and tolerates more
_BACKOFF = (10.0, 20.0, 40.0)

_last_request = [0.0]
_last_quote = [0.0]

# the sections of a page that list things instead of quoting them
_LIST_SECTION = re.compile(
    r"character|cast|starring|episode|season|list of|appearance|production|"
    r"reference|external link|see also|note", re.IGNORECASE)

# the category families that mean "the story really happened"
_TRUE_STORY = re.compile(
    r"based on actual events|based on (a )?true stor|"
    r"(?<!semi-auto)\bbiographical\b (drama )?films?|biographical dramas?|"
    r"based on non-fiction books|based on (biographies|memoirs|diaries|letters)",
    re.IGNORECASE,
)
# a documentary is the true story itself, not a story based on one
_DOCUMENTARY = re.compile(r"documentary", re.IGNORECASE)
# something that is a film or a series, not a game, an album or a person
_IS_WORK = re.compile(
    r"\((film|movie|tv series|television series|miniseries|series)\)$|"
    r"\b(films?|television series|tv series|miniseries)\b",
    re.IGNORECASE,
)

# the article names a title can hide behind
_SUFFIXES = {
    "movie": ("(film)", "(movie)", "({year} film)"),
    "series": ("(TV series)", "(miniseries)", "(television series)",
               "({year} TV series)"),
}


@dataclass
class Match:
    """What the lookup found for one library item."""
    title: str
    is_true_story: bool = False
    article: str | None = None
    categories: list[str] = field(default_factory=list)


class Throttled(RuntimeError):
    """Wikimedia is throttling us - the request can be repeated later."""


def _get(url: str, data: bytes | None = None, tries: int = 4,
         gap: float = _GAP, tracker: list | None = None) -> dict:
    """GET a json document. A throttled answer is retried.

    Wikipedia blocks a burst of requests for a few minutes, so the calls
    are spread out and a 429 is waited out.
    """
    last: Exception | None = None
    clock = tracker if tracker is not None else _last_request
    for attempt in range(tries):
        if gap:
            wait = time.time() - clock[0]
            if wait < gap:
                time.sleep(gap - wait)
            clock[0] = time.time()
        try:
            request = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
        if attempt < len(_BACKOFF):
            time.sleep(_BACKOFF[attempt])
    if isinstance(last, urllib.error.HTTPError) and last.code == 429:
        raise Throttled(f"throttled by wikimedia: {last}")
    raise RuntimeError(f"request failed: {last}")


def _api(params: dict) -> dict:
    url = _API + "?" + urllib.parse.urlencode(params, safe="|")
    return _get(url)


def normalize(title: str) -> str:
    """'Argo (2012 film)' -> 'argo'"""
    text = re.sub(r"\s*\([^)]*\)\s*$", "", str(title or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def candidates(title: str, year: int | None = None,
               media_type: str | None = None) -> list[str]:
    """The article names a title can have - the plain one first."""
    title = str(title or "").strip()
    if not title:
        return []
    out = [title]
    suffixes = _SUFFIXES.get(media_type or "movie", _SUFFIXES["movie"])
    for suffix in suffixes:
        if "{year}" in suffix:
            if year:
                out.append(f"{title} {suffix.format(year=year)}")
        else:
            out.append(f"{title} {suffix}")
    return out


def classify(categories: list[str]) -> bool:
    """Do these categories say the story is based on a true story?"""
    for category in categories:
        name = category.replace("Category:", "")
        if _DOCUMENTARY.search(name):
            continue
        if _TRUE_STORY.search(name):
            return True
    return False


def _resolve(pages: dict, mapping: dict) -> dict:
    """Map every requested title to the page it ends up on."""
    by_title = {page.get("title", ""): page for page in pages.values()}
    resolved: dict[str, dict] = {}
    for title in mapping:
        final = mapping[title]
        if final in by_title:
            resolved[title] = by_title[final]
    return resolved


def _categories_of(page: dict) -> list[str]:
    return [c.get("title", "") for c in page.get("categories", [])]


def _page_year(page: dict) -> int | None:
    """The year in the article name - 'A Beautiful Mind (2001 film)'."""
    match = re.search(r"\(((?:19|20)\d{2})", str(page.get("title") or ""))
    return int(match.group(1)) if match else None


def _is_disambiguation(page: dict) -> bool:
    categories = _categories_of(page)
    if not categories:
        return True
    return any("disambiguation" in c.lower() for c in categories)


def _looks_like_work(page: dict) -> bool:
    """Is this page about a film or a series (and not a game or an album)?"""
    if re.search(r"\((film|movie|tv series|television series|miniseries)\)$",
                 str(page.get("title") or ""), re.IGNORECASE):
        return True
    return any(_IS_WORK.search(c) for c in _categories_of(page))


def _pick(pages: list[dict], title: str, year: int | None) -> dict | None:
    """The best article for an item - the work itself, and the right year."""
    usable = [p for p in pages if not _is_disambiguation(p)]
    if not usable:
        usable = pages
    if not usable:
        return None
    wanted = normalize(title)

    def score(page: dict) -> tuple:
        return (
            _looks_like_work(page),
            normalize(page.get("title", "")) == wanted,
            year is None or _page_year(page) in (None, year),
        )

    return max(usable, key=score)


def _fetch_pages(titles: list[str], progress=None) -> dict[str, dict]:
    """The categories and the Wikidata id of every title, in batches."""
    pages: dict[str, dict] = {}
    for start in range(0, len(titles), _MAX_TITLES):
        chunk = titles[start:start + _MAX_TITLES]
        data = _api({
            "action": "query", "prop": "categories|pageprops",
            "titles": "|".join(chunk), "cllimit": "max", "redirects": "1",
            "format": "json",
        })
        query = data.get("query", {})
        mapping = {c: c for c in chunk}
        for entry in query.get("normalized", []):
            mapping[entry["from"]] = entry["to"]
        for entry in query.get("redirects", []):
            mapping[entry["from"]] = entry["to"]
        pages.update(_resolve(query.get("pages", {}), mapping))
        if progress:
            progress(min(start + _MAX_TITLES, len(titles)), len(titles))
    return pages


def check(items: list[dict], progress=None, on_result=None,
         should_stop=None) -> list[Match]:
    """Check a list of library items for a true story.

    `items` are dicts with "title", "year" and "media_type". The result
    holds one Match per item, in the same order.

    `progress(done, total, item)` is called after every slice, so the app
    can show what is going on; `on_result(index, match)` is called for
    every item as soon as it is known, so the verdicts are saved even if
    the run is interrupted.
    """
    matches = [Match(title=str(i.get("title") or "")) for i in items]
    usable = [(index, i, str(i.get("title") or "").strip())
              for index, i in enumerate(items)
              if str(i.get("title") or "").strip()]
    if not usable:
        return matches

    for start in range(0, len(usable), _SLICE):
        if should_stop is not None and should_stop():
            break
        slice_ = usable[start:start + _SLICE]
        pages = _fetch_pages([t for _, _, t in slice_])

        # the titles that turned out to be a disambiguation page get a
        # second try with the names a film can have
        retry: list[str] = []
        for _, item, title in slice_:
            page = pages.get(title)
            if page is None or _is_disambiguation(page):
                retry.extend(candidates(title, item.get("year"),
                                        item.get("media_type")))
        retry = [t for t in dict.fromkeys(retry) if t not in pages]
        if retry:
            pages.update(_fetch_pages(retry))

        for index, item, title in slice_:
            found = [pages[t] for t in candidates(title, item.get("year"),
                                                 item.get("media_type"))
                     if t in pages]
            page = _pick(found, title, item.get("year"))
            match = matches[index]
            if page is not None:
                match.article = page.get("title")
                match.categories = _categories_of(page)
                match.is_true_story = classify(match.categories)
            if on_result is not None:
                on_result(index, match)
        if progress:
            progress(min(start + _SLICE, len(usable)), len(usable), slice_[-1][1])
    return matches

def article_info(title: str) -> tuple[str | None, str | None]:
    """The wikitext and the Wikidata id of an article, in one request."""
    if not title:
        return None, None
    data = _api({
        "action": "query", "prop": "revisions|pageprops", "titles": title,
        "rvprop": "content", "rvslots": "main", "redirects": "1",
        "format": "json",
    })
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            continue
        revisions = page.get("revisions") or []
        text = None
        if revisions:
            main = revisions[0].get("slots", {}).get("main", {})
            # the old api format calls the text "*", the new one "content"
            text = main.get("content") or main.get("*")
        return text, page.get("pageprops", {}).get("wikibase_item")
    return None, None


def stories(items: list[dict], progress=None) -> dict[int, dict]:
    """The whole story of many films, in three requests per batch.

    `items` are dicts with "id" and "true_story_article" (the Wikipedia
    article of the film). The result holds, per item, the lead of the film,
    the lead of the work it is based on, and the places, the people and the
    awards from Wikidata.
    """
    articles: dict[int, str] = {}
    for item in items:
        article = str(item.get("true_story_article") or "").strip()
        if article:
            articles[int(item["id"])] = article
    out: dict[int, dict] = {}
    ids = list(articles)
    for start in range(0, len(ids), 20):
        chunk = ids[start:start + 20]
        pages = _pages([articles[i] for i in chunk])
        sources: dict[int, str] = {}
        for media_id in chunk:
            text = pages.get(articles[media_id], ("", ""))[0]
            source = based_on(text)
            if source:
                sources[media_id] = source
        source_pages = _pages(list(dict.fromkeys(sources.values())))
        qids = [pages[articles[i]][1] for i in chunk
                if pages.get(articles[i]) and pages[articles[i]][1]]
        extras = _details(list(dict.fromkeys(qids)))
        for media_id in chunk:
            text, qid = pages.get(articles[media_id], ("", ""))
            source = sources.get(media_id)
            real = None
            if source and source_pages.get(source):
                real = _lead(source_pages[source][0])
            extra = extras.get(qid, {})
            out[media_id] = {
                "movie": _lead(text),
                "real": real,
                "source": source,
                "places": extra.get("places", []),
                "people": extra.get("people", []),
                "awards": extra.get("awards", []),
            }
        if progress:
            progress(min(start + 20, len(ids)), len(ids))
    return out


def _pages(titles: list[str]) -> dict[str, tuple[str, str]]:
    """The wikitext and the Wikidata id of many pages, in one request."""
    if not titles:
        return {}
    url = (_API + "?" + urllib.parse.urlencode({
        "action": "query", "prop": "revisions|pageprops", "titles": "|".join(titles),
        "rvprop": "content", "rvslots": "main", "redirects": "1", "format": "json",
    }))
    data = _get(url)
    query = data.get("query", {})
    mapping = {t: t for t in titles}
    for entry in query.get("normalized", []):
        mapping[entry["from"]] = entry["to"]
    for entry in query.get("redirects", []):
        mapping[entry["from"]] = entry["to"]
    by_title: dict[str, tuple[str, str]] = {}
    for page in query.get("pages", {}).values():
        revisions = page.get("revisions") or []
        text = ""
        if revisions:
            main = revisions[0].get("slots", {}).get("main", {})
            text = main.get("content") or main.get("*") or ""
        by_title[page.get("title", "")] = (text, page.get("pageprops", {}).get(
            "wikibase_item") or "")
    out: dict[str, tuple[str, str]] = {}
    for title in titles:
        found = by_title.get(mapping.get(title, title))
        if found:
            out[title] = found
    return out


def _lead(wikitext: str) -> str | None:
    """The introduction of a page, without the wiki markup."""
    text = (wikitext or "").split("\n==")[0]
    text = re.sub(r"\[\[File:[^\]]*\]\]", " ", text)
    text = _strip_templates(text)
    text = re.sub(r"\[\[([^\]|]+)\|([^\]]*)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _strip_templates(text: str) -> str:
    """Remove the {{templates}} of a page, nested ones included."""
    out = text
    for _ in range(6):
        new = re.sub(r"\{\{[^{}]*\}\}", " ", out)
        if new == out:
            break
        out = new
    return out


def details(qid: str | None) -> dict[str, list[tuple[str, str]]]:
    """The places, the people and the awards of a film.

    Every entry is (name, the Wikipedia article of the name).
    """
    if not qid:
        return {}
    return _details([qid]).get(qid, {})


def _details(qids: list[str]) -> dict[str, dict[str, list[tuple[str, str]]]]:
    """The places, the people and the awards of many films."""
    out: dict[str, dict[str, list[tuple[str, str]]]] = {}
    qids = [q for q in dict.fromkeys(qids) if q]
    if not qids:
        return out
    for start in range(0, len(qids), 40):
        chunk = qids[start:start + 40]
        values = " ".join("wd:" + q for q in chunk)
        query = (
            "SELECT ?film ?prop ?item ?name ?article WHERE {"
            "  VALUES ?film { %s }"
            "  VALUES ?prop { wdt:P915 wdt:P57 wdt:P58 wdt:P161 wdt:P166 }"
            "  ?film ?prop ?item ."
            "  ?article schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ."
            "  ?item rdfs:label ?name . FILTER(LANG(?name) = 'en')"
            "}" % values
        )
        url = _SPARQL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        try:
            data = _get(url, gap=0.0)
        except Exception as exc:
            log.warning("wikidata lookup failed: %s", exc)
            return out
        for row in data.get("results", {}).get("bindings", []):
            film = row["film"]["value"].split("/")[-1]
            prop = row["prop"]["value"].rsplit("/", 1)[-1]
            key = {"P915": "places", "P57": "people", "P58": "people",
                   "P161": "people", "P166": "awards"}[prop]
            name = row["name"]["value"]
            title = urllib.parse.unquote(
                row["article"]["value"].rsplit("/wiki/", 1)[-1])
            entries = out.setdefault(film, {}).setdefault(key, [])
            if not any(entry[0] == name for entry in entries):
                entries.append((name, title))
    return out


def quotes_for(items: list[dict], limit: int = 6) -> dict[int, list[tuple[str, str]]]:
    """The quotes of many films, in as few requests as possible.

    `items` are dicts with "id", "title", "year" and "media_type". The
    result maps the id of an item to its quotes. Twenty films are asked
    for in one request - a request per film gets throttled.
    """
    wanted: dict[int, list[str]] = {}
    for item in items:
        names = _quote_names(item)
        if names:
            wanted[int(item["id"])] = names
    out: dict[int, list[tuple[str, str]]] = {}
    ids = list(wanted)
    for start in range(0, len(ids), 6):
        chunk = ids[start:start + 6]
        titles: list[str] = []
        for media_id in chunk:
            titles.extend(wanted[media_id])
        texts = _wikitexts(titles)
        for media_id in chunk:
            text = None
            for name in wanted[media_id]:
                if name in texts:
                    text = texts[name]
                    break
            out[media_id] = _parse_quotes(text, limit) if text else []
    return out


def _quote_names(item: dict) -> list[str]:
    """The names a film can have on Wikiquote - the plain one first."""
    title = str(item.get("title") or "").strip()
    if not title:
        return []
    year = item.get("year")
    names = [title]
    # the wikipedia article of the film is the name wikiquote uses
    article = str(item.get("true_story_article") or "").strip()
    if article and article not in names:
        names.append(article)
    if item.get("media_type") == "series":
        names += [f"{title} (TV series)", f"{title} (miniseries)"]
    else:
        names.append(f"{title} (film)")
        if year:
            names.append(f"{title} ({year} film)")
    return names


def _wikitexts(titles: list[str]) -> dict[str, str]:
    """The wikitext of many pages in one request."""
    if not titles:
        return {}
    url = ("https://en.wikiquote.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "prop": "revisions", "titles": "|".join(titles),
        "rvprop": "content", "rvslots": "main", "redirects": "1",
        "format": "json",
    }))
    data = _get(url, gap=_QUOTE_GAP, tracker=_last_quote)
    query = data.get("query", {})
    mapping = {t: t for t in titles}
    for entry in query.get("normalized", []):
        mapping[entry["from"]] = entry["to"]
    for entry in query.get("redirects", []):
        mapping[entry["from"]] = entry["to"]
    by_title: dict[str, str] = {}
    for page in query.get("pages", {}).values():
        revisions = page.get("revisions") or []
        if not revisions:
            continue
        main = revisions[0].get("slots", {}).get("main", {})
        text = main.get("content") or main.get("*")
        if text:
            by_title[page.get("title", "")] = text
    out: dict[str, str] = {}
    for title in titles:
        text = by_title.get(mapping.get(title, title))
        if text:
            out[title] = text
    return out


def _parse_quotes(text: str, limit: int) -> list[tuple[str, str]]:
    """Pull the quotes out of a Wikiquote page.

    A film page lists the quotes of every character in a section of its
    own; a series page writes them as dialogue, line by line. Both are
    read here - and the lists of characters and episodes, which are not
    quotes, are left alone.
    """
    found: list[tuple[str, str]] = []
    speaker = ""
    in_section = False
    # the sources of a quote are in <ref> tags - they are no quote
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text or "", flags=re.DOTALL)
    text = re.sub(r"<ref[^>]*/>", "", text)
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("=="):
            section = line.strip("= ").strip()
            speaker = "" if len(section) > 40 else section
            in_section = True
            if _LIST_SECTION.search(section):
                speaker = ""
                continue  # a list of characters, not quotes
            continue
        if not in_section:
            continue  # the introduction of the page is no quote
        if not line.startswith(("*", ":")):
            continue
        line = re.sub(r"<[^>]+>", "", line)     # the html of the series pages
        line = line.lstrip("*:").strip()
        if not line or line.startswith("{"):
            continue
        if re.fullmatch(r"(\[\[[^\]]+\]\]\s*,?\s*)+", line):
            continue  # only a name - a character, not a quote
        match = re.match(
            r"^(?:'''(.*?)'''|\[\[([^\]|]+)(?:\|[^\]]*)?\]\])\s*:?\s*(.+)$", line)
        if match:
            name = (match.group(1) or match.group(2) or "").strip().rstrip(":")
            body = match.group(3).strip()
            if name and body:
                speaker = name
                line = body
        line = re.sub(r"\[\[([^\]|]+)\|([^\]]*)\]\]", r"\2", line)
        line = re.sub(r"\[\[([^\]]+)\]\]", r"\1", line)
        line = line.replace("'''", "").replace("''", "")
        line = re.sub(r"\{\{[^{}]*\}\}", "", line).strip()
        if re.fullmatch(r"\[[^\]]+\]", line):
            continue  # a note about the quote, not the quote
        if len(line) < 20 or len(line.split()) < 4:
            continue
        found.append((line, speaker))
        if len(found) >= limit:
            break
    return found


def based_on(wikitext: str | None) -> str | None:
    """The work or the person a film is based on - from its infobox."""
    if not wikitext:
        return None
    return _based_on(wikitext)


def _based_on(wikitext: str) -> str | None:
    """The first link of the 'based on' field of an infobox."""
    lines = wikitext.splitlines()
    for index, line in enumerate(lines):
        if not re.match(r"\s*\|\s*based_on\s*=", line, re.IGNORECASE):
            continue
        block = [line]
        for follow in lines[index + 1:index + 8]:
            if re.match(r"\s*\|", follow):
                break
            block.append(follow)
        link = re.search(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", "\n".join(block))
        return link.group(1).strip() if link else None
    return None


def summary(title: str) -> str | None:
    """The Wikipedia lead paragraph of an article."""
    if not title:
        return None
    url = _SUMMARY + urllib.parse.quote(str(title).replace(" ", "_"))
    try:
        data = _get(url, gap=0.0)
    except Exception as exc:
        log.warning("wikipedia summary failed for %s: %s", title, exc)
        return None
    return data.get("extract")


def article_url(title: str) -> str | None:
    if not title:
        return None
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(
        str(title).replace(" ", "_"))
