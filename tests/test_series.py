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


class TestTheMergeDoesNotCostTheSeason:
    """Three regressions the merge introduced, all found by re-reviewing it.

    Merging means every disc of a box set shares one season folder. Three
    places treated "the job's output folder" as "the job's files", and one
    silently replaced an episode with another.
    """

    def _series_job(self, folder, tracks=()):
        import types

        return types.SimpleNamespace(
            id=1, content_type="series", output_path=str(folder), plex_path=None,
            tracks=[types.SimpleNamespace(output_path=str(p)) for p in tracks],
        )

    def _config(self, tmp_path):
        import types

        return types.SimpleNamespace(
            completed_path=tmp_path, raw_path=tmp_path / "raw",
            staging_path=tmp_path / "staging", plex_path="", tv_path=str(tmp_path),
            music_path="", data_disc_path="",
        )

    def test_deleting_one_disc_does_not_delete_the_season(self, tmp_path):
        from adr import cleanup

        season = tmp_path / "The Wire (2002)" / "Season 02"
        season.mkdir(parents=True)
        mine, theirs = [], []
        for n in range(1, 5):
            p = season / f"The Wire (2002) - S02E0{n}.mp4"
            p.write_bytes(b"x")
            mine.append(p)
        for n in range(5, 9):
            p = season / f"The Wire (2002) - S02E0{n}.mp4"
            p.write_bytes(b"x")
            theirs.append(p)

        job = self._series_job(season, mine)
        found = cleanup.job_files(job, self._config(tmp_path))
        assert sorted(found) == sorted(mine), (
            "deleting disc 1 would have taken the other discs' episodes"
        )

    def test_a_film_still_gets_the_folder_fallback(self, tmp_path):
        """Unchanged: a film's folder is its own, and a job interrupted before
        its rows were written still needs the scan."""
        import types

        from adr import cleanup

        folder = tmp_path / "The Matrix (1999)"
        folder.mkdir()
        (folder / "The Matrix (1999).mp4").write_bytes(b"x")
        job = types.SimpleNamespace(
            id=2, content_type="movie", output_path=str(folder),
            plex_path=None, tracks=[],
        )
        assert len(cleanup.job_files(job, self._config(tmp_path))) == 1

    def test_an_episode_is_never_silently_overwritten(self, tmp_path):
        import types

        from adr.pipeline import transfer_to_destination

        library = tmp_path / "TV"
        existing = library / "The Wire (2002)" / "Season 02"
        existing.mkdir(parents=True)
        clash = existing / "The Wire (2002) - S02E01.mp4"
        clash.write_bytes(b"the first disc")

        staged = tmp_path / "staging" / "The Wire (2002)" / "Season 02"
        staged.mkdir(parents=True)
        (staged / "The Wire (2002) - S02E01.mp4").write_bytes(b"the second disc")

        job = types.SimpleNamespace(
            id=9, content_type="series", output_path=str(staged),
            tracks=[], error_message=None,
        )
        assert transfer_to_destination(
            job, types.SimpleNamespace(commit=lambda: None), library) is True

        assert clash.read_bytes() == b"the first disc", "an episode was replaced"
        assert (existing / "The Wire (2002) - S02E01 (2).mp4").is_file(), (
            "the arriving episode was lost instead of set aside"
        )


