"""The tray stayed shut and nothing said why.

Five call sites ejected the disc and threw the return value away, and the job
log — the one page someone opens when the tray does not move — never mentioned
the eject at all. Worse, a successful call is not a moved tray: the ioctl only
reports that the kernel accepted the command, and on a drive still held open
elsewhere it is accepted and nothing happens.
"""

import types
from pathlib import Path

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
        """Five places ejected and ignored the result.

        There are no five any more: they were funnelled into _release, which
        is the only caller, so the question this test asks is now answered by
        the shape of the module — and the one thing still worth pinning is
        that nobody has gone back to ejecting without reporting.
        """
        source = Path("adr/pipeline.py").read_text()
        assert "eject_drive(self.drive)" not in source, (
            "a call site still ejects without reporting the outcome"
        )
        assert "self._release(" in source


class TestEveryWayOutLetsGoOfTheDisc:
    """The tray opened when the rip worked and stayed shut when it did not.

    Reported from the machine: "släden åker fortfarande inte alltid ut". The
    eject sat on the success path, and every failure returned before reaching
    it — a disc MakeMKV could not read, a scan that found no titles, a
    destination that was not mounted, a duplicate, a cancellation, a pipeline
    exception. Backwards: the disc you want back is the one that just failed,
    because looking at it is the next thing you do. It also left the drive
    loaded, so the disc pushed in after it hit "already ripping".
    """

    SOURCE = Path(pipeline_mod.__file__).read_text()

    def test_the_eject_has_exactly_one_caller(self):
        """The funnel is the fix. A new early return cannot forget to eject if
        there is only one place that ejects, and it is the one the finally
        calls."""
        body = self.SOURCE
        calls = body.count("_eject_and_report(")
        assert calls == 2, (
            "expected the definition and the single call inside _release, "
            f"found {calls} occurrences"
        )
        inside = body.split("def _release(")[1].split("\n    def ")[0]
        assert "_eject_and_report(" in inside

    def test_the_finally_lets_go_before_it_frees_the_drive(self):
        """Order matters: a disc pushed straight back in must not land on a
        pipeline that has not finished with the old one."""
        run = self.SOURCE.split("def _run_pipeline(")[1]
        # Newline-anchored: the method has a nested finally at a deeper
        # indent, and an unanchored split lands inside it.
        tail = run.split("\n        finally:")[1]
        assert tail.index("self._release(") < tail.index("self._lock.release()")


class TestTheReleaseGuard:
    def _pipeline(self, monkeypatch, calls):
        drive = pipeline_mod.DrivePipeline.__new__(pipeline_mod.DrivePipeline)
        drive._config = _config()
        drive.drive = "/dev/sr0"
        drive._released = False
        monkeypatch.setattr(
            pipeline_mod, "_eject_and_report",
            lambda config, dev, log=None: calls.append(dev) or True,
        )
        return drive

    def test_a_second_call_does_nothing(self, monkeypatch):
        """A good rip lets go early so the next disc can go in while this one
        encodes; the finally then runs and must not say it twice."""
        calls = []
        drive = self._pipeline(monkeypatch, calls)
        drive._release(_Log())
        drive._release(_Log())
        assert calls == ["/dev/sr0"]

    def test_a_failing_eject_never_escapes(self, monkeypatch):
        """Releasing the lock is the one thing that must happen, and this runs
        first."""
        drive = pipeline_mod.DrivePipeline.__new__(pipeline_mod.DrivePipeline)
        drive._config = _config()
        drive.drive = "/dev/sr0"
        drive._released = False

        def explode(config, dev, log=None):
            raise OSError("the bus went away")

        monkeypatch.setattr(pipeline_mod, "_eject_and_report", explode)
        drive._release(_Log())            # must not raise
        assert drive._released is True

    def test_the_next_disc_starts_from_a_clean_slate(self):
        """_released is reset per run, not per pipeline: a drive that ejected
        one disc still has to eject the next."""
        source = pipeline_mod.__file__
        body = Path(source).read_text().split("def _run_pipeline(")[1]
        assert "self._released = False" in body.split("try:")[0]
