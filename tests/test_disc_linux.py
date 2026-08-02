"""Tests for the Linux optical-drive detection layer (adr.disc)."""

import errno
import logging
import os
import subprocess

import pytest

from adr import disc

# ------------------------------------------------------------------ #
# _sr_devices
# ------------------------------------------------------------------ #

class TestSrDevices:
    def test_finds_sr_devices(self, tmp_path, monkeypatch):
        sys_block = tmp_path / "sys" / "block"
        for name in ("sr0", "sr1", "sda", "loop0"):
            (sys_block / name).mkdir(parents=True)
        monkeypatch.setattr(disc, "Path", _path_factory(tmp_path))
        # /dev/sr0 and /dev/sr1 must "exist" for them to be returned
        monkeypatch.setattr(disc.os.path, "exists", lambda p: p in ("/dev/sr0", "/dev/sr1"))
        assert disc._sr_devices() == ["/dev/sr0", "/dev/sr1"]

    def test_no_sys_block(self, monkeypatch):
        monkeypatch.setattr(disc.os.path, "exists", lambda p: False)
        # Point Path("/sys/block") at a non-existent dir
        import pathlib
        real = pathlib.Path
        monkeypatch.setattr(disc, "Path", lambda p="": real("/nonexistent-sys-block-xyz") if str(p) == "/sys/block" else real(p))
        assert disc._sr_devices() == []


def _path_factory(base):
    """Return a Path shim that redirects /sys/... under a temp dir."""
    import pathlib
    real = pathlib.Path

    def _factory(p=""):
        s = str(p)
        if s.startswith("/sys/"):
            return real(str(base) + s)
        return real(p)
    return _factory


# ------------------------------------------------------------------ #
# _has_media
# ------------------------------------------------------------------ #

class TestHasMedia:
    @pytest.fixture(autouse=True)
    def _forget_denials(self):
        """The 'logged this already' set is module state; keep tests independent."""
        disc._denied_devices.clear()
        yield
        disc._denied_devices.clear()

    def _openable(self, monkeypatch):
        monkeypatch.setattr(disc.os, "open", lambda *a, **k: 99)
        monkeypatch.setattr(disc.os, "close", lambda fd: None)

    def _open_fails(self, monkeypatch, err):
        def _raise(*a, **k):
            raise OSError(err, os.strerror(err))
        monkeypatch.setattr(disc.os, "open", _raise)

    def test_capacity_positive_means_media(self, monkeypatch):
        self._openable(monkeypatch)
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4324560)
        assert disc._has_media("/dev/sr0") is True

    def test_empty_tray(self, monkeypatch):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 0)
        self._open_fails(monkeypatch, errno.ENOMEDIUM)
        assert disc._has_media("/dev/sr0") is False

    def test_spinning_up_eio(self, monkeypatch):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 0)
        self._open_fails(monkeypatch, errno.EIO)
        assert disc._has_media("/dev/sr0") is True

    @pytest.mark.parametrize("err", [errno.EPERM, errno.EACCES])
    def test_a_disc_we_cannot_open_is_not_a_disc(self, monkeypatch, err):
        """The LXC trap: /sys is the HOST's, so sysfs sees the host's disc.

        Believing it starts a rip that MakeMKV cannot possibly complete.
        """
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4324560)
        self._open_fails(monkeypatch, err)
        assert disc._has_media("/dev/sr0") is False

    def test_missing_device_node_is_not_a_disc(self, monkeypatch):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4324560)
        self._open_fails(monkeypatch, errno.ENOENT)
        assert disc._has_media("/dev/sr0") is False

    def test_the_denial_is_logged_once_not_every_poll(self, monkeypatch, caplog):
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4324560)
        self._open_fails(monkeypatch, errno.EPERM)
        with caplog.at_level(logging.ERROR, logger="adr.disc"):
            for _ in range(5):
                disc._has_media("/dev/sr0")
        assert len(caplog.records) == 1, "polling every 3s must not flood the journal"
        assert "adr-doctor" in caplog.records[0].getMessage()

    def test_recovery_re_arms_the_warning(self, monkeypatch, caplog):
        """After a fix, a later regression must be reported again."""
        monkeypatch.setattr(disc, "_device_capacity", lambda dev: 4324560)
        self._open_fails(monkeypatch, errno.EPERM)
        disc._has_media("/dev/sr0")
        self._openable(monkeypatch)
        assert disc._has_media("/dev/sr0") is True
        self._open_fails(monkeypatch, errno.EPERM)
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="adr.disc"):
            disc._has_media("/dev/sr0")
        assert len(caplog.records) == 1


