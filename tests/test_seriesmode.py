"""Tests for adr.seriesmode.

The value of the mode is not "mark this disc as a series" — it is that the
episode counter carries across discs. So the tests are mostly about the
counter: that it advances by what a disc actually produced, that it cannot be
handed out twice, and that a wrong count can be corrected without re-ripping
the rest of the season.
"""

import threading
import types

import pytest
import yaml

from adr import seriesmode
from adr.config import Config


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(yaml.safe_dump({
        "raw_path": str(tmp_path / "raw"),
        "completed_path": str(tmp_path / "completed"),
        "staging_path": str(tmp_path / "staging"),
    }))
    return Config(path)


def _job():
    return types.SimpleNamespace(
        content_type="movie", title=None, year=None, tmdb_id=None,
        poster_url="http://x/film.jpg", series_season=None,
        series_first_episode=None,
    )


class TestStartAndStop:
    def test_off_by_default(self, config):
        assert seriesmode.is_active(config) is False

    def test_starting_records_the_show(self, config):
        state = seriesmode.start(config, "The Wire", season=2, first_episode=5,
                                 year=2002, tmdb_id=1438)
        assert state["active"] is True
        assert state["show"] == "The Wire"
        assert state["season"] == 2
        assert state["next_episode"] == 5

    def test_it_survives_a_restart(self, config, tmp_path):
        """The counter lives in the config file, not in memory."""
        seriesmode.start(config, "The Wire", season=2, first_episode=5)
        reloaded = Config(tmp_path / "adr.yaml")
        assert seriesmode.state(reloaded)["next_episode"] == 5

    def test_a_show_name_is_required(self, config):
        with pytest.raises(ValueError):
            seriesmode.start(config, "   ", season=1)

    def test_stopping_keeps_the_details(self, config):
        """Restarting the same season should not mean typing it all again."""
        seriesmode.start(config, "The Wire", season=2, first_episode=5)
        state = seriesmode.stop(config)
        assert state["active"] is False
        assert state["show"] == "The Wire"
        assert state["next_episode"] == 5

    def test_an_empty_show_counts_as_inactive(self, config):
        config.update({"series_mode": True, "series_mode_show": ""})
        assert seriesmode.is_active(config) is False


class TestApplyToJob:
    def test_it_stamps_the_show_and_episode_block(self, config):
        seriesmode.start(config, "The Wire", season=2, first_episode=5,
                         year=2002, tmdb_id=1438)
        job = _job()
        assert seriesmode.apply_to(job, config) is True
        assert job.content_type == "series"
        assert job.title == "The Wire"
        assert job.year == 2002
        assert job.series_season == 2
        assert job.series_first_episode == 5

    def test_the_film_poster_is_cleared(self, config):
        """It was whatever the film search found; it is not this show."""
        seriesmode.start(config, "The Wire", season=1, tmdb_id=1438)
        job = _job()
        seriesmode.apply_to(job, config)
        assert job.poster_url is None

    def test_an_inactive_mode_touches_nothing(self, config):
        job = _job()
        assert seriesmode.apply_to(job, config) is False
        assert job.content_type == "movie"
        assert job.title is None

    def test_applying_does_not_advance_the_counter(self, config):
        """How many episodes the disc holds is not known yet — reserving a
        guessed block would leave gaps when the guess was wrong."""
        seriesmode.start(config, "The Wire", season=2, first_episode=5)
        seriesmode.apply_to(_job(), config)
        assert seriesmode.state(config)["next_episode"] == 5


