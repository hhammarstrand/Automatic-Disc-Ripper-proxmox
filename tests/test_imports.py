"""Files added by hand join the pipeline where a ripped disc does.

An uploaded MP4 is its own raw file; an ISO is ripped with MakeMKV from an
``iso:`` source. Either way the job is identified, planned, encoded and filed
by the same code a disc goes through.
"""

import queue
from pathlib import Path

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


# ------------------------------------------------------------------ #
# Files already on the share
# ------------------------------------------------------------------ #

class TestBrowsing:
    def test_the_roots_are_the_configured_folders_not_the_filesystem(self, config, tmp_path):
        roots = imports.browse_roots(config)
        assert tmp_path / "completed" in roots
        assert Path("/") not in roots
        assert all(tmp_path in r.parents for r in roots)

    def test_a_library_inside_the_share_is_not_listed_twice(self, config, tmp_path):
        import yaml as _yaml
        data = _yaml.safe_load(open(config._path))
        data["plex_path"] = str(tmp_path / "completed" / "Filmer")
        (tmp_path / "completed" / "Filmer").mkdir()
        config._path.write_text(_yaml.safe_dump(data))
        roots = imports.browse_roots(Config(str(config._path)))
        assert tmp_path / "completed" / "Filmer" not in roots

    def test_climbing_out_of_a_root_is_refused(self, config, tmp_path):
        for escape in ("/etc", str(tmp_path / "completed" / ".." / ".."),):
            with pytest.raises(ValueError):
                imports.list_dir(config, escape)

    def test_a_symlink_out_of_the_share_is_refused(self, config, tmp_path):
        (tmp_path / "completed" / "sneaky").symlink_to("/etc")
        with pytest.raises(ValueError):
            imports.list_dir(config, str(tmp_path / "completed" / "sneaky"))

    def test_only_folders_and_importable_files_are_listed(self, config, tmp_path):
        share = tmp_path / "completed"
        (share / "Downloads").mkdir()
        (share / "film.mkv").write_bytes(b"m")
        (share / "notes.txt").write_text("x")
        (share / ".hidden.mkv").write_bytes(b"m")
        listing = imports.list_dir(config, str(share))
        assert listing["dirs"] == ["Downloads"]
        assert [f["name"] for f in listing["files"]] == ["film.mkv"]
        assert listing["parent"] is None, "a root has nowhere further up to go"


class TestImportInPlace:
    def test_a_video_is_linked_not_copied_and_the_original_stays(self, config, tmp_path):
        original = tmp_path / "completed" / "Happy.Feet.2006.mkv"
        original.write_bytes(b"film")
        worker = imports.ImportWorker(config, queue.Queue())
        submitted = []
        worker.submit = lambda job_id, path: submitted.append((job_id, path))

        job_id = imports.import_in_place(config, str(original), worker)

        (_, linked), = submitted
        assert linked.is_symlink() and linked.resolve() == original
        assert linked.parent == config.raw_path / str(job_id)
        assert _job(job_id).disc_label == "Happy Feet 2006"

    def test_an_iso_is_read_where_it_lies_and_never_deleted(
            self, config, tmp_path, identified, monkeypatch):
        from adr import ripper as ripper_mod

        iso = tmp_path / "completed" / "HAPPY_FEET.iso"
        iso.write_bytes(b"iso")
        sources = []

        def fake_rip(self, source, job_id, progress_callback=None, title_index=None):
            sources.append(source)
            out = config.raw_path / str(job_id)
            (out / "t00.mkv").write_bytes(b"m")
            result = ripper_mod.RipResult()
            result.success = True
            return result
        monkeypatch.setattr(ripper_mod.MakeMKVRipper, "rip", fake_rip)
        encodes = queue.Queue()
        worker = imports.ImportWorker(config, encodes)
        worker.submit = lambda job_id, path: worker.process(job_id, path)

        imports.import_in_place(config, str(iso), worker)

        assert sources == [f"iso:{iso}"]
        assert iso.exists(), "somebody's ISO on the share is not ours to delete"
        assert encodes.get_nowait().input_path.name == "t00.mkv"

    def test_a_file_outside_the_share_is_refused(self, config):
        worker = imports.ImportWorker(config, queue.Queue())
        with pytest.raises(ValueError):
            imports.import_in_place(config, "/etc/passwd", worker)


def test_passthrough_copies_a_linked_original_instead_of_moving_the_link(tmp_path):
    from adr.pipeline import EncoderWorker, EncodeTask

    original = tmp_path / "share" / "film.mp4"
    original.parent.mkdir()
    original.write_bytes(b"film")
    raw = tmp_path / "raw" / "1"
    raw.mkdir(parents=True)
    link = raw / "film.mp4"
    link.symlink_to(original)
    task = EncodeTask(job_id=1, track_id=1, input_path=link,
                      output_dir=tmp_path / "out", output_filename="Film (2000)",
                      passthrough=True)

    result = EncoderWorker._passthrough(task)

    assert result.success, result.error
    assert result.output_path.name == "Film (2000).mp4"
    assert not result.output_path.is_symlink()
    assert result.output_path.read_bytes() == b"film"
    assert original.read_bytes() == b"film"