# ------------------------------------------------------------------ #
# _blkid_label
# ------------------------------------------------------------------ #

class TestBlkidLabel:
    def test_returns_label(self, monkeypatch):
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="THE_MATRIX\n", stderr=""),
        )
        assert disc._blkid_label("/dev/sr0") == "THE_MATRIX"

    def test_empty_label_is_none(self, monkeypatch):
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 2, stdout="\n", stderr=""),
        )
        assert disc._blkid_label("/dev/sr0") is None

    def test_blkid_missing_is_none(self, monkeypatch):
        def _raise(*a, **k):
            raise FileNotFoundError("blkid")

        monkeypatch.setattr(disc.subprocess, "run", _raise)
        assert disc._blkid_label("/dev/sr0") is None


# ------------------------------------------------------------------ #
# eject_drive
# ------------------------------------------------------------------ #

class TestEjectDrive:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(disc.shutil, "which", lambda n: "/usr/bin/eject")
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
        )
        assert disc.eject_drive("/dev/sr0") is True

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(disc.shutil, "which", lambda n: "/usr/bin/eject")
        monkeypatch.setattr(
            disc.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="not found"),
        )
        assert disc.eject_drive("/dev/sr0") is False

    def test_exception(self, monkeypatch):
        monkeypatch.setattr(disc.shutil, "which", lambda n: "/usr/bin/eject")

        def _raise(*a, **k):
            raise OSError("boom")

        monkeypatch.setattr(disc.subprocess, "run", _raise)
        assert disc.eject_drive("/dev/sr0") is False


# ------------------------------------------------------------------ #
# list_optical_drives
# ------------------------------------------------------------------ #

class TestListOpticalDrives:
    def test_assembles_entries(self, monkeypatch):
        monkeypatch.setattr(disc, "_sr_devices", lambda: ["/dev/sr0", "/dev/sr1"])
        monkeypatch.setattr(disc, "_has_media", lambda dev: dev == "/dev/sr0")
        monkeypatch.setattr(disc, "_blkid_label", lambda dev: "MOVIE" if dev == "/dev/sr0" else None)
        result = disc.list_optical_drives()
        assert result == [
            {"drive": "/dev/sr0", "volume_name": "MOVIE", "has_disc": True},
            {"drive": "/dev/sr1", "volume_name": None, "has_disc": False},
        ]


# ------------------------------------------------------------------ #
# DiscWatcher
# ------------------------------------------------------------------ #

class TestDiscWatcher:
    def test_register_drive_normalizes(self):
        w = disc.DiscWatcher(drives=["/dev/sr0"])
        w.register_drive("/dev/sr0")
        assert "/dev/sr0" in w._known_drives

    def test_resolve_drives_list_mode(self):
        w = disc.DiscWatcher(drives=["/dev/sr0", "/dev/sr1"])
        assert w._resolve_drives() == ["/dev/sr0", "/dev/sr1"]

    def test_fire_callbacks_invokes_listeners(self):
        w = disc.DiscWatcher(drives=["/dev/sr0"])
        seen = []
        w.on_disc_inserted(lambda drive, label: seen.append((drive, label)))
        w._fire_callbacks("/dev/sr0", "DISC")
        assert seen == [("/dev/sr0", "DISC")]

    def test_callback_exception_is_isolated(self):
        # A throwing callback must not break the watcher or other callbacks.
        w = disc.DiscWatcher(drives=["/dev/sr0"])
        seen = []
        w.on_disc_inserted(lambda d, lbl: (_ for _ in ()).throw(RuntimeError("boom")))
        w.on_disc_inserted(lambda d, lbl: seen.append(d))
        w._fire_callbacks("/dev/sr0", None)
        assert seen == ["/dev/sr0"]

    def test_new_drive_callback_fires(self):
        w = disc.DiscWatcher(drives="auto")
        seen = []
        w.on_new_drive(lambda drive: seen.append(drive))
        w._fire_new_drive_callbacks("/dev/sr2")
        assert seen == ["/dev/sr2"]
