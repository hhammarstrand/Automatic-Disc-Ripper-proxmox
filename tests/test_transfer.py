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


class TestASecondDiscDoesNotStealTheFirstDiscsFiles:
    """The merge sets a colliding arrival aside as "… (2).mkv" and then said
    nothing about it, so _rebase_tracks rebuilt each path as the name the file
    *would* have had — which is the name the previous disc's file already has.

    Disc 2's rows then named disc 1's episodes. The Play button opened the
    wrong file, and "delete this job with its files" unlinked the *earlier*
    disc's episode while disc 2's own copy was left referenced by nothing and
    invisible to every later preview. cleanup.job_files uses only the track
    rows for a series, so there was no second opinion.
    """

    def _job(self, src, tracks):
        import types

        return types.SimpleNamespace(
            id=2, content_type="series", output_path=str(src), plex_path=None,
            tracks=[types.SimpleNamespace(output_path=str(p)) for p in tracks],
        )

    def test_a_colliding_episode_row_points_at_the_copy_it_made(self, tmp_path):
        from adr.pipeline import _merge_into, _rebase_tracks

        src, dest = tmp_path / "stage" / "Season 01", tmp_path / "tv" / "Season 01"
        src.mkdir(parents=True)
        dest.mkdir(parents=True)
        (dest / "Show - S01E01.mkv").write_bytes(b"disc one")
        arriving = src / "Show - S01E01.mkv"
        arriving.write_bytes(b"disc two")

        job = self._job(src, [arriving])
        renames = _merge_into(src, dest, job)
        _rebase_tracks(job, src, dest, renames)

        assert (dest / "Show - S01E01 (2).mkv").read_bytes() == b"disc two"
        assert (dest / "Show - S01E01.mkv").read_bytes() == b"disc one"
        assert job.tracks[0].output_path == str(dest / "Show - S01E01 (2).mkv"), (
            "the row names the earlier disc's file"
        )

    def test_an_episode_that_did_not_collide_is_rebased_normally(self, tmp_path):
        from adr.pipeline import _merge_into, _rebase_tracks

        src, dest = tmp_path / "stage" / "Season 01", tmp_path / "tv" / "Season 01"
        src.mkdir(parents=True)
        dest.mkdir(parents=True)
        arriving = src / "Show - S01E06.mkv"
        arriving.write_bytes(b"six")

        job = self._job(src, [arriving])
        renames = _merge_into(src, dest, job)
        _rebase_tracks(job, src, dest, renames)
        assert job.tracks[0].output_path == str(dest / "Show - S01E06.mkv")

    def test_the_extras_folder_is_merged_rather_than_renamed(self, tmp_path):
        """Other/ exists on every disc of a set. Treating it as a colliding
        item gave disc 2 an "Other (2)/" folder, which is not a name Plex
        recognises — so those extras stopped being extras."""
        from adr.pipeline import _merge_into, _rebase_tracks

        src, dest = tmp_path / "stage" / "Season 01", tmp_path / "tv" / "Season 01"
        (src / "Other").mkdir(parents=True)
        (dest / "Other").mkdir(parents=True)
        (dest / "Other" / "Extra 1.mkv").write_bytes(b"disc one extra")
        arriving = src / "Other" / "Extra 1.mkv"
        arriving.write_bytes(b"disc two extra")

        job = self._job(src, [arriving])
        renames = _merge_into(src, dest, job)
        _rebase_tracks(job, src, dest, renames)

        assert not (dest / "Other (2)").exists(), "Plex does not read Other (2)"
        assert (dest / "Other" / "Extra 1.mkv").read_bytes() == b"disc one extra"
        assert (dest / "Other" / "Extra 1 (2).mkv").read_bytes() == b"disc two extra"
        assert job.tracks[0].output_path == str(
            dest / "Other" / "Extra 1 (2).mkv")

    def test_a_new_extras_folder_still_arrives_whole(self, tmp_path):
        from adr.pipeline import _merge_into

        src, dest = tmp_path / "stage" / "Season 01", tmp_path / "tv" / "Season 01"
        (src / "Other").mkdir(parents=True)
        (src / "Other" / "Extra 1.mkv").write_bytes(b"x")
        dest.mkdir(parents=True)

        _merge_into(src, dest, self._job(src, []))
        assert (dest / "Other" / "Extra 1.mkv").exists()