class TestADiscOfEpisodesAndOneClip:
    """End to end, through the real video path, because the two halves of this
    have to agree and they live in different modules.

    Saltkråkan's disc carried five episodes and a 2:55 clip. Numbering every
    ripped title in disc order made the clip an episode, which shifted the
    ones after it *and* claimed a sixth number from series mode — so the next
    disc in the box set started one episode too high as well. Getting the
    names right while claiming the wrong count would leave the season just as
    broken, one disc later.
    """

    EPISODE = "0:25:00"
    CLIP = "0:02:55"

    @pytest.fixture
    def harness(self, tmp_path, monkeypatch):
        import queue

        from adr import disctype
        from adr import pipeline as pipeline_mod
        from adr.config import Config
        from adr.disctype import DiscInfo
        from adr.models import init_db

        path = tmp_path / "adr.yaml"
        path.write_text(
            f"raw_path: {tmp_path / 'raw'}\n"
            f"completed_path: {tmp_path / 'completed'}\n"
            f"staging_path: {tmp_path / 'staging'}\n"
            f"tv_path: {tmp_path / 'tv'}\n"
            "eject_after_rip: false\n"
            "notify_enabled: false\n"
            "require_completed_mount: false\n"
            "transcode_enabled: false\n"
            "main_feature_only: true\n"
            "series_detection: true\n",
        )
        config = Config(str(path))
        for name in ("raw", "completed", "staging", "tv"):
            (tmp_path / name).mkdir(exist_ok=True)
        init_db()

        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video."),
        )
        # No network, and no confident film match — a box-set disc must not
        # be renamed into a movie halfway through this test.
        from adr.identify import MovieInfo

        monkeypatch.setattr(
            pipeline_mod, "identify_disc",
            lambda *a, **k: MovieInfo("Saltkrakan", confidence=0.0),
        )

        encode_queue = queue.Queue()
        drive = pipeline_mod.DrivePipeline("/dev/sr0", config, encode_queue)
        return drive, config, encode_queue, tmp_path, monkeypatch, pipeline_mod

    def _rip(self, tmp_path, durations):
        """A finished rip of len(durations) titles, on disk."""
        from adr.ripper import RipResult

        raw = tmp_path / "raw" / "1"
        raw.mkdir(parents=True, exist_ok=True)
        out = RipResult()
        out.success = True
        out.disc_name = "SALTKRAKAN"
        out.mkv_files = []
        out.title_info = {}
        for index, duration in enumerate(durations):
            path = raw / f"t{index:02d}.mkv"
            path.write_bytes(b"M" * 4096)
            out.mkv_files.append(path)
            out.title_info[index] = {"filename": path.name, "duration": duration}
        return out

    def _run(self, harness, durations):
        drive, config, encode_queue, tmp_path, monkeypatch, pipeline_mod = harness
        from adr import seriesmode

        claimed = []
        real = seriesmode.take_episodes
        monkeypatch.setattr(
            seriesmode, "take_episodes",
            lambda cfg, count: (claimed.append(count), real(cfg, count))[1],
        )
        titles = {
            index: {"filename": f"t{index:02d}.mkv", "duration": duration}
            for index, duration in enumerate(durations)
        }
        monkeypatch.setattr(drive._ripper, "scan_disc", lambda d, job_id=None: titles)
        monkeypatch.setattr(
            drive._ripper, "rip",
            lambda **kw: self._rip(tmp_path, durations),
        )
        drive._run_pipeline(None)

        names = []
        while not encode_queue.empty():
            names.append(encode_queue.get().output_filename)
        return names, claimed

    def test_the_clip_does_not_become_an_episode(self, harness):
        names, _ = self._run(harness, [self.EPISODE] * 5 + [self.CLIP])
        episodes = [n for n in names if "S01E" in n]
        extras = [n for n in names if n.startswith("Other/")]
        assert len(episodes) == 5, names
        assert len(extras) == 1, names

    def test_the_episodes_keep_consecutive_numbers(self, harness):
        """A clip in the middle must not push the ones after it along."""
        names, _ = self._run(
            harness, [self.EPISODE] * 2 + [self.CLIP] + [self.EPISODE] * 2)
        episodes = sorted(n for n in names if "S01E" in n)
        assert [n[-3:] for n in episodes] == ["E01", "E02", "E03", "E04"], names

    def test_series_mode_claims_episodes_not_files(self, harness):
        """The half that only shows up on the *next* disc. Claiming one number
        per ripped title starts the following disc an episode too high, and
        nothing about this disc's own names would reveal it."""
        drive, config, *_ = harness
        config.update({
            "series_mode": True, "series_mode_show": "Life on Seacrow Island",
            "series_mode_season": 1, "series_mode_next_episode": 1,
        })
        _, claimed = self._run(harness, [self.EPISODE] * 5 + [self.CLIP])
        assert claimed == [5], f"claimed {claimed} for 5 episodes and 1 clip"
        assert config.series_mode_next_episode == 6

    def test_a_disc_of_only_episodes_is_unchanged(self, harness):
        names, _ = self._run(harness, [self.EPISODE] * 4)
        assert len([n for n in names if "S01E" in n]) == 4
        assert not [n for n in names if n.startswith("Other/")]


