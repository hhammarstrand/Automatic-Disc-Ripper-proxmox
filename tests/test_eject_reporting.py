"""The tray stayed shut and nothing said why.

Five call sites ejected the disc and threw the return value away, and the job
log — the one page someone opens when the tray does not move — never mentioned
the eject at all. Worse, a successful call is not a moved tray: the ioctl only
reports that the kernel accepted the command, and on a drive still held open
elsewhere it is accepted and nothing happens.
"""

import types

import pytest

from adr import pipeline as pipeline_mod


class _Log:
    def __init__(self):
        self.lines = []

    def append(self, stage, text):
        self.lines.append(text)


def _config(eject=True, labels=None):
    return types.SimpleNamespace(
        should_eject=lambda d: eject,
        drive_display=lambda d: (labels or {}).get(d, d),
    )


def _states(monkeypatch, sequence):
    """media_status answers from *sequence*, repeating the last entry."""
    seen = list(sequence)

    def fake(device, display=None):
        state = seen.pop(0) if len(seen) > 1 else seen[0]
        return {"ready": state == "ready", "state": state, "detail": ""}

    monkeypatch.setattr("adr.disc.media_status", fake)


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(pipeline_mod.time, "sleep", lambda s: None)


class TestWhenTheTrayOpens:
    def test_it_says_so_in_the_job_log(self, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "eject_drive", lambda d: True)
        _states(monkeypatch, ["tray_open"])
        log = _Log()
        assert pipeline_mod._eject_and_report(
            _config(labels={"/dev/sr0": "Internal"}), "/dev/sr0", log) is True
        assert log.lines == ["Ejected the disc from Internal."]

    def test_an_empty_drive_counts_as_opened(self, monkeypatch):
        """A slot loader that swallows the disc reports empty, not tray_open."""
        monkeypatch.setattr(pipeline_mod, "eject_drive", lambda d: True)
        _states(monkeypatch, ["empty"])
        assert pipeline_mod._eject_and_report(_config(), "/dev/sr0", _Log()) is True

    def test_it_waits_for_the_tray_rather_than_asking_once(self, monkeypatch):
        """Asking immediately reports the disc still there on a drive that is
        opening perfectly well."""
        monkeypatch.setattr(pipeline_mod, "eject_drive", lambda d: True)
        _states(monkeypatch, ["ready", "ready", "tray_open"])
        log = _Log()
        assert pipeline_mod._eject_and_report(_config(), "/dev/sr0", log) is True
        assert "Ejected" in log.lines[0]


class TestWhenItDoesNot:
    def test_a_command_the_drive_accepted_but_did_not_act_on(self, monkeypatch):
        """The case the return value could never have caught: eject_drive says
        True, the disc is still sitting there."""
        monkeypatch.setattr(pipeline_mod, "eject_drive", lambda d: True)
        _states(monkeypatch, ["ready"])
        log = _Log()
        assert pipeline_mod._eject_and_report(
            _config(labels={"/dev/sr0": "Internal"}), "/dev/sr0", log) is False
        assert "Could not eject Internal" in log.lines[0]
        assert "accepted the command but the disc is still in it" in log.lines[0]

    def test_a_refusal_is_named_differently(self, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "eject_drive", lambda d: False)
        _states(monkeypatch, ["ready"])
        log = _Log()
        pipeline_mod._eject_and_report(_config(), "/dev/sr0", log)
        assert "refused the command" in log.lines[0]

    def test_it_says_what_to_do_and_does_not_stop_the_encode(self, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "eject_drive", lambda d: False)
        _states(monkeypatch, ["ready"])
        log = _Log()
        pipeline_mod._eject_and_report(_config(), "/dev/sr0", log)
        assert "Encoding carries on" in log.lines[0]
        assert "by hand" in log.lines[0]


class TestWhenItIsTurnedOff:
    def test_nothing_is_ejected_and_the_log_says_why(self, monkeypatch):
        """Silence here reads as a failure. It is a setting."""
        called = []
        monkeypatch.setattr(
            pipeline_mod, "eject_drive", lambda d: called.append(d) or True)
        log = _Log()
        assert pipeline_mod._eject_and_report(
            _config(eject=False, labels={"/dev/sr0": "Internal"}),
            "/dev/sr0", log) is False
        assert called == []
        assert "auto-eject is off" in log.lines[0]
        assert "Internal" in log.lines[0]


class TestEverySiteGoesThroughIt:
    def test_no_call_site_throws_the_answer_away(self):
        """Five places ejected and ignored the result. Read rather than run:
        each sits in the middle of a method that needs a disc and a drive."""
        from pathlib import Path

        source = Path("adr/pipeline.py").read_text()
        assert "eject_drive(self.drive)" not in source, (
            "a call site still ejects without reporting the outcome"
        )
        assert source.count("_eject_and_report(") >= 6      # 5 sites + the def