class TestTheCounter:
    def test_it_advances_by_what_the_disc_produced(self, config):
        seriesmode.start(config, "The Wire", season=2, first_episode=1)
        state = seriesmode.advance(config, 4)
        assert state["next_episode"] == 5
        assert state["discs_done"] == 1

    def test_disc_two_continues_where_disc_one_stopped(self, config):
        """The whole point: nobody types '5' for the second disc."""
        seriesmode.start(config, "The Wire", season=2, first_episode=1)

        first = _job()
        seriesmode.apply_to(first, config)
        assert first.series_first_episode == 1
        seriesmode.advance(config, 4)

        second = _job()
        seriesmode.apply_to(second, config)
        assert second.series_first_episode == 5

    def test_an_inactive_mode_does_not_advance(self, config):
        seriesmode.start(config, "The Wire", season=1, first_episode=1)
        seriesmode.stop(config)
        assert seriesmode.advance(config, 4)["next_episode"] == 1

    def test_a_zero_count_does_not_advance(self, config):
        """A disc that produced nothing must not consume episode numbers."""
        seriesmode.start(config, "The Wire", season=1, first_episode=3)
        assert seriesmode.advance(config, 0)["next_episode"] == 3
        assert seriesmode.advance(config, -2)["next_episode"] == 3

    def test_it_can_be_corrected_by_hand(self, config):
        """A disc with a feature-length extra pushes the count too far."""
        seriesmode.start(config, "The Wire", season=2, first_episode=1)
        seriesmode.advance(config, 5)          # one of those was a documentary
        assert seriesmode.set_next_episode(config, 5)["next_episode"] == 5

    def test_the_counter_cannot_be_set_below_one(self, config):
        seriesmode.start(config, "The Wire", season=1, first_episode=4)
        assert seriesmode.set_next_episode(config, 0)["next_episode"] == 1

    def test_two_drives_cannot_take_the_same_numbers(self, config):
        """Advancing is read-modify-write; two pipeline threads race it."""
        seriesmode.start(config, "The Wire", season=1, first_episode=1)

        def bump():
            for _ in range(20):
                seriesmode.advance(config, 1)

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert seriesmode.state(config)["next_episode"] == 81
        assert seriesmode.state(config)["discs_done"] == 80


class TestDescribe:
    def test_it_says_where_the_season_is_up_to(self, config):
        seriesmode.start(config, "The Wire", season=2, first_episode=5, year=2002)
        text = seriesmode.describe(config)
        assert "The Wire (2002)" in text
        assert "season 2" in text
        assert "episode 5" in text

    def test_an_inactive_mode_says_so(self, config):
        assert "off" in seriesmode.describe(config)


class TestThroughTheNamingLayer:
    """What actually lands on disk, not just what the counter says."""

    def _plan_for(self, config, count):
        from adr.naming import plan_output

        job = _job()
        seriesmode.apply_to(job, config)
        plan = plan_output(job, count)
        seriesmode.advance(config, len(plan.episodes))
        return plan

    def test_a_box_set_numbers_straight_through(self, config):
        seriesmode.start(config, "The Wire", season=2, first_episode=1, year=2002)

        disc1 = self._plan_for(config, 4)
        disc2 = self._plan_for(config, 4)
        disc3 = self._plan_for(config, 4)

        assert disc1.filenames[0] == "The Wire (2002) - S02E01"
        assert disc1.filenames[-1] == "The Wire (2002) - S02E04"
        assert disc2.filenames[0] == "The Wire (2002) - S02E05"
        assert disc3.filenames[0] == "The Wire (2002) - S02E09"
        assert disc3.filenames[-1] == "The Wire (2002) - S02E12"

    def test_every_disc_lands_in_the_same_season_folder(self, config):
        seriesmode.start(config, "The Wire", season=2, first_episode=1, year=2002)
        folders = {self._plan_for(config, 3).folder for _ in range(3)}
        assert folders == {"The Wire (2002)/Season 02"}

    def test_a_disc_with_an_odd_count_still_chains(self, config):
        """Disc 3 of a season often has five episodes, not four."""
        seriesmode.start(config, "Firefly", season=1, first_episode=1)
        self._plan_for(config, 4)
        self._plan_for(config, 5)
        assert self._plan_for(config, 5).filenames[0].endswith("S01E10")

    def test_a_correction_mid_season_takes_effect_on_the_next_disc(self, config):
        seriesmode.start(config, "The Wire", season=2, first_episode=1, year=2002)
        self._plan_for(config, 5)               # one was a documentary extra
        seriesmode.set_next_episode(config, 5)  # so wind it back
        assert self._plan_for(config, 4).filenames[0] == "The Wire (2002) - S02E05"
