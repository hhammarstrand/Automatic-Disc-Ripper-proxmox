"""Bulk delete and re-encode, from the history page.

The delete half can lose someone's film collection, so the tests are mostly
about the guards: a running job is never deleted, files are never deleted
unless asked for, and what would go can be seen before it goes.
"""

import queue
import types

import pytest

from adr.config import Config
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.utils import utcnow
from web.app import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    import adr.config as config_module

    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
    for name in ("raw", "completed", "staging"):
        (tmp_path / name).mkdir()
    config = Config(str(tmp_path / "adr.yaml"))
    config.update({
        "completed_path": str(tmp_path / "completed"),
        "raw_path": str(tmp_path / "raw"),
        "staging_path": str(tmp_path / "staging"),
    })
    init_db()
    return create_app(config), config, tmp_path


def _finished_job(tmp_path, title="The Matrix", status=JobStatus.DONE):
    session = get_session()
    folder = tmp_path / "completed" / f"{title} (1999)"
    folder.mkdir(parents=True, exist_ok=True)
    movie = folder / f"{title} (1999).mp4"
    movie.write_bytes(b"X" * 4096)

    job = Job(disc_label=title.upper(), title=title, year=1999, drive="/dev/sr0",
              status=status, started_at=utcnow(), output_path=str(folder),
              rip_completed_at=utcnow())
    session.add(job)
    session.commit()
    session.add(Track(job_id=job.id, track_number=1, filename=movie.name,
                      status=TrackStatus.DONE, output_path=str(movie)))
    session.commit()
    job_id = job.id
    session.close()
    return job_id, movie


class TestSeeingWhatWouldGo:
    def test_the_preview_names_the_files(self, app):
        flask_app, _, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        result = flask_app.test_client().post(
            "/api/jobs/delete-preview", json={"ids": [job_id]}).get_json()
        assert str(movie) in result["files"]
        assert result["size"] == "4.0 KB"

    def test_the_preview_deletes_nothing(self, app):
        flask_app, _, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        flask_app.test_client().post("/api/jobs/delete-preview", json={"ids": [job_id]})
        assert movie.exists()

    def test_a_malformed_id_does_not_lose_the_others(self, app):
        """The caller is a checkbox list; one bad value must not cost the
        other nineteen."""
        flask_app, _, tmp_path = app
        job_id, _ = _finished_job(tmp_path)
        result = flask_app.test_client().post(
            "/api/jobs/delete-preview",
            json={"ids": [job_id, "nonsense", None]}).get_json()
        assert result["jobs"] == 1


class TestDeleting:
    def test_history_only_by_default(self, app):
        """Deleting rows is cheap and reversible by re-ripping. Deleting the
        library is neither, so it never happens without being asked for."""
        flask_app, _, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        result = flask_app.test_client().post(
            "/api/jobs/delete", json={"ids": [job_id]}).get_json()
        assert result["deleted"] == 1
        assert movie.exists(), "the film stays"

    def test_the_files_go_when_asked(self, app):
        flask_app, _, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        result = flask_app.test_client().post(
            "/api/jobs/delete",
            json={"ids": [job_id], "delete_files": True}).get_json()
        assert result["files_deleted"] == 1
        assert not movie.exists()

    def test_several_at_once(self, app):
        flask_app, _, tmp_path = app
        ids = [_finished_job(tmp_path, title=f"Film {n}")[0] for n in range(3)]
        result = flask_app.test_client().post(
            "/api/jobs/delete", json={"ids": ids}).get_json()
        assert result["deleted"] == 3

    def test_a_running_job_is_skipped_not_deleted(self, app):
        """Deleting a job mid-rip would leave MakeMKV writing into a folder
        nothing owns any more."""
        flask_app, _, tmp_path = app
        job_id, _ = _finished_job(tmp_path, status=JobStatus.RIPPING)
        result = flask_app.test_client().post(
            "/api/jobs/delete", json={"ids": [job_id]}).get_json()
        assert result["deleted"] == 0
        assert any("still running" in s for s in result["skipped"])

    def test_nothing_selected_is_refused(self, app):
        flask_app, _, _ = app
        response = flask_app.test_client().post("/api/jobs/delete", json={"ids": []})
        assert response.status_code == 400


