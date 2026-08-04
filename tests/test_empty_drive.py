"""An empty drive is an empty drive, and says so in words.

The report: the container starts with nothing in the drive, and it rips
anyway — a red job with a MakeMKV exit code in it. Pressing Rip on an empty
drive did the same.

The cause was one line. ``open(device, O_RDONLY | O_NONBLOCK)`` on an optical
drive is *specified* to succeed with an empty tray — that is how you are meant
to open one in order to ask what is in it — and ``_has_media`` read a
successful open as "media present". So the drive was reported loaded from the
moment the service came up, the watcher fired its startup event, and a job
started on an empty drive.

The fix is to ask the drive instead of inferring: CDROM_DRIVE_STATUS answers
exactly this question. These tests pin that down, and pin down that each way
of having no disc gets its own sentence — an empty tray, an open tray, a
device node that was never passed through and a cgroup denial were one message
between them, and only one of the four is fixed by putting a disc in.
"""

import errno
import os

import pytest

from adr import disc


@pytest.fixture(autouse=True)
def _forget_denials():
    disc._denied_devices.clear()
    yield
    disc._denied_devices.clear()


def _drive(monkeypatch, *, status=None, open_errno=None, capacity=0, exists=True):
    """Stand in for one optical drive at the syscall boundary."""
    monkeypatch.setattr(os.path, "exists", lambda p: exists)
    monkeypatch.setattr(disc, "_device_capacity", lambda dev: capacity)
    if open_errno is not None:
        def _raise(*a, **k):
            raise OSError(open_errno, os.strerror(open_errno))
        monkeypatch.setattr(disc.os, "open", _raise)
    else:
        monkeypatch.setattr(disc.os, "open", lambda *a, **k: 99)
        monkeypatch.setattr(disc.os, "close", lambda fd: None)
    monkeypatch.setattr(disc, "_drive_status", lambda fd: status)


# ------------------------------------------------------------------ #
# The bug itself
# ------------------------------------------------------------------ #

class TestAnOpenThatSucceedsIsNotADisc:
    def test_an_empty_tray_that_opens_fine_is_still_empty(self, monkeypatch):
        """The whole bug in one assertion. O_NONBLOCK succeeds on an empty
        optical drive, and this used to be read as media present."""
        _drive(monkeypatch, status=disc.CDS_NO_DISC)
        assert disc._has_media("/dev/sr0") is False

    def test_an_open_tray_is_not_a_disc(self, monkeypatch):
        _drive(monkeypatch, status=disc.CDS_TRAY_OPEN)
        assert disc._has_media("/dev/sr0") is False

    def test_a_loaded_disc_is_a_disc(self, monkeypatch):
        _drive(monkeypatch, status=disc.CDS_DISC_OK)
        assert disc._has_media("/dev/sr0") is True

    def test_the_drive_outranks_the_host_s_sysfs(self, monkeypatch):
        """Inside an LXC /sys is the host's, and it can be stale. The drive
        itself cannot be."""
        _drive(monkeypatch, status=disc.CDS_NO_DISC, capacity=4_700_000_000)
        assert disc._has_media("/dev/sr0") is False

    def test_a_drive_that_will_not_answer_falls_back_to_capacity(self, monkeypatch):
        """Some USB enclosures do not implement the ioctl at all."""
        _drive(monkeypatch, status=None, capacity=4_700_000_000)
        assert disc._has_media("/dev/sr0") is True

    def test_and_reports_empty_when_there_is_nothing_to_go_on(self, monkeypatch):
        _drive(monkeypatch, status=None, capacity=0)
        assert disc._has_media("/dev/sr0") is False

    def test_a_disc_still_spinning_up_counts(self, monkeypatch):
        """Losing this event loses the insertion: the watcher fires on the
        transition, so there is no second chance."""
        _drive(monkeypatch, open_errno=errno.EIO)
        assert disc._has_media("/dev/sr0") is True


# ------------------------------------------------------------------ #
# Saying why, in words
# ------------------------------------------------------------------ #

