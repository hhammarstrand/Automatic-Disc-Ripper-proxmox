

class TestMakeMkvKnowsWhatTheDiscIsCalled:
    """A rip came out as "Unknown - pt1.mp4" while MakeMKV had the disc's name
    the whole time.

    blkid reads the label, and blkid times out on a busy drive — which the
    drive is for the entire rip. MakeMKV reports the same name in its CINFO
    records, parsed into RipResult.disc_name since the first version of the
    ripper and never read by anything.
    """

    def test_the_ripper_still_parses_it(self):
        from adr.ripper import MakeMKVRipper, RipResult

        result = RipResult()
        MakeMKVRipper._parse_cinfo('CINFO:0,2,0,"DINOSAUR"', result)
        assert result.disc_name == "DINOSAUR"

    def test_the_pipeline_falls_back_to_it(self):
        """Read rather than run: the fallback sits in the middle of a method
        that needs a disc, a database and a drive."""
        import inspect

        from adr.pipeline import DrivePipeline

        source = inspect.getsource(DrivePipeline._run_pipeline)
        assert "rip_result.disc_name" in source, (
            "the disc name MakeMKV reports is unused again"
        )

    def test_a_label_beats_makemkvs_name(self):
        """blkid's label is the filesystem's own, and closer to the release
        name than MakeMKV's guess when both exist."""
        import inspect

        from adr.pipeline import DrivePipeline

        source = inspect.getsource(DrivePipeline._run_pipeline)
        assert "volume_name or rip_result.disc_name" in source


from adr import naming


class TestAShortClipIsNotAnEpisode:
    """Saltkråkan is why this exists.

    The disc carried five ~25-minute episodes and one 2:55 clip. Every ripped
    title was numbered in disc order, so the clip became an episode — and that
    does not merely add one bad file. It shifts every episode after it by one,
    puts the disc out of step with the season, and the names are what Plex
    reads, so the whole run was wrong from there on.
    """

    EPISODE = 25 * 60
    CLIP = 175          # 2:55, measured off the real disc
    PLAY_ALL = 150 * 60

    def _series(self, season=1, first=1):
        import types

        return types.SimpleNamespace(
            title="Life on Seacrow Island", year=1964, content_type="series",
            series_season=season, series_first_episode=first,
        )

    def test_the_clip_is_not_an_episode(self):
        mask = naming.episode_mask([self.EPISODE] * 5 + [self.CLIP])
        assert mask == [True] * 5 + [False]

    def test_a_play_all_title_is_not_an_episode_either(self):
        """The whole disc end to end, which would otherwise be filed as an
        episode of its own and duplicate every one of them."""
        mask = naming.episode_mask([self.PLAY_ALL] + [self.EPISODE] * 3)
        assert mask == [False, True, True, True]

    def test_a_length_that_could_not_be_read_counts_as_an_episode(self):
        """Unknown is not evidence, and demoting a real episode loses it from
        the season — worse than admitting an extra."""
        assert naming.episode_mask([None, self.EPISODE, None]) == [True] * 3

    def test_a_disc_of_short_programmes_is_not_all_extras(self):
        """Ten-minute cartoons against a fifteen-minute floor would arrive as
        a folder of extras and no programme at all. The rule is abandoned
        rather than applied to everything."""
        assert naming.episode_mask([600] * 6) == [True] * 6

    def test_the_window_is_configurable(self):
        assert naming.episode_mask([600] * 3 + [self.EPISODE],
                                   window=(5 * 60, 75 * 60)) == [True] * 4

    def test_the_extra_does_not_take_an_episode_number(self):
        """The heart of it. Five episodes and a clip must be E01-E05, not
        E01-E06 with a clip somewhere in the middle."""
        mask = naming.episode_mask([self.EPISODE] * 2 + [self.CLIP] + [self.EPISODE] * 2)
        plan = naming.plan_output(self._series(), 5, episodes_mask=mask)
        assert plan.episodes == [1, 2, None, 3, 4]

    def test_the_episodes_after_it_are_not_shifted(self):
        mask = naming.episode_mask([self.CLIP] + [self.EPISODE] * 3)
        plan = naming.plan_output(self._series(), 4, episodes_mask=mask)
        assert plan.episodes == [None, 1, 2, 3]
        assert "S01E01" in plan.filenames[1]

    def test_the_extra_is_filed_where_plex_reads_extras(self):
        mask = [True, False]
        plan = naming.plan_output(self._series(), 2, episodes_mask=mask)
        assert plan.filenames[1] == f"{naming.EXTRAS_FOLDER}/Extra 1"

    def test_several_extras_are_numbered_among_themselves(self):
        plan = naming.plan_output(
            self._series(), 4, episodes_mask=[True, False, True, False])
        assert plan.filenames[1] == f"{naming.EXTRAS_FOLDER}/Extra 1"
        assert plan.filenames[3] == f"{naming.EXTRAS_FOLDER}/Extra 2"
        assert plan.episodes == [1, None, 2, None]

    def test_numbering_still_starts_where_the_season_left_off(self):
        plan = naming.plan_output(
            self._series(first=7), 3, episodes_mask=[True, False, True])
        assert plan.episodes == [7, None, 8]

    def test_without_a_mask_nothing_changes(self):
        """Every existing caller passes no mask and must be unaffected."""
        plan = naming.plan_output(self._series(), 4)
        assert plan.episodes == [1, 2, 3, 4]
        assert all("S01E" in name for name in plan.filenames)

    def test_a_mask_of_the_wrong_length_is_ignored_rather_than_trusted(self):
        plan = naming.plan_output(self._series(), 3, episodes_mask=[True, False])
        assert plan.episodes == [1, 2, 3]

    def test_a_film_is_untouched_by_any_of_this(self):
        import types

        movie = types.SimpleNamespace(
            title="Jumanji", year=1995, content_type="movie",
            series_season=None, series_first_episode=None,
        )
        plan = naming.plan_output(movie, 1, episodes_mask=[True])
        assert plan.is_series is False
        assert plan.episodes == []


