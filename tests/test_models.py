"""Tests for adr.models — Job/Track models and status constants."""


import pytest

from adr.models import (
    ACTIVE_STATUSES,
    ENCODE_PHASE_STATUSES,
    RIP_PHASE_STATUSES,
    TERMINAL_STATUSES,
    JobStatus,
    TrackStatus,
)

# ------------------------------------------------------------------ #
# Status enum values
# ------------------------------------------------------------------ #

class TestJobStatus:
    def test_all_values_present(self):
        expected = {"pending", "identifying", "ripping", "ripped", "encoding", "done", "cancelled", "error"}
        actual = {s.value for s in JobStatus}
        assert actual == expected

    def test_value_access(self):
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.DONE.value == "done"
        assert JobStatus.ERROR.value == "error"


class TestTrackStatus:
    def test_all_values_present(self):
        expected = {"pending", "encoding", "done", "error"}
        actual = {s.value for s in TrackStatus}
        assert actual == expected


# ------------------------------------------------------------------ #
# Status frozensets
# ------------------------------------------------------------------ #

class TestStatusSets:
    def test_active_statuses_correct(self):
        assert frozenset({
            JobStatus.PENDING,
            JobStatus.IDENTIFYING,
            JobStatus.RIPPING,
            JobStatus.RIPPED,
            JobStatus.ENCODING,
        }) == ACTIVE_STATUSES

    def test_rip_phase_statuses_correct(self):
        assert frozenset({
            JobStatus.PENDING,
            JobStatus.IDENTIFYING,
            JobStatus.RIPPING,
        }) == RIP_PHASE_STATUSES

    def test_encode_phase_statuses_correct(self):
        assert frozenset({
            JobStatus.RIPPED,
            JobStatus.ENCODING,
        }) == ENCODE_PHASE_STATUSES

    def test_terminal_statuses_correct(self):
        assert frozenset({
            JobStatus.DONE,
            JobStatus.ERROR,
            JobStatus.CANCELLED,
        }) == TERMINAL_STATUSES

    def test_active_and_terminal_are_disjoint(self):
        assert ACTIVE_STATUSES.isdisjoint(TERMINAL_STATUSES)

    def test_active_plus_terminal_covers_all(self):
        all_statuses = ACTIVE_STATUSES | TERMINAL_STATUSES
        for status in JobStatus:
            assert status in all_statuses

    def test_rip_plus_encode_subset_of_active(self):
        assert RIP_PHASE_STATUSES | ENCODE_PHASE_STATUSES <= ACTIVE_STATUSES

    def test_sets_are_frozen(self):
        """frozensets should not be mutable."""
        with pytest.raises(AttributeError):
            ACTIVE_STATUSES.add(JobStatus.DONE)  # type: ignore


class TestTellingTwoDiscsOfOneSetApart:
    """Every disc of a box set renders as the same string. Three cards all
    reading "Life on Seacrow Island (1964)" is no help at all with two drives
    loaded and a third disc waiting behind them — and it is the state a box
    set spends its whole evening in.
    """

    def _job(self, **values):
        from adr.models import Job

        base = dict(disc_label="", title=None, year=None, drive="/dev/sr0",
                    content_type="movie", series_season=None)
        base.update(values)
        return Job(**base)

    def test_the_label_says_which_disc_before_anything_is_planned(self):
        """While it is ripping there are no episode numbers yet, and the
        label is all there is — which is also all most labels carry."""
        job = self._job(disc_label="Saltkråkan DVD 2", title="Life on Seacrow Island",
                        year=1964, content_type="series", series_season=1)
        assert job.disc_hint == "Disc 2"

    def test_the_episodes_take_over_once_they_are_known(self):
        """Better than the disc number, because it says what is *on* it."""
        from adr.models import Track

        job = self._job(disc_label="Saltkråkan DVD 2", title="Show",
                        content_type="series", series_season=1)
        job.tracks = [
            Track(track_number=n - 5, filename=f"t{n}.mkv", episode_number=n)
            for n in (6, 7, 8, 9, 10)
        ]
        assert job.disc_hint == "S01E06–E10"

    def test_a_single_episode_is_not_a_range(self):
        from adr.models import Track

        job = self._job(content_type="series", series_season=2)
        job.tracks = [Track(track_number=1, filename="t.mkv", episode_number=4)]
        assert job.disc_hint == "S02E04"

    def test_extras_do_not_widen_the_range(self):
        """An extra has no episode number, which is exactly how it is
        recorded — and it must not appear as episode 0."""
        from adr.models import Track

        job = self._job(content_type="series", series_season=1)
        job.tracks = [
            Track(track_number=1, filename="a.mkv", episode_number=6),
            Track(track_number=2, filename="b.mkv", episode_number=None),
            Track(track_number=3, filename="c.mkv", episode_number=7),
        ]
        assert job.disc_hint == "S01E06–E07"

    def test_season_zero_is_not_rewritten_to_one(self):
        from adr.models import Track

        job = self._job(content_type="series", series_season=0)
        job.tracks = [Track(track_number=1, filename="t.mkv", episode_number=1)]
        assert job.disc_hint == "S00E01"

    def test_an_ordinary_film_says_nothing(self):
        """Two different films already have two different titles, and a badge
        on every card would be noise."""
        job = self._job(disc_label="LG_COMBI_RECORDER", title="Jumanji", year=1995)
        assert job.disc_hint == ""

    def test_the_title_itself_is_left_alone(self):
        """It names the programme, and it is what the rematch dialog and the
        notifications quote."""
        job = self._job(disc_label="Saltkråkan DVD 2", title="Life on Seacrow Island",
                        year=1964, content_type="series")
        assert job.display_title == "Life on Seacrow Island (1964)"

    def test_it_reaches_the_api(self):
        job = self._job(disc_label="SHOW_D3", title="Show", content_type="series")
        assert job.to_dict()["disc_hint"] == "Disc 3"