class TestTheLabelSaysWhichDiscThisIs:
    """Feeding a box set disc by disc numbers every disc from 1, because each
    is detected on its own and cannot know what the last one used. Saltkråkan
    arrived as three jobs all claiming S01E01-E06, and the season folder ended
    up holding "(2)" and "(3)" copies of every episode.

    Only the disc number on the label may start the continuation, and that is
    the whole safety argument. "Carry on from what is in the season folder"
    cannot tell a second disc from a second *rip of the same disc* — both find
    five episodes there, and the re-rip would be silently filed as 6-10. A
    label that says D2 is a claim about which disc this is; a re-rip of disc 1
    says D1.
    """

    def _after(self, this_disc, previous):
        from adr.series import episode_after_previous_discs

        return episode_after_previous_discs(this_disc, previous)

    def test_a_label_with_no_disc_number_changes_nothing(self):
        """Which is most discs, including every one of the user's."""
        assert self._after(None, [{"disc": 1, "last_episode": 5}]) == (1, "")

    def test_disc_one_starts_at_one_and_says_nothing(self):
        assert self._after(1, []) == (1, "")

    def test_disc_two_carries_on_from_disc_one(self):
        start, why = self._after(2, [{"disc": 1, "last_episode": 5}])
        assert start == 6
        assert "disc 2" in why and "episode 6" in why

    def test_disc_three_carries_on_from_the_highest_earlier_disc(self):
        start, _ = self._after(3, [
            {"disc": 1, "last_episode": 5},
            {"disc": 2, "last_episode": 11},
        ])
        assert start == 12

    def test_a_later_disc_that_was_fed_first_is_not_counted(self):
        """Only discs *before* this one say where it starts."""
        start, _ = self._after(2, [
            {"disc": 1, "last_episode": 5},
            {"disc": 4, "last_episode": 30},
        ])
        assert start == 6

    def test_ripping_the_same_disc_again_does_not_advance(self):
        """The case the whole design turns on. Disc 2 has been done; disc 2
        comes back. Continuing would file it as 12-16 and quietly duplicate
        the season."""
        start, why = self._after(2, [
            {"disc": 1, "last_episode": 5},
            {"disc": 2, "last_episode": 11},
        ])
        assert start == 1
        assert "same disc again" in why

    def test_a_later_disc_with_nothing_on_record_declines_to_guess(self):
        """"Disc 3" does not say how long discs 1 and 2 were."""
        start, why = self._after(3, [])
        assert start == 1
        assert "no way to tell" in why

    def test_an_earlier_disc_that_numbered_nothing_is_not_evidence(self):
        """A disc that failed, or was ripped as a film, says nothing about
        where the next one starts."""
        start, why = self._after(2, [{"disc": 1, "last_episode": None}])
        assert start == 1
        assert "no way to tell" in why

    def test_every_answer_can_be_corrected(self):
        """The banner keeps its Change button, so none of this is final —
        which is what makes guessing at all acceptable."""
        for disc, previous in (
            (2, [{"disc": 1, "last_episode": 5}]),
            (3, []),
            (2, [{"disc": 2, "last_episode": 9}]),
        ):
            _, why = self._after(disc, previous)
            assert "hange it" in why, why


