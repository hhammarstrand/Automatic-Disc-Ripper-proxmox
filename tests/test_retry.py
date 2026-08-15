"""Tests for adr.retry.

Retry is not one operation. A rip is forty minutes and several gigabytes, and
most failures happen after the expensive part — so the question that matters is
which part still exists on disk. These tests pin down that the answer comes
from the filesystem rather than from the job's status, and that a job with
nothing left says so instead of pretending.
"""

import queue
import types

import pytest

from adr import retry
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
from adr.utils import utcnow


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
def failed_job(tmp_path):
    init_db()
    session = get_session()
    job = Job(
        disc_label="THE_MATRIX", title="The Matrix", year=1999,
        drive="/dev/sr0", status=JobStatus.ERROR,
        error_message="Destination not mounted", started_at=utcnow(),
    )
    session.add(job)
    session.commit()
    yield session, job
    session.close()


def _make_encoded(config, job, tmp_path, count=1, tracks_done=True):
    """A finished encode sitting in staging, as after a failed transfer.

    The Track rows matter as much as the files. HandBrake writes straight to
    the final name with no temp file, so a truncated encode is a file of the
    right name and the wrong length — indistinguishable from this one in a
    directory listing. What tells them apart is whether the tracks say DONE,
    which only happens after the encoder returns success.
    """
    from adr.models import Track, TrackStatus

    out = config.staging_path / "The Matrix (1999)"
    out.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        name = "The Matrix (1999).mp4" if count == 1 else f"The Matrix (1999) - pt{i+1}.mp4"
        (out / name).write_bytes(b"X" * 2048)
        job.tracks.append(Track(
            track_number=i + 1,
            filename=f"title{i:02d}.mkv",
            output_path=str(out / name),
            status=TrackStatus.DONE if tracks_done else TrackStatus.ENCODING,
        ))
    job.output_path = str(out)
    return out


