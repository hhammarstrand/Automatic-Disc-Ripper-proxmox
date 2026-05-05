"""TMDb-based disc identification.

Parses the disc volume label into a search query, looks up the movie
on The Movie Database (TMDb), and returns structured metadata.
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any

import requests

from adr.utils import parse_disc_label, extract_tmdb_year

logger = logging.getLogger(__name__)

TMDB_SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TMDB_DETAIL_URL = "https://api.themoviedb.org/3/movie"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w300"
TMDB_IMAGE_BASE_SMALL = "https://image.tmdb.org/t/p/w200"

MIN_CONFIDENCE_FOR_RENAME = 0.85


class MovieInfo:
    def __init__(self, title, year=None, tmdb_id=None, poster_url=None, overview=None, confidence=0.0):
        self.title = title
        self.year = year
        self.tmdb_id = tmdb_id
        self.poster_url = poster_url
        self.overview = overview
        self.confidence = confidence

    @property
    def high_confidence(self) -> bool:
        return self.confidence >= MIN_CONFIDENCE_FOR_RENAME

    def __repr__(self):
        y = self.year or "?"
        return f"<MovieInfo '{self.title}' ({y}) tmdb={self.tmdb_id} conf={self.confidence:.2f}>"


def _normalise(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_similarity(query: str, candidate: str) -> float:
    q = _normalise(query)
    c = _normalise(candidate)
    if q == c:
        return 1.0
    return SequenceMatcher(None, q, c).ratio()


def _score_result(result, query, year):
    title = result.get("title", "")
    original_title = result.get("original_title", "")
    sim_title = _title_similarity(query, title)
    sim_original = _title_similarity(query, original_title)
    best_sim = max(sim_title, sim_original)
    exact_bonus = 30.0 if best_sim > 0.85 else 0.0
    popularity = result.get("popularity", 0.0)
    vote_count = result.get("vote_count", 0)
    year_bonus = 0.0
    release_date = result.get("release_date", "")
    result_year = extract_tmdb_year(release_date)
    if year and result_year:
        if result_year == year:
            year_bonus = 20.0
        elif abs(result_year - year) <= 1:
            year_bonus = 10.0
    score = (best_sim * 50.0) + exact_bonus + (popularity * 0.05) + (vote_count * 0.005) + year_bonus
    logger.debug(
        "  TMDb score: %.1f  sim=%.2f pop=%.0f votes=%d year_bonus=%.0f  '%s' (%s)",
        score, best_sim, popularity, vote_count, year_bonus, title, release_date[:4] if release_date else "?"
    )
    return score


def identify_disc(disc_label: str, api_key: str) -> MovieInfo:
    parsed_title, parsed_year = parse_disc_label(disc_label)
    logger.info("Disc label: '%s' → parsed title: '%s', year: %s", disc_label, parsed_title, parsed_year)
    if not disc_label.strip() or parsed_title.lower() == "unknown":
        logger.info("Empty/generic disc label — skipping TMDb lookup")
        return MovieInfo(title=parsed_title, year=parsed_year)
    if not api_key:
        logger.info("No TMDb API key configured – using parsed label: %s (%s)", parsed_title, parsed_year)
        return MovieInfo(title=parsed_title, year=parsed_year)
    try:
        info = _search_tmdb(parsed_title, parsed_year, api_key)
        if info:
            logger.info("TMDb match: %s", info)
            return info
        if parsed_year:
            info = _search_tmdb(parsed_title, None, api_key)
            if info:
                logger.info("TMDb match (no year filter): %s", info)
                return info
    except (requests.RequestException, ValueError, KeyError):
        logger.exception("TMDb lookup failed for '%s'", disc_label)
    logger.info("TMDb found no match – using parsed label: %s (%s)", parsed_title, parsed_year)
    return MovieInfo(title=parsed_title, year=parsed_year)


def _search_tmdb(query, year, api_key):
    params: dict[str, Any] = {
        "api_key": api_key,
        "query": query,
        "include_adult": "false",
    }
    if year:
        params["year"] = str(year)
    logger.debug("TMDb search: query=%r year=%s", query, year)
    params["language"] = "en-US"
    resp = requests.get(TMDB_SEARCH_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    results: list[dict[str, Any]] = data.get("results", [])
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
    scored = [(r, _score_result(r, query, year)) for r in results]
    scored.sort(key=lambda x: x[1], reverse=True)
    best, best_score = scored[0]
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
    confidence = max(
        _title_similarity(query, best_title),
        _title_similarity(query, original_title),
    )
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
