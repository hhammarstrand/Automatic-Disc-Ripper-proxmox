"""Tests for television support: recognising TV discs and naming episodes.

Two failure modes are worth more than the rest. Calling a film a series renames
it into a season folder, which is annoying to undo — so detection must be
conservative and never automatic. And an off-by-one in episode numbering
silently mislabels a whole season, which Plex then happily displays as the
wrong episodes.
"""

import types

import pytest

from adr import series
from adr.naming import folder_depth, plan_output, relative_folder


def _titles(*durations: str) -> dict[int, dict]:
    """MakeMKV-style title_info keyed by index."""
    return {i: {"duration": d} for i, d in enumerate(durations)}


class TestEpisodeCandidates:
    def test_six_episodes_of_similar_length_are_found(self):
        found = series.episode_candidates(_titles(*(["0:42:15"] * 6)))
        assert found == [0, 1, 2, 3, 4, 5]

    def test_a_film_with_extras_is_not_a_series(self):
        """One long feature and some short featurettes."""
        assert series.episode_candidates(_titles("2:16:00", "0:04:30", "0:07:12")) == []

    def test_two_similar_titles_are_not_enough(self):
        """A double feature is not a season."""
        assert series.episode_candidates(_titles("0:44:00", "0:43:30")) == []

    def test_the_play_all_track_is_excluded(self):
        """A 4-hour play-all sits beside the episodes and must not join them."""
        found = series.episode_candidates(_titles("0:42:00", "0:41:30", "0:42:30", "2:46:00"))
        assert found == [0, 1, 2]

    def test_menu_loops_are_excluded(self):
        found = series.episode_candidates(
            _titles("0:00:30", "0:00:12", "0:42:00", "0:41:00", "0:43:00")
        )
        assert found == [2, 3, 4]

    def test_the_largest_similar_group_wins(self):
        """Four episodes beside three featurettes: the episodes are the answer."""
        found = series.episode_candidates(
            _titles("0:22:00", "0:21:30", "0:22:30", "0:21:45", "0:16:00", "0:16:30", "0:15:50")
        )
        assert found == [0, 1, 2, 3]

    def test_a_featurette_cannot_bridge_into_the_episode_group(self):
        """The bug a pivot-relative window has.

        22:00/21:30/22:30/21:45 are episodes; 16:00/16:30/15:50 are extras. A
        window centred on the shortest episode reaches far enough down to
        swallow the longest extra, producing a group of five that spans two
        genuinely separate clusters. Every member must be close to every other
        member, not merely to whichever one the scan started from.
        """
        found = series.episode_candidates(
            _titles("0:22:00", "0:21:30", "0:22:30", "0:21:45", "0:16:00", "0:16:30", "0:15:50")
        )
        assert found == [0, 1, 2, 3]

    def test_a_group_is_returned_in_disc_order_not_length_order(self):
        """Episodes are numbered by position on the disc."""
        found = series.episode_candidates(_titles("0:43:00", "0:41:00", "0:42:00", "0:40:30"))
        assert found == [0, 1, 2, 3]

    def test_an_empty_disc_is_not_a_series(self):
        assert series.episode_candidates({}) == []
        assert series.episode_candidates(None) == []


class TestLooksLikeSeries:
    def test_a_tv_disc_is_recognised_with_its_reasoning(self):
        result = series.looks_like_series(_titles(*(["0:42:00"] * 5)))
        assert result["is_series"] is True
        assert result["episode_titles"] == [0, 1, 2, 3, 4]
        assert result["confidence"] > 0.5
        assert "similar length" in result["reason"]

    def test_a_film_says_why_it_is_a_film(self):
        result = series.looks_like_series(_titles("2:16:00", "0:05:00"))
        assert result["is_series"] is False
        assert "Treating it as a film" in result["reason"]

    def test_more_episodes_means_more_confidence(self):
        three = series.looks_like_series(_titles(*(["0:42:00"] * 3)))["confidence"]
        eight = series.looks_like_series(_titles(*(["0:42:00"] * 8)))["confidence"]
        assert eight > three

    def test_confidence_never_reaches_certainty(self):
        """It is a guess from durations alone; the user confirms."""
        result = series.looks_like_series(_titles(*(["0:42:00"] * 24)))
        assert result["confidence"] < 1.0


class TestParseSeriesLabel:
    @pytest.mark.parametrize("label,show,season", [
        ("THE_WIRE_S02_D3", "The Wire", 2),
        ("Firefly Season 1 Disc 2", "Firefly", 1),
        ("BREAKING_BAD_SEASON_4", "Breaking Bad", 4),
        ("Sopranos S06", "Sopranos", 6),
    ])
    def test_show_and_season_are_extracted(self, label, show, season):
        result = series.parse_series_label(label)
        assert result["season"] == season
        assert result["show"].lower().replace("_", " ").strip() == show.lower()

    def test_the_disc_number_is_extracted(self):
        assert series.parse_series_label("THE_WIRE_S02_D3")["disc"] == 3

    def test_a_label_with_no_season_yields_none(self):
        result = series.parse_series_label("THE_MATRIX")
        assert result["season"] is None

    def test_an_empty_label_is_handled(self):
        assert series.parse_series_label("") == {"show": "", "season": None, "disc": None}
        assert series.parse_series_label(None)["show"] == ""


