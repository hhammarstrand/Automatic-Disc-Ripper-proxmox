"""Tests for starting a rip on a disc that is already loaded.

Insertion is an *event*; a disc sitting in a drive is a *state*. The watcher
only ever sees the event, so after a failed job nothing restarts however long
you wait. These pin down that the manual trigger exists, and — more
importantly — that it refuses in every case where starting would be wrong.
"""

import types
from unittest.mock import MagicMock

import pytest

from adr import pipeline as pipeline_mod


class FakeDrivePipeline:
    def __init__(self, busy=False):
        self.busy = busy
        self.calls = []

    @property
    def is_busy(self):
        return self.busy

    def handle_disc_inserted(self, drive, label, manual=False):
        self.calls.append({"drive": drive, "label": label, "manual": manual})


@pytest.fixture
def manager():
    mgr = MagicMock(spec=pipeline_mod.PipelineManager)
    mgr.config = types.SimpleNamespace(disabled_drives=[])
    mgr.drive_pipelines = {"/dev/sr0": FakeDrivePipeline()}
    # Bind the real method to the stand-in.
    mgr.rip_now = pipeline_mod.PipelineManager.rip_now.__get__(mgr)
    return mgr


@pytest.fixture
def loaded(monkeypatch):
    monkeypatch.setattr("adr.disc._has_media", lambda d: True)
    monkeypatch.setattr("adr.disc._blkid_label", lambda d: "THE_MATRIX")


class TestItStarts:
    def test_a_loaded_disc_is_ripped(self, manager, loaded):
        ok, message = manager.rip_now("/dev/sr0")
        assert ok is True
        assert "THE_MATRIX" in message
        assert manager.drive_pipelines["/dev/sr0"].calls == [
            {"drive": "/dev/sr0", "label": "THE_MATRIX", "manual": True},
        ]

    def test_it_is_marked_manual(self, manager, loaded):
        """Manual means: skip the 'disc inserted' notification. Whoever pressed
        the button is standing right there."""
        manager.rip_now("/dev/sr0")
        assert manager.drive_pipelines["/dev/sr0"].calls[0]["manual"] is True

    def test_an_unlabelled_disc_still_starts(self, manager, monkeypatch):
        monkeypatch.setattr("adr.disc._has_media", lambda d: True)
        monkeypatch.setattr("adr.disc._blkid_label", lambda d: None)
        ok, message = manager.rip_now("/dev/sr0")
        assert ok is True
        assert "None" not in message


class TestItRefuses:
    def test_an_unknown_drive(self, manager, loaded):
        ok, message = manager.rip_now("/dev/sr9")
        assert ok is False
        assert "/dev/sr9" in message

    def test_a_drive_already_ripping(self, manager, loaded):
        """Starting a second rip on one drive would fight for the device."""
        manager.drive_pipelines["/dev/sr0"].busy = True
        ok, message = manager.rip_now("/dev/sr0")
        assert ok is False
        assert "already ripping" in message
        assert manager.drive_pipelines["/dev/sr0"].calls == []

    def test_a_disabled_drive(self, manager, loaded):
        manager.config.disabled_drives = ["/dev/sr0"]
        ok, message = manager.rip_now("/dev/sr0")
        assert ok is False
        assert "disabled" in message

    def test_an_empty_drive_points_at_the_doctor_page(self, manager, monkeypatch):
        """'No disc' and 'the container cannot open the drive' look identical
        from here, so the message names the place that can tell them apart."""
        monkeypatch.setattr("adr.disc._has_media", lambda d: False)
        monkeypatch.setattr("adr.disc._blkid_label", lambda d: None)
        ok, message = manager.rip_now("/dev/sr0")
        assert ok is False
        assert "No readable disc" in message
        assert "Doctor" in message
        assert manager.drive_pipelines["/dev/sr0"].calls == []
