"""Tests for adr.duplicates.

Two failure modes pull in opposite directions. Missing a duplicate costs forty
minutes and leaves Plex with two copies. Reporting one falsely — and with
skip_duplicates on, *cancelling* the disc — costs a rip the user wanted and is
harder to notice, because nothing visibly went wrong.

So the checks are ordered by how much they can be trusted, and each is tested
for what it must *not* match as much as for what it must.
"""

import types

import pytest

from adr import duplicates
from adr.models import Job, JobStatus, get_session, init_db
from adr.utils import utcnow


@pytest.fixture
def config(tmp_path):
    for name in ("completed", "staging", "raw"):
        (tmp_path / name).mkdir(exist_ok=True)
    return types.SimpleNamespace(
        completed_path=tmp_path / "completed",
        staging_path=tmp_path / "staging",
        raw_path=tmp_path / "raw",
        plex_path="",
        tv_path="",
        auto_move_to_plex=False,
        stage_locally=True,
        require_completed_mount=False,
        skip_duplicates=False,
    )


@pytest.fixture
def session():
    init_db()
    s = get_session()
    yield s
    s.close()


def _job(session, *, title="The Matrix", year=1999, tmdb_id=603,
         label="THE_MATRIX", status=JobStatus.IDENTIFYING, commit=True,
         content_type="movie"):
    job = Job(
        disc_label=label, title=title, year=year, tmdb_id=tmdb_id,
        drive="/dev/sr0", status=status, started_at=utcnow(),
        content_type=content_type,
        completed_at=utcnow() if status == JobStatus.DONE else None,
    )
    session.add(job)
    if commit:
        session.commit()
    return job


def _in_library(config, folder="The Matrix (1999)", filename="The Matrix (1999).mp4"):
    target = config.completed_path / folder
    target.mkdir(parents=True, exist_ok=True)
    if filename:
        (target / filename).write_bytes(b"X" * 1024)
    return target


class TestTheLibraryIsTheTruth:
    """The only check that survives a cleared history or a reinstall."""

    def test_an_existing_film_is_found(self, session, config):
        _in_library(config)
        found = duplicates.find_duplicate(_job(session), session, config)
        assert found is not None
        assert found["kind"] == duplicates.MATCH_LIBRARY
        assert "The Matrix (1999)" in found["detail"]

    def test_it_beats_a_database_match(self, session, config):
        """Both true: report the one that is a fact rather than a memory."""
        _job(session, status=JobStatus.DONE)
        _in_library(config)
        assert duplicates.find_duplicate(_job(session), session, config)["kind"] == (
            duplicates.MATCH_LIBRARY
        )

    def test_an_empty_folder_is_a_failed_attempt_not_a_film(self, session, config):
        """A folder with no video in it is where a rip died, not where it landed."""
        _in_library(config, filename=None)
        assert duplicates.find_duplicate(_job(session), session, config) is None

    def test_a_different_film_does_not_match(self, session, config):
        _in_library(config, "Heat (1995)", "Heat (1995).mp4")
        assert duplicates.find_duplicate(_job(session), session, config) is None

    @pytest.mark.parametrize("suffix", [".mp4", ".mkv", ".m4v", ".avi"])
    def test_any_video_file_counts(self, session, config, suffix):
        _in_library(config, filename=f"The Matrix (1999){suffix}")
        assert duplicates.find_duplicate(_job(session), session, config) is not None

    def test_a_stray_non_video_file_does_not_count(self, session, config):
        """A leftover .nfo or poster is not a film."""
        _in_library(config, filename="poster.jpg")
        assert duplicates.find_duplicate(_job(session), session, config) is None

    def test_an_unidentified_disc_cannot_be_matched_by_library(self, session, config):
        """Without a title there is no folder name to look for."""
        _in_library(config)
        job = _job(session, title=None, year=None, tmdb_id=None, label="UNKNOWN_DISC")
        assert duplicates.find_duplicate(job, session, config) is None


