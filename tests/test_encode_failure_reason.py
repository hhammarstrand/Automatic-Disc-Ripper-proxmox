"""A failed encode has to say what went wrong.

"One or more tracks failed to encode" is true of every encode failure there
has ever been. It names the symptom, does not say which track on a multi-title
disc, and sends the reader to the log to find the one line that mattered —
which is exactly what the message in the history is for.
"""

import queue
import types

import pytest

from adr import pipeline as pipeline_mod
from adr.config import Config
from adr.encoder import EncodeResult, _last_meaningful
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.pipeline import EncoderWorker, EncodeTask


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        "notify_enabled: false\n",
    )
    init_db()
    return Config(str(path))


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(pipeline_mod.Notifier, "job_done", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.Notifier, "job_failed", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.PlexNotifier, "refresh_for", lambda *a, **k: True)


def _job_with_tracks(count):
    session = get_session()
    try:
        job = Job(drive="/dev/sr0", status=JobStatus.RIPPED, title="The Film", year=1999)
        session.add(job)
        session.commit()
        ids = []
        for n in range(1, count + 1):
            track = Track(job_id=job.id, track_number=n, filename=f"t{n}.mkv",
                          status=TrackStatus.PENDING)
            session.add(track)
            session.commit()
            ids.append(track.id)
        return job.id, ids
    finally:
        session.close()


def _run(config, job_id, track_id, tmp_path, error):
    def fake_encode(self, **kwargs):
        result = EncodeResult()
        result.success = False
        result.error = error
        return result

    worker = EncoderWorker(config, queue.Queue())
    worker._encoder.encode = types.MethodType(fake_encode, worker._encoder)
    worker._process_task(EncodeTask(
        job_id=job_id, track_id=track_id,
        input_path=tmp_path / "in.mkv",
        output_dir=tmp_path / "out", output_filename="The Film (1999)",
    ))


def _reload(job_id):
    session = get_session()
    try:
        return session.get(Job, job_id)
    finally:
        session.close()


class TestTheJobSaysWhy:
    def test_a_single_track_carries_handbrakes_words(self, config, tmp_path, quiet):
        job_id, tracks = _job_with_tracks(1)
        _run(config, job_id, tracks[0], tmp_path,
             "HandBrake exited with code 1. HandBrake said: Invalid preset")

        job = _reload(job_id)
        assert job.status == JobStatus.ERROR
        assert "Invalid preset" in job.error_message
        assert "One or more tracks" not in job.error_message

    def test_the_reason_is_kept_on_the_track_too(self, config, tmp_path, quiet):
        """On a multi-title disc, which one failed is half the answer."""
        job_id, tracks = _job_with_tracks(1)
        _run(config, job_id, tracks[0], tmp_path, "no space left on device")

        session = get_session()
        try:
            track = session.get(Track, tracks[0])
            assert track.status == TrackStatus.ERROR
            assert track.error_message == "no space left on device"
        finally:
            session.close()

    def test_several_failures_are_counted_and_listed(self, config, tmp_path, quiet):
        job_id, tracks = _job_with_tracks(2)
        _run(config, job_id, tracks[0], tmp_path, "disk full")
        _run(config, job_id, tracks[1], tmp_path, "codec unavailable")

        message = _reload(job_id).error_message
        assert "2 of 2 tracks failed" in message
        assert "disk full" in message
        assert "codec unavailable" in message

    def test_identical_failures_are_not_repeated(self, config, tmp_path, quiet):
        job_id, tracks = _job_with_tracks(2)
        for track_id in tracks:
            _run(config, job_id, track_id, tmp_path, "disk full")

        message = _reload(job_id).error_message
        assert message.count("disk full") == 1

    def test_a_silent_failure_says_so_rather_than_pretending(self, config, tmp_path, quiet):
        job_id, tracks = _job_with_tracks(1)
        _run(config, job_id, tracks[0], tmp_path, None)

        message = _reload(job_id).error_message
        assert "gave no reason" in message
        assert "tool output" in message


class TestHandBrakesLastWords:
    def test_an_error_line_beats_a_status_banner(self):
        assert _last_meaningful([
            "Invalid preset: Super HQ",
            "Encode done!",
            "HandBrake has exited.",
        ]) == "Invalid preset: Super HQ"

    def test_the_latest_error_wins(self):
        assert _last_meaningful(["error: first", "error: second"]) == "error: second"

    def test_without_an_error_line_the_last_real_line_is_used(self):
        assert _last_meaningful(["Scanning title 1", "Encode done!"]) == "Scanning title 1"

    def test_nothing_but_noise_says_nothing(self):
        assert _last_meaningful(["Encode done!", "HandBrake has exited."]) == ""

    def test_nothing_at_all(self):
        assert _last_meaningful([]) == ""

    @pytest.mark.parametrize("line", [
        "Error opening input",
        "Failed to open output",
        "cannot allocate memory",
        "No such file or directory",
        "invalid argument",
    ])
    def test_the_words_that_mark_a_real_failure(self, line):
        assert _last_meaningful(["Scanning", line, "Encode done!"]) == line
