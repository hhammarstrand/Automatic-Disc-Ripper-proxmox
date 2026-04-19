"""Tests for adr.identify — TMDb lookup and scoring helpers."""

import pytest
from unittest.mock import patch, MagicMock

from adr.identify import (
    _normalise,
    _title_similarity,
    _score_result,
    MovieInfo,
    MIN_CONFIDENCE_FOR_RENAME,
    identify_disc,
)


# ------------------------------------------------------------------ #
# _normalise
# ------------------------------------------------------------------ #

class TestNormalise:
    def test_lowercases(self):
        assert _normalise("THE MATRIX") == "the matrix"

    def test_strips_punctuation(self):
        assert _normalise("Hello, World!") == "hello world"

    def test_collapses_whitespace(self):
        assert _normalise("too   many   spaces") == "too many spaces"

    def test_empty_string(self):
        assert _normalise("") == ""


# ------------------------------------------------------------------ #
# _title_similarity
# ------------------------------------------------------------------ #

class TestTitleSimilarity:
    def test_identical_strings(self):
        assert _title_similarity("The Matrix", "The Matrix") == 1.0

    def test_case_insensitive_match(self):
        assert _title_similarity("the matrix", "THE MATRIX") == 1.0

    def test_completely_different(self):
        sim = _title_similarity("ABCDEF", "ZYXWVU")
        assert sim < 0.3

    def test_partial_match(self):
        sim = _title_similarity("The Matrix", "The Matrix Reloaded")
        assert 0.5 < sim < 1.0


# ------------------------------------------------------------------ #
# _score_result
# ------------------------------------------------------------------ #

class TestScoreResult:
    def _make_result(self, title="The Matrix", year="1999-03-31", popularity=100, votes=5000):
        return {
            "title": title,
            "original_title": title,
            "release_date": year,
            "popularity": popularity,
            "vote_count": votes,
        }

    def test_exact_match_scores_high(self):
        result = self._make_result()
        score = _score_result(result, "The Matrix", 1999)
        # Exact match should score well above 50
        assert score > 80

    def test_year_match_bonus(self):
        result = self._make_result()
        score_with_year = _score_result(result, "The Matrix", 1999)
        score_no_year = _score_result(result, "The Matrix", None)
        assert score_with_year > score_no_year

    def test_wrong_year_no_bonus(self):
        result = self._make_result(year="1999-03-31")
        score = _score_result(result, "The Matrix", 2020)
        score_correct = _score_result(result, "The Matrix", 1999)
        assert score_correct > score

    def test_popularity_affects_score(self):
        popular = self._make_result(popularity=500, votes=10000)
        obscure = self._make_result(popularity=1, votes=10)
        score_popular = _score_result(popular, "The Matrix", 1999)
        score_obscure = _score_result(obscure, "The Matrix", 1999)
        assert score_popular > score_obscure

    def test_bad_title_match_scores_low(self):
        result = self._make_result(title="Completely Different Movie")
        score = _score_result(result, "The Matrix", 1999)
        # Even with year match, a bad title should score lower
        assert score < 80


# ------------------------------------------------------------------ #
# MovieInfo
# ------------------------------------------------------------------ #

class TestMovieInfo:
    def test_high_confidence_true(self):
        info = MovieInfo(title="Test", confidence=0.95)
        assert info.high_confidence is True

    def test_high_confidence_false(self):
        info = MovieInfo(title="Test", confidence=0.5)
        assert info.high_confidence is False

    def test_high_confidence_threshold(self):
        info = MovieInfo(title="Test", confidence=MIN_CONFIDENCE_FOR_RENAME)
        assert info.high_confidence is True

    def test_repr(self):
        info = MovieInfo(title="The Matrix", year=1999, tmdb_id=603, confidence=0.95)
        r = repr(info)
        assert "The Matrix" in r
        assert "1999" in r
        assert "603" in r

    def test_repr_no_year(self):
        info = MovieInfo(title="Unknown")
        r = repr(info)
        assert "?" in r


# ------------------------------------------------------------------ #
# identify_disc
# ------------------------------------------------------------------ #

class TestIdentifyDisc:
    def test_empty_label_skips_tmdb(self):
        result = identify_disc("", "fake_key")
        assert result.title == "Unknown"
        assert result.year is None

    def test_no_api_key_uses_parsed_label(self):
        result = identify_disc("THE_MATRIX_1999", "")
        assert result.title == "The Matrix"
        assert result.year == 1999

    def test_generic_unknown_label_skips_tmdb(self):
        # parse_disc_label("_") returns ("Unknown", None)
        # Since it's "unknown", identify_disc should skip TMDb
        result = identify_disc("_", "fake_key")
        assert result.title == "Unknown"

    @patch("adr.identify._search_tmdb")
    def test_tmdb_match_returned(self, mock_search):
        mock_search.return_value = MovieInfo(
            title="The Matrix", year=1999, tmdb_id=603, confidence=0.95
        )
        result = identify_disc("THE_MATRIX_1999", "fake_key")
        assert result.title == "The Matrix"
        assert result.tmdb_id == 603

    @patch("adr.identify._search_tmdb")
    def test_tmdb_no_match_falls_back(self, mock_search):
        mock_search.return_value = None
        result = identify_disc("THE_MATRIX_1999", "fake_key")
        # Should fall back to parsed label
        assert result.title == "The Matrix"
        assert result.year == 1999
        assert result.tmdb_id is None

    @patch("adr.identify._search_tmdb")
    def test_tmdb_retry_without_year(self, mock_search):
        """If first search (with year) returns None, retries without year."""
        mock_search.side_effect = [None, MovieInfo(title="Matrix", year=1999, tmdb_id=603)]
        result = identify_disc("THE_MATRIX_1999", "fake_key")
        assert mock_search.call_count == 2
        # Second call should have year=None
        assert mock_search.call_args_list[1][0][1] is None
        assert result.tmdb_id == 603

    @patch("adr.identify._search_tmdb")
    def test_tmdb_exception_falls_back(self, mock_search):
        import requests
        mock_search.side_effect = requests.RequestException("timeout")
        result = identify_disc("THE_MATRIX_1999", "fake_key")
        assert result.title == "The Matrix"
        assert result.tmdb_id is None
