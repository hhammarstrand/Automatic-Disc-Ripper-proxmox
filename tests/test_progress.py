"""Estimating how much longer a rip has to go.

Two properties matter more than accuracy. The estimate must reflect the recent
rate, not the average — a rip opens with a scan and a spin-up, and averaging
that in drags the number up for the rest of the run. And it must say nothing
until it can say something true: "4 hours remaining" on a disc that finishes in
twenty is worse than no estimate, because someone walks away on it.
"""

import pytest

from adr.progress import Rate, directory_size, format_eta, format_speed


def _feed(rate, points):
    """Feed (time, value) pairs, oldest first."""
    for at, value in points:
        rate.update(value, now=at)
    return rate


class TestSilenceBeforeCertainty:
    def test_a_single_sample_says_nothing(self):
        rate = Rate()
        rate.update(0.1, now=0)
        assert rate.per_second() is None
        assert rate.eta_to() is None

    def test_too_few_samples_say_nothing(self):
        rate = _feed(Rate(min_samples=3), [(0, 0.0), (30, 0.5)])
        assert rate.eta_to() is None

    def test_too_short_a_span_says_nothing(self):
        """Three readings a millisecond apart measure noise, not speed."""
        rate = _feed(Rate(min_elapsed=8), [(0, 0.0), (0.001, 0.01), (0.002, 0.02)])
        assert rate.per_second() is None

    def test_no_progress_at_all_says_nothing(self):
        """A stalled rip has no rate. Reporting 'infinite' would be worse."""
        rate = _feed(Rate(), [(0, 0.4), (10, 0.4), (20, 0.4), (30, 0.4)])
        assert rate.per_second() is None
        assert rate.eta_to() is None

    def test_nothing_recorded_at_all(self):
        assert Rate().per_second() is None
        assert Rate().eta_to() is None


class TestTheEstimate:
    def test_a_steady_rate_gives_the_obvious_answer(self):
        # 1% per second, 40% done → 60 seconds left.
        rate = _feed(Rate(), [(t, t / 100) for t in range(0, 41, 10)])
        assert rate.per_second() == pytest.approx(0.01)
        assert rate.eta_to() == pytest.approx(60, abs=1)

    def test_it_follows_the_recent_rate_not_the_average(self):
        """The opening stall must not haunt the estimate for the whole run."""
        rate = Rate(window=60)
        # Two minutes of almost nothing — the scan and the spin-up.
        for t in range(0, 121, 10):
            rate.update(0.001 * (t / 10), now=t)
        # Then a steady 1% per second.
        for step in range(1, 41):
            rate.update(0.012 + 0.01 * step, now=120 + step * 5)
        assert rate.per_second() == pytest.approx(0.002, rel=0.5)

    def test_a_faster_stretch_shortens_the_estimate(self):
        slow = _feed(Rate(), [(t, 0.001 * t) for t in range(0, 61, 10)])
        fast = _feed(Rate(), [(t, 0.01 * t) for t in range(0, 61, 10)])
        assert fast.eta_to() < slow.eta_to()

    def test_already_finished_is_zero_not_negative(self):
        rate = _feed(Rate(), [(0, 0.9), (10, 0.95), (20, 1.0), (30, 1.0)])
        assert rate.eta_to() == 0

    def test_an_arbitrary_target(self):
        rate = _feed(Rate(), [(t, float(t)) for t in range(0, 41, 10)])
        assert rate.eta_to(100) == pytest.approx(60, abs=1)


class TestStartingOver:
    def test_progress_going_backwards_starts_a_fresh_measurement(self):
        """A new title or a retry resets the count. Carrying the old samples
        across would give a negative rate and an ETA in the past."""
        rate = _feed(Rate(), [(0, 0.0), (10, 0.5), (20, 0.9)])
        rate.update(0.0, now=21)
        assert rate.per_second() is None, "the old samples should have gone"

    def test_it_recovers_after_the_reset(self):
        rate = _feed(Rate(), [(0, 0.0), (10, 0.9)])
        rate.update(0.0, now=11)
        _feed(rate, [(21, 0.1), (31, 0.2), (41, 0.3)])
        assert rate.per_second() == pytest.approx(0.01, rel=0.1)

    def test_reset_clears_everything(self):
        rate = _feed(Rate(), [(t, t / 100) for t in range(0, 41, 10)])
        assert rate.per_second() is not None
        rate.reset()
        assert rate.per_second() is None


