"""Tests for duplicate-disc detection.

Ripping the same film twice by accident is forty wasted minutes and a duplicate
in the library. But a disc label is not a unique identifier — plenty of discs
ship as DVD_VIDEO — so this may only ever annotate a job, never block one, and
must not turn every unlabelled disc into a duplicate of the previous one.
"""

import pytest

from adr.models import Job, JobStatus, get_session, init_db
from adr.pipeline import find_previous_rip
from adr.utils import utcnow


@pytest.fixture
def session():
    init_db()
    s = get_session()
    yield s
    s.close()


def _job(session, label, status=JobStatus.DONE, title=None, commit=True):
    job = Job(
        disc_label=label, title=title, drive="/dev/sr0", status=status,
        started_at=utcnow(),
        completed_at=utcnow() if status == JobStatus.DONE else None,
    )
    session.add(job)
    if commit:
        session.commit()
    return job


class TestFindPreviousRip:
    def test_an_earlier_completed_rip_is_found(self, session):
        first = _job(session, "THE_MATRIX", title="The Matrix")
        second = _job(session, "THE_MATRIX")
        found = find_previous_rip(second, session)
        assert found is not None
        assert found.id == first.id

    def test_a_new_disc_matches_nothing(self, session):
        _job(session, "THE_MATRIX")
        assert find_previous_rip(_job(session, "HEAT"), session) is None

    def test_a_job_does_not_match_itself(self, session):
        job = _job(session, "THE_MATRIX")
        assert find_previous_rip(job, session) is None

    def test_a_failed_earlier_attempt_is_not_a_duplicate(self, session):
        """Re-ripping after a failure is the point, not a mistake."""
        _job(session, "THE_MATRIX", status=JobStatus.ERROR)
        assert find_previous_rip(_job(session, "THE_MATRIX"), session) is None

    def test_a_cancelled_earlier_attempt_is_not_a_duplicate(self, session):
        _job(session, "THE_MATRIX", status=JobStatus.CANCELLED)
        assert find_previous_rip(_job(session, "THE_MATRIX"), session) is None

    def test_the_most_recent_match_is_returned(self, session):
        _job(session, "THE_MATRIX", title="First")
        latest = _job(session, "THE_MATRIX", title="Second")
        current = _job(session, "THE_MATRIX")
        assert find_previous_rip(current, session).id == latest.id


class TestLabelsThatMeanNothing:
    @pytest.mark.parametrize("label", [
        "DVD_VIDEO", "dvd_video", "BLURAY", "UNTITLED", "UNKNOWN",
        "LOGICAL_VOLUME_ID", "VIDEO_TS",
    ])
    def test_generic_labels_never_match(self, session, label):
        """Otherwise every unlabelled disc is a duplicate of the last one."""
        _job(session, label, title="Some Film")
        assert find_previous_rip(_job(session, label), session) is None

    @pytest.mark.parametrize("label", [None, "", "   "])
    def test_a_blank_label_never_matches(self, session, label):
        _job(session, label)
        assert find_previous_rip(_job(session, label), session) is None


class TestItOnlyAnnotates:
    def test_the_column_exists_and_defaults_to_none(self, session):
        job = _job(session, "THE_MATRIX")
        assert job.duplicate_of is None

    def test_marking_a_duplicate_does_not_change_the_status(self, session):
        """A flagged job must still rip — the user may want a second copy."""
        first = _job(session, "THE_MATRIX")
        second = _job(session, "THE_MATRIX", status=JobStatus.IDENTIFYING)
        second.duplicate_of = find_previous_rip(second, session).id
        session.commit()

        assert second.duplicate_of == first.id
        assert second.status == JobStatus.IDENTIFYING