class TestWhenNobodySaysHowLongTheTitlesAre:
    """The gap the episode split fell through in the wild.

    Durations come from MakeMKV's TINFO records, and MakeMKV reports them
    generously while *scanning* and sparsely while *ripping* — often not at
    all. The pipeline read only the rip, so every title came back with an
    unknown length; unknown counts as an episode by design, and a 2:55 bonus
    clip was filed as S01E06 between five real episodes of Saltkråkan.

    The scan's records are consulted now, and size is the last resort behind
    them.
    """

    def test_a_clip_is_caught_on_size_when_no_duration_is_known(self):
        mask = naming.episode_mask(
            [None] * 6, sizes=[1_100_000_000] * 5 + [100_000_000])
        assert mask == [True] * 5 + [False]

    def test_titles_of_similar_size_are_all_episodes(self):
        """Nothing to distinguish them, so nothing is demoted."""
        assert naming.episode_mask([None] * 6, sizes=[1_000_000_000] * 6) == [True] * 6

    def test_a_genuinely_shorter_episode_survives(self):
        """Episodes vary. The floor is a fraction of the middle one, not a
        demand that they all match."""
        mask = naming.episode_mask(
            [None] * 4,
            sizes=[1_000_000_000, 1_000_000_000, 900_000_000, 600_000_000])
        assert mask == [True] * 4

    def test_a_known_duration_beats_the_size(self):
        """A long episode that happens to compress well must not be demoted
        for being small — the length was measured, so size says nothing."""
        mask = naming.episode_mask(
            [25 * 60] * 3 + [25 * 60],
            sizes=[1_000_000_000] * 3 + [50_000_000])
        assert mask == [True] * 4

    def test_size_never_promotes_a_title_the_clock_ruled_out(self):
        """A 2:55 clip in a huge file is still 2:55."""
        mask = naming.episode_mask(
            [25 * 60] * 3 + [175],
            sizes=[1_000_000_000] * 3 + [9_000_000_000])
        assert mask == [True, True, True, False]

    def test_without_sizes_the_old_behaviour_is_unchanged(self):
        assert naming.episode_mask([None] * 6) == [True] * 6

    def test_a_mismatched_size_list_is_ignored_rather_than_trusted(self):
        assert naming.episode_mask([None] * 6, sizes=[1, 2]) == [True] * 6


class TestThePlayAllTitleInsideTheWindow:
    """Alfons & Milla: episodes of 9.6 minutes, so the user lowered the
    episode floor to 8 — at which point the disc's ~48-minute play-all title
    fits INSIDE the 8-75 window and was numbered as an episode, shifting
    every real one after it. The length rule only catches a play-all while
    the episodes are long enough to push it over the ceiling.

    Arithmetic is what gives it away: the play-all runs about as long as the
    rest of the disc put together, and no episode does.
    """

    EP = 574.5                     # 9.6 min, straight from the user's log
    WINDOW = (8 * 60, 75 * 60)

    def test_a_play_all_inside_the_window_is_not_an_episode(self):
        mask = naming.episode_mask([5 * self.EP] + [self.EP] * 5, self.WINDOW)
        assert mask == [False] + [True] * 5

    def test_wherever_it_sits_on_the_disc(self):
        mask = naming.episode_mask([self.EP] * 5 + [5 * self.EP], self.WINDOW)
        assert mask == [True] * 5 + [False]

    def test_five_equal_episodes_are_left_alone(self):
        assert naming.episode_mask([self.EP] * 5, self.WINDOW) == [True] * 5

    def test_a_two_parter_is_not_two_play_alls(self):
        """Each half of a two-parter is exactly the sum of the others. Fewer
        than three known titles and the rule stays out of it."""
        assert naming.episode_mask([45 * 60] * 2, self.WINDOW) == [True] * 2

    def test_at_most_one_title_is_the_whole_disc(self):
        """Two titles that each dwarf the rest is not a shape this rule
        understands — it must not empty the disc of episodes."""
        mask = naming.episode_mask(
            [100 * 60, 100 * 60, 10 * 60], (8 * 60, 120 * 60))
        assert sum(1 for m in mask if not m) == 1

    def test_the_known_trade_a_double_finale_goes_to_extras(self):
        """20+20+40: the 40 is indistinguishable from a play-all of the other
        two. A misfiled extra someone renames is cheaper than a phantom
        episode renumbering the season — documented, not accidental."""
        mask = naming.episode_mask([20 * 60, 20 * 60, 40 * 60], self.WINDOW)
        assert mask == [True, True, False]

    def test_unknown_lengths_do_not_trip_the_arithmetic(self):
        mask = naming.episode_mask([None, self.EP, self.EP, None], self.WINDOW)
        assert mask == [True] * 4
