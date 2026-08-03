"""Tests for adr.retry.

Retry is not one operation. A rip is forty minutes and several gigabytes, and
most failures happen after the expensive part — so the question that matters is
which part still exists on disk. These tests pin down that the answer comes
from the filesystem rather than from the job's status, and that a job with
nothing left says so instead of pretending.
"""

import queue
import types

import pytest

from adr import retry
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.utils import utcnow


@pytest.fixture
def config(tmp_path):
    for name in ("raw", "completed", "staging"):
        (tmp_path / name).mkdir()
    return types.SimpleNamespace(
        raw_path=tmp_path / "raw",
        completed_path=tmp_path / "completed",
        staging_path=tmp_path / "staging",
        plex_path="",
        auto_move_to_plex=False,
        stage_locally=True,
        require_completed_mount=False,
    )


@pytest.fixture
def failed_job(tmp_path):
    init_db()
    session = get_session()
    job = Job(
        disc_label="THE_MATRIX", title="The Matrix", year=1999,
        drive="/dev/sr0", status=JobStatus.ERROR,
        error_message="Destination not mounted", started_at=utcnow(),
    )
    session.add(job)
    session.commit()
    yield session, job
    session.close()


def _make_encoded(config, job, tmp_path, count=1):
    """A finished encode sitting in staging, as after a failed transfer."""
    out = config.staging_path / "The Matrix (1999)"
    out.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        name = "The Matrix (1999).mp4" if count == 1 else f"The Matrix (1999) - pt{i+1}.mp4"
        (out / name).write_bytes(b"X" * 2048)
    job.output_path = str(out)
    return out


def _make_raw(config, job, count=1):
    """Raw MKVs from a completed rip, as after a failed encode."""
    raw_dir = config.raw_path / str(job.id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (raw_dir / f"title_t{i:02d}.mkv").write_bytes(b"M" * 4096)
    return raw_dir


class TestPlan:
    def test_a_successful_job_cannot_be_retried(self, failed_job, config):
        session, job = failed_job
        job.status = JobStatus.DONE
        session.commit()
        result = retry.plan(job, config)
        assert result["can_retry"] is False
        assert "failed or cancelled" in result["reason"]

    def test_intact_encoded_files_mean_only_the_transfer_is_redone(
        self, failed_job, config, tmp_path,
    ):
        """The most common failure, and the cheapest to recover from."""
        session, job = failed_job
        _make_encoded(config, job, tmp_path)
        session.commit()

        result = retry.plan(job, config)
        assert result["resume"] == retry.RESUME_TRANSFER
        assert result["can_retry"] is True
        assert "no re-encoding" in result["reason"]

    def test_raw_files_mean_the_encode_is_redone(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=2)
        session.commit()

        result = retry.plan(job, config)
        assert result["resume"] == retry.RESUME_ENCODE
        assert result["can_retry"] is True
        assert "disc is not needed" in result["reason"]
        assert len(result["files"]) == 2

    def test_encoded_files_win_over_raw_files(self, failed_job, config, tmp_path):
        """Both present: resume from the furthest point, not the earliest."""
        session, job = failed_job
        _make_raw(config, job)
        _make_encoded(config, job, tmp_path)
        session.commit()
        assert retry.plan(job, config)["resume"] == retry.RESUME_TRANSFER

    def test_nothing_on_disk_says_so_honestly(self, failed_job, config):
        session, job = failed_job
        result = retry.plan(job, config)
        assert result["can_retry"] is False
        assert result["resume"] == retry.RESUME_IMPOSSIBLE
        assert "disc back in" in result["reason"]

    def test_a_stale_output_path_is_not_believed(self, failed_job, config):
        """The status says one thing; only the filesystem is authoritative."""
        session, job = failed_job
        job.output_path = "/gone/The Matrix (1999)"
        session.commit()
        assert retry.plan(job, config)["can_retry"] is False

    def test_a_cancelled_job_can_also_be_retried(self, failed_job, config):
        session, job = failed_job
        job.status = JobStatus.CANCELLED
        _make_raw(config, job)
        session.commit()
        assert retry.plan(job, config)["can_retry"] is True


class TestRetryTransfer:
    def test_a_still_broken_destination_is_reported_before_trying(
        self, failed_job, config, tmp_path,
    ):
        """A second identical error twenty seconds later helps nobody."""
        session, job = failed_job
        _make_encoded(config, job, tmp_path)
        session.commit()
        config.completed_path = tmp_path / "does-not-exist"

        ok, message = retry.retry_transfer(job, session, config)
        assert ok is False
        assert "still is not usable" in message

    def test_a_working_destination_completes_the_job(self, failed_job, config, tmp_path):
        session, job = failed_job
        _make_encoded(config, job, tmp_path)
        session.commit()

        ok, message = retry.retry_transfer(job, session, config)
        assert ok is True
        assert job.status == JobStatus.DONE
        assert job.error_message is None
        assert (config.completed_path / "The Matrix (1999)" / "The Matrix (1999).mp4").exists()
        assert "The Matrix" in message

    def test_the_staging_copy_is_gone_afterwards(self, failed_job, config, tmp_path):
        session, job = failed_job
        staged = _make_encoded(config, job, tmp_path)
        session.commit()
        retry.retry_transfer(job, session, config)
        assert not staged.exists()


class TestRequeueEncode:
    def test_every_raw_file_is_queued(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=3)
        session.commit()

        q = queue.Queue()
        assert retry.requeue_encode(job, session, config, q) == 3
        assert q.qsize() == 3

    def test_the_job_returns_to_encoding_with_the_error_cleared(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job)
        session.commit()

        retry.requeue_encode(job, session, config, queue.Queue())
        session.refresh(job)
        assert job.status == JobStatus.ENCODING
        assert job.error_message is None
        assert job.completed_at is None

    def test_stale_tracks_from_the_failed_attempt_are_replaced(self, failed_job, config):
        """Inheriting the previous attempt's error state is hard to reason about."""
        session, job = failed_job
        session.add(Track(
            job_id=job.id, track_number=1, filename="old.mkv",
            status=TrackStatus.ERROR, output_path="/gone/old.mp4",
        ))
        session.commit()
        _make_raw(config, job, count=2)

        retry.requeue_encode(job, session, config, queue.Queue())
        session.refresh(job)
        assert len(job.tracks) == 2
        assert all(t.status == TrackStatus.PENDING for t in job.tracks)
        assert not any(t.filename == "old.mkv" for t in job.tracks)

    def test_the_queued_tasks_name_the_right_files(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=2)
        session.commit()

        q = queue.Queue()
        retry.requeue_encode(job, session, config, q)
        tasks = [q.get(), q.get()]
        assert {t.input_path.name for t in tasks} == {"title_t00.mkv", "title_t01.mkv"}
        # Multiple tracks get part suffixes so they do not overwrite each other.
        assert {t.output_filename for t in tasks} == {
            "The Matrix (1999) - pt1", "The Matrix (1999) - pt2",
        }

    def test_a_single_track_gets_no_part_suffix(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=1)
        session.commit()

        q = queue.Queue()
        retry.requeue_encode(job, session, config, q)
        assert q.get().output_filename == "The Matrix (1999)"

    def test_no_raw_files_queues_nothing(self, failed_job, config):
        session, job = failed_job
        q = queue.Queue()
        assert retry.requeue_encode(job, session, config, q) == 0
        assert q.empty()
