"""The deep drive test runs in the background and is polled.

It allows five minutes, because that is what a Blu-ray with many playlists
legitimately needs. Holding an HTTP request open that long does not work: a
phone browser gives up long before, and the only thing the page could then say
was "Test failed: Load failed" — which reads as a broken drive when the drive
is fine and still reading.
"""

import threading
import time

import pytest

from adr import drivetest


@pytest.fixture(autouse=True)
def clean_registry():
    with drivetest._probes_lock:
        drivetest._probes.clear()
    yield
    with drivetest._probes_lock:
        drivetest._probes.clear()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestStartProbe:
    def test_it_returns_before_the_probe_finishes(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: (release.wait(5), {"ok": True, "steps": []})[1],
        )

        started = time.monotonic()
        state = drivetest.start_probe("/dev/sr0", deep=True)
        assert time.monotonic() - started < 1.0, "start_probe blocked"
        assert state["running"] is True
        assert state["result"] is None
        release.set()

    def test_the_result_appears_when_it_is_done(self, monkeypatch):
        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: {"ok": True, "summary": "fine", "steps": []},
        )
        drivetest.start_probe("/dev/sr0", deep=True)
        assert _wait_for(lambda: not drivetest.probe_status("/dev/sr0")["running"])
        state = drivetest.probe_status("/dev/sr0")
        assert state["result"]["summary"] == "fine"
        assert state["error"] is None

    def test_a_probe_that_raises_is_reported_not_lost(self, monkeypatch):
        def boom(device, deep=False):
            raise RuntimeError("the drive caught fire")

        monkeypatch.setattr(drivetest, "probe_drive", boom)
        drivetest.start_probe("/dev/sr0", deep=True)
        assert _wait_for(lambda: not drivetest.probe_status("/dev/sr0")["running"])
        state = drivetest.probe_status("/dev/sr0")
        assert "caught fire" in state["error"]
        assert state["result"] is None

    def test_asking_twice_joins_the_probe_already_running(self, monkeypatch):
        """Two MakeMKV processes on one drive is how a working drive is made
        to fail."""
        starts = []
        release = threading.Event()

        def slow(device, deep=False):
            starts.append(device)
            release.wait(5)
            return {"ok": True, "steps": []}

        monkeypatch.setattr(drivetest, "probe_drive", slow)
        drivetest.start_probe("/dev/sr0", deep=True)
        assert _wait_for(lambda: len(starts) == 1)
        drivetest.start_probe("/dev/sr0", deep=True)
        time.sleep(0.1)
        assert starts == ["/dev/sr0"], "a second probe was started on the same drive"
        release.set()

    def test_a_finished_probe_can_be_run_again(self, monkeypatch):
        runs = []
        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: (runs.append(device), {"ok": True, "steps": []})[1],
        )
        drivetest.start_probe("/dev/sr0", deep=True)
        assert _wait_for(lambda: not drivetest.probe_status("/dev/sr0")["running"])
        drivetest.start_probe("/dev/sr0", deep=True)
        assert _wait_for(lambda: len(runs) == 2)

    def test_two_drives_are_probed_independently(self, monkeypatch):
        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: {"ok": True, "summary": device, "steps": []},
        )
        drivetest.start_probe("/dev/sr0", deep=True)
        drivetest.start_probe("/dev/sr1", deep=True)
        assert _wait_for(lambda: all(
            not drivetest.probe_status(d)["running"] for d in ("/dev/sr0", "/dev/sr1")
        ))
        assert drivetest.probe_status("/dev/sr0")["result"]["summary"] == "/dev/sr0"
        assert drivetest.probe_status("/dev/sr1")["result"]["summary"] == "/dev/sr1"

    def test_elapsed_time_is_reported_so_the_page_can_say_something(self, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: (release.wait(5), {"ok": True, "steps": []})[1],
        )
        drivetest.start_probe("/dev/sr0", deep=True)
        time.sleep(0.15)
        assert drivetest.probe_status("/dev/sr0")["elapsed"] > 0
        release.set()

    def test_a_drive_never_probed_has_no_status(self):
        assert drivetest.probe_status("/dev/sr9") is None
