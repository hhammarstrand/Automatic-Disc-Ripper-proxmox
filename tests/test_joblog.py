"""Tests for adr.joblog.

Job logs wrap the actual work, so the governing rule is that they must never
become the reason a rip fails. The second rule is that they must stay bounded:
a pathological disc can make MakeMKV emit tens of thousands of lines, and a
hundred unbounded logs in a container is a filled disk.
"""

import types

import pytest

from adr import joblog


@pytest.fixture
def config(tmp_path):
    return types.SimpleNamespace(log_path=tmp_path / "logs")


class TestAppend:
    def test_a_line_is_written_with_its_stage(self, config):
        log = joblog.JobLog(config, 7)
        log.append("rip", "Reading title 3")
        text = joblog.read(config, 7)
        assert "Reading title 3" in text
        assert "[rip]" in text

    def test_the_directory_is_created_on_demand(self, config):
        assert not joblog.log_dir(config).exists()
        joblog.JobLog(config, 1).append("rip", "hello")
        assert joblog.log_dir(config).exists()

    def test_lines_accumulate_in_order(self, config):
        log = joblog.JobLog(config, 1)
        for i in range(5):
            log.append("encode", f"line {i}")
        lines = [ln for ln in joblog.read(config, 1).splitlines() if ln.strip()]
        assert len(lines) == 5
        assert "line 0" in lines[0]
        assert "line 4" in lines[-1]

    def test_an_empty_message_writes_nothing(self, config):
        joblog.JobLog(config, 1).append("rip", "")
        assert joblog.read(config, 1) == ""

    def test_the_sink_is_a_one_argument_callable(self, config):
        """This is what gets handed to the ripper and encoder."""
        sink = joblog.JobLog(config, 1).sink("rip")
        sink("from makemkv")
        assert "from makemkv" in joblog.read(config, 1)


class TestNeverFatal:
    def test_an_unwritable_directory_does_not_raise(self, tmp_path):
        """A full or read-only disk must not turn a good rip into a failed one."""
        blocked = tmp_path / "file-not-a-dir"
        blocked.write_text("x")
        config = types.SimpleNamespace(log_path=blocked / "logs")

        log = joblog.JobLog(config, 1)
        log.append("rip", "this cannot be written")   # must not raise
        assert log._failed is True

    def test_it_warns_once_not_once_per_line(self, tmp_path, caplog):
        """Logging that we cannot log, once per line, is worse than the problem."""
        import logging

        blocked = tmp_path / "nope"
        blocked.write_text("x")
        config = types.SimpleNamespace(log_path=blocked / "logs")

        log = joblog.JobLog(config, 1)
        with caplog.at_level(logging.WARNING, logger="adr.joblog"):
            for _ in range(50):
                log.append("rip", "x")
        assert len(caplog.records) == 1

    def test_reading_a_missing_log_is_an_empty_string(self, config):
        assert joblog.read(config, 999) == ""


class TestBounded:
    def test_a_huge_log_is_trimmed(self, config):
        log = joblog.JobLog(config, 1)
        for i in range(20_000):
            log.append("rip", f"noisy line {i} " + "x" * 60)
        size = joblog.log_path(config, 1).stat().st_size
        assert size <= joblog.MAX_BYTES * 1.1, f"log grew to {size} bytes"

    def test_trimming_keeps_the_end_not_the_beginning(self, config):
        """The last thing before a failure is the interesting part."""
        log = joblog.JobLog(config, 1)
        for i in range(20_000):
            log.append("rip", f"line {i} " + "x" * 60)
        text = joblog.read(config, 1)
        assert "line 19999" in text
        assert "line 0 " not in text

    def test_trimming_says_that_it_trimmed(self, config):
        log = joblog.JobLog(config, 1)
        for i in range(20_000):
            log.append("rip", f"line {i} " + "x" * 60)
        # The marker lives at the head of the file; read the whole thing.
        assert "trimmed" in joblog.read(config, 1, tail_bytes=joblog.MAX_BYTES * 2)

    def test_a_truncated_read_announces_itself(self, config):
        """A log that silently begins mid-stream reads as the whole story."""
        log = joblog.JobLog(config, 1)
        for i in range(5_000):
            log.append("rip", f"line {i} " + "x" * 60)
        text = joblog.read(config, 1, tail_bytes=4096)
        assert text.startswith("[... showing the last")

    def test_a_short_log_is_returned_whole_without_a_notice(self, config):
        joblog.JobLog(config, 1).append("rip", "just one line")
        text = joblog.read(config, 1)
        assert not text.startswith("[...")
        assert "just one line" in text

    def test_the_reader_returns_at_most_the_tail(self, config):
        """The browser is never handed the whole file."""
        log = joblog.JobLog(config, 1)
        for i in range(20_000):
            log.append("rip", f"line {i} " + "x" * 60)
        # tail_bytes of content, plus the one-line "this is a tail" notice.
        assert len(joblog.read(config, 1).encode()) <= joblog.TAIL_BYTES + 200


class TestCleanup:
    def test_delete_removes_the_file(self, config):
        joblog.JobLog(config, 3).append("rip", "x")
        assert joblog.delete(config, 3) is True
        assert joblog.read(config, 3) == ""

    def test_deleting_a_missing_log_is_not_an_error(self, config):
        assert joblog.delete(config, 404) is False

    def test_prune_removes_logs_for_jobs_that_no_longer_exist(self, config):
        for job_id in (1, 2, 3):
            joblog.JobLog(config, job_id).append("rip", "x")
        removed = joblog.prune(config, keep_job_ids={2})
        assert removed == 2
        assert joblog.read(config, 2) != ""
        assert joblog.read(config, 1) == ""
        assert joblog.read(config, 3) == ""

    def test_prune_keeps_everything_when_given_no_job_ids(self, config):
        """A caller that cannot list jobs must not cause a mass delete."""
        joblog.JobLog(config, 1).append("rip", "x")
        assert joblog.prune(config, keep_job_ids=None) == 0
        assert joblog.read(config, 1) != ""

    def test_prune_removes_expired_logs(self, config):
        import os
        import time

        joblog.JobLog(config, 1).append("rip", "x")
        path = joblog.log_path(config, 1)
        old = time.time() - 60 * 86400
        os.utime(path, (old, old))

        assert joblog.prune(config, keep_job_ids={1}, max_age_days=30) == 1

    def test_prune_ignores_files_it_does_not_own(self, config):
        joblog.log_dir(config).mkdir(parents=True)
        stray = joblog.log_dir(config) / "adr.log"
        stray.write_text("someone else's file")
        joblog.prune(config, keep_job_ids=set())
        assert stray.exists(), "only job-<id>.log files are ours to delete"

    def test_prune_on_a_missing_directory_is_zero(self, config):
        assert joblog.prune(config, keep_job_ids=set()) == 0