class TestFindingTheEarlierDiscs:
    """Identity comes from the parsed show name, because that is the part two
    discs of one box set agree on: SALTKRAKAN_D2 and SALTKRAKAN_D3 are the
    same programme and different strings."""

    def _job(self, session, label, season, episodes, content_type="series"):
        from adr.models import Job, Track

        job = Job(disc_label=label, drive="/dev/sr0", content_type=content_type,
                  series_season=season)
        session.add(job)
        session.commit()
        for number in episodes:
            session.add(Track(job_id=job.id, track_number=number,
                              filename=f"t{number}.mkv", episode_number=number))
        session.commit()
        return job

    def test_it_matches_the_show_across_different_labels(self, tmp_path):
        from adr.models import get_session, init_db
        from adr.series import earlier_discs

        init_db()
        session = get_session()
        try:
            self._job(session, "SALTKRAKAN_D1", 1, [1, 2, 3, 4, 5])
            this = self._job(session, "SALTKRAKAN_D2", 1, [])
            found = earlier_discs(session, "Saltkrakan", this)
            assert found == [{"disc": 1, "last_episode": 5}]
        finally:
            session.close()

    def test_another_season_is_not_carried_on_from(self, tmp_path):
        """Season 2 disc 1 must not continue season 1."""
        from adr.models import get_session, init_db
        from adr.series import earlier_discs

        init_db()
        session = get_session()
        try:
            self._job(session, "SALTKRAKAN_S01_D1", 1, [1, 2, 3])
            this = self._job(session, "SALTKRAKAN_S02_D2", 2, [])
            assert earlier_discs(session, "Saltkrakan", this) == []
        finally:
            session.close()

    def test_a_different_show_is_not_carried_on_from(self, tmp_path):
        from adr.models import get_session, init_db
        from adr.series import earlier_discs

        init_db()
        session = get_session()
        try:
            self._job(session, "THE_WIRE_D1", 1, [1, 2, 3])
            this = self._job(session, "SALTKRAKAN_D2", 1, [])
            assert earlier_discs(session, "Saltkrakan", this) == []
        finally:
            session.close()

    def test_a_disc_ripped_as_a_film_is_ignored(self, tmp_path):
        from adr.models import get_session, init_db
        from adr.series import earlier_discs

        init_db()
        session = get_session()
        try:
            self._job(session, "SALTKRAKAN_D1", 1, [], content_type="movie")
            this = self._job(session, "SALTKRAKAN_D2", 1, [])
            assert earlier_discs(session, "Saltkrakan", this) == []
        finally:
            session.close()

    def test_a_nameless_disc_looks_nothing_up(self, tmp_path):
        from adr.models import get_session, init_db
        from adr.series import earlier_discs

        init_db()
        session = get_session()
        try:
            this = self._job(session, "", 1, [])
            assert earlier_discs(session, "", this) == []
        finally:
            session.close()


