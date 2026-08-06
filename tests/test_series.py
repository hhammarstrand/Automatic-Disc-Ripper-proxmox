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


class TestConfigurableThresholds:
    """The thresholds are a judgement about what television looks like, not a
    fact. Anime runs to 24 minutes, a documentary series to 55 — a wrong guess
    has to be a value the user can change, not a patch they wait for."""

    def _config(self, **overrides):
        data = {"series_min_minutes": 15, "series_max_minutes": 75,
                "series_min_episodes": 3}
        data.update(overrides)
        return types.SimpleNamespace(**data)

    def test_shorter_episodes_can_be_admitted(self, ):
        """Anime at 12 minutes: excluded by default, found once told."""
        titles = _titles(*(["0:12:00"] * 4))
        assert series.episode_candidates(titles) == []
        assert series.episode_candidates(
            titles, self._config(series_min_minutes=10)) == [0, 1, 2, 3]

    def test_longer_episodes_can_be_admitted(self):
        """A 90-minute drama slot is above the default ceiling."""
        titles = _titles(*(["1:28:00"] * 3))
        assert series.episode_candidates(titles) == []
        assert series.episode_candidates(
            titles, self._config(series_max_minutes=95)) == [0, 1, 2]

    def test_the_required_count_can_be_raised(self):
        """Someone with lots of two-part films wants a stricter rule."""
        titles = _titles(*(["0:42:00"] * 3))
        assert series.episode_candidates(titles) == [0, 1, 2]
        assert series.episode_candidates(
            titles, self._config(series_min_episodes=5)) == []

    def test_a_config_missing_the_keys_falls_back_to_defaults(self):
        """Any object may be passed; absent settings must not crash it."""
        bare = types.SimpleNamespace()
        assert series.episode_candidates(_titles(*(["0:42:00"] * 4)), bare) == [0, 1, 2, 3]

    def test_no_config_at_all_still_works(self):
        assert series.episode_candidates(_titles(*(["0:42:00"] * 4))) == [0, 1, 2, 3]


class TestTheVerdictIsDiagnosable:
    """A wrong verdict that does not say what it saw is not correctable."""

    def test_a_rejection_lists_the_title_lengths(self):
        result = series.looks_like_series(_titles("2:16:00", "0:04:30"))
        assert result["is_series"] is False
        assert "2:16" in result["observed"]
        assert "4:30" in result["observed"]
        assert result["observed"] in result["reason"]

    def test_an_acceptance_lists_them_too(self):
        result = series.looks_like_series(_titles(*(["0:42:00"] * 4)))
        assert result["is_series"] is True
        assert "42:00" in result["observed"]

    def test_the_reason_quotes_the_thresholds_in_force(self):
        """Reading '15 and 75' when the config says 10 and 90 is worse than
        saying nothing."""
        config = types.SimpleNamespace(
            series_min_minutes=10, series_max_minutes=90, series_min_episodes=4)
        result = series.looks_like_series(_titles("0:03:00"), config)
        assert "10 and 90" in result["reason"]
        assert "4 or more" in result["reason"]

    def test_an_empty_disc_does_not_render_as_none(self):
        assert series.looks_like_series({})["observed"] == "none"

    def test_a_feature_length_title_is_readable(self):
        """'136:00' for a 2:16:00 feature is the number nobody wants to have
        to divide by sixty while working out why detection went wrong."""
        result = series.looks_like_series(_titles("2:16:00", "0:04:30"))
        assert "2:16:00" in result["observed"]
        assert "136:00" not in result["observed"]


