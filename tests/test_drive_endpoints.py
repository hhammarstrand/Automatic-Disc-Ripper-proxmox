"""The drive a button acts on travels in the request body, not the URL.

A Linux optical drive is identified by a device path, and putting "/dev/sr0"
into a URL path means percent-encoding its slashes. Werkzeug then either
308-redirects to the same path with the leading slash gone — so the handler
received "dev/sr0", treated it as a Windows drive letter and upper-cased it
into "DEV/SR0" — or refuses to match at all. Both happened: Rip answered
"DEV/SR0 is not a drive this instance watches", and auto-eject and hide-drive
returned 404 without ever reaching their handlers.
"""

import types
from unittest.mock import MagicMock

import pytest

from adr import pipeline as pipeline_mod
from adr.config import Config
from adr.models import init_db
from adr.utils import normalize_drive
from web.app import create_app

DEVICE = "/dev/sr0"


class FakeDrivePipeline:
    def __init__(self):
        self.calls = []

    @property
    def is_busy(self):
        return False

    def handle_disc_inserted(self, drive, label, manual=False):
        self.calls.append(drive)


@pytest.fixture
def manager():
    mgr = MagicMock(spec=pipeline_mod.PipelineManager)
    mgr.config = types.SimpleNamespace(disabled_drives=[])
    mgr.drive_pipelines = {DEVICE: FakeDrivePipeline()}
    mgr.all_drives = [DEVICE]
    mgr.rip_now = pipeline_mod.PipelineManager.rip_now.__get__(mgr)
    return mgr


@pytest.fixture
def client(tmp_path, manager, monkeypatch):
    monkeypatch.setattr("adr.disc._has_media", lambda d: True)
    monkeypatch.setattr("adr.disc._blkid_label", lambda d: "THE_MATRIX")
    monkeypatch.setattr("web.app.eject_drive", lambda d: True)
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n",
    )
    init_db()
    app = create_app(Config(str(path)), pipeline_manager=manager)
    app.config["TESTING"] = True
    return app.test_client()


# ------------------------------------------------------------------ #
# normalize_drive
# ------------------------------------------------------------------ #

class TestNormalizeDrive:
    def test_a_device_path_is_unchanged(self):
        assert normalize_drive("/dev/sr0") == "/dev/sr0"

    def test_a_device_path_that_lost_its_slash_is_repaired(self):
        """What a browser holding the old page still produces."""
        assert normalize_drive("dev/sr0") == "/dev/sr0"

    def test_it_is_no_longer_upper_cased_into_nonsense(self):
        assert normalize_drive("dev/sr0") != "DEV/SR0"

    def test_windows_drive_letters_still_work(self):
        assert normalize_drive("d:") == "D:"
        assert normalize_drive("d:\\") == "D:"

    def test_something_that_is_not_a_device_is_left_to_the_old_rule(self):
        assert normalize_drive("dev/sr0/../etc") == "DEV/SR0/../ETC"

    def test_empty(self):
        assert normalize_drive("") == ""


# ------------------------------------------------------------------ #
# The routes the page uses
# ------------------------------------------------------------------ #

class TestBodyRoutes:
    def test_rip_reaches_the_right_drive(self, client, manager):
        response = client.post("/api/drives/rip", json={"device": DEVICE})
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert manager.drive_pipelines[DEVICE].calls == [DEVICE]

    def test_eject_toggle_no_longer_404s(self, client):
        """It never reached its handler before — the URL did not match."""
        response = client.post("/api/drives/eject-toggle",
                               json={"device": DEVICE, "auto_eject": False})
        assert response.status_code == 200
        assert response.get_json()["auto_eject"] is False

    def test_hiding_a_drive_no_longer_404s(self, client):
        response = client.post("/api/drives/toggle", json={"device": DEVICE, "disabled": True})
        assert response.status_code == 200
        assert response.get_json()["disabled_drives"] == [DEVICE]

    def test_the_device_is_stored_with_its_slashes(self, client):
        """A mangled name in the config would silently stop matching the drive
        it was meant to be about."""
        client.post("/api/drives/toggle", json={"device": DEVICE, "disabled": True})
        stored = client.get("/api/settings").get_json()["disabled_drives"]
        assert stored == ["/dev/sr0"]

    def test_labelling_a_drive(self, client):
        response = client.post("/api/drives/label", json={"device": DEVICE, "label": "Top"})
        assert response.status_code == 200
        assert client.get("/api/settings").get_json()["drive_labels"] == {DEVICE: "Top"}

    def test_ejecting(self, client):
        assert client.post("/api/drives/eject", json={"device": DEVICE}).status_code == 200

    def test_a_request_with_no_body_is_refused_not_crashed(self, client):
        response = client.post("/api/drives/rip")
        assert response.status_code == 409
        assert "is not a drive" in response.get_json()["message"]