class TestMarkingADiscAsASeriesByHand:
    """The path that actually gets used, and the one the continuation missed.

    Saltkråkan was tagged as a series by hand from the dashboard, not by
    detection — and that path set episode 1 every time, however plainly the
    label said "dvd 2". Three discs, three claims on S01E01.
    """

    def _job(self, session, label, season=None, episodes=(), content_type="movie",
             first_episode=None):
        from adr.models import Job, Track

        job = Job(disc_label=label, drive="/dev/sr0", content_type=content_type,
                  series_season=season, series_first_episode=first_episode)
        session.add(job)
        session.commit()
        for number in episodes:
            session.add(Track(job_id=job.id, track_number=number,
                              filename=f"t{number}.mkv", episode_number=number))
        session.commit()
        return job

    def _suggest(self, session, job):
        from adr.series import suggest_numbering

        return suggest_numbering(session, job)

    def test_a_dvd_2_label_is_offered_the_next_episode(self, tmp_path):
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            self._job(session, "Saltkrakan dvd 1", season=1, episodes=[1, 2, 3, 4, 5],
                      content_type="series", first_episode=1)
            this = self._job(session, "Saltkrakan dvd 2")
            out = self._suggest(session, this)
            assert out["disc"] == 2
            assert out["first_episode"] == 6
            assert out["apply"] is True
            assert "episode 6" in out["reason"]
        finally:
            session.close()

    def test_the_first_disc_is_offered_episode_one_quietly(self, tmp_path):
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            this = self._job(session, "Saltkrakan dvd 1")
            out = self._suggest(session, this)
            assert out["first_episode"] == 1
            assert out["reason"] == ""
        finally:
            session.close()

    def test_a_label_with_no_disc_number_suggests_nothing(self, tmp_path):
        """Which is every home-burned disc. The dialog must be left alone."""
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            this = self._job(session, "LG_COMBI_RECORDER")
            out = self._suggest(session, this)
            assert out["disc"] is None
            assert out["apply"] is False
        finally:
            session.close()

    def test_reopening_the_dialog_cannot_overwrite_a_chosen_number(self, tmp_path):
        """Someone who typed 12 and reopened the dialog must still see 12."""
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            self._job(session, "Saltkrakan dvd 1", season=1, episodes=[1, 2, 3],
                      content_type="series", first_episode=1)
            this = self._job(session, "Saltkrakan dvd 2", season=1,
                             content_type="series", first_episode=12)
            assert self._suggest(session, this)["apply"] is False
        finally:
            session.close()

    def test_the_season_comes_off_the_label_when_it_says_one(self, tmp_path):
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            this = self._job(session, "THE_WIRE_S02_D1")
            assert self._suggest(session, this)["season"] == 2
        finally:
            session.close()

    def test_the_endpoint_answers_for_the_dialog(self, flask_client_factory=None):
        """Wired up, not merely written."""
        from adr.config import Config
        from adr.models import get_session, init_db
        from web.app import create_app

        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp())
        for name in ("raw", "completed", "staging"):
            (root / name).mkdir()
        config = Config(str(root / "adr.yaml"))
        config.update({
            "completed_path": str(root / "completed"),
            "raw_path": str(root / "raw"),
            "staging_path": str(root / "staging"),
        })
        init_db()
        session = get_session()
        try:
            self._job(session, "Saltkrakan dvd 1", season=1, episodes=[1, 2, 3, 4, 5],
                      content_type="series", first_episode=1)
            this = self._job(session, "Saltkrakan dvd 2")
            job_id = this.id
        finally:
            session.close()

        client = create_app(config).test_client()
        body = client.get(f"/api/jobs/{job_id}/series-suggestion").get_json()
        assert body["ok"] is True
        assert body["first_episode"] == 6
        assert body["apply"] is True

    def test_a_missing_job_is_a_404(self):
        import tempfile
        from pathlib import Path

        from adr.config import Config
        from adr.models import init_db
        from web.app import create_app

        root = Path(tempfile.mkdtemp())
        for name in ("raw", "completed", "staging"):
            (root / name).mkdir()
        config = Config(str(root / "adr.yaml"))
        config.update({"completed_path": str(root / "completed"),
                       "raw_path": str(root / "raw"),
                       "staging_path": str(root / "staging")})
        init_db()
        client = create_app(config).test_client()
        assert client.get("/api/jobs/999999/series-suggestion").status_code == 404


class TestTheDiscMarkerIsReadCorrectly:
    """"dvd 2" found the right number by accident and cut the show name in the
    wrong place — "Saltkråkan dvd 2" parsed as show "Saltkråkan dv", because
    the bare `d` alternative matched the second d of "dvd". Both discs of a
    set mangled it identically, so they still matched each other, which is
    exactly how a bug like that survives being used."""

    def _parse(self, label):
        from adr.series import parse_series_label

        return parse_series_label(label)

    def test_dvd_2_gives_a_clean_show_name(self):
        assert self._parse("Saltkråkan dvd 2") == {
            "show": "Saltkråkan", "season": None, "disc": 2}

    def test_dvd_without_a_space(self):
        assert self._parse("Saltkråkan dvd1")["disc"] == 1
        assert self._parse("Saltkråkan dvd1")["show"] == "Saltkråkan"

    def test_an_underscore_label_still_works(self):
        """\\b would have broken this: "_" is a word character, and every
        second disc label is SHOW_D2."""
        assert self._parse("SALTKRAKAN_D2") == {
            "show": "Saltkrakan", "season": None, "disc": 2}
        assert self._parse("SALTKRAKAN_DVD2")["disc"] == 2

    def test_the_older_spellings_still_work(self):
        assert self._parse("Firefly Season 1 Disc 2") == {
            "show": "Firefly", "season": 1, "disc": 2}
        assert self._parse("THE_WIRE_S02_D3")["disc"] == 3
        assert self._parse("Disk 4")["disc"] == 4

    def test_a_title_ending_in_d_and_a_number_is_not_a_disc(self):
        """"Deadwood 2" claimed to be disc 2 on the d of "-wood"."""
        assert self._parse("Deadwood 2")["disc"] is None

    def test_a_year_in_a_title_is_not_a_disc_number(self):
        assert self._parse("Blade Runner 2049")["disc"] is None

    def test_dvd_with_no_number_is_not_a_disc_marker(self):
        assert self._parse("MY_DVD")["disc"] is None


