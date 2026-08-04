"""The pipeline routes a disc by what is actually on it.

Before this existed, every disc went to MakeMKV. An audio CD or a data disc
came back as "no titles found", which is exactly what an unreachable drive
looks like — so the failure sent people to debug hardware that was fine.
These tests run the real DrivePipeline against a real database, with the
drive and the two rippers stood in for.
"""

import queue

import pytest

from adr import disctype
from adr import pipeline as pipeline_mod
from adr.config import Config
from adr.disctype import DiscInfo, Toc, TocTrack
from adr.models import Job, JobStatus, get_session, init_db
from adr.musicbrainz import AlbumInfo


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        "eject_after_rip: false\n"
        "notify_enabled: false\n",
    )
    return Config(str(path))


@pytest.fixture
def drive(config):
    init_db()
    return pipeline_mod.DrivePipeline("/dev/sr0", config, queue.Queue())


@pytest.fixture
def no_notifications(monkeypatch):
    """Notifications are tested elsewhere; here they only add network calls."""
    monkeypatch.setattr(pipeline_mod.Notifier, "job_done", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.Notifier, "job_failed", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_mod.Notifier, "disc_inserted", lambda *a, **k: True)


def _audio_toc(count=2):
    tracks = [TocTrack(number=i + 1, lba=i * 15000, is_audio=True) for i in range(count)]
    return Toc(first=1, last=count, leadout_lba=count * 15000, tracks=tracks)


def _audio_disc(count=2):
    return DiscInfo(kind=disctype.KIND_AUDIO, detail="Audio CD.", toc=_audio_toc(count))


def _data_disc():
    return DiscInfo(kind=disctype.KIND_DATA, detail="Data disc.", root_entries=["SETUP.EXE"])


def _fresh_job(session, drive="/dev/sr0", label=None):
    job = Job(disc_label=label, drive=drive, status=JobStatus.IDENTIFYING)
    session.add(job)
    session.commit()
    return job


# ------------------------------------------------------------------ #
# Routing
# ------------------------------------------------------------------ #

class TestRouting:
    def test_an_audio_cd_never_reaches_makemkv(self, drive, monkeypatch, no_notifications):
        monkeypatch.setattr(disctype, "classify", lambda d: _audio_disc())
        seen = {}
        monkeypatch.setattr(
            pipeline_mod.DrivePipeline, "_run_audio_cd",
            lambda self, job, session, disc: seen.setdefault("kind", disc.kind),
        )
        monkeypatch.setattr(
            drive._ripper, "rip",
            lambda **kw: pytest.fail("MakeMKV must not be handed an audio CD"),
        )
        drive._run_pipeline(None)
        assert seen["kind"] == disctype.KIND_AUDIO

    def test_a_data_disc_never_reaches_makemkv(self, drive, monkeypatch, no_notifications):
        monkeypatch.setattr(disctype, "classify", lambda d: _data_disc())
        seen = {}
        monkeypatch.setattr(
            pipeline_mod.DrivePipeline, "_run_data_disc",
            lambda self, job, session, disc: seen.setdefault("kind", disc.kind),
        )
        monkeypatch.setattr(
            drive._ripper, "rip",
            lambda **kw: pytest.fail("MakeMKV must not be handed a data disc"),
        )
        drive._run_pipeline(None)
        assert seen["kind"] == disctype.KIND_DATA

    def test_the_kind_is_recorded_on_the_job(self, drive, monkeypatch, no_notifications):
        monkeypatch.setattr(disctype, "classify", lambda d: _audio_disc())
        monkeypatch.setattr(
            pipeline_mod.DrivePipeline, "_run_audio_cd", lambda self, job, session, disc: None,
        )
        drive._run_pipeline(None)
        session = get_session()
        try:
            assert session.query(Job).one().content_type == disctype.KIND_AUDIO
        finally:
            session.close()

    def test_a_video_disc_takes_the_old_path(self, drive, monkeypatch, no_notifications):
        """A DVD must be unaffected by any of this."""
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video."),
        )
        monkeypatch.setattr(
            pipeline_mod.DrivePipeline, "_run_audio_cd",
            lambda *a: pytest.fail("a video disc must not go to the audio path"),
        )
        monkeypatch.setattr(
            pipeline_mod.DrivePipeline, "_run_data_disc",
            lambda *a: pytest.fail("a video disc must not go to the imaging path"),
        )
        reached = {}

        def fake_rip(**kwargs):
            reached["ripped"] = True
            return _rip_failure()

        monkeypatch.setattr(drive._ripper, "scan_disc", lambda d, job_id=None: {})
        monkeypatch.setattr(drive._ripper, "rip", fake_rip)
        drive._run_pipeline(None)
        assert reached.get("ripped")

    def test_a_video_disc_keeps_content_type_movie(self, drive, monkeypatch, no_notifications):
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video."),
        )
        monkeypatch.setattr(drive._ripper, "scan_disc", lambda d, job_id=None: {})
        monkeypatch.setattr(drive._ripper, "rip", lambda **kw: _rip_failure())
        drive._run_pipeline(None)
        session = get_session()
        try:
            assert session.query(Job).one().content_type == "movie"
        finally:
            session.close()