class TestNaming:
    def test_the_series_folder_carries_the_year(self):
        assert series.make_series_folder_name("The Wire", 2002) == "The Wire (2002)"

    def test_a_show_without_a_year_still_works(self):
        assert series.make_series_folder_name("The Wire", None) == "The Wire"

    def test_the_season_folder_is_zero_padded(self):
        assert series.make_season_folder_name(2) == "Season 02"
        assert series.make_season_folder_name(12) == "Season 12"

    def test_specials_are_season_zero(self):
        """Plex's convention for extras and specials."""
        assert series.make_season_folder_name(0) == "Season 00"

    def test_the_episode_filename_matches_plex(self):
        assert series.make_episode_filename("The Wire", 2002, 2, 5) == (
            "The Wire (2002) - S02E05"
        )

    def test_double_digit_episodes_are_not_padded_further(self):
        assert series.make_episode_filename("The Wire", 2002, 2, 12).endswith("S02E12")

    def test_a_slash_in_the_show_name_cannot_escape_the_folder(self):
        name = series.make_series_folder_name("Face/Off", 1997)
        assert "/" not in name


class TestEpisodeNumbers:
    def test_numbering_starts_where_told(self):
        assert series.episode_numbers(4, 5) == [5, 6, 7, 8]

    def test_the_default_start_is_one(self):
        assert series.episode_numbers(3, 1) == [1, 2, 3]

    def test_a_nonsense_start_is_clamped_not_crashed(self):
        assert series.episode_numbers(2, 0) == [1, 2]
        assert series.episode_numbers(2, -5) == [1, 2]

    def test_no_titles_means_no_numbers(self):
        assert series.episode_numbers(0, 1) == []


def _job(**overrides):
    data = {
        "title": "The Wire", "year": 2002, "content_type": "series",
        "series_season": 2, "series_first_episode": 5,
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


class TestPlanOutput:
    def test_a_series_gets_a_show_and_season_folder(self):
        plan = plan_output(_job(), 3)
        assert plan.folder == "The Wire (2002)/Season 02"
        assert plan.is_series is True

    def test_episodes_are_numbered_from_the_chosen_start(self):
        plan = plan_output(_job(), 3)
        assert plan.filenames == [
            "The Wire (2002) - S02E05",
            "The Wire (2002) - S02E06",
            "The Wire (2002) - S02E07",
        ]
        assert plan.episodes == [5, 6, 7]

    def test_a_film_keeps_the_flat_layout(self):
        plan = plan_output(_job(content_type="movie", title="Heat", year=1995), 1)
        assert plan.folder == "Heat (1995)"
        assert plan.filenames == ["Heat (1995)"]
        assert plan.is_series is False
        assert plan.episodes == []

    def test_a_multi_part_film_gets_numbered_parts(self):
        plan = plan_output(_job(content_type="movie", title="Heat", year=1995), 2)
        assert plan.filenames == ["Heat (1995) - pt1", "Heat (1995) - pt2"]

    def test_an_unidentified_disc_falls_back_to_its_label(self):
        job = _job(content_type="movie", title=None, year=None)
        plan = plan_output(job, 1, fallback_title="SOME_DISC", fallback_year=1998)
        assert plan.folder == "SOME_DISC (1998)"

    def test_a_series_with_no_season_defaults_to_one(self):
        plan = plan_output(_job(series_season=None, series_first_episode=None), 1)
        assert plan.folder.endswith("Season 01")
        assert plan.filenames == ["The Wire (2002) - S01E01"]


class TestFolderDepth:
    def test_a_film_occupies_one_level(self):
        assert folder_depth(_job(content_type="movie")) == 1

    def test_a_series_occupies_two(self):
        """Taking only the last component would scatter seasons at the root."""
        assert folder_depth(_job()) == 2

    def test_the_relative_path_of_a_series_keeps_the_show_folder(self):
        assert relative_folder(
            "/opt/adr/staging/The Wire (2002)/Season 02", _job(),
        ) == "The Wire (2002)/Season 02"

    def test_the_relative_path_of_a_film_is_one_component(self):
        assert relative_folder(
            "/opt/adr/staging/Heat (1995)", _job(content_type="movie"),
        ) == "Heat (1995)"

    def test_a_short_path_does_not_over_reach(self):
        assert relative_folder("Season 02", _job()) == "Season 02"
