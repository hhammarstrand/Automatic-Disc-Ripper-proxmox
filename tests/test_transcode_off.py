"""Transcoding can be turned off: the MKV off the disc is kept as it is.

The risk in a passthrough mode is not the copy — it is everything downstream
that quietly assumed MP4. Renaming, retrying and collision detection all
globbed for *.mp4, so with transcoding off they would have looked at a folder
full of finished films and concluded it was empty. Those are what most of
these tests are about.
"""

import queue
import types

import pytest

from adr import pipeline as pipeline_mod
from adr import retry
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.naming import finished_files
from adr.pipeline import EncoderWorker, EncodeTask
from adr.utils import unique_output_dir


@pytest.fixture
def config(tmp_path):
    for name in ("raw", "completed", "staging"):
        (tmp_path / name).mkdir()
    return types.SimpleNamespace(
        raw_path=tmp_path / "raw",
        completed_path=tmp_path / "completed",
        staging_path=tmp_path / "staging",
        plex_path="",
        tv_path="",
        auto_move_to_plex=False,
        stage_locally=True,
        require_completed_mount=False,
        transcode_enabled=False,
        encoder_backend="handbrake",
        handbrake_path="/usr/bin/HandBrakeCLI",
        handbrake_preset="Fast 1080p30",
        handbrake_preset_file="",
        handbrake_extra_args="",
        plex_refresh_enabled=False,
        notify_enabled=False,
    )


# ------------------------------------------------------------------ #
# The move itself
# ------------------------------------------------------------------ #

class TestPassthrough:
    def test_the_ripped_file_is_moved_not_copied(self, tmp_path):
        source = tmp_path / "raw" / "title_t00.mkv"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"the film")
        task = EncodeTask(
            job_id=1, track_id=1, input_path=source,
            output_dir=tmp_path / "out", output_filename="The Film (1999)",
            passthrough=True,
        )
        result = EncoderWorker._passthrough(task)
        assert result.success
        assert result.output_path == tmp_path / "out" / "The Film (1999).mkv"
        assert result.output_path.read_bytes() == b"the film"
        # A move, not a copy: the same bytes twice would need twice the disk.
        assert not source.exists()

    def test_the_output_directory_is_created(self, tmp_path):
        source = tmp_path / "in.mkv"
        source.write_bytes(b"x")
        task = EncodeTask(
            job_id=1, track_id=1, input_path=source,
            output_dir=tmp_path / "a" / "b", output_filename="Film",
            passthrough=True,
        )
        assert EncoderWorker._passthrough(task).success

    def test_a_missing_source_is_reported_not_raised(self, tmp_path):
        task = EncodeTask(
            job_id=1, track_id=1, input_path=tmp_path / "gone.mkv",
            output_dir=tmp_path / "out", output_filename="Film",
            passthrough=True,
        )
        result = EncoderWorker._passthrough(task)
        assert not result.success
        assert "gone.mkv" in result.error

    def test_tasks_default_to_transcoding(self, tmp_path):
        """The flag has to be asked for. Silently keeping 30 GB MKVs because a
        default flipped is not a mistake anyone would notice quickly."""
        task = EncodeTask(
            job_id=1, track_id=1, input_path=tmp_path / "x.mkv",
            output_dir=tmp_path, output_filename="Film",
        )
        assert task.passthrough is False


# ------------------------------------------------------------------ #
# What everything downstream now has to accept
# ------------------------------------------------------------------ #

