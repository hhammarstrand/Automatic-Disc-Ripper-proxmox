"""Is that all of them?

Feeding a box set one disc at a time, that is the question after every disc,
and nothing answered it: the disc came out, the folder grew, and whether
episode 9 existed anywhere was something to notice weeks later in Plex.

How many *discs* a set has cannot be looked up — the number is a property of
one physical release in one region, and TMDb describes programmes rather than
pressings. The episode list can be, which is the better question anyway: discs
are packaging, episodes are what you wanted, and counting episodes also catches
a gap in the middle that counting discs never would.
"""

import types
from pathlib import Path

import pytest

from adr import seasoncheck


def _job(tmp_path, season=1, tmdb_id=42, content_type="series"):
    return types.SimpleNamespace(
        content_type=content_type, series_season=season, tmdb_id=tmdb_id,
        plex_path=str(tmp_path), output_path=str(tmp_path),
    )


def _config(key="tmdbkey"):
    return types.SimpleNamespace(tmdb_api_key=key)


def _episodes(tmp_path, numbers, season=1, show="Life on Seacrow Island (1964)"):
    for number in numbers:
        (tmp_path / f"{show} - S{season:02d}E{number:02d}.mp4").write_bytes(b"x")


def _tmdb(monkeypatch, count):
    monkeypatch.setattr(
        "adr.identify.get_season_episodes",
        lambda tmdb_id, season, key: [
            {"episode_number": n, "name": f"Episode {n}", "air_date": ""}
            for n in range(1, count + 1)
        ],
    )


class TestWhatIsOnDisk:
    def test_it_reads_the_episode_numbers_out_of_the_filenames(self, tmp_path):
        _episodes(tmp_path, [1, 2, 3])
        assert seasoncheck.episodes_on_disk(tmp_path, 1) == {1, 2, 3}

    def test_another_season_in_the_same_folder_is_not_counted(self, tmp_path):
        """Deliberately different episode numbers per season. The first
        version of this test used 1 and 2 for both, so ignoring the season
        entirely produced the same set and the test passed on the bug."""
        _episodes(tmp_path, [1, 2], season=1)
        _episodes(tmp_path, [7, 8], season=2)
        assert seasoncheck.episodes_on_disk(tmp_path, 1) == {1, 2}
        assert seasoncheck.episodes_on_disk(tmp_path, 2) == {7, 8}

    def test_extras_are_not_episodes(self, tmp_path):
        _episodes(tmp_path, [1])
        (tmp_path / "Other").mkdir()
        (tmp_path / "Extra 1.mp4").write_bytes(b"x")
        assert seasoncheck.episodes_on_disk(tmp_path, 1) == {1}

    def test_mkv_counts_too(self, tmp_path):
        """Transcoding off leaves MKVs, and they are just as much an episode."""
        (tmp_path / "Show (1964) - S01E04.mkv").write_bytes(b"x")
        assert seasoncheck.episodes_on_disk(tmp_path, 1) == {4}

    def test_a_folder_that_is_not_there_is_not_a_crash(self, tmp_path):
        assert seasoncheck.episodes_on_disk(tmp_path / "gone", 1) == set()


class TestHowCompleteTheSeasonIs:
    def test_it_says_what_is_missing(self, tmp_path, monkeypatch):
        _episodes(tmp_path, [1, 2, 3, 4, 5])
        _tmdb(monkeypatch, 13)
        out = seasoncheck.check(_job(tmp_path), _config())
        assert out["known"] is True
        assert out["missing"] == [6, 7, 8, 9, 10, 11, 12, 13]
        assert "5 of 13" in out["text"]
        assert "6-13" in out["text"]
        assert "Put the next disc in" in out["text"]

    def test_a_gap_in_the_middle_is_found(self, tmp_path, monkeypatch):
        """The thing counting discs could never have caught: a disc that
        failed halfway, or a title skipped for a navigation error."""
        _episodes(tmp_path, [1, 2, 3, 6, 7])
        _tmdb(monkeypatch, 7)
        out = seasoncheck.check(_job(tmp_path), _config())
        assert out["missing"] == [4, 5]
        assert "4-5" in out["text"]

    def test_a_complete_season_says_so(self, tmp_path, monkeypatch):
        _episodes(tmp_path, list(range(1, 14)))
        _tmdb(monkeypatch, 13)
        out = seasoncheck.check(_job(tmp_path), _config())
        assert out["missing"] == []
        assert "complete" in out["text"]

    def test_scattered_gaps_read_as_ranges(self, tmp_path, monkeypatch):
        """Five missing episodes one by one is a wall of numbers; this line is
        read in a notification on a phone."""
        _episodes(tmp_path, [1, 5, 6, 7, 12])
        _tmdb(monkeypatch, 12)
        out = seasoncheck.check(_job(tmp_path), _config())
        assert "2-4" in out["text"] and "8-11" in out["text"]