class TestTheWindowStaysSmall:
    def test_old_samples_are_dropped(self):
        rate = Rate(window=30)
        for t in range(0, 601, 5):
            rate.update(t / 1000, now=t)
        assert len(rate._samples) <= 10, "the window must not grow without bound"

    def test_two_samples_are_always_kept(self):
        """Even when both are older than the window — otherwise a slow feed
        would empty the deque and never produce a rate again."""
        rate = Rate(window=1)
        rate.update(0.0, now=0)
        rate.update(0.5, now=100)
        assert len(rate._samples) == 2


class TestFormatting:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (90, "1m 30s"),
        (600, "10m"),
        (3600, "1h"),
        (12000, "3h 20m"),
    ])
    def test_durations_read_naturally(self, seconds, expected):
        assert format_eta(seconds) == expected

    def test_two_units_at_most(self):
        """'2h 13m 44s' implies a precision an estimate does not have, and the
        seconds are stale by the time they are read."""
        assert format_eta(8024).count(" ") <= 1

    def test_nothing_to_say(self):
        assert format_eta(None) == ""
        assert format_eta(-1) == ""

    @pytest.mark.parametrize("rate,expected", [
        (None, ""),
        (0, ""),
        (-5, ""),
        (512, "512 B/s"),
        (2048, "2 KB/s"),
        (1_572_864, "1.5 MB/s"),
        (12_582_912, "12 MB/s"),
    ])
    def test_speeds_read_naturally(self, rate, expected):
        assert format_speed(rate) == expected


class TestDirectorySize:
    def test_it_sums_the_files(self, tmp_path):
        (tmp_path / "a.mkv").write_bytes(b"x" * 100)
        (tmp_path / "b.mkv").write_bytes(b"x" * 50)
        assert directory_size(tmp_path) == 150

    def test_subdirectories_are_not_counted(self, tmp_path):
        (tmp_path / "a.mkv").write_bytes(b"x" * 100)
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.mkv").write_bytes(b"x" * 5000)
        assert directory_size(tmp_path) == 100

    def test_a_missing_directory_is_zero_not_an_error(self, tmp_path):
        assert directory_size(tmp_path / "nope") == 0

    def test_an_empty_directory_is_zero(self, tmp_path):
        assert directory_size(tmp_path) == 0


def test_a_realistic_rip(tmp_path):
    """End to end: a disc that writes 8 MB/s and is a quarter done."""
    fraction, written = Rate(), Rate()
    for step in range(0, 7):
        at = step * 10
        fraction.update(0.25 * (at / 600) if at else 0.0, now=at)
        written.update(8 * 1_048_576 * at, now=at)

    assert format_speed(written.per_second()) == "8.0 MB/s"
    eta = fraction.eta_to()
    assert eta and 1800 < eta < 3000, f"about 40 minutes left, got {format_eta(eta)}"


# ------------------------------------------------------------------ #
# What the pipeline actually publishes
# ------------------------------------------------------------------ #