class TestTheSameFilmFromAnotherDisc:
    def test_a_matching_tmdb_id_is_found(self, session, config):
        """A re-release has a different label but the same film."""
        _job(session, label="MATRIX_SPECIAL_ED", status=JobStatus.DONE)
        found = duplicates.find_duplicate(
            _job(session, label="THE_MATRIX_1999"), session, config)
        assert found is not None
        assert found["kind"] == duplicates.MATCH_TMDB

    def test_an_unfinished_earlier_job_is_not_a_duplicate(self, session, config):
        """Re-ripping after a failure is the point, not a mistake."""
        _job(session, status=JobStatus.ERROR)
        assert duplicates.find_duplicate(_job(session), session, config) is None

    def test_a_different_film_does_not_match(self, session, config):
        _job(session, title="Heat", year=1995, tmdb_id=949, label="HEAT",
             status=JobStatus.DONE)
        assert duplicates.find_duplicate(_job(session), session, config) is None

    def test_a_shared_label_loses_to_a_differing_tmdb_id(self, session, config):
        """Two pressings sharing a label is a coincidence, not a duplicate.

        The label is the weakest signal; when TMDb has identified both discs
        and called them different films, that is the better evidence.
        """
        _job(session, title="Heat", year=1995, tmdb_id=949, label="MOVIE_DISC",
             status=JobStatus.DONE)
        job = _job(session, title="The Matrix", year=1999, tmdb_id=603,
                   label="MOVIE_DISC")
        assert duplicates.find_duplicate(job, session, config) is None

    def test_a_shared_label_still_matches_when_one_is_unidentified(self, session, config):
        """Without a TMDb id on both sides there is nothing to overrule it."""
        _job(session, title=None, tmdb_id=None, label="MOVIE_DISC",
             status=JobStatus.DONE)
        job = _job(session, title=None, tmdb_id=None, label="MOVIE_DISC")
        assert duplicates.find_duplicate(job, session, config)["kind"] == (
            duplicates.MATCH_LABEL
        )

    def test_no_tmdb_id_means_no_tmdb_match(self, session, config):
        _job(session, tmdb_id=None, label="A", status=JobStatus.DONE)
        job = _job(session, tmdb_id=None, label="B")
        assert duplicates.find_duplicate(job, session, config) is None


class TestTheLabelFallback:
    def test_the_same_label_is_found_when_nothing_else_matches(self, session, config):
        _job(session, title=None, tmdb_id=None, label="SOME_DISC",
             status=JobStatus.DONE)
        found = duplicates.find_duplicate(
            _job(session, title=None, tmdb_id=None, label="SOME_DISC"), session, config)
        assert found is not None
        assert found["kind"] == duplicates.MATCH_LABEL

    def test_a_generic_label_never_matches(self, session, config):
        """Otherwise every unlabelled disc is a duplicate of the last one."""
        _job(session, title=None, tmdb_id=None, label="DVD_VIDEO",
             status=JobStatus.DONE)
        job = _job(session, title=None, tmdb_id=None, label="DVD_VIDEO")
        assert duplicates.find_duplicate(job, session, config) is None


class TestSeriesAreExempt:
    def test_a_series_disc_is_never_a_duplicate(self, session, config):
        """Every disc of a box set writes into the same show folder — that is
        the normal case, not a warning. Episodes are protected by numbering."""
        (config.completed_path / "The Wire (2002)" / "Season 02").mkdir(parents=True)
        (config.completed_path / "The Wire (2002)" / "Season 02"
         / "The Wire (2002) - S02E01.mp4").write_bytes(b"X")

        job = _job(session, title="The Wire", year=2002, tmdb_id=1438,
                   content_type="series")
        job.series_season = 2
        job.series_first_episode = 5
        session.commit()
        assert duplicates.find_duplicate(job, session, config) is None