class TestABoxSetIsNeverReducedToOneEpisode:
    """The worst bug this repository has had, and it lived for six versions.

    "Main feature only" is on by default. A box-set disc is detected as a
    series and every episode is ripped — correctly. Then, after the rip, the
    main-feature choice ran anyway: six titles of 42 minutes have no 1.5× gap,
    so the timid rule declined, and every fallback added after it picked the
    longest episode and called the other five extras. only_the_feature then
    dropped them, plan_output named the survivor ``S02E01``, and five episodes
    were silently gone.

    plan_output has guarded this since it was written. The two functions in
    front of it did not, and nothing checked that the guard was reached.
    """

    EPISODES = [2520, 2530, 2515, 2540, 2505, 2600]      # six of ~42 minutes

    def test_the_naming_rule_would_pick_one(self):
        """The precondition. If this ever stops being true the guard below is
        no longer load-bearing and this file should say so."""
        from adr.naming import resolve_main_feature

        assert resolve_main_feature(self.EPISODES, True) is not None

    def test_the_guard_lives_where_the_rule_does(self):
        """Behaviour, not source: feature_index is the one place that knows
        the whole rule, and it declines outright for a series."""
        import types

        from adr.naming import feature_index

        show = types.SimpleNamespace(content_type="series")
        film = types.SimpleNamespace(content_type="movie")
        sizes = [900] * len(self.EPISODES)
        assert feature_index(show, self.EPISODES, sizes, True) is None
        assert feature_index(film, self.EPISODES, sizes, True) is not None

    def test_every_caller_goes_through_it(self):
        """Three now — a fresh rip, a retry, and an encode-again — and the
        part that is easy to leave out is the series guard."""
        import inspect

        from adr import reencode, retry
        from adr.pipeline import DrivePipeline

        for source in (inspect.getsource(DrivePipeline._run_pipeline),
                       inspect.getsource(retry.requeue_encode),
                       inspect.getsource(reencode._requeue_finished)):
            assert "feature_index(" in source

    def test_and_never_drops_episodes(self):
        import inspect

        from adr.pipeline import DrivePipeline

        source = inspect.getsource(DrivePipeline._run_pipeline)
        assert "main_feature_only and not is_series" in source, (
            "only_the_feature can reduce a box set to one episode again"
        )

    def test_plan_output_still_refuses_to_call_an_episode_an_extra(self):
        """The last line of defence, and the one that always worked."""
        import types

        from adr.naming import EXTRAS_FOLDER, plan_output

        show = types.SimpleNamespace(
            title="The Show", year=2019, content_type="series",
            series_season=2, series_first_episode=1,
        )
        plan = plan_output(show, len(self.EPISODES), main_index=0)
        assert len(plan.filenames) == len(self.EPISODES)
        assert all(EXTRAS_FOLDER not in name for name in plan.filenames)