def _rip_failure():
    """A MakeMKV result that ends the video path early, without ripping."""
    from adr.ripper import RipResult
    out = RipResult()
    out.success = False
    out.error = "stubbed — the video path was reached, which is all this checks"
    return out


# ------------------------------------------------------------------ #
# Audio CDs
# ------------------------------------------------------------------ #

class TestAudioCD:
    def test_a_finished_cd_is_done_with_tracks(self, drive, config, monkeypatch, no_notifications):
        files = [config.music_path / "Kent" / "Isola (1997)" / "01 - 747.flac"]
        files[0].parent.mkdir(parents=True, exist_ok=True)
        files[0].write_bytes(b"x" * 2048)

        monkeypatch.setattr(
            pipeline_mod.musicbrainz, "lookup",
            lambda toc: AlbumInfo(disc_id="d", artist="Kent", album="Isola", year=1997),
        )
        monkeypatch.setattr(
            pipeline_mod.AudioCDRipper, "rip",
            lambda self, **kw: _audio_result(True, files, files[0].parent),
        )

        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_audio_cd(job, session, _audio_disc())
            session.refresh(job)
            assert job.status == JobStatus.DONE
            assert job.title == "Kent — Isola"
            assert job.year == 1997
            assert job.progress_rip == 1.0
            # No encode phase: a bar frozen at 40% for good reads as a hang.
            assert job.progress_encode == 1.0
            assert job.output_path == str(files[0].parent)
            assert [t.filename for t in job.tracks] == ["01 - 747.flac"]
        finally:
            session.close()

    def test_a_failed_cd_is_an_error(self, drive, monkeypatch, no_notifications):
        monkeypatch.setattr(pipeline_mod.musicbrainz, "lookup", lambda toc: AlbumInfo(disc_id="d"))
        monkeypatch.setattr(
            pipeline_mod.AudioCDRipper, "rip",
            lambda self, **kw: _audio_result(False, [], None, "the disc is damaged"),
        )
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_audio_cd(job, session, _audio_disc())
            session.refresh(job)
            assert job.status == JobStatus.ERROR
            assert "damaged" in job.error_message
        finally:
            session.close()

    def test_partial_success_is_still_done_but_says_so(self, drive, config, monkeypatch,
                                                       no_notifications):
        out = config.music_path / "Unknown Artist" / "Unidentified CD d"
        out.mkdir(parents=True, exist_ok=True)
        one = out / "01 - Track 01.flac"
        one.write_bytes(b"x")
        monkeypatch.setattr(pipeline_mod.musicbrainz, "lookup", lambda toc: AlbumInfo(disc_id="d"))
        monkeypatch.setattr(
            pipeline_mod.AudioCDRipper, "rip",
            lambda self, **kw: _audio_result(True, [one], out, "Ripped 1 of 2 tracks; failed: 2."),
        )
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_audio_cd(job, session, _audio_disc())
            session.refresh(job)
            assert job.status == JobStatus.DONE
            assert "failed: 2" in job.error_message
        finally:
            session.close()

    def test_turning_the_feature_off_leaves_the_disc_alone(self, drive, config, monkeypatch,
                                                           no_notifications):
        config.update({"audio_cd_enabled": False})
        monkeypatch.setattr(
            pipeline_mod.AudioCDRipper, "rip",
            lambda self, **kw: pytest.fail("the ripper must not run when the feature is off"),
        )
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_audio_cd(job, session, _audio_disc())
            session.refresh(job)
            # Cancelled, not errored: nothing went wrong, and an error would
            # fire a failure notification for a setting chosen on purpose.
            assert job.status == JobStatus.CANCELLED
            assert "turned off under Settings" in job.error_message
        finally:
            session.close()

    def test_a_toc_that_vanished_is_reported_not_crashed(self, drive, monkeypatch,
                                                         no_notifications):
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_audio_cd(job, session, DiscInfo(kind=disctype.KIND_AUDIO, detail="", toc=None))
            session.refresh(job)
            assert job.status == JobStatus.CANCELLED
            assert "Try it again" in job.error_message
        finally:
            session.close()

    def test_an_unidentified_cd_keeps_its_label(self, drive, config, monkeypatch,
                                                no_notifications):
        out = config.music_path / "Unknown Artist" / "Unidentified CD abc-"
        out.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            pipeline_mod.musicbrainz, "lookup", lambda toc: AlbumInfo(disc_id="abc-"),
        )
        monkeypatch.setattr(
            pipeline_mod.AudioCDRipper, "rip", lambda self, **kw: _audio_result(True, [], out),
        )
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_audio_cd(job, session, _audio_disc())
            session.refresh(job)
            assert job.title is None
            assert "abc-" in job.disc_label
        finally:
            session.close()