class TestDownstreamAcceptsMkv:
    def test_finished_files_finds_both_containers(self, tmp_path):
        (tmp_path / "a.mp4").write_bytes(b"x")
        (tmp_path / "b.mkv").write_bytes(b"x")
        (tmp_path / "notes.txt").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        assert [p.name for p in finished_files(tmp_path)] == ["a.mp4", "b.mkv"]

    def test_finished_files_of_a_missing_directory_is_empty(self, tmp_path):
        assert finished_files(tmp_path / "nope") == []

    def test_a_collision_is_seen_through_mkvs(self, tmp_path):
        """The earlier job left MKVs. Writing the next film into the same
        folder would put two films in one Plex entry."""
        first = tmp_path / "The Film (1999)"
        first.mkdir()
        (first / "The Film (1999).mkv").write_bytes(b"x")
        second = unique_output_dir(first)
        assert second.name == "The Film (1999) (2)"

    def test_renaming_moves_the_mkv_too(self, tmp_path, config):
        session = get_session()
        try:
            init_db()
            folder = tmp_path / "DISC_LABEL"
            folder.mkdir()
            (folder / "DISC_LABEL.mkv").write_bytes(b"x")
            job = Job(drive="/dev/sr0", status=JobStatus.DONE, title="The Film",
                      year=1999, output_path=str(folder))
            session.add(job)
            session.commit()

            pipeline_mod.rename_job_output(job, session)

            new_folder = tmp_path / "The Film (1999)"
            assert new_folder.is_dir()
            assert (new_folder / "The Film (1999).mkv").exists()
            assert job.output_path == str(new_folder)
        finally:
            session.close()

    def test_a_retry_can_still_salvage_mkvs(self, tmp_path, config):
        """Before this, a failed transfer of a passthrough job reported that
        nothing was left and sent the user to find the disc again."""
        session = get_session()
        try:
            init_db()
            folder = tmp_path / "The Film (1999)"
            folder.mkdir()
            (folder / "The Film (1999).mkv").write_bytes(b"x")
            job = Job(drive="/dev/sr0", status=JobStatus.ERROR, output_path=str(folder))
            session.add(job)
            session.commit()

            plan = retry.plan(job, config)
            assert plan["can_retry"]
            assert plan["resume"] == retry.RESUME_TRANSFER
        finally:
            session.close()


# ------------------------------------------------------------------ #
# Queueing
# ------------------------------------------------------------------ #

class TestQueueing:
    def test_a_retry_keeps_the_setting(self, tmp_path, config):
        session = get_session()
        try:
            init_db()
            raw = tmp_path / "raw" / "5"
            raw.mkdir(parents=True)
            (raw / "title_t00.mkv").write_bytes(b"x")
            job = Job(id=5, drive="/dev/sr0", status=JobStatus.ERROR,
                      title="The Film", year=1999)
            session.add(job)
            session.commit()

            q = queue.Queue()
            assert retry.requeue_encode(job, session, config, q) == 1
            assert q.get_nowait().passthrough is True
        finally:
            session.close()

    def test_a_retry_transcodes_when_the_setting_is_on(self, tmp_path, config):
        config.transcode_enabled = True
        session = get_session()
        try:
            init_db()
            raw = tmp_path / "raw" / "6"
            raw.mkdir(parents=True)
            (raw / "title_t00.mkv").write_bytes(b"x")
            job = Job(id=6, drive="/dev/sr0", status=JobStatus.ERROR,
                      title="The Film", year=1999)
            session.add(job)
            session.commit()

            q = queue.Queue()
            retry.requeue_encode(job, session, config, q)
            assert q.get_nowait().passthrough is False
        finally:
            session.close()


# ------------------------------------------------------------------ #
# End to end through the worker
# ------------------------------------------------------------------ #

def test_a_passthrough_task_finishes_the_job(tmp_path, config, monkeypatch):
    """No HandBrake is installed here, and none should be needed."""
    init_db()
    session = get_session()
    try:
        out = tmp_path / "completed" / "The Film (1999)"
        job = Job(drive="/dev/sr0", status=JobStatus.RIPPED, title="The Film",
                  year=1999, output_path=str(out))
        session.add(job)
        session.commit()
        track = Track(job_id=job.id, track_number=1, filename="title_t00.mkv",
                      status=TrackStatus.PENDING)
        session.add(track)
        session.commit()
        job_id, track_id = job.id, track.id
    finally:
        session.close()

    source = tmp_path / "raw" / str(job_id) / "title_t00.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"the film")

    monkeypatch.setattr(pipeline_mod.Notifier, "job_done", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.Notifier, "job_failed", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.PlexNotifier, "refresh_for", lambda *a, **k: True)
    from adr.encoder import HandBrakeEncoder

    monkeypatch.setattr(
        HandBrakeEncoder, "encode",
        lambda *a, **k: pytest.fail("transcoding is off; HandBrake must not run"),
    )

    worker = EncoderWorker(config, queue.Queue())
    worker._process_task(EncodeTask(
        job_id=job_id, track_id=track_id, input_path=source,
        output_dir=out, output_filename="The Film (1999)",
        passthrough=True,
    ))

    session = get_session()
    try:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.DONE
        assert job.progress_encode == 1.0
        assert (out / "The Film (1999).mkv").read_bytes() == b"the film"
    finally:
        session.close()
