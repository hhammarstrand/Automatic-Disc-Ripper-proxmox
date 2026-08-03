"""Two background threads must not be killable by one bad moment.

Both had the same shape of bug: an unguarded call outside the try block, whose
failure escaped past the cleanup. For the drive pipeline that meant the lock
was never released and the drive stayed busy for the life of the service, with
no disc in it and nothing running. For the encoder worker it meant the thread
died and every future encode queued up behind a consumer that no longer
existed — silently, because a dead daemon thread says nothing.
"""

import queue
import threading

import pytest

from adr import pipeline as pipeline_mod
from adr.config import Config
from adr.models import init_db
from adr.pipeline import DrivePipeline, EncoderWorker, EncodeTask


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


# ------------------------------------------------------------------ #
# DrivePipeline
# ------------------------------------------------------------------ #

class TestDriveStaysUsable:
    def test_a_database_failure_does_not_leave_the_drive_busy(self, config, monkeypatch):
        """This is the one that matters: the lock is what makes the drive
        usable again, and it is released in a finally the exception used to
        jump over."""
        def boom():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(pipeline_mod, "get_session", boom)
        drive = DrivePipeline("/dev/sr0", config, queue.Queue())

        drive._run_pipeline(None)

        assert drive.is_busy is False

    def test_the_drive_can_start_again_afterwards(self, config, monkeypatch):
        calls = {"n": 0}
        real = pipeline_mod.get_session

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return real()

        monkeypatch.setattr(pipeline_mod, "get_session", flaky)
        monkeypatch.setattr(
            pipeline_mod.disctype, "classify",
            lambda d: pipeline_mod.disctype.DiscInfo(kind="data", detail="Data."),
        )
        monkeypatch.setattr(
            DrivePipeline, "_run_data_disc", lambda self, job, session, disc: None,
        )
        drive = DrivePipeline("/dev/sr0", config, queue.Queue())

        drive._run_pipeline(None)          # fails
        drive._run_pipeline(None)          # must be allowed to run

        assert calls["n"] == 2
        assert drive.is_busy is False

    def test_an_error_mid_pipeline_still_frees_the_drive(self, config, monkeypatch):
        monkeypatch.setattr(
            pipeline_mod.disctype, "classify",
            lambda d: (_ for _ in ()).throw(RuntimeError("sysfs went away")),
        )
        drive = DrivePipeline("/dev/sr0", config, queue.Queue())
        drive._run_pipeline(None)
        assert drive.is_busy is False


# ------------------------------------------------------------------ #
# EncoderWorker
# ------------------------------------------------------------------ #

class TestWorkerSurvives:
    def test_a_task_that_blows_up_does_not_kill_the_worker(self, config):
        """A dead worker is invisible: tasks keep arriving and nothing runs."""
        q = queue.Queue()
        worker = EncoderWorker(config, q, name="EncoderWorker-test")

        seen = []

        def explode(task):
            seen.append(task.job_id)
            if task.job_id == 1:
                raise RuntimeError("something outside the task handler")

        worker._process_task = explode
        worker.start()
        try:
            q.put(_task(1))
            q.put(_task(2))
            _wait_until(lambda: seen == [1, 2])
            assert seen == [1, 2], "the worker stopped after the first failure"
            assert worker.is_alive()
        finally:
            worker.stop()
            worker.join(timeout=5)

    def test_the_queue_is_not_left_with_unfinished_work(self, config):
        """task_done has to be called even for a task that raised, or join()
        on the queue never returns."""
        q = queue.Queue()
        worker = EncoderWorker(config, q, name="EncoderWorker-test")
        worker._process_task = lambda task: (_ for _ in ()).throw(RuntimeError("boom"))
        worker.start()
        try:
            q.put(_task(1))
            done = threading.Event()
            threading.Thread(target=lambda: (q.join(), done.set()), daemon=True).start()
            assert done.wait(timeout=5), "the queue still thinks the task is running"
        finally:
            worker.stop()
            worker.join(timeout=5)

    def test_a_database_failure_inside_a_task_is_contained(self, config, monkeypatch):
        def boom():
            raise RuntimeError("database is locked")

        monkeypatch.setattr(pipeline_mod, "get_session", boom)
        worker = EncoderWorker(config, queue.Queue())
        # No exception, and nothing left half-open.
        worker._process_task(_task(1))


def _task(job_id: int) -> EncodeTask:
    from pathlib import Path

    return EncodeTask(
        job_id=job_id, track_id=job_id, input_path=Path("/nonexistent.mkv"),
        output_dir=Path("/tmp"), output_filename="x",
    )


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = threading.Event()
    waited = 0.0
    while waited < timeout:
        if predicate():
            return
        deadline.wait(0.05)
        waited += 0.05
