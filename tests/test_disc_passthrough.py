"""Tests for adr.disc.diagnose_passthrough.

Inside an LXC, /sys is the host's sysfs, so the host's optical drives are always
visible there — while /dev/sr* only exists if the passthrough applied at
container start. That asymmetry produces two failure modes that look identical
from the dashboard, and these tests pin down that each is reported distinctly.
"""

import errno
import os

import pytest

import adr.disc as disc


class FakeSysBlock:
    """Stands in for pathlib.Path('/sys/block') with the given sr* entries."""

    def __init__(self, names):
        self._names = names

    def exists(self):
        return True

    def iterdir(self):
        return [type("P", (), {"name": n})() for n in self._names]


@pytest.fixture
def host_has_sr0(monkeypatch):
    """The host exposes /sys/block/sr0 with a model, and a disc is loaded."""
    monkeypatch.setattr(disc, "Path", lambda p="": FakeSysBlock(["sr0"]) if str(p) == "/sys/block" else _real_path(p))
    monkeypatch.setattr(disc, "_drive_model", lambda dev: "HL-DT-ST BD-RE WH16NS40")
    monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4_700_000)


_real_path = __import__("pathlib").Path


class TestPassthroughMissing:
    """The container never got the device node — the classic post-reboot case."""

    def test_missing_node_is_reported(self, host_has_sr0, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        result = disc.diagnose_passthrough()

        assert result["ok"] is False
        assert result["drives"][0]["node_present"] is False
        problem = result["problems"][0]
        assert "not present in this container" in problem
        assert "pct reboot" in problem, "must tell the user how to fix it"

    def test_the_drive_is_still_described(self, host_has_sr0, monkeypatch):
        """Knowing which drive is missing matters on a multi-drive host."""
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        drive = disc.diagnose_passthrough()["drives"][0]
        assert drive["device"] == "/dev/sr0"
        assert "WH16NS40" in drive["model"]


class TestCgroupDenied:
    """The node exists but the device cgroup refuses it — the silent trap."""

    def _deny(self, monkeypatch, err):
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        def fake_open(path, flags):
            raise OSError(err, os.strerror(err))

        monkeypatch.setattr(os, "open", fake_open)

    def test_eperm_is_reported_with_the_fix(self, host_has_sr0, monkeypatch):
        self._deny(monkeypatch, errno.EPERM)
        result = disc.diagnose_passthrough()

        assert result["ok"] is False
        drive = result["drives"][0]
        assert drive["node_present"] is True
        assert drive["openable"] is False
        assert "EPERM" in drive["error"]

        problem = result["problems"][0]
        assert "device cgroup is denying access" in problem
        assert "b 11:* rwm" in problem, "must name the correct cgroup rule"
        assert "block, not char" in problem

    def test_eacces_is_also_caught(self, host_has_sr0, monkeypatch):
        self._deny(monkeypatch, errno.EACCES)
        assert disc.diagnose_passthrough()["ok"] is False

    def test_media_shown_by_sysfs_does_not_mask_the_denial(self, host_has_sr0, monkeypatch):
        """The exact trap: sysfs says a disc is loaded, but it cannot be read."""
        self._deny(monkeypatch, errno.EPERM)
        result = disc.diagnose_passthrough()
        assert result["drives"][0]["has_media"] is True
        assert result["ok"] is False, "media presence must not imply a working drive"


class TestHealthyDrive:
    def test_open_succeeds(self, host_has_sr0, monkeypatch):
        monkeypatch.setattr(os.path, "exists", lambda p: True)
        monkeypatch.setattr(os, "open", lambda p, f: 99)
        monkeypatch.setattr(os, "close", lambda fd: None)
        result = disc.diagnose_passthrough()
        assert result["ok"] is True
        assert result["problems"] == []
        assert result["drives"][0]["openable"] is True

    @pytest.mark.parametrize("err", [errno.ENOMEDIUM, errno.ENXIO, errno.EIO, errno.EBUSY])
    def test_empty_tray_is_not_a_fault(self, host_has_sr0, monkeypatch, err):
        """An empty or spinning-up drive is reachable, just not loaded."""
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        def fake_open(path, flags):
            raise OSError(err, os.strerror(err))

        monkeypatch.setattr(os, "open", fake_open)
        result = disc.diagnose_passthrough()
        assert result["ok"] is True, f"errno {err} should not be treated as a failure"


class TestNoDriveAtAll:
    def test_reports_that_the_host_has_none(self, monkeypatch):
        monkeypatch.setattr(disc, "Path", lambda p="": FakeSysBlock([]))
        result = disc.diagnose_passthrough()
        assert result["ok"] is False
        assert "even on the Proxmox host" in result["problems"][0]