def _make_raw(config, job, count=1, rip_finished=True):
    """Raw MKVs from a rip, as after a failed encode.

    ``rip_finished`` is what tells a complete file from a truncated one, and
    it is the job's own record rather than anything on disk: MakeMKV writes
    each title as it goes, so a rip killed part-way leaves MKVs that look
    perfectly ordinary in a directory listing.
    """
    raw_dir = config.raw_path / str(job.id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (raw_dir / f"title_t{i:02d}.mkv").write_bytes(b"M" * 4096)
    if rip_finished:
        job.rip_completed_at = utcnow()
    return raw_dir


class TestPlan:
    def test_a_successful_job_cannot_be_retried(self, failed_job, config):
        session, job = failed_job
        job.status = JobStatus.DONE
        session.commit()
        result = retry.plan(job, config)
        assert result["can_retry"] is False
        assert "failed or cancelled" in result["reason"]

    def test_intact_encoded_files_mean_only_the_transfer_is_redone(
        self, failed_job, config, tmp_path,
    ):
        """The most common failure, and the cheapest to recover from."""
        session, job = failed_job
        _make_encoded(config, job, tmp_path)
        session.commit()

        result = retry.plan(job, config)
        assert result["resume"] == retry.RESUME_TRANSFER
        assert result["can_retry"] is True
        assert "no re-encoding" in result["reason"]

    def test_raw_files_mean_the_encode_is_redone(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=2)
        session.commit()

        result = retry.plan(job, config)
        assert result["resume"] == retry.RESUME_ENCODE
        assert result["can_retry"] is True
        assert "disc is not needed" in result["reason"]
        assert len(result["files"]) == 2

    def test_a_rip_that_never_finished_leaves_nothing_to_encode(
        self, failed_job, config,
    ):
        """The hour-losing case.

        MakeMKV writes each title as it goes, so a rip killed part-way — a
        service restart, a cancel, a crash — leaves MKVs that look perfectly
        ordinary in a directory listing and are truncated mid-frame. Offering
        to "re-encode them, the disc is not needed" spends an hour and ends in
        "Invalid data found when processing input", which reads as an encoder
        fault and is nothing of the kind.
        """
        session, job = failed_job
        _make_raw(config, job, count=2, rip_finished=False)
        session.commit()

        result = retry.plan(job, config)
        assert result["can_retry"] is False
        assert result["resume"] == retry.RESUME_IMPOSSIBLE
        assert "did not finish" in result["reason"]
        assert "press Rip" in result["reason"], "it must say what to do instead"

    def test_the_incomplete_files_are_still_named(self, failed_job, config):
        """So the reason can be checked rather than taken on trust."""
        session, job = failed_job
        _make_raw(config, job, count=2, rip_finished=False)
        session.commit()
        assert len(retry.plan(job, config)["files"]) == 2

    def test_a_finished_rip_is_still_resumable(self, failed_job, config):
        """The distinction has to cut one way only: a job that ripped fine and
        failed in the encoder must not be sent back to the disc."""
        session, job = failed_job
        _make_raw(config, job, count=2, rip_finished=True)
        session.commit()
        assert retry.plan(job, config)["resume"] == retry.RESUME_ENCODE

    def test_encoded_files_survive_an_unfinished_rip(self, failed_job, config, tmp_path):
        """If the encode is already done, how the rip ended stopped mattering
        a long time ago."""
        session, job = failed_job
        job.output_path = str(config.staging_path / "The Matrix (1999)")
        _make_encoded(config, job, tmp_path)
        _make_raw(config, job, rip_finished=False)
        session.commit()
        assert retry.plan(job, config)["resume"] == retry.RESUME_TRANSFER

    def test_encoded_files_win_over_raw_files(self, failed_job, config, tmp_path):
        """Both present: resume from the furthest point, not the earliest."""
        session, job = failed_job
        _make_raw(config, job)
        _make_encoded(config, job, tmp_path)
        session.commit()
        assert retry.plan(job, config)["resume"] == retry.RESUME_TRANSFER

    def test_nothing_on_disk_says_so_honestly(self, failed_job, config):
        session, job = failed_job
        result = retry.plan(job, config)
        assert result["can_retry"] is False
        assert result["resume"] == retry.RESUME_IMPOSSIBLE
        assert "nothing to resume from" in result["reason"]
        assert "press Rip" in result["reason"]

    def test_a_stale_output_path_is_not_believed(self, failed_job, config):
        """The status says one thing; only the filesystem is authoritative."""
        session, job = failed_job
        job.output_path = "/gone/The Matrix (1999)"
        session.commit()
        assert retry.plan(job, config)["can_retry"] is False

    def test_a_cancelled_job_can_also_be_retried(self, failed_job, config):
        session, job = failed_job
        job.status = JobStatus.CANCELLED
        _make_raw(config, job)
        session.commit()
        assert retry.plan(job, config)["can_retry"] is True


class TestRetryTransfer:
    def test_a_still_broken_destination_is_reported_before_trying(
        self, failed_job, config, tmp_path,
    ):
        """A second identical error twenty seconds later helps nobody."""
        session, job = failed_job
        _make_encoded(config, job, tmp_path)
        session.commit()
        config.completed_path = tmp_path / "does-not-exist"

        ok, message = retry.retry_transfer(job, session, config)
        assert ok is False
        assert "still is not usable" in message

    def test_a_working_destination_completes_the_job(self, failed_job, config, tmp_path):
        session, job = failed_job
        _make_encoded(config, job, tmp_path)
        session.commit()

        ok, message = retry.retry_transfer(job, session, config)
        assert ok is True
        assert job.status == JobStatus.DONE
        assert job.error_message is None
        assert (config.completed_path / "The Matrix (1999)" / "The Matrix (1999).mp4").exists()
        assert "The Matrix" in message

    def test_the_staging_copy_is_gone_afterwards(self, failed_job, config, tmp_path):
        session, job = failed_job
        staged = _make_encoded(config, job, tmp_path)
        session.commit()
        retry.retry_transfer(job, session, config)
        assert not staged.exists()


class TestRequeueEncode:
    def test_every_raw_file_is_queued(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=3)
        session.commit()

        q = queue.Queue()
        assert retry.requeue_encode(job, session, config, q) == 3
        assert q.qsize() == 3

    def test_the_job_returns_to_encoding_with_the_error_cleared(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job)
        session.commit()

        retry.requeue_encode(job, session, config, queue.Queue())
        session.refresh(job)
        assert job.status == JobStatus.ENCODING
        assert job.error_message is None
        assert job.completed_at is None

    def test_stale_tracks_from_the_failed_attempt_are_replaced(self, failed_job, config):
        """Inheriting the previous attempt's error state is hard to reason about."""
        session, job = failed_job
        session.add(Track(
            job_id=job.id, track_number=1, filename="old.mkv",
            status=TrackStatus.ERROR, output_path="/gone/old.mp4",
        ))
        session.commit()
        _make_raw(config, job, count=2)

        retry.requeue_encode(job, session, config, queue.Queue())
        session.refresh(job)
        assert len(job.tracks) == 2
        assert all(t.status == TrackStatus.PENDING for t in job.tracks)
        assert not any(t.filename == "old.mkv" for t in job.tracks)

    def test_the_queued_tasks_name_the_right_files(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=2)
        session.commit()

        q = queue.Queue()
        retry.requeue_encode(job, session, config, q)
        tasks = [q.get(), q.get()]
        assert {t.input_path.name for t in tasks} == {"title_t00.mkv", "title_t01.mkv"}
        # Multiple tracks get part suffixes so they do not overwrite each other.
        assert {t.output_filename for t in tasks} == {
            "The Matrix (1999) - pt1", "The Matrix (1999) - pt2",
        }

    def test_a_single_track_gets_no_part_suffix(self, failed_job, config):
        session, job = failed_job
        _make_raw(config, job, count=1)
        session.commit()

        q = queue.Queue()
        retry.requeue_encode(job, session, config, q)
        assert q.get().output_filename == "The Matrix (1999)"

    def test_no_raw_files_queues_nothing(self, failed_job, config):
        session, job = failed_job
        q = queue.Queue()
        assert retry.requeue_encode(job, session, config, q) == 0
        assert q.empty()


class TestRetryingASeries:
    """A series job must not come back as a film.

    retry.py was written before television existed. Re-queuing an encode built
    its own filenames instead of asking adr.naming, so retrying a failed season
    would rename 'Show/Season 02/Show - S02E05' into 'Show/Show - pt1' — the
    files land in the wrong folder, with the wrong names, in the wrong library.
    """

    def test_the_season_layout_survives_a_retry(self, failed_job, config):
        session, job = failed_job
        job.content_type = "series"
        job.title = "The Wire"
        job.year = 2002
        job.series_season = 2
        job.series_first_episode = 5
        session.commit()
        _make_raw(config, job, count=3)

        q = queue.Queue()
        assert retry.requeue_encode(job, session, config, q) == 3

        tasks = [q.get(), q.get(), q.get()]
        assert [t.output_filename for t in tasks] == [
            "The Wire (2002) - S02E05",
            "The Wire (2002) - S02E06",
            "The Wire (2002) - S02E07",
        ]
        assert str(tasks[0].output_dir).endswith("The Wire (2002)/Season 02")

    def test_episode_numbers_are_recorded_on_the_tracks(self, failed_job, config):
        session, job = failed_job
        job.content_type = "series"
        job.title = "The Wire"
        job.year = 2002
        job.series_season = 2
        job.series_first_episode = 5
        session.commit()
        _make_raw(config, job, count=2)

        retry.requeue_encode(job, session, config, queue.Queue())
        session.refresh(job)
        assert sorted(t.episode_number for t in job.tracks) == [5, 6]


class TestAHalfWrittenEncodeIsNotAFinishedOne:
    """The worst outcome this module can produce: a truncated film published to
    the library and reported as a success.

    HandBrake writes straight to the final name with no temp file. A job killed
    at 60% — Cancel, a full disk, a source read error — leaves an unfinalised
    MP4 that a directory listing cannot tell from a finished one. Retry used to
    call that "intact", move it into the Plex library, mark the job DONE and
    clear the error, while the raw MKVs that would have re-encoded perfectly
    sat unread: the encoded branch is consulted before the raw one.

    The rip branch has had the right shape all along — it refuses to trust a
    directory listing for a process killed part-way, and uses rip_completed_at
    as the witness. This is the same test for the other half.
    """

    def test_a_cancelled_encode_is_re_encoded_not_transferred(
        self, failed_job, config, tmp_path,
    ):
        session, job = failed_job
        _make_encoded(config, job, tmp_path, tracks_done=False)
        _make_raw(config, job, count=1)
        session.commit()

        result = retry.plan(job, config)
        assert result["resume"] == retry.RESUME_ENCODE, (
            "a truncated encode was offered for transfer"
        )
        assert result["can_retry"] is True

    def test_a_job_with_no_tracks_at_all_is_not_trusted(
        self, failed_job, config, tmp_path,
    ):
        """Files with no rows behind them: a leftover from an older run, or a
        job cancelled before the tracks were created."""
        session, job = failed_job
        out = config.staging_path / "The Matrix (1999)"
        out.mkdir(parents=True, exist_ok=True)
        (out / "The Matrix (1999).mp4").write_bytes(b"X" * 2048)
        job.output_path = str(out)
        _make_raw(config, job, count=1)
        session.commit()

        assert retry.plan(job, config)["resume"] == retry.RESUME_ENCODE

    def test_one_failed_track_among_finished_ones_still_re_encodes(
        self, failed_job, config, tmp_path,
    ):
        from adr.models import TrackStatus

        session, job = failed_job
        _make_encoded(config, job, tmp_path, count=2)
        job.tracks[1].status = TrackStatus.ERROR
        _make_raw(config, job, count=2)
        session.commit()

        assert retry.plan(job, config)["resume"] == retry.RESUME_ENCODE

    def test_a_failed_transfer_still_only_redoes_the_transfer(
        self, failed_job, config, tmp_path,
    ):
        """The case this branch exists for, and the one the fix must not break:
        every track encoded, and the move to the NAS is what failed."""
        session, job = failed_job
        _make_encoded(config, job, tmp_path, count=2)
        session.commit()

        result = retry.plan(job, config)
        assert result["resume"] == retry.RESUME_TRANSFER
        assert "no re-encoding" in result["reason"]


class TestARetryRemembersWhichFilesWereEpisodes:
    """requeue_encode reads the old Track rows to recover the episode numbers
    and the episode/extra split, and no test ever gave it rows that name the
    raw files — so the whole branch was dead in the suite while looking
    covered. It is the branch that stops a retry renumbering a half-finished
    season from 1.
    """

    def _job(self, tmp_path, names, episode_of):
        import types

        raw = tmp_path / "raw" / "5"
        raw.mkdir(parents=True)
        for name in names:
            (raw / name).write_bytes(b"M" * 4096)
        tracks = [
            types.SimpleNamespace(
                filename=name, episode_number=episode_of.get(name),
                output_path=None, id=index,
            )
            for index, name in enumerate(names)
        ]
        return types.SimpleNamespace(
            id=5, title="Show", year=1964, disc_label="SHOW_D2",
            content_type="series", series_season=1, series_first_episode=1,
            tracks=tracks, output_path=None, status=None, error_message=None,
            completed_at=None, progress_encode=0.0,
        ), raw

    def _run(self, job, tmp_path):
        import queue
        import types

        from adr import retry

        config = types.SimpleNamespace(
            raw_path=tmp_path / "raw", staging_path=tmp_path / "staging",
            completed_path=tmp_path / "completed", tv_path=tmp_path / "tv",
            plex_path=tmp_path / "plex", stage_locally=False,
            transcode_enabled=True, main_feature_only=False,
        )
        for name in ("staging", "completed", "tv", "plex"):
            (tmp_path / name).mkdir(exist_ok=True)

        class _Session:
            def delete(self, obj): pass
            def add(self, obj): pass
            def commit(self): pass

        out = queue.Queue()
        retry.requeue_encode(job, _Session(), config, out)
        names = []
        while not out.empty():
            names.append(out.get().output_filename)
        return names

    def test_the_remembered_numbers_come_back(self, tmp_path):
        """Disc 2 of a box set was E06-E08. A retry that renumbers from 1
        overwrites disc 1."""
        names = ["t0.mkv", "t1.mkv", "t2.mkv"]
        job, _ = self._job(tmp_path, names, {"t0.mkv": 6, "t1.mkv": 7, "t2.mkv": 8})
        out = self._run(job, tmp_path)
        assert [n[-3:] for n in sorted(out)] == ["E06", "E07", "E08"], out

    def test_a_file_that_was_an_extra_stays_an_extra(self, tmp_path):
        """It had a Track row and no episode number, which is exactly how an
        extra is recorded — and re-planning it as an episode shifts the rest."""
        names = ["t0.mkv", "t1.mkv", "t2.mkv"]
        job, _ = self._job(tmp_path, names, {"t0.mkv": 6, "t2.mkv": 7})
        out = self._run(job, tmp_path)
        assert len([n for n in out if "S01E" in n]) == 2, out
        assert len([n for n in out if n.startswith("Other/")]) == 1, out