class TestASeasonIsMergedNotForked:
    """Disc 2 of a season landed in "Season 02 (2)".

    The collision rule is a film rule: a folder already taken means a different
    film, so it is forked. A season folder already taken means *the previous
    disc of this season*, which is exactly where these episodes belong — and a
    six-disc box set became Season 02, Season 02 (2) … (6), four episodes in
    each. The filenames already carry SxxEyy, so merging is safe and any real
    collision is still visible per file.
    """

    def test_unique_output_dir_merges_when_asked(self, tmp_path):
        from adr.utils import unique_output_dir

        season = tmp_path / "The Wire (2002)" / "Season 02"
        season.mkdir(parents=True)
        (season / "The Wire (2002) - S02E01.mp4").write_bytes(b"x")

        assert unique_output_dir(season, merge=True) == season

    def test_and_still_forks_for_a_film(self, tmp_path):
        """Unchanged: two films in one folder is one Plex entry with two
        movies in it."""
        from adr.utils import unique_output_dir

        folder = tmp_path / "The Matrix (1999)"
        folder.mkdir()
        (folder / "The Matrix (1999).mp4").write_bytes(b"x")

        assert unique_output_dir(folder).name == "The Matrix (1999) (2)"

    def test_the_transfer_merges_a_season(self, tmp_path):
        import types

        from adr.pipeline import transfer_to_destination

        library = tmp_path / "TV"
        existing = library / "The Wire (2002)" / "Season 02"
        existing.mkdir(parents=True)
        (existing / "The Wire (2002) - S02E01.mp4").write_bytes(b"one")

        staged = tmp_path / "staging" / "The Wire (2002)" / "Season 02"
        staged.mkdir(parents=True)
        (staged / "The Wire (2002) - S02E05.mp4").write_bytes(b"five")

        committed = []
        job = types.SimpleNamespace(
            id=7, content_type="series", output_path=str(staged),
            tracks=[], error_message=None,
        )
        session = types.SimpleNamespace(commit=lambda: committed.append(True))

        assert transfer_to_destination(job, session, library) is True
        assert sorted(p.name for p in existing.iterdir()) == [
            "The Wire (2002) - S02E01.mp4",
            "The Wire (2002) - S02E05.mp4",
        ]
        assert job.output_path == str(existing)
        assert not staged.exists(), "the emptied staging folder was left behind"

    def test_a_film_transfer_still_forks(self, tmp_path):
        import types

        from adr.pipeline import transfer_to_destination

        library = tmp_path / "Films"
        (library / "The Matrix (1999)").mkdir(parents=True)
        (library / "The Matrix (1999)" / "The Matrix (1999).mp4").write_bytes(b"x")

        staged = tmp_path / "staging" / "The Matrix (1999)"
        staged.mkdir(parents=True)
        (staged / "The Matrix (1999).mp4").write_bytes(b"y")

        job = types.SimpleNamespace(
            id=8, content_type="movie", output_path=str(staged),
            tracks=[], error_message=None,
        )
        session = types.SimpleNamespace(commit=lambda: None)

        assert transfer_to_destination(job, session, library) is True
        assert job.output_path.endswith("The Matrix (1999) (2)")


class TestSeasonZeroIsARealSeason:
    """Plex files specials in season 0. `or 1` turned it into season 1, where
    the episode numbers then collide with the actual first season."""

    def _specials(self):
        import types

        return types.SimpleNamespace(
            title="The Show", year=2019, content_type="series",
            series_season=0, series_first_episode=1,
        )

    def test_a_specials_disc_keeps_its_season(self):
        from adr.naming import plan_output

        plan = plan_output(self._specials(), 2)
        assert "Season 00" in plan.folder
        assert plan.filenames[0] == "The Show (2019) - S00E01"

    def test_an_unset_season_still_defaults_to_one(self):
        import types

        from adr.naming import plan_output

        job = types.SimpleNamespace(
            title="The Show", year=2019, content_type="series",
            series_season=None, series_first_episode=1,
        )
        assert "Season 01" in plan_output(job, 1).folder


class TestEpisodeNumbersAreClaimedAtomically:
    """Two drives fed discs a minute apart both produced S02E01-E04.

    apply_to stamped series_first_episode when the disc went in, and the
    counter only moved once the rip had finished — so the read and the write
    were minutes apart with another drive between them.
    """

    def _config(self, tmp_path):
        from adr.config import Config

        config = Config(str(tmp_path / "adr.yaml"))
        config.update({
            "series_mode": True, "series_mode_show": "The Wire",
            "series_mode_season": 2, "series_mode_next_episode": 1,
        })
        return config

    def test_two_claims_do_not_overlap(self, tmp_path):
        from adr import seriesmode

        config = self._config(tmp_path)
        first = seriesmode.take_episodes(config, 4)
        second = seriesmode.take_episodes(config, 4)
        assert first == 1
        assert second == 5, "the second disc reused the first disc's numbers"

    def test_concurrent_claims_never_collide(self, tmp_path):
        import threading

        from adr import seriesmode

        config = self._config(tmp_path)
        claimed = []
        barrier = threading.Barrier(4)

        def claim():
            barrier.wait()
            claimed.append(seriesmode.take_episodes(config, 3))

        threads = [threading.Thread(target=claim) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(claimed) == [1, 4, 7, 10]

    def test_an_inactive_mode_claims_nothing(self, tmp_path):
        from adr import seriesmode

        config = self._config(tmp_path)
        config.update({"series_mode": False})
        assert seriesmode.take_episodes(config, 4) is None