class TestItNeverBreaksARip:
    def test_a_check_that_raises_does_not_stop_the_rip(self, session, config, monkeypatch):
        """A duplicate check is an optimisation. It must not become a failure."""
        def _boom(*a, **k):
            raise RuntimeError("filesystem went away")

        monkeypatch.setattr(duplicates, "_library_match", _boom)
        _job(session, status=JobStatus.DONE)
        # The tmdb check still runs and finds the earlier job.
        found = duplicates.find_duplicate(_job(session), session, config)
        assert found is not None
        assert found["kind"] == duplicates.MATCH_TMDB

    def test_every_check_raising_yields_no_duplicate(self, session, config, monkeypatch):
        for name in ("_library_match", "_tmdb_match", "_label_match"):
            monkeypatch.setattr(
                duplicates, name,
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert duplicates.find_duplicate(_job(session), session, config) is None

    def test_a_fresh_install_finds_nothing(self, session, config):
        assert duplicates.find_duplicate(_job(session), session, config) is None


class TestDescribe:
    def test_it_reports_the_match(self, session, config):
        _in_library(config)
        match = duplicates.find_duplicate(_job(session), session, config)
        assert "The Matrix (1999)" in duplicates.describe(match)

    def test_no_match_says_so(self):
        assert "No earlier rip" in duplicates.describe(None)


class TestADiscLabelIsNotAnIdentity:
    """The report: two films ripped fine, then every disc after them was
    cancelled as a duplicate.

    A set-top DVD recorder writes its own name onto every disc it burns, so a
    whole shelf of home recordings all said LG_COMBI_RECORDER. The first was
    ripped and renamed by hand — which is the proof the label never identified
    it — and every disc after it matched that label and was skipped.

    find_previous_rip's docstring has said since it was written that a label
    "is not a unique identifier" and "only ever annotates a job, never blocks
    one". skip_duplicates blocked on it anyway.
    """

    def test_a_label_match_never_blocks(self):
        from adr import duplicates

        assert duplicates.blocks_a_rip({"kind": duplicates.MATCH_LABEL}) is False

    def test_the_two_that_compare_the_film_still_do(self):
        from adr import duplicates

        assert duplicates.blocks_a_rip({"kind": duplicates.MATCH_LIBRARY}) is True
        assert duplicates.blocks_a_rip({"kind": duplicates.MATCH_TMDB}) is True

    def test_nothing_found_blocks_nothing(self):
        from adr import duplicates

        assert duplicates.blocks_a_rip(None) is False

    def test_the_pipeline_asks_before_cancelling(self):
        import inspect

        from adr.pipeline import DrivePipeline

        source = inspect.getsource(DrivePipeline._run_pipeline)
        assert "duplicates.blocks_a_rip(duplicate)" in source, (
            "a disc label can cancel a rip again"
        )

    def test_a_label_match_is_still_reported(self):
        """It is real information — the same disc may well be in the drive
        again. It is just not grounds for refusing to rip."""
        from adr import duplicates

        detail = duplicates.describe({
            "kind": duplicates.MATCH_LABEL, "detail": "…was already ripped…",
        })
        assert detail


class TestEquipmentLabelsAreGeneric:
    """Labels that name the machine rather than the film. Listing brands would
    never keep up; the word is what gives it away."""

    def _generic(self, label):
        from adr.pipeline import _is_generic_label

        return _is_generic_label(label)

    def test_the_reported_one(self):
        assert self._generic("LG_COMBI_RECORDER")

    def test_other_recorders_nobody_listed(self):
        for label in ("PHILIPS_DVDR", "SONY_RDR_RECORDER", "MY_RECORDING",
                      "PANASONIC_DVD_RECORDER", "CAMCORDER_01"):
            assert self._generic(label), label

    def test_the_old_list_still_counts(self):
        for label in ("DVD_VIDEO", "UNTITLED", "", "   "):
            assert self._generic(label), label

    def test_a_real_film_label_is_not_generic(self):
        for label in ("JUMANJI", "THE_MATRIX", "DINOSAUR", "LOGAN_S01D2"):
            assert not self._generic(label), label
