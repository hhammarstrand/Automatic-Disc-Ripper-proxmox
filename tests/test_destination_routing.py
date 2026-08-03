"""Tests for where a finished film is written, and how many times.

A job bound for the Plex library used to be written to completed_path first and
moved afterwards. When the library is on a NAS that is a multi-GB network write
into a folder nothing reads — and if the two paths sit on different mounts, a
second full copy on top. The finished folder must cross the network once, into
the folder it is going to live in.
"""

import pathlib
import types

import pytest

from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.pipeline import final_destination, move_to_plex
from adr.utils import utcnow


def _config(tmp_path, plex="", **overrides):
    """A stand-in for adr.config.Config with just the keys this code reads."""
    data = {
        "completed_path": tmp_path / "completed",
        "plex_path": str(plex) if plex else "",
        "tv_path": "",
        "staging_path": tmp_path / "staging",
        "stage_locally": True,
        "require_completed_mount": False,
    }
    data.update(overrides)
    return types.SimpleNamespace(**data)


@pytest.fixture
def finished_job(tmp_path):
    """A job whose encoded folder is sitting in local staging."""
    init_db()
    staging = tmp_path / "staging" / "The Matrix (1999)"
    staging.mkdir(parents=True)
    (staging / "The Matrix (1999).mp4").write_bytes(b"X" * 4096)

    session = get_session()
    job = Job(
        disc_label="THE_MATRIX_1999", title="The Matrix", year=1999,
        drive="/dev/sr0", status=JobStatus.ENCODING,
        started_at=utcnow(), output_path=str(staging),
    )
    session.add(job)
    session.commit()
    session.add(Track(
        job_id=job.id, track_number=1, filename="title_t00.mkv",
        status=TrackStatus.DONE, output_path=str(staging / "The Matrix (1999).mp4"),
    ))
    session.commit()
    yield session, job, staging
    session.close()


class TestFinalDestination:
    def test_plex_job_goes_straight_to_the_library(self, tmp_path):
        plex = tmp_path / "plex"
        job = types.SimpleNamespace(move_to_plex=True, content_type="movie")
        parent, is_plex = final_destination(job, _config(tmp_path, plex=plex))

        assert parent == plex, "completed_path is not a waypoint on the way to Plex"
        assert is_plex is True

    def test_unflagged_job_uses_completed_path(self, tmp_path):
        plex = tmp_path / "plex"
        job = types.SimpleNamespace(move_to_plex=False, content_type="movie")
        parent, is_plex = final_destination(job, _config(tmp_path, plex=plex))

        assert parent == tmp_path / "completed"
        assert is_plex is False

    def test_no_plex_configured_uses_completed_path(self, tmp_path):
        job = types.SimpleNamespace(move_to_plex=True, content_type="movie")
        parent, is_plex = final_destination(job, _config(tmp_path))

        assert parent == tmp_path / "completed"
        assert is_plex is False, "a flag without a library path decides nothing"


class TestNothingIsWrittenTwice:
    """The point of the change, stated as an observable fact."""

    def test_completed_path_is_never_touched_for_a_plex_job(self, finished_job, tmp_path):
        from adr.pipeline import transfer_to_destination

        session, job, _ = finished_job
        job.move_to_plex = True
        session.commit()

        completed = tmp_path / "completed"
        completed.mkdir()
        plex = tmp_path / "plex"
        plex.mkdir()
        config = _config(tmp_path, plex=plex)

        parent, _is_plex = final_destination(job, config)
        assert transfer_to_destination(job, session, parent) is True
        move_to_plex(job, session, config)

        assert list(completed.iterdir()) == [], (
            "the film reached the library without ever being written to completed_path"
        )
        assert (plex / "The Matrix (1999)" / "The Matrix (1999).mp4").exists()

    def test_move_to_plex_is_a_no_op_once_the_file_is_there(self, finished_job, tmp_path):
        session, job, staging = finished_job
        plex = tmp_path / "plex"
        plex.mkdir()
        delivered = plex / "The Matrix (1999)"
        staging.rename(delivered)
        job.move_to_plex = True
        job.output_path = str(delivered)
        session.commit()

        config = _config(tmp_path, plex=plex)
        assert move_to_plex(job, session, config) is True

        assert job.output_path == str(delivered), "the folder must not be moved again"
        assert job.plex_path == str(delivered)
        assert (delivered / "The Matrix (1999).mp4").exists()
        assert not (plex / "The Matrix (1999) (2)").exists(), (
            "re-running must not create a duplicate beside the original"
        )

    def test_an_unflagged_job_still_lands_in_completed(self, finished_job, tmp_path):
        from adr.pipeline import transfer_to_destination

        session, job, _ = finished_job
        job.move_to_plex = False
        session.commit()

        plex = tmp_path / "plex"
        plex.mkdir()
        config = _config(tmp_path, plex=plex)

        parent, _ = final_destination(job, config)
        assert transfer_to_destination(job, session, parent) is True
        move_to_plex(job, session, config)

        assert (tmp_path / "completed" / "The Matrix (1999)").exists()
        assert list(plex.iterdir()) == []


class TestStagingFollowsTheRealDestination:
    def test_staging_is_decided_by_where_the_film_is_going(self, tmp_path, monkeypatch):
        """With a NAS-backed library, the encode must still be staged locally."""
        import adr.storage as storage

        plex = tmp_path / "plex"
        network = {str(plex)}
        monkeypatch.setattr(
            storage, "describe_path",
            lambda p, _orig=storage.describe_path: (
                {**_orig(p), "is_network": str(p) in network}
            ),
        )
        assert storage.should_stage(plex, True) is True
        assert storage.should_stage(tmp_path / "completed", True) is False


class TestPathsAreRecorded:
    def test_track_paths_follow_the_move(self, finished_job, tmp_path):
        from adr.pipeline import transfer_to_destination

        session, job, _ = finished_job
        job.move_to_plex = True
        session.commit()
        plex = tmp_path / "plex"
        plex.mkdir()

        parent, _ = final_destination(job, _config(tmp_path, plex=plex))
        transfer_to_destination(job, session, parent)
        move_to_plex(job, session, _config(tmp_path, plex=plex))

        session.refresh(job)
        assert job.plex_path == str(plex / "The Matrix (1999)")
        for track in job.tracks:
            assert pathlib.Path(track.output_path).exists()
