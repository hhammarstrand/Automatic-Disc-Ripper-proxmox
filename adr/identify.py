"""TMDb-based disc identification.

Parses the disc volume label into a search query, looks up the movie
on The Movie Database (TMDb), and returns structured metadata.
Uses title-similarity scoring and popularity weighting to pick the
best match instead of blindly trusting TMDb's result order.
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any

import requests

from adr.utils import extract_tmdb_year, parse_disc_label

logger = logging.getLogger(__name__)

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_DETAIL_URL = "https://api.themoviedb.org/3/movie"  # append /{tmdb_id}
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w300"
TMDB_IMAGE_BASE_SMALL = "https://image.tmdb.org/t/p/w200"
#: What every poster URL this application stores has to start with. The
#: dashboard renders these into an img src, and a URL from a request is a
#: request from every later page load.
TMDB_IMAGE_PREFIX = "https://image.tmdb.org/t/p/"

#: Episode stills are 16:9, not 2:3, so they get their own width. w185 is the
#: smallest size TMDb offers that still shows a face.
TMDB_STILL_BASE = "https://image.tmdb.org/t/p/w185"


# Minimum title similarity (0-1) required to trust a TMDb match for renaming.
# Below this, the file keeps the disc label name.
MIN_CONFIDENCE_FOR_RENAME = 0.85


class MovieInfo:
    """Metadata for an identified movie."""

    def __init__(
        self,
        title: str,
        year: int | None = None,
        tmdb_id: int | None = None,
        poster_url: str | None = None,
        overview: str | None = None,
        confidence: float = 0.0,
    ):
        self.title = title
        self.year = year
        self.tmdb_id = tmdb_id
        self.poster_url = poster_url
        self.overview = overview
        self.confidence = confidence  # 0.0-1.0, title similarity to disc label

    @property
    def high_confidence(self) -> bool:
        """True if the TMDb match is confident enough for file renaming."""
        return self.confidence >= MIN_CONFIDENCE_FOR_RENAME

    def __repr__(self) -> str:
        y = self.year or "?"
        return f"<MovieInfo '{self.title}' ({y}) tmdb={self.tmdb_id} conf={self.confidence:.2f}>"


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for comparison."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_similarity(query: str, candidate: str) -> float:
    """Return 0.0-1.0 similarity between the search query and a candidate title."""
    q = _normalise(query)
    c = _normalise(candidate)
    if q == c:
        return 1.0
    return SequenceMatcher(None, q, c).ratio()


def _score_result(result: dict[str, Any], query: str, year: int | None) -> float:
    """Score a TMDb result by title similarity, popularity, and year match.

    Higher is better. Typical range: 0 - ~200.
    """
    title = result.get("title", "")
    original_title = result.get("original_title", "")

    # Title similarity (0-1) — check both localised and original title
    sim_title = _title_similarity(query, title)
    sim_original = _title_similarity(query, original_title)
    best_sim = max(sim_title, sim_original)

    # Exact-ish match bonus
    exact_bonus = 30.0 if best_sim > 0.85 else 0.0

    # Popularity (TMDb popularity score, typically 0-500+; log-scale it)
    popularity = result.get("popularity", 0.0)

    # Vote count as quality signal — well-known movies have 1000+ votes
    vote_count = result.get("vote_count", 0)

    # Year match bonus
    year_bonus = 0.0
    release_date = result.get("release_date", "")
    result_year = extract_tmdb_year(release_date)
    if year and result_year:
        if result_year == year:
            year_bonus = 20.0
        elif abs(result_year - year) <= 1:
            year_bonus = 10.0

    # Combined score:
    #   title_similarity * 50  → max 50 (dominates when good match)
    #   exact_bonus            → +30 for near-exact title
    #   popularity * 0.05      → popular movies get a nudge
    #   vote_count * 0.005     → well-known movies get extra
    #   year_bonus             → +10-20 for year match
    score = (best_sim * 50.0) + exact_bonus + (popularity * 0.05) + (vote_count * 0.005) + year_bonus

    logger.debug(
        "  TMDb score: %.1f  sim=%.2f pop=%.0f votes=%d year_bonus=%.0f  '%s' (%s)",
        score, best_sim, popularity, vote_count, year_bonus, title, release_date[:4] if release_date else "?"
    )

    return score


def identify_disc(disc_label: str, api_key: str) -> MovieInfo:
    """Identify a disc by its volume label using TMDb.

    Falls back to parsing the label locally if the API key is missing
    or the search returns no results.

    Args:
        disc_label: The raw volume label from the disc (e.g. "THE_MATRIX_1999").
        api_key: TMDb API key (v3). If empty, uses local parsing only.

    Returns:
        MovieInfo with the best-matching title/year/poster.
    """
    parsed_title, parsed_year = parse_disc_label(disc_label)
    logger.info("Disc label: '%s' → parsed title: '%s', year: %s", disc_label, parsed_title, parsed_year)

    # Skip TMDb for empty/generic labels — "Unknown" would match a real movie
    if not disc_label.strip() or parsed_title.lower() == "unknown":
        logger.info("Empty/generic disc label — skipping TMDb lookup")
        return MovieInfo(title=parsed_title, year=parsed_year)

    if not api_key:
        logger.info("No TMDb API key configured – using parsed label: %s (%s)", parsed_title, parsed_year)
        return MovieInfo(title=parsed_title, year=parsed_year)

    try:
        # Search with year first for precision
        info = _search_tmdb(parsed_title, parsed_year, api_key)
        if info:
            logger.info("TMDb match: %s", info)
            return info
        # Retry without year in case it was wrong
        if parsed_year:
            info = _search_tmdb(parsed_title, None, api_key)
            if info:
                logger.info("TMDb match (no year filter): %s", info)
                return info
    except (requests.RequestException, ValueError, KeyError):
        logger.exception("TMDb lookup failed for '%s'", disc_label)

    logger.info("TMDb found no match – using parsed label: %s (%s)", parsed_title, parsed_year)
    return MovieInfo(title=parsed_title, year=parsed_year)


def _search_tmdb(query: str, year: int | None, api_key: str) -> MovieInfo | None:
    """Query TMDb search API and return the best-scored result."""
    params: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "include_adult": "false",
    }
    if year:
        params["year"] = str(year)

    logger.debug("TMDb search: query=%r year=%s", query, year)

    # Search in English
    params["language"] = "en-US"
    resp = requests.get(TMDB_SEARCH_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    results: list[dict[str, Any]] = data.get("results", [])

    # Also search in Swedish and merge results (dedup by tmdb id)
    params_sv = {**params, "language": "sv-SE"}
    try:
        resp_sv = requests.get(TMDB_SEARCH_URL, params=params_sv, timeout=10)
        resp_sv.raise_for_status()
        sv_results = resp_sv.json().get("results", [])
        seen_ids = {r.get("id") for r in results}
        for r in sv_results:
            if r.get("id") not in seen_ids:
                results.append(r)
    except (requests.RequestException, ValueError):
        logger.debug("Swedish TMDb search failed, continuing with English results only", exc_info=True)

    if not results:
        return None

    logger.debug("TMDb returned %d results for '%s', scoring...", len(results), query)

    # Score all results and pick the best
    scored = [(r, _score_result(r, query, year)) for r in results]
    scored.sort(key=lambda x: x[1], reverse=True)

    best, best_score = scored[0]

    # Safety: if best score is very low, the match is probably bad
    if best_score < 20.0:
        logger.warning("Best TMDb score is only %.1f — likely no good match for '%s'", best_score, query)
        return None

    best_title = best.get("title", query)
    original_title = best.get("original_title", "")
    release_date = best.get("release_date", "")
    movie_year = extract_tmdb_year(release_date, fallback=year)
    tmdb_id = best.get("id")
    poster_path = best.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    overview = best.get("overview")

    # Confidence = best title similarity between query and TMDb title/original_title
    confidence = max(
        _title_similarity(query, best_title),
        _title_similarity(query, original_title),
    )

    # Log runner-up for debugging
    if len(scored) > 1:
        runner_up, ru_score = scored[1]
        logger.debug(
            "Best: '%s' (%.1f) | Runner-up: '%s' (%.1f)",
            best_title, best_score, runner_up.get("title", "?"), ru_score
        )

    logger.info(
        "TMDb best match: '%s' (%s) confidence=%.2f (threshold=%.2f)",
        best_title, release_date[:4] if release_date else "?", confidence, MIN_CONFIDENCE_FOR_RENAME
    )

    return MovieInfo(
        title=best_title,
        year=movie_year,
        tmdb_id=tmdb_id,
        poster_url=poster_url,
        overview=overview,
        confidence=confidence,
    )


# ------------------------------------------------------------------ #
# Television
#
# TMDb keeps films and shows in separate namespaces with different field
# names — 'name'/'first_air_date' rather than 'title'/'release_date' — so a
# TV lookup cannot reuse the movie path, and using the movie endpoint for a
# show returns confident nonsense.
# ------------------------------------------------------------------ #

TMDB_TV_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
TMDB_TV_DETAIL_URL = "https://api.themoviedb.org/3/tv"  # append /{tmdb_id}


def search_series(query: str, api_key: str, limit: int = 8) -> list[dict[str, Any]]:
    """Search TMDb for shows matching *query*.

    Returns a list rather than a single best guess: naming a whole season from
    the wrong show is a worse outcome than one wrong film, so the user picks.
    """
    if not api_key or not (query or "").strip():
        return []

    results: list[dict[str, Any]] = []
    seen: set[Any] = set()
    # Both languages, as the film search already does. English alone cannot
    # find "Saltkråkan": TMDb answers in the language it was asked in, and the
    # show's English name is "Life on Seacrow Island" — nothing a Swedish
    # search term resembles. The original name is what connects them, so it is
    # carried through to the scoring and the list.
    for language in ("en-US", "sv-SE"):
        try:
            resp = requests.get(
                TMDB_TV_SEARCH_URL,
                params={
                    "api_key": api_key,
                    "query": query.strip(),
                    "include_adult": "false",
                    "language": language,
                },
                timeout=10,
            )
            resp.raise_for_status()
            for result in resp.json().get("results", []):
                if result.get("id") in seen:
                    continue
                seen.add(result.get("id"))
                results.append(result)
        except (requests.RequestException, ValueError, KeyError):
            logger.warning(
                "TMDb TV search failed for %r in %s", query, language, exc_info=True,
            )

    shows = []
    for result in results[:limit]:
        first_air = result.get("first_air_date") or ""
        name = result.get("name") or result.get("original_name") or ""
        original = result.get("original_name") or ""
        shows.append({
            "tmdb_id": result.get("id"),
            "name": name,
            "original_name": original,
            "year": int(first_air[:4]) if first_air[:4].isdigit() else None,
            "overview": (result.get("overview") or "")[:300],
            "poster_url": (
                f"{TMDB_IMAGE_BASE}{result['poster_path']}"
                if result.get("poster_path") else None
            ),
            "similarity": max(
                _title_similarity(query, name),
                _title_similarity(query, original),
            ),
        })
    shows = [s for s in shows if s["name"]]
    shows.sort(key=lambda s: s["similarity"], reverse=True)
    return shows


#: How close a show name has to be before the application will use it without
#: being asked. Deliberately high: naming a whole season from the wrong show is
#: a worse outcome than one wrong film, and the dialog is one click away.
SERIES_AUTO_CONFIDENCE = 0.75


def best_series(query: str, api_key: str) -> dict[str, Any] | None:
    """The one show a disc label clearly means, or None.

    The film search is the wrong namespace for a box set and returns a
    confident-looking film — so a detected series was named after whatever
    movie its label happened to resemble, and the only way to fix it was to
    open the dialog and search by hand. This is the same search that dialog
    runs, run for you.

    None unless the name is a close match. A season named after the wrong show
    is worse than one named after the disc label, which at least says where it
    came from.
    """
    best = next(iter(search_series(query, api_key, limit=8)), None)
    if best and best["similarity"] >= SERIES_AUTO_CONFIDENCE:
        return best
    if best:
        logger.info(
            "TMDb TV: best match for %r is %r at %.2f, below %.2f — not using it",
            query, best["name"], best["similarity"], SERIES_AUTO_CONFIDENCE,
        )
    return None


def get_season_episodes(tmdb_id: int, season: int, api_key: str) -> list[dict[str, Any]]:
    """Episode list for one season, so the UI can show real titles.

    Best-effort: without it the user still gets correctly numbered files, which
    is all Plex needs. Titles just make the mapping checkable by eye, which is
    how an off-by-one gets caught before forty minutes of encoding.
    """
    if not api_key or not tmdb_id:
        return []
    try:
        resp = requests.get(
            f"{TMDB_TV_DETAIL_URL}/{int(tmdb_id)}/season/{int(season)}",
            params={"api_key": api_key, "language": "en-US"},
            timeout=10,
        )
        resp.raise_for_status()
        episodes = resp.json().get("episodes", [])
    except (requests.RequestException, ValueError, KeyError):
        logger.debug("TMDb season lookup failed for %s S%s", tmdb_id, season, exc_info=True)
        return []

    return [
        {
            "episode_number": ep.get("episode_number"),
            "name": ep.get("name") or "",
            "air_date": ep.get("air_date") or "",
            # The frame TMDb picked for this episode. Already in the response
            # and thrown away until now: a title tells you which episode this
            # is meant to be, and a picture tells you whether it is — which is
            # the actual question when a box set starts at episode six.
            "still_url": (f"{TMDB_STILL_BASE}{ep['still_path']}"
                          if ep.get("still_path") else ""),
        }
        for ep in episodes
        if ep.get("episode_number") is not None
    ]