class TestTheSeasonUsedForTheLookupIsTheOneReported:
    """The continuation worked only for season 1, and said so confidently.

    suggest_numbering answers for a disc nobody has marked as a series yet, so
    ``job.series_season`` is still NULL and it takes the season off the label.
    The lookup behind it re-derived the season from the row, read NULL as 1,
    and searched season 1 — so a season-2 box set found nothing and was
    offered episode 1, and if season 1 of the same show happened to be in the
    library it was offered *that* season's numbers under a sentence claiming
    they came from "this season".

    Three of the nine review dimensions found this independently, which is
    what a genuinely load-bearing mistake looks like.
    """

    def _disc(self, session, label, season=None, episodes=(), content_type="series"):
        from adr.models import Job, Track

        job = Job(disc_label=label, drive="/dev/sr0", content_type=content_type,
                  series_season=season)
        session.add(job)
        session.commit()
        for number in episodes:
            session.add(Track(job_id=job.id, track_number=number,
                              filename=f"{label}-{number}.mkv",
                              episode_number=number))
        session.commit()
        return job

    def _suggest(self, session, job):
        from adr.series import suggest_numbering

        return suggest_numbering(session, job)

    def test_a_season_two_box_set_continues(self, tmp_path):
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            self._disc(session, "THE_WIRE_S02_D1", season=2, episodes=[1, 2, 3, 4, 5])
            # Not yet marked as a series: series_season is NULL, which is the
            # only state this endpoint is ever called in.
            this = self._disc(session, "THE_WIRE_S02_D2", content_type="movie")
            out = self._suggest(session, this)
            assert out["season"] == 2
            assert out["first_episode"] == 6, (
                "the lookup used the row's season, not the label's"
            )
        finally:
            session.close()

    def test_another_season_of_the_same_show_is_not_borrowed_from(self, tmp_path):
        """The worse half: season 1's numbers offered for a season-3 disc,
        under a sentence saying they came from this season."""
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            self._disc(session, "THE_WIRE_S01_D1", season=1,
                       episodes=list(range(1, 14)))
            this = self._disc(session, "THE_WIRE_S03_D2", content_type="movie")
            out = self._suggest(session, this)
            assert out["season"] == 3
            assert out["first_episode"] == 1
            assert "nothing from an earlier disc of this season" in out["reason"]
        finally:
            session.close()

    def test_season_one_still_works(self, tmp_path):
        """The half that always worked must keep working."""
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            self._disc(session, "FIREFLY_D1", season=1, episodes=[1, 2, 3])
            this = self._disc(session, "FIREFLY_D2", content_type="movie")
            assert self._suggest(session, this)["first_episode"] == 4
        finally:
            session.close()

    def test_earlier_discs_takes_the_season_it_is_given(self, tmp_path):
        from adr.models import get_session, init_db
        from adr.series import earlier_discs

        init_db()
        session = get_session()
        try:
            self._disc(session, "SHOW_S02_D1", season=2, episodes=[1, 2])
            this = self._disc(session, "SHOW_S02_D2", content_type="movie")
            assert earlier_discs(session, "Show", this, 2) == [
                {"disc": 1, "last_episode": 2}]
            assert earlier_discs(session, "Show", this, 1) == []
        finally:
            session.close()

    def test_the_show_name_is_inherited_from_the_right_season(self, tmp_path):
        """suggest_numbering also carries the show across discs, and it read
        the same wrong season to find it."""
        from adr.models import get_session, init_db

        init_db()
        session = get_session()
        try:
            first = self._disc(session, "THE_WIRE_S02_D1", season=2, episodes=[1, 2])
            first.title = "The Wire"
            first.year = 2002
            session.commit()
            this = self._disc(session, "THE_WIRE_S02_D2", content_type="movie")
            assert self._suggest(session, this)["show"] == "The Wire"
        finally:
            session.close()