def _audio_result(success, files, output_dir, error=None):
    from adr.audiocd import AudioRipResult
    return AudioRipResult(
        success=success, files=list(files), output_dir=output_dir, error=error,
    )


# ------------------------------------------------------------------ #
# Data discs
# ------------------------------------------------------------------ #

class TestDataDisc:
    def test_a_finished_image_is_done(self, drive, config, monkeypatch, no_notifications):
        image = config.data_disc_path / "SETUP.iso"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"x" * 4096)
        monkeypatch.setattr(
            pipeline_mod.isobackup, "create_image",
            lambda **kw: _iso_result(True, image, 4096),
        )
        session = get_session()
        try:
            job = _fresh_job(session, label="SETUP")
            drive._run_data_disc(job, session, _data_disc())
            session.refresh(job)
            assert job.status == JobStatus.DONE
            assert job.output_path == str(image)
            assert job.title == "SETUP"
            assert [t.filename for t in job.tracks] == ["SETUP.iso"]
        finally:
            session.close()

    def test_a_failed_image_is_an_error(self, drive, monkeypatch, no_notifications):
        monkeypatch.setattr(
            pipeline_mod.isobackup, "create_image",
            lambda **kw: _iso_result(False, None, 0, "the disc could not be read"),
        )
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_data_disc(job, session, _data_disc())
            session.refresh(job)
            assert job.status == JobStatus.ERROR
            assert "could not be read" in job.error_message
        finally:
            session.close()

    def test_turning_the_feature_off_leaves_the_disc_alone(self, drive, config, monkeypatch,
                                                           no_notifications):
        config.update({"data_disc_enabled": False})
        monkeypatch.setattr(
            pipeline_mod.isobackup, "create_image",
            lambda **kw: pytest.fail("imaging must not run when the feature is off"),
        )
        session = get_session()
        try:
            job = _fresh_job(session)
            drive._run_data_disc(job, session, _data_disc())
            session.refresh(job)
            assert job.status == JobStatus.CANCELLED
        finally:
            session.close()

    def test_the_label_names_the_image(self, drive, monkeypatch, no_notifications):
        captured = {}

        def fake(**kwargs):
            captured.update(kwargs)
            return _iso_result(False, None, 0, "stop here")

        monkeypatch.setattr(pipeline_mod.isobackup, "create_image", fake)
        session = get_session()
        try:
            drive._run_data_disc(_fresh_job(session, label="WIN98"), session, _data_disc())
        finally:
            session.close()
        assert captured["label"] == "WIN98"

    def test_cancelling_mid_image_is_not_reported_as_a_failure(self, drive, session_cancel,
                                                               monkeypatch, no_notifications):
        """Cancel arrives while the copy is running, which is the only time it
        can — so the stub cancels from inside create_image, as the real one
        would observe it through should_cancel."""
        state = {}

        def cancel_then_stop(**kwargs):
            session_cancel(state["job_id"])
            return _iso_result(False, None, 0, "Cancelled.")

        monkeypatch.setattr(pipeline_mod.isobackup, "create_image", cancel_then_stop)
        session = get_session()
        try:
            job = _fresh_job(session)
            state["job_id"] = job.id
            drive._run_data_disc(job, session, _data_disc())
            session.refresh(job)
            assert job.status == JobStatus.CANCELLED
            assert job.completed_at is not None
        finally:
            session.close()