class TestWhenItRefusesToAnswer:
    """Saying nothing beats guessing: "0 of 0 episodes" reads as a fault, and
    the season may well be complete."""

    def test_a_film_is_not_asked_about(self, tmp_path, monkeypatch):
        _tmdb(monkeypatch, 13)
        out = seasoncheck.check(_job(tmp_path, content_type="movie"), _config())
        assert out["known"] is False and out["text"] == ""

    def test_a_show_with_no_tmdb_match_says_it_cannot_check(
        self, tmp_path, monkeypatch,
    ):
        """Named off the disc label, so there is nothing to count against."""
        _episodes(tmp_path, [1, 2])
        out = seasoncheck.check(_job(tmp_path, tmdb_id=None), _config())
        assert out["known"] is False
        assert "unknown" in out["text"]
        assert "name it from the dashboard" in out["text"]

    def test_no_api_key_is_the_same_case(self, tmp_path):
        _episodes(tmp_path, [1, 2])
        out = seasoncheck.check(_job(tmp_path), _config(key=""))
        assert out["known"] is False

    def test_tmdb_not_listing_the_season_says_nothing(self, tmp_path, monkeypatch):
        _episodes(tmp_path, [1, 2])
        _tmdb(monkeypatch, 0)
        out = seasoncheck.check(_job(tmp_path), _config())
        assert out["known"] is False and out["text"] == ""

    def test_tmdb_falling_over_does_not_fail_the_job(self, tmp_path, monkeypatch):
        def boom(tmdb_id, season, key):
            raise RuntimeError("TMDb exploded")

        monkeypatch.setattr("adr.identify.get_season_episodes", boom)
        _episodes(tmp_path, [1, 2])
        assert seasoncheck.check(_job(tmp_path), _config())["text"] == ""

    def test_a_missing_folder_says_nothing(self, tmp_path, monkeypatch):
        _tmdb(monkeypatch, 13)
        job = _job(tmp_path)
        job.plex_path = str(tmp_path / "gone")
        job.output_path = str(tmp_path / "gone")
        assert seasoncheck.check(job, _config())["known"] is False


class TestItReachesTheUser:
    def test_the_notification_carries_it(self):
        """The whole point is that whoever fed the disc walked away, and "put
        the next one in" is the one thing they can act on without coming
        back."""
        from adr.notify import Notifier

        sent = {}
        config = types.SimpleNamespace(
            notify_enabled=True, notify_provider="ntfy",
            notify_url="https://ntfy.sh/x", notify_token="",
            notify_events=["job_done"],
        )
        notifier = Notifier(config)
        notifier.notify = lambda event, title, message: sent.update(
            {"event": event, "message": message}) or True
        job = types.SimpleNamespace(display_title="Saltkråkan", avg_fps=None)
        notifier.job_done(job, "/mnt/media/Serier", "Season 1 has 5 of 13 episodes.")
        assert "5 of 13" in sent["message"]

    def test_a_film_notification_is_unchanged(self):
        from adr.notify import Notifier

        sent = {}
        config = types.SimpleNamespace(
            notify_enabled=True, notify_provider="ntfy",
            notify_url="https://ntfy.sh/x", notify_token="",
            notify_events=["job_done"],
        )
        notifier = Notifier(config)
        notifier.notify = lambda event, title, message: sent.update(
            {"message": message}) or True
        job = types.SimpleNamespace(display_title="Jumanji (1995)", avg_fps=42.0)
        notifier.job_done(job, "/mnt/media/Filmer")
        assert sent["message"].endswith("fps average.")


class TestRanges:
    @pytest.mark.parametrize("numbers,expected", [
        ([9, 10, 11, 12, 13], "9-13"),
        ([4, 5], "4-5"),
        ([7], "7"),
        ([2, 4, 6], "2, 4, 6"),
        ([1, 2, 5, 6, 7, 20], "1-2, 5-7, 20"),
        ([], ""),
    ])
    def test_runs_collapse(self, numbers, expected):
        assert seasoncheck._runs(numbers) == expected