class TestTheRipReportsItsPace:
    """The rip is the long phase and had no estimate at all — only a
    percentage, which says where you are but not whether to wait."""

    def _run(self, tmp_path, monkeypatch, steps):
        """Run one disc through the real pipeline with a controlled clock.

        The clock stand-in replaces the *name* `time` inside adr.pipeline, not
        an attribute of the time module — patching time.monotonic itself would
        reach every other test and every thread in the process.

        It is installed before the pipeline starts, because the rip records
        its start time before the first progress report arrives; setting the
        clock afterwards would put that start in a different epoch and make
        every elapsed time nonsense.
        """
        import json
        import queue
        import time as real_time
        import types as _types

        from adr import disctype
        from adr import pipeline as pipeline_mod
        from adr.config import Config
        from adr.disctype import DiscInfo
        from adr.models import Job, get_session, init_db
        from adr.ripper import RipResult

        clock = [1000.0]
        monkeypatch.setattr(pipeline_mod, "time", _types.SimpleNamespace(
            monotonic=lambda: clock[0],
            time=lambda: clock[0],
            sleep=real_time.sleep,
        ))

        path = tmp_path / "adr.yaml"
        path.write_text(
            f"raw_path: {tmp_path / 'raw'}\n"
            f"completed_path: {tmp_path / 'completed'}\n"
            f"staging_path: {tmp_path / 'staging'}\n"
            "notify_enabled: false\n",
        )
        init_db()
        config = Config(str(path))
        monkeypatch.setattr(pipeline_mod.Notifier, "job_failed", lambda *a, **k: True)
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video."),
        )
        drive = pipeline_mod.DrivePipeline("/dev/sr0", config, queue.Queue())
        monkeypatch.setattr(drive._ripper, "scan_disc", lambda d: {})

        published = []

        def fake_rip(drive_letter, job_id, progress_callback=None, title_index=None):
            raw = config.raw_path / str(job_id)
            raw.mkdir(parents=True, exist_ok=True)
            for fraction, written in steps:
                (raw / "title_t00.mkv").write_bytes(b"\0" * written)
                progress_callback({
                    "overall": fraction, "title_progress": fraction,
                    "title_current": 1, "title_total": 2,
                    "description": "Saving title 1/2",
                })
                other = get_session()
                try:
                    row = other.get(Job, job_id)
                    if row and row.progress_info:
                        published.append(json.loads(row.progress_info))
                finally:
                    other.close()
                clock[0] += 10
            result = RipResult()
            result.success = False
            result.error = "stop here"
            return result

        monkeypatch.setattr(drive._ripper, "rip", fake_rip)
        drive._run_pipeline("HAPPY_FEET_TWO")
        return published

    def test_it_publishes_time_left_speed_and_elapsed(self, tmp_path, monkeypatch):
        # Small files: the rate is measured from the numbers, and writing
        # gigabytes to prove that only slows the suite down.
        steps = [(i / 20, i * 64 * 1024) for i in range(1, 9)]
        published = self._run(tmp_path, monkeypatch, steps)

        assert published, "no progress was committed at all"
        last = published[-1]
        assert last["phase"] == "ripping"
        assert last["eta_seconds"] > 0
        assert last["bytes_per_second"] > 0
        assert last["elapsed_seconds"] > 0

    def test_the_speed_matches_what_was_written(self, tmp_path, monkeypatch):
        """64 KB every 10 seconds is 6.5 KB/s, whatever the percentage says.

        The point is that the speed comes from bytes on disk, not from
        MakeMKV's percentage — they are different questions, and only the
        first tells a healthy read from a drive re-reading a bad sector.
        """
        steps = [(i / 20, i * 64 * 1024) for i in range(1, 9)]
        published = self._run(tmp_path, monkeypatch, steps)
        measured = published[-1]["bytes_per_second"]
        assert measured == pytest.approx(64 * 1024 / 10, rel=0.2)

    def test_nothing_is_estimated_from_the_first_report(self, tmp_path, monkeypatch):
        """One reading is a position, not a rate."""
        published = self._run(tmp_path, monkeypatch, [(0.05, 64 * 1024)])
        assert published[0]["eta_seconds"] is None
        assert published[0]["bytes_per_second"] is None

    def test_a_stalled_rip_offers_no_estimate(self, tmp_path, monkeypatch):
        """No progress means no rate. An ETA here would be invented."""
        steps = [(0.4, 512 * 1024) for _ in range(8)]
        published = self._run(tmp_path, monkeypatch, steps)
        assert published[-1]["eta_seconds"] is None
        assert published[-1]["bytes_per_second"] is None
        # But the clock keeps running, which is how a stall becomes visible.
        assert published[-1]["elapsed_seconds"] > 0
