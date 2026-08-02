"""Tests for the local-staging transfer step in adr.pipeline.

Encoding is staged on local disk when the destination is network storage, so
HandBrake is not writing across the network for the whole encode. The finished
folder is then transferred once. These tests cover that transfer — above all,
that a failure never destroys a finished rip.
"""

import pathlib

import pytest

from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.pipeline import transfer_to_destination
from adr.utils import utcnow


@pytest.fixture
def job_in_staging(tmp_path):
    """A finished job whose output is sitting in a local staging directory."""
    init_db()
    staging = tmp_path / "staging" / "The Matrix (1999)"
    staging.mkdir(parents=True)
    movie = staging / "The Matrix (1999).mp4"
    movie.write_bytes(b"X" * 4096)

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
        status=TrackStatus.DONE, output_path=str(movie),
    ))
    session.commit()
    yield session, job, staging, movie
    session.close()


class TestSuccessfulTransfer:
    def test_files_end_up_at_the_destination(self, job_in_staging, tmp_path):
        session, job, staging, _ = job_in_staging
        dest_parent = tmp_path / "nas"
        dest_parent.mkdir()

        assert list(dest_parent.iterdir()) == [], "nothing should reach the NAS before the transfer"
        assert transfer_to_destination(job, session, dest_parent) is True

        moved = dest_parent / "The Matrix (1999)" / "The Matrix (1999).mp4"
        assert moved.exists()
        assert moved.read_bytes() == b"X" * 4096
        assert not staging.exists(), "staging copy should be gone after a move"

    def test_job_and_track_paths_are_updated(self, job_in_staging, tmp_path):
        session, job, _, _ = job_in_staging
        dest_parent = tmp_path / "nas"
        dest_parent.mkdir()

        transfer_to_destination(job, session, dest_parent)
        session.refresh(job)

        assert job.output_path == str(dest_parent / "The Matrix (1999)")
        assert job.tracks[0].output_path == str(
            dest_parent / "The Matrix (1999)" / "The Matrix (1999).mp4"
        )

    def test_destination_parent_is_created_if_missing(self, job_in_staging, tmp_path):
        session, job, _, _ = job_in_staging
        dest_parent = tmp_path / "nas" / "not" / "yet"
        assert transfer_to_destination(job, session, dest_parent) is True
        assert (dest_parent / "The Matrix (1999)").is_dir()

    def test_existing_folder_is_not_overwritten(self, job_in_staging, tmp_path):
        session, job, _, _ = job_in_staging
        dest_parent = tmp_path / "nas"
        clash = dest_parent / "The Matrix (1999)"
        clash.mkdir(parents=True)
        (clash / "existing.mp4").write_bytes(b"KEEP")

        assert transfer_to_destination(job, session, dest_parent) is True
        assert (clash / "existing.mp4").read_bytes() == b"KEEP", "must not clobber earlier rip"
        assert (dest_parent / "The Matrix (1999) (2)" / "The Matrix (1999).mp4").exists()


class TestFailedTransfer:
    def test_a_finished_rip_is_never_lost(self, job_in_staging, tmp_path):
        """The critical guarantee: a failed transfer must not destroy the file.

        The destination is a regular file, so mkdir fails for any user
        (including root, which lets this assert hold in CI containers).
        """
        session, job, staging, movie = job_in_staging
        broken = tmp_path / "nas_is_a_file"
        broken.write_text("not a directory")

        assert transfer_to_destination(job, session, broken) is False
        assert movie.exists(), "the encoded film must still be there"
        assert movie.read_bytes() == b"X" * 4096, "and be byte-for-byte intact"
        assert staging.exists()

    def test_error_message_says_where_the_files_are(self, job_in_staging, tmp_path):
        session, job, staging, _ = job_in_staging
        broken = tmp_path / "nas_is_a_file"
        broken.write_text("not a directory")

        transfer_to_destination(job, session, broken)
        assert str(staging) in job.error_message
        assert "re-run" in job.error_message

    def test_missing_staging_directory_is_reported(self, job_in_staging, tmp_path):
        session, job, staging, _ = job_in_staging
        import shutil as _shutil
        _shutil.rmtree(staging)

        assert transfer_to_destination(job, session, tmp_path / "nas") is False
        assert "not found in staging" in job.error_message

    def test_job_without_output_path_is_reported(self, job_in_staging, tmp_path):
        session, job, _, _ = job_in_staging
        job.output_path = None
        assert transfer_to_destination(job, session, tmp_path / "nas") is False
        assert job.error_message


def test_pipeline_stages_only_for_network_destinations(monkeypatch, tmp_path):
    """A local destination must not pay for a pointless extra copy."""
    import adr.storage as s

    assert s.should_stage(tmp_path, enabled=True) is False
    monkeypatch.setattr(s, "describe_path", lambda p: {"is_network": True})
    assert s.should_stage(pathlib.Path("/mnt/nas"), enabled=True) is True
