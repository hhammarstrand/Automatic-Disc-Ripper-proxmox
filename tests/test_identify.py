"""Tests for adr.identify — TMDb lookup and scoring helpers."""

from unittest.mock import patch

from adr.identify import (
    MIN_CONFIDENCE_FOR_RENAME,
    MovieInfo,
    _normalise,
    _score_result,
    _title_similarity,
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


class TestNamingTheShowWithoutBeingAsked:
    """A detected series was named after whatever *film* its disc label
    resembled, because identification runs TMDb's movie search and nothing ran
    the TV one unless a person clicked the button.

    Saltkråkan is the case that shows why English alone was never going to
    work: TMDb answers in the language it is asked in, and the show's English
    name is "Life on Seacrow Island" — which a Swedish search term resembles
    not at all. The original name is what connects them.
    """

    SEACROW_EN = {
        "id": 42, "name": "Life on Seacrow Island",
        "original_name": "Vi på Saltkråkan", "first_air_date": "1964-02-01",
        "overview": "", "poster_path": "/p.jpg",
    }

    def _responses(self, monkeypatch, by_language):
        """Stub TMDb, answering differently per language like the real one."""
        from adr import identify

        class Reply:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return {"results": self._payload}

        calls = []

        def fake_get(url, params=None, timeout=None):
            params = params or {}
            calls.append(params.get("language"))
            return Reply(by_language.get(params.get("language"), []))

        monkeypatch.setattr(identify.requests, "get", fake_get)
        return calls

    def test_it_searches_swedish_as_well_as_english(self, monkeypatch):
        from adr import identify

        calls = self._responses(monkeypatch, {"sv-SE": [self.SEACROW_EN]})
        results = identify.search_series("Saltkråkan", "key")
        assert "sv-SE" in calls, "English only cannot find a Swedish show"
        assert results and results[0]["name"] == "Life on Seacrow Island"

    def test_the_original_name_is_what_matches(self, monkeypatch):
        """The query resembles the original name, not the English one."""
        from adr import identify

        self._responses(monkeypatch, {"en-US": [self.SEACROW_EN]})
        results = identify.search_series("Saltkråkan", "key")
        assert results[0]["original_name"] == "Vi på Saltkråkan"
        assert results[0]["similarity"] > 0.5

    def test_a_show_is_not_listed_twice_when_both_languages_return_it(
        self, monkeypatch,
    ):
        from adr import identify

        self._responses(
            monkeypatch, {"en-US": [self.SEACROW_EN], "sv-SE": [self.SEACROW_EN]})
        assert len(identify.search_series("Saltkråkan", "key")) == 1

    def test_the_best_match_is_used_without_asking(self, monkeypatch):
        from adr import identify

        self._responses(monkeypatch, {"sv-SE": [self.SEACROW_EN]})
        best = identify.best_series("Vi på Saltkråkan", "key")
        assert best["tmdb_id"] == 42
        assert best["year"] == 1964

    def test_a_poor_match_is_refused(self, monkeypatch):
        """Naming a whole season from the wrong show is worse than naming it
        from the disc label, which at least says where it came from."""
        from adr import identify

        self._responses(monkeypatch, {"en-US": [{
            "id": 9, "name": "Something Else Entirely",
            "original_name": "Something Else Entirely",
            "first_air_date": "2011-01-01", "overview": "", "poster_path": None,
        }]})
        assert identify.best_series("Saltkråkan", "key") is None

    def test_no_api_key_asks_nothing(self):
        from adr import identify

        assert identify.search_series("Saltkråkan", "") == []
        assert identify.best_series("Saltkråkan", "") is None

    def test_a_failing_search_is_not_a_crash(self, monkeypatch):
        import requests

        from adr import identify

        def boom(*a, **k):
            raise requests.RequestException("TMDb is down")

        monkeypatch.setattr(identify.requests, "get", boom)
        assert identify.search_series("Saltkråkan", "key") == []
        assert identify.best_series("Saltkråkan", "key") is None


class TestTheDetectedSeriesGetsItsName:
    """The pipeline half: what actually lands on the job."""

    def _job(self, title="Some Unrelated Film", year=1999):
        import types

        return types.SimpleNamespace(
            title=title, year=year, tmdb_id=123, poster_url="http://poster",
        )

    def _config(self):
        import types

        return types.SimpleNamespace(tmdb_api_key="key")

    def test_a_tmdb_match_replaces_the_film_the_label_resembled(self, monkeypatch):
        from adr import pipeline

        monkeypatch.setattr(
            pipeline, "_name_the_show", pipeline._name_the_show)  # real one
        monkeypatch.setattr(
            "adr.identify.best_series",
            lambda q, key: {"tmdb_id": 42, "name": "Life on Seacrow Island",
                            "year": 1964, "poster_url": "http://p"},
        )
        job = self._job()
        said = pipeline._name_the_show(job, "Saltkråkan", self._config())
        assert job.title == "Life on Seacrow Island"
        assert job.year == 1964
        assert job.tmdb_id == 42
        assert "Life on Seacrow Island" in said

    def test_without_a_match_the_label_beats_the_film(self, monkeypatch):
        """A box set is not the film its label resembles, so the film title is
        dropped either way."""
        from adr import pipeline

        monkeypatch.setattr("adr.identify.best_series", lambda q, key: None)
        job = self._job()
        said = pipeline._name_the_show(job, "Saltkråkan", self._config())
        assert job.title == "Saltkråkan"
        assert job.tmdb_id is None
        assert job.poster_url is None
        assert "disc label is used" in said

    def test_a_label_with_no_show_name_changes_nothing(self, monkeypatch):
        from adr import pipeline

        job = self._job()
        assert pipeline._name_the_show(job, "", self._config()) == ""
        assert job.title == "Some Unrelated Film"

    def test_tmdb_falling_over_does_not_fail_the_rip(self, monkeypatch):
        from adr import pipeline

        def boom(query, key):
            raise RuntimeError("TMDb exploded")

        monkeypatch.setattr("adr.identify.best_series", boom)
        job = self._job()
        said = pipeline._name_the_show(job, "Saltkråkan", self._config())
        assert job.title == "Saltkråkan"
        assert said
