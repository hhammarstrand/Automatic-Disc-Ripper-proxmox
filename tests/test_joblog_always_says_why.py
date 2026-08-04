"""Every failed job leaves something in its own log.

The terminal icon in the history is where someone looks to answer "why did
this fail". It was empty for the two failures most likely to happen: the
destination check, which runs before any tool does, and an exception in the
pipeline itself. So a run of red jobs could be completely undiagnosable from
the UI — which is exactly when someone needs it.

The disc-type decision is recorded for every disc for the same reason. A video
disc that logged nothing left no way to tell "classification ran and chose
video" from "classification never ran".
"""

import queue

import pytest

from adr import disctype, joblog
from adr import pipeline as pipeline_mod
from adr.config import Config
from adr.disctype import DiscInfo
from adr.models import Job, JobStatus, get_session, init_db


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        "notify_enabled: false\n"
        "eject_after_rip: false\n",
    )
    return Config(str(path))


@pytest.fixture
def drive(config, monkeypatch):
    init_db()
    monkeypatch.setattr(pipeline_mod.Notifier, "job_failed", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.Notifier, "disc_inserted", lambda *a, **k: True)
    return pipeline_mod.DrivePipeline("/dev/sr0", config, queue.Queue())


def _log_of_last_job(config) -> str:
    session = get_session()
    try:
        job = session.query(Job).order_by(Job.id.desc()).first()
        assert job is not None, "no job was created"
        return joblog.read(config, job.id)
    finally:
        session.close()


def _last_job():
    session = get_session()
    try:
        return session.query(Job).order_by(Job.id.desc()).first()
    finally:
        session.close()


class TestTheDecisionIsRecorded:
    def test_a_video_disc_says_so(self, drive, config, monkeypatch):
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video disc: found VIDEO_TS."),
        )
        monkeypatch.setattr(drive._ripper, "scan_disc", lambda d: {})
        monkeypatch.setattr(drive._ripper, "rip", lambda **kw: _rip_failure())
        drive._run_pipeline("HAPPY_FEET_TWO")
        assert "found VIDEO_TS" in _log_of_last_job(config)

    def test_an_audio_cd_says_so(self, drive, config, monkeypatch):
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_AUDIO, detail="Audio CD with 12 tracks."),
        )
        monkeypatch.setattr(
            pipeline_mod.DrivePipeline, "_run_audio_cd", lambda self, j, s, d: None,
        )
        drive._run_pipeline(None)
        assert "Audio CD with 12 tracks" in _log_of_last_job(config)


class TestFailuresAreDiagnosable:
    def test_the_destination_failure_reaches_the_log(self, drive, config, monkeypatch):
        """This runs before any tool, so nothing else would have written."""
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video."),
        )
        monkeypatch.setattr(
            pipeline_mod.preflight, "destination_blocker",
            lambda config: "Destination /mnt/media/Filmer is not writable by uid 8420.",
        )
        drive._run_pipeline("HAPPY_FEET_TWO")

        assert _last_job().status == JobStatus.ERROR
        log = _log_of_last_job(config)
        assert "Aborted before ripping" in log
        assert "not writable by uid 8420" in log

    def test_a_pipeline_exception_reaches_the_log(self, drive, config, monkeypatch):
        def boom(_device):
            raise RuntimeError("the drive fell off the bus")

        monkeypatch.setattr(disctype, "classify", boom)
        drive._run_pipeline("HAPPY_FEET_TWO")

        log = _log_of_last_job(config)
        assert "the drive fell off the bus" in log
        assert "Traceback" in log, "the traceback is the useful part"

    def test_the_drive_is_still_free_afterwards(self, drive, monkeypatch):
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        drive._run_pipeline(None)
        assert drive.is_busy is False


def _rip_failure():
    from adr.ripper import RipResult

    result = RipResult()
    result.success = False
    result.error = "stubbed"
    return result
