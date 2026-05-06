"""Tests for adr.models — Job/Track models and status constants."""

import pytest

from adr.models import (
    JobStatus,
    TrackStatus,
    ACTIVE_STATUSES,
    RIP_PHASE_STATUSES,
    ENCODE_PHASE_STATUSES,
    TERMINAL_STATUSES,
)


class TestJobStatus:
    def test_all_values_present(self):
        expected = {"pending", "identifying", "ripping", "ripped", "encoding", "done", "cancelled", "error"}
        assert {s.value for s in JobStatus} == expected

    def test_value_access(self):
        assert JobStatus.PENDING.value == "pending"
        assert JobStatus.DONE.value == "done"
        assert JobStatus.ERROR.value == "error"


class TestTrackStatus:
    def test_all_values_present(self):
        expected = {"pending", "encoding", "done", "error"}
        assert {s.value for s in TrackStatus} == expected


class TestStatusSets:
    def test_active_statuses_correct(self):
        assert ACTIVE_STATUSES == frozenset({
            JobStatus.PENDING, JobStatus.IDENTIFYING, JobStatus.RIPPING,
            JobStatus.RIPPED, JobStatus.ENCODING,
        })

    def test_rip_phase_statuses_correct(self):
        assert RIP_PHASE_STATUSES == frozenset({
            JobStatus.PENDING, JobStatus.IDENTIFYING, JobStatus.RIPPING,
        })

    def test_encode_phase_statuses_correct(self):
        assert ENCODE_PHASE_STATUSES == frozenset({JobStatus.RIPPED, JobStatus.ENCODING})

    def test_terminal_statuses_correct(self):
        assert TERMINAL_STATUSES == frozenset({JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED})

    def test_active_and_terminal_are_disjoint(self):
        assert ACTIVE_STATUSES.isdisjoint(TERMINAL_STATUSES)

    def test_active_plus_terminal_covers_all(self):
        all_statuses = ACTIVE_STATUSES | TERMINAL_STATUSES
        for status in JobStatus:
            assert status in all_statuses

    def test_rip_plus_encode_subset_of_active(self):
        assert RIP_PHASE_STATUSES | ENCODE_PHASE_STATUSES <= ACTIVE_STATUSES

    def test_sets_are_frozen(self):
        with pytest.raises(AttributeError):
            ACTIVE_STATUSES.add(JobStatus.DONE)