@pytest.fixture
def session_cancel():
    """Mark a job cancelled the way the web UI's cancel button does.

    From another session, because that is where the button's request runs.
    """
    def _cancel(job_id):
        other = get_session()
        try:
            row = other.get(Job, job_id)
            row.status = JobStatus.CANCELLED
            other.commit()
        finally:
            other.close()

    return _cancel


def _iso_result(success, path, size, error=None):
    from adr.isobackup import IsoResult
    return IsoResult(success=success, path=path, size_bytes=size, error=error)


# ------------------------------------------------------------------ #
# Progress plumbing
# ------------------------------------------------------------------ #

class TestProgressCommitter:
    def test_the_first_and_final_reports_get_through(self, drive, monkeypatch):
        session = get_session()
        try:
            job = _fresh_job(session)
            report = pipeline_mod._progress_committer(job, session, "imaging")
            report({"overall": 0.0, "description": "start"})
            report({"overall": 1.0, "description": "end"})
            session.refresh(job)
            assert job.progress_rip == 1.0
            assert '"phase": "imaging"' in job.progress_info
            assert "end" in job.progress_info
        finally:
            session.close()

    def test_reports_in_between_are_throttled(self, drive):
        session = get_session()
        try:
            job = _fresh_job(session)
            report = pipeline_mod._progress_committer(job, session, "ripping", min_interval=999)
            report({"overall": 0.1})
            report({"overall": 0.5})
            session.refresh(job)
            assert job.progress_rip == pytest.approx(0.1)
        finally:
            session.close()

    def test_a_finished_job_never_sits_below_one(self, drive):
        session = get_session()
        try:
            job = _fresh_job(session)
            report = pipeline_mod._progress_committer(job, session, "ripping", min_interval=999)
            report({"overall": 0.1})
            report({"overall": 1.0})
            session.refresh(job)
            assert job.progress_rip == 1.0
        finally:
            session.close()

    def test_backwards_jumps_are_ignored(self, drive):
        session = get_session()
        try:
            job = _fresh_job(session)
            report = pipeline_mod._progress_committer(job, session, "ripping", min_interval=0)
            report({"overall": 0.6})
            report({"overall": 0.2})
            session.refresh(job)
            assert job.progress_rip == pytest.approx(0.6)
        finally:
            session.close()
