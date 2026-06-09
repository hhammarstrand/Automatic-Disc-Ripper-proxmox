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