class TestEncodingAgain:
    def test_it_prefers_the_raw_rip(self, app):
        flask_app, config, tmp_path = app
        job_id, _ = _finished_job(tmp_path)
        raw = tmp_path / "raw" / str(job_id)
        raw.mkdir(parents=True)
        (raw / "title_t00.mkv").write_bytes(b"M" * 2048)

        plan = flask_app.test_client().get(f"/api/jobs/{job_id}/reencode").get_json()
        assert plan["can_reencode"] is True
        assert plan["source"] == "raw"
        assert "disc is not needed" in plan["reason"]

    def test_it_falls_back_to_the_finished_file_and_says_so(self, app):
        """Encoding an encode loses a little more each time, and "re-encode"
        sounds free. It is not, and the person deciding should know."""
        flask_app, _, tmp_path = app
        job_id, _ = _finished_job(tmp_path)
        plan = flask_app.test_client().get(f"/api/jobs/{job_id}/reencode").get_json()
        assert plan["source"] == "finished"
        assert "second-generation" in plan["reason"]

    def test_nothing_on_disk_is_refused_with_a_reason(self, app):
        flask_app, _, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        movie.unlink()
        plan = flask_app.test_client().get(f"/api/jobs/{job_id}/reencode").get_json()
        assert plan["can_reencode"] is False
        assert "put the disc back in" in plan["reason"].lower()

    def test_a_rip_that_never_finished_is_not_re_encoded(self, app):
        """Its files are truncated mid-frame; an encoder can do nothing with
        them but waste an hour."""
        flask_app, _, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        movie.unlink()
        session = get_session()
        job = session.get(Job, job_id)
        job.rip_completed_at = None
        session.commit()
        session.close()
        raw = tmp_path / "raw" / str(job_id)
        raw.mkdir(parents=True)
        (raw / "title_t00.mkv").write_bytes(b"M")

        plan = flask_app.test_client().get(f"/api/jobs/{job_id}/reencode").get_json()
        assert plan["can_reencode"] is False
        assert "never finished" in plan["reason"]

    def test_asking_does_not_start_anything(self, app):
        flask_app, _, tmp_path = app
        job_id, _ = _finished_job(tmp_path)
        flask_app.test_client().get(f"/api/jobs/{job_id}/reencode")
        session = get_session()
        assert session.get(Job, job_id).status == JobStatus.DONE
        session.close()

    def test_it_queues_the_finished_file(self, app, monkeypatch):
        import web.app as app_module

        flask_app, config, tmp_path = app
        job_id, movie = _finished_job(tmp_path)
        work = queue.Queue()
        monkeypatch.setattr(
            app_module, "_pipeline_manager",
            types.SimpleNamespace(encode_queue=work), raising=False)

        result = flask_app.test_client().post(f"/api/jobs/{job_id}/reencode").get_json()
        assert result["ok"] is True
        assert result["queued"] == 1
        task = work.get_nowait()
        assert task.input_path == movie

    def test_it_never_passes_the_file_through_unchanged(self, app, monkeypatch):
        """A "re-encode" that copies the file would report success and change
        nothing, which is the worst possible reading of the button."""
        import web.app as app_module

        flask_app, config, tmp_path = app
        config.update({"transcode_enabled": False})
        job_id, _ = _finished_job(tmp_path)
        work = queue.Queue()
        monkeypatch.setattr(
            app_module, "_pipeline_manager",
            types.SimpleNamespace(encode_queue=work), raising=False)

        flask_app.test_client().post(f"/api/jobs/{job_id}/reencode")
        assert work.get_nowait().passthrough is False

    def test_without_an_encoder_running_it_says_so(self, app, monkeypatch):
        import web.app as app_module

        flask_app, _, tmp_path = app
        job_id, _ = _finished_job(tmp_path)
        monkeypatch.setattr(app_module, "_pipeline_manager", None, raising=False)
        response = flask_app.test_client().post(f"/api/jobs/{job_id}/reencode")
        assert response.status_code == 503
        assert "not running" in response.get_json()["message"]
