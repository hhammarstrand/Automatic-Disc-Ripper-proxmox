"""Files added by hand join the pipeline where a ripped disc does.

An uploaded MP4 is its own raw file; an ISO is ripped with MakeMKV from an
``iso:`` source. Either way the job is identified, planned, encoded and filed
by the same code a disc goes through.
"""

import queue

import pytest
import yaml

from adr import imports
from adr.config import Config
from adr.identify import MovieInfo
from adr.models import Job, JobStatus, get_session, init_db
from web.app import create_app


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(yaml.safe_dump({
        "raw_path": str(tmp_path / "raw"),
        "completed_path": str(tmp_path / "completed"),
        "staging_path": str(tmp_path / "staging"),
        "plex_path": str(tmp_path / "plex"),
        "auto_move_to_plex": True,
        "notify_enabled": False,
        "main_feature_only": True,
    }))
    for d in ("raw", "completed", "staging", "plex"):
        (tmp_path / d).mkdir()
    init_db()
    return Config(str(path))


@pytest.fixture
def identified(monkeypatch):
    monkeypatch.setattr(
        "adr.identify.identify_disc",
        lambda label, key: MovieInfo("Happy Feet", 2006, tmdb_id=9836, confidence=1.0),
    )


def _job(job_id):
    session = get_session()
    try:
        return session.get(Job, job_id)
    finally:
        session.close()


class TestTheWorker:
    def test_a_video_file_is_named_and_queued_like_a_ripped_disc(self, config, identified):
        job, target = imports.create_job(config, "happy feet.mp4")
        target.write_bytes(b"x" * 1024)
        encodes = queue.Queue()

        imports.ImportWorker(config, encodes).process(job.id, target)

        task = encodes.get_nowait()
        assert task.input_path == target
        assert task.output_filename == "Happy Feet (2006)"
        # Bound for the library, as a confident match from a disc would be.
        assert str(config.plex_path) in str(task.output_dir)
        done = _job(job.id)
        assert done.status == JobStatus.ENCODING
        assert done.move_to_plex is True

    def test_an_iso_is_ripped_from_an_iso_source_and_then_dropped(
            self, config, identified, monkeypatch):
        from adr import ripper as ripper_mod

        job, target = imports.create_job(config, "HAPPY_FEET.iso")
        target.write_bytes(b"iso")
        sources = []

        def fake_rip(self, source, job_id, progress_callback=None, title_index=None):
            sources.append(source)
            (target.parent / "title_t00.mkv").write_bytes(b"m" * 2048)
            result = ripper_mod.RipResult()
            result.success = True
            return result
        monkeypatch.setattr(ripper_mod.MakeMKVRipper, "rip", fake_rip)
        encodes = queue.Queue()

        imports.ImportWorker(config, encodes).process(job.id, target)

        assert sources == [f"iso:{target}"]
        assert not target.exists(), "the image is several GB nothing will read again"
        assert encodes.get_nowait().input_path.name == "title_t00.mkv"
        assert _job(job.id).rip_completed_at is not None

    def test_an_iso_makemkv_cannot_read_fails_the_job(self, config, identified, monkeypatch):
        from adr import ripper as ripper_mod

        job, target = imports.create_job(config, "broken.iso")
        target.write_bytes(b"iso")

        def fake_rip(self, *a, **k):
            result = ripper_mod.RipResult()
            result.error = "MakeMKV could not open the image"
            return result
        monkeypatch.setattr(ripper_mod.MakeMKVRipper, "rip", fake_rip)
        encodes = queue.Queue()

        imports.ImportWorker(config, encodes).process(job.id, target)

        assert encodes.empty()
        failed = _job(job.id)
        assert failed.status == JobStatus.ERROR
        assert "could not open" in failed.error_message


def test_makemkv_is_handed_an_iso_source_unchanged():
    from adr.ripper import MakeMKVRipper

    assert MakeMKVRipper._make_dev_source("iso:/opt/adr/raw/3/a.iso") == "iso:/opt/adr/raw/3/a.iso"


class TestTheEndpoint:
    @pytest.fixture
    def client(self, config, monkeypatch):
        from unittest.mock import MagicMock

        import web.app as app_mod

        manager = MagicMock()
        app = create_app(config, pipeline_manager=manager)
        app.config["TESTING"] = True
        monkeypatch.setattr(imports, "free_bytes", lambda c: 100 * 1024**3)
        monkeypatch.setattr("adr.preflight.destination_blocker", lambda c: None)
        monkeypatch.setattr(app_mod, "_pipeline_manager", manager)
        return app.test_client(), manager

    def test_the_body_lands_where_a_rip_would_have_put_it(self, client, config):
        http, manager = client
        response = http.put("/api/upload?name=Happy%20Feet.mp4", data=b"film" * 100)

        assert response.status_code == 200, response.get_json()
        job_id = response.get_json()["job_id"]
        written = config.raw_path / str(job_id) / "Happy_Feet.mp4"
        assert written.read_bytes() == b"film" * 100
        manager.import_worker.submit.assert_called_once_with(job_id, written)

    def test_anything_but_video_and_images_is_refused(self, client):
        http, manager = client
        response = http.put("/api/upload?name=notes.txt", data=b"hello")
        assert response.status_code == 400
        manager.import_worker.submit.assert_not_called()

    def test_a_file_too_big_for_the_disk_is_refused_before_it_is_sent(
            self, client, monkeypatch):
        http, manager = client
        monkeypatch.setattr(imports, "free_bytes", lambda c: 1024)
        response = http.put("/api/upload?name=a.iso", data=b"x" * 10)
        assert response.status_code == 507
        assert "Not enough room" in response.get_json()["error"]

    def test_a_destination_that_cannot_take_it_is_said_up_front(self, client, monkeypatch):
        http, _ = client
        monkeypatch.setattr("adr.preflight.destination_blocker",
                            lambda c: "Destination /mnt/media is not writable")
        response = http.put("/api/upload?name=a.mp4", data=b"x")
        assert response.status_code == 409
        assert "not writable" in response.get_json()["error"]


@pytest.mark.parametrize("filename, label", [
    ("The.Matrix.1999.1080p.BluRay.x264.mkv", "The Matrix 1999"),
    ("Jumanji.1995.mp4", "Jumanji 1995"),
    ("Happy_Feet_(2006)_[DVDRip].mkv", "Happy Feet 2006"),
    ("HAPPY_FEET.iso", "HAPPY FEET"),
    ("2001 A Space Odyssey 1968.mkv", "2001 A Space Odyssey 1968"),
])
def test_a_filename_reads_like_a_disc_label(filename, label):
    assert imports.label_from_filename(filename) == label