class TestMediaStatusExplainsItself:
    def test_an_empty_drive_says_to_put_a_disc_in(self, monkeypatch):
        _drive(monkeypatch, status=disc.CDS_NO_DISC)
        state = disc.media_status("/dev/sr0")
        assert state["ready"] is False
        assert state["state"] == "empty"
        assert "no disc in /dev/sr0" in state["detail"]

    def test_an_open_tray_says_to_close_it(self, monkeypatch):
        _drive(monkeypatch, status=disc.CDS_TRAY_OPEN)
        state = disc.media_status("/dev/sr0")
        assert state["state"] == "tray_open"
        assert "tray" in state["detail"]

    def test_a_missing_node_names_the_passthrough(self, monkeypatch):
        """A different problem with a different fix, on the host rather than
        in front of the machine."""
        _drive(monkeypatch, exists=False)
        state = disc.media_status("/dev/sr0")
        assert state["state"] == "missing"
        assert "adr-doctor" in state["detail"]
        assert "passed through" in state["detail"]

    def test_a_cgroup_denial_is_not_an_empty_drive(self, monkeypatch):
        _drive(monkeypatch, open_errno=errno.EACCES)
        state = disc.media_status("/dev/sr0")
        assert state["state"] == "denied"
        assert "not allowed to open it" in state["detail"]
        assert "adr-doctor" in state["detail"]

    def test_still_spinning_up_says_to_wait(self, monkeypatch):
        _drive(monkeypatch, status=disc.CDS_DRIVE_NOT_READY)
        state = disc.media_status("/dev/sr0")
        assert state["state"] == "not_ready"
        assert "few seconds" in state["detail"]

    def test_a_loaded_disc_is_ready(self, monkeypatch):
        _drive(monkeypatch, status=disc.CDS_DISC_OK)
        state = disc.media_status("/dev/sr0")
        assert state["ready"] is True
        assert state["state"] == "ready"

    def test_every_detail_is_a_sentence_not_a_code(self, monkeypatch):
        """These strings go straight to a person. An errno is allowed to be in
        one; it is not allowed to be the whole of one."""
        for kwargs in ({"status": disc.CDS_NO_DISC},
                       {"status": disc.CDS_TRAY_OPEN},
                       {"status": disc.CDS_DRIVE_NOT_READY},
                       {"exists": False},
                       {"open_errno": errno.EACCES}):
            _drive(monkeypatch, **kwargs)
            detail = disc.media_status("/dev/sr0")["detail"]
            assert detail.endswith("."), detail
            assert len(detail.split()) >= 6, detail

    def test_waiting_helps_only_where_it_can(self, monkeypatch):
        """A disc spinning up must not be dropped; the other four cannot
        become ready on their own."""
        assert "not_ready" not in disc.NOTHING_TO_RIP
        assert {"missing", "denied", "empty", "tray_open"} == disc.NOTHING_TO_RIP


# ------------------------------------------------------------------ #
# And no job is created for a drive with nothing in it
# ------------------------------------------------------------------ #

class TestTheWatcherDoesNotStartAJob:
    def _pipeline(self, monkeypatch, state):
        import types

        from adr.pipeline import DrivePipeline

        monkeypatch.setattr(
            "adr.disc.media_status",
            lambda d: {"ready": state == "ready", "state": state, "detail": "x."},
        )
        started = []
        obj = types.SimpleNamespace(
            drive="/dev/sr0",
            _config=types.SimpleNamespace(disabled_drives=[]),
            _run_pipeline=lambda volume_name: started.append(volume_name),
        )
        obj.handle_disc_inserted = DrivePipeline.handle_disc_inserted.__get__(obj)
        return obj, started

    @pytest.mark.parametrize("state", ["empty", "tray_open", "missing", "denied"])
    def test_an_empty_drive_creates_no_job(self, monkeypatch, state):
        import threading

        pipeline, started = self._pipeline(monkeypatch, state)
        spawned = []
        monkeypatch.setattr(
            threading, "Thread",
            lambda **kw: spawned.append(kw) or _NullThread(),
        )
        pipeline.handle_disc_inserted("/dev/sr0", None)
        assert spawned == [], "a job was started for a drive with no disc in it"

    def test_a_loaded_disc_still_starts(self, monkeypatch):
        import threading

        pipeline, _ = self._pipeline(monkeypatch, "ready")
        monkeypatch.setattr("adr.pipeline.Notifier", lambda config: _NullNotifier())
        spawned = []
        monkeypatch.setattr(
            threading, "Thread",
            lambda **kw: spawned.append(kw) or _NullThread(),
        )
        pipeline.handle_disc_inserted("/dev/sr0", "THE_MATRIX")
        assert len(spawned) == 1


class _NullThread:
    def start(self):
        pass


class _NullNotifier:
    def disc_inserted(self, *a, **k):
        pass