# ------------------------------------------------------------------ #
# The old routes, for a page cached in a browser
# ------------------------------------------------------------------ #

class TestOldRoutesStillWork:
    def test_the_percent_encoded_path_no_longer_mangles_the_device(self, client, manager):
        """Exactly what the previous page sent. Werkzeug redirects it to
        /api/drives/dev/sr0/rip, and that has to still mean /dev/sr0."""
        response = client.post("/api/drives/%2Fdev%2Fsr0/rip", follow_redirects=True)
        assert response.status_code == 200
        assert manager.drive_pipelines[DEVICE].calls == [DEVICE]

    def test_the_error_no_longer_shouts_a_made_up_name(self, client, manager):
        manager.drive_pipelines.clear()
        response = client.post("/api/drives/%2Fdev%2Fsr0/rip", follow_redirects=True)
        assert "DEV/SR0" not in response.get_json()["message"]
        assert "/dev/sr0" in response.get_json()["message"]

    def test_eject_toggle_by_path(self, client):
        response = client.post("/api/drives/%2Fdev%2Fsr0/eject-toggle",
                               json={"auto_eject": False}, follow_redirects=True)
        assert response.status_code == 200
        assert response.get_json()["auto_eject"] is False


# ------------------------------------------------------------------ #
# The deep drive test
# ------------------------------------------------------------------ #

class TestDriveTestEndpoint:
    @pytest.fixture(autouse=True)
    def visible_device(self, monkeypatch):
        from adr import drivetest

        monkeypatch.setattr("adr.disc._sr_devices", lambda: [DEVICE])
        with drivetest._probes_lock:
            drivetest._probes.clear()

    def test_the_deep_test_returns_at_once(self, client, monkeypatch):
        """Blocking for five minutes is what made a phone say 'Load failed'."""
        import threading

        from adr import drivetest
        release = threading.Event()
        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: (release.wait(5), {"ok": True, "steps": []})[1],
        )
        response = client.post("/api/drives/test", json={"device": DEVICE, "deep": True})
        assert response.status_code == 202
        assert response.get_json()["running"] is True
        release.set()

    def test_the_shallow_test_still_answers_directly(self, client, monkeypatch):
        from adr import drivetest

        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: {"ok": True, "summary": "fine", "steps": []},
        )
        response = client.post("/api/drives/test", json={"device": DEVICE, "deep": False})
        assert response.status_code == 200
        assert response.get_json()["summary"] == "fine"

    def test_the_status_endpoint_hands_back_the_result(self, client, monkeypatch):
        from adr import drivetest

        monkeypatch.setattr(
            drivetest, "probe_drive",
            lambda device, deep=False: {"ok": True, "summary": "fine", "steps": []},
        )
        client.post("/api/drives/test", json={"device": DEVICE, "deep": True})
        import time
        for _ in range(200):
            body = client.get(f"/api/drives/test/status?device={DEVICE}").get_json()
            if not body["running"]:
                break
            time.sleep(0.02)
        assert body["result"]["summary"] == "fine"

    def test_asking_about_a_drive_never_tested(self, client):
        response = client.get(f"/api/drives/test/status?device={DEVICE}")
        assert response.status_code == 404

    def test_an_unknown_device_is_refused(self, client):
        response = client.post("/api/drives/test", json={"device": "/dev/sda", "deep": True})
        assert response.status_code == 400
        assert "Unknown optical device" in response.get_json()["error"]
