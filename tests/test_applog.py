"""The service log, in a file the web UI can read.

Everything the application said went to stderr and from there to journald,
which needs a shell on the Proxmox host. Every diagnosis in this application's
history has ended with "paste me the output of journalctl" — a design failure,
not a support process.
"""

import logging

import pytest

from adr import applog
from adr.config import Config


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        f"log_path: {tmp_path / 'logs'}\n",
    )
    return Config(str(path))


def _write(config, lines):
    path = applog.log_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _line(level, message, name="adr.pipeline", when="2026-08-04 07:26:01"):
    return f"{when} [{level}] {name}: {message}"


class TestConfigure:
    def test_it_writes_where_it_says(self, config):
        path = applog.configure(config)
        try:
            assert path == applog.log_path(config)
            logging.getLogger("adr.test").error("a thing happened")
            assert "a thing happened" in path.read_text(encoding="utf-8")
        finally:
            _detach()

    def test_calling_it_twice_does_not_double_every_line(self, config):
        """configure() runs once at startup, but a reload must not duplicate."""
        applog.configure(config)
        applog.configure(config)
        try:
            logging.getLogger("adr.test").error("said once")
            text = applog.log_path(config).read_text(encoding="utf-8")
            assert text.count("said once") == 1
        finally:
            _detach()

    def test_a_directory_it_cannot_create_is_a_warning_not_a_crash(self, tmp_path):
        import types

        blocked = tmp_path / "blocked"
        blocked.write_text("I am a file")
        config = types.SimpleNamespace(log_path=str(blocked / "logs"))
        assert applog.configure(config) is None


def _detach():
    """Remove the file handler so one test's log does not follow the next."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            root.removeHandler(handler)
            handler.close()


class TestReadingTheTail:
    def test_a_missing_file_is_reported_not_raised(self, config):
        data = applog.read_tail(config)
        assert data["exists"] is False
        assert data["lines"] == []

    def test_it_returns_the_lines(self, config):
        _write(config, [_line("INFO", "one"), _line("INFO", "two")])
        data = applog.read_tail(config)
        assert data["exists"] is True
        assert len(data["lines"]) == 2

    def test_only_the_last_lines_are_returned(self, config):
        _write(config, [_line("INFO", f"line {i}") for i in range(50)])
        data = applog.read_tail(config, lines=10)
        assert len(data["lines"]) == 10
        assert "line 49" in data["lines"][-1]
        assert data["truncated"] is True

    def test_a_huge_file_is_not_read_from_the_top(self, config, monkeypatch):
        """A month-old log should not have to be read whole to show its end."""
        monkeypatch.setattr(applog, "_TAIL_BYTES", 2000)
        _write(config, [_line("INFO", f"line {i:05d}") for i in range(2000)])
        data = applog.read_tail(config, lines=5)
        assert data["truncated"] is True
        assert "line 01999" in data["lines"][-1]


class TestFiltering:
    def test_a_level_keeps_that_level_and_above(self, config):
        """Asking for WARNING must not hide the ERROR you were looking for."""
        _write(config, [
            _line("DEBUG", "noise"),
            _line("INFO", "ordinary"),
            _line("WARNING", "odd"),
            _line("ERROR", "broken"),
        ])
        lines = applog.read_tail(config, level="WARNING")["lines"]
        assert len(lines) == 2
        assert any("odd" in line for line in lines)
        assert any("broken" in line for line in lines)

    def test_no_level_keeps_everything(self, config):
        _write(config, [_line("DEBUG", "noise"), _line("ERROR", "broken")])
        assert len(applog.read_tail(config, level="")["lines"]) == 2

    def test_an_unknown_level_is_ignored_rather_than_hiding_everything(self, config):
        _write(config, [_line("INFO", "ordinary")])
        assert applog.read_tail(config, level="BANANA")["lines"]

    def test_search_matches_within_the_line(self, config):
        _write(config, [
            _line("INFO", "ripping /dev/sr0"),
            _line("INFO", "encoding something"),
        ])
        lines = applog.read_tail(config, search="/dev/sr0")["lines"]
        assert len(lines) == 1

    def test_search_ignores_case(self, config):
        _write(config, [_line("ERROR", "HandBrake exited with code 1")])
        assert applog.read_tail(config, search="handbrake")["lines"]

    def test_a_traceback_is_kept_whole(self, config):
        """A traceback filtered down to its first line is not a traceback."""
        _write(config, [
            _line("INFO", "ordinary"),
            _line("ERROR", "Pipeline error"),
            "Traceback (most recent call last):",
            '  File "adr/pipeline.py", line 1, in run',
            "RuntimeError: boom",
        ])
        lines = applog.read_tail(config, level="ERROR")["lines"]
        assert len(lines) == 4
        assert lines[-1] == "RuntimeError: boom"

    def test_a_tracebacks_body_is_not_matched_by_the_level_of_a_later_line(self, config):
        _write(config, [
            _line("ERROR", "Pipeline error"),
            "RuntimeError: boom",
            _line("DEBUG", "afterwards"),
        ])
        lines = applog.read_tail(config, level="ERROR")["lines"]
        assert "afterwards" not in "\n".join(lines)

    def test_level_and_search_together(self, config):
        _write(config, [
            _line("ERROR", "HandBrake exited"),
            _line("ERROR", "something else"),
            _line("INFO", "HandBrake started"),
        ])
        lines = applog.read_tail(config, level="ERROR", search="handbrake")["lines"]
        assert len(lines) == 1
        assert "exited" in lines[0]


class TestDescribe:
    def test_a_missing_log_is_described_not_invented(self, config):
        info = applog.describe(config)
        assert info["exists"] is False
        assert info["size_kb"] == 0

    def test_size_and_rotations(self, config):
        path = _write(config, [_line("INFO", "x") for _ in range(10)])
        (path.parent / (applog.LOG_FILENAME + ".1")).write_text("old")
        info = applog.describe(config)
        assert info["exists"] is True
        assert info["size_kb"] > 0
        assert info["rotated"] == 1
