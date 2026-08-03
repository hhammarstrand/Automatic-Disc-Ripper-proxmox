"""Jobs left mid-flight by a restart are closed out at startup.

A job's progress is in the database; the thread doing the work is not. Before
this, updating the app mid-rip left a job saying RIPPING for ever — the
dashboard showed a rip in progress and the drive it named stayed busy, so the
card offered no way to start again. Updating mid-rip is not a rare thing to
do: it is what happens when someone presses Update while a disc is in.
"""

import queue
import types

import pytest

from adr import recovery
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db


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
        transcode_enabled=True,
    )


@pytest.fixture
def db():
    init_db()
    session = get_session()
    yield session
    session.close()


def _job(session, status, **kwargs):
    job = Job(drive="/dev/sr0", status=status, **kwargs)
    session.add(job)
    session.commit()
    return job


def _raw_files(config, job_id, count=1):
    raw = config.raw_path / str(job_id)
    raw.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (raw / f"title_t{i:02d}.mkv").write_bytes(b"x")
    return raw


# ------------------------------------------------------------------ #
# Nothing to do
# ------------------------------------------------------------------ #

class TestQuiet:
    def test_an_empty_database_is_not_a_problem(self, config, db):
        assert recovery.recover_interrupted_jobs(config, queue.Queue()) == {
            "resumed": [], "failed": [],
        }

    def test_finished_jobs_are_left_alone(self, config, db):
        for status in (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED):
            _job(db, status)
        recovery.recover_interrupted_jobs(config, queue.Queue())
        statuses = {j.status for j in db.query(Job).all()}
        assert statuses == {JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED}


# ------------------------------------------------------------------ #
# Mid-rip
# ------------------------------------------------------------------ #

class TestMidRip:
    @pytest.mark.parametrize(
        "status", [JobStatus.PENDING, JobStatus.IDENTIFYING, JobStatus.RIPPING],
    )
    def test_it_is_failed_with_a_reason(self, config, db, status):
        job = _job(db, status)
        outcome = recovery.recover_interrupted_jobs(config, queue.Queue())
        db.refresh(job)
        assert outcome["failed"] == [job.id]
        assert job.status == JobStatus.ERROR
        assert "Interrupted when the service restarted" in job.error_message
        assert job.completed_at is not None

    def test_the_message_says_what_to_do_next(self, config, db):
        """The disc is usually still in the drive, and Rip starts it again."""
        job = _job(db, JobStatus.RIPPING)
        recovery.recover_interrupted_jobs(config, queue.Queue())
        db.refresh(job)
        assert "press Rip" in job.error_message

    def test_a_truncated_rip_is_not_resumed(self, config, db):
        """MakeMKV was killed part-way; what it wrote is not a film."""
        job = _job(db, JobStatus.RIPPING)
        _raw_files(config, job.id)
        q = queue.Queue()
        recovery.recover_interrupted_jobs(config, q)
        assert q.empty()
        db.refresh(job)
        assert job.status == JobStatus.ERROR


# ------------------------------------------------------------------ #
# Mid-encode
# ------------------------------------------------------------------ #

class TestMidEncode:
    @pytest.mark.parametrize("status", [JobStatus.RIPPED, JobStatus.ENCODING])
    def test_the_encode_is_simply_queued_again(self, config, db, status):
        """The expensive part is already done and sitting on disk."""
        job = _job(db, status, title="The Film", year=1999)
        _raw_files(config, job.id, count=2)

        q = queue.Queue()
        outcome = recovery.recover_interrupted_jobs(config, q)

        db.refresh(job)
        assert outcome["resumed"] == [job.id]
        assert job.status == JobStatus.ENCODING
        assert job.error_message is None
        assert q.qsize() == 2

    def test_the_old_tracks_are_replaced(self, config, db):
        job = _job(db, JobStatus.ENCODING, title="The Film", year=1999)
        db.add(Track(job_id=job.id, track_number=1, filename="old.mkv",
                     status=TrackStatus.ERROR))
        db.commit()
        _raw_files(config, job.id)

        recovery.recover_interrupted_jobs(config, queue.Queue())
        db.refresh(job)
        assert [t.status for t in job.tracks] == [TrackStatus.PENDING]

    def test_finished_files_wait_for_a_manual_retry(self, config, db):
        """The encode is done; the move is what failed, and the destination may
        be exactly why the service was restarted."""
        out = config.staging_path / "The Film (1999)"
        out.mkdir(parents=True)
        (out / "The Film (1999).mp4").write_bytes(b"x")
        job = _job(db, JobStatus.ENCODING, title="The Film", year=1999,
                   output_path=str(out))

        q = queue.Queue()
        outcome = recovery.recover_interrupted_jobs(config, q)

        db.refresh(job)
        assert outcome["failed"] == [job.id]
        assert job.status == JobStatus.ERROR
        assert "press Retry" in job.error_message
        assert q.empty()

    def test_nothing_on_disk_is_said_plainly(self, config, db):
        job = _job(db, JobStatus.ENCODING, title="The Film", year=1999)
        recovery.recover_interrupted_jobs(config, queue.Queue())
        db.refresh(job)
        assert job.status == JobStatus.ERROR
        assert "nothing to resume from" in job.error_message


# ------------------------------------------------------------------ #
# Robustness
# ------------------------------------------------------------------ #

class TestRobustness:
    def test_one_bad_job_does_not_strand_the_others(self, config, db, monkeypatch):
        first = _job(db, JobStatus.RIPPING)
        second = _job(db, JobStatus.RIPPING)

        real = recovery._recover_one

        def explode(job, session, cfg, q):
            if job.id == first.id:
                raise RuntimeError("bad row")
            return real(job, session, cfg, q)

        monkeypatch.setattr(recovery, "_recover_one", explode)
        outcome = recovery.recover_interrupted_jobs(config, queue.Queue())

        db.refresh(second)
        assert second.status == JobStatus.ERROR
        assert outcome["failed"] == [second.id]

    def test_a_database_that_will_not_open_does_not_stop_startup(self, config, monkeypatch):
        def boom():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(recovery, "get_session", boom)
        assert recovery.recover_interrupted_jobs(config, queue.Queue()) == {
            "resumed": [], "failed": [],
        }

    def test_jobs_are_handled_in_order(self, config, db):
        ids = [_job(db, JobStatus.RIPPING).id for _ in range(3)]
        outcome = recovery.recover_interrupted_jobs(config, queue.Queue())
        assert outcome["failed"] == sorted(ids)
