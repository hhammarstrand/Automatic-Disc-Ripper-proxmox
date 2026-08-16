"""A drive card says what the drive is doing, not what its last job is doing.

The dashboard used to read "Encoding" under a drive that was standing idle.
Encoding runs in a worker pool over files already on the disk; the drive's own
lock is held only for the rip. So a drive whose previous disc is encoding is
free — you can put the next disc in and it starts immediately — and the card
was reporting a job's state as if it were the drive's.

It cost more than a wrong word. The Rip button was drawn only on a drive whose
status was "idle", so for the whole length of an encode — the longest phase
there is — a perfectly free drive had no button to start the disc sitting in
it.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adr import pipeline as pipeline_mod
from adr.config import Config
from adr.disc import drive_state
from adr.models import Job, JobStatus, get_session, init_db
from web.app import create_app

DEVICE = "/dev/sr0"


class TestWhatTheDriveIsDoing:
    """The pure part: a media state plus a rip flag, and nothing else."""

    def test_a_rip_is_the_only_thing_that_makes_a_drive_busy(self):
        assert drive_state("ready", True) == "ripping"
        assert drive_state("empty", True) == "ripping"

    def test_an_encoding_job_is_not_a_drive_state_at_all(self):
        """There is no argument for it to arrive by, which is the point —
        the encode never reaches this function."""
        assert drive_state("ready", False) == "loaded"
        assert drive_state("empty", False) == "empty"

    def test_a_disc_being_spun_up_is_a_disc_that_is_in_there(self):
        assert drive_state("not_ready", False) == "loaded"

    def test_an_open_tray_is_its_own_answer(self):
        """Not folded into "empty", though neither holds a readable disc.

        The drive reports the two separately — CDS_TRAY_OPEN and CDS_NO_DISC
        are different answers to the same ioctl — and they mean different
        things to the person standing there: an empty drive is waiting for a
        disc, an open tray is usually a disc that has just been ejected and is
        waiting to be taken out. Collapsing them threw away the one fact that
        says the machine has finished with something.
        """
        assert drive_state("tray_open", False) == "tray_open"
        assert drive_state("empty", False) == "empty"

    def test_a_tray_that_opens_mid_rip_still_reads_as_ripping(self):
        """The rip is what the drive is doing; the tray is where it is. The
        job's own log is where a disc vanishing mid-rip gets reported."""
        assert drive_state("tray_open", True) == "ripping"

    @pytest.mark.parametrize("state", ["missing", "denied"])
    def test_a_drive_this_container_cannot_reach_says_so(self, state):
        assert drive_state(state, False) == "unavailable"


class FakeDrivePipeline:
    is_busy = False

    def handle_disc_inserted(self, drive, label, manual=False):
        pass


@pytest.fixture
def app_config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n",
    )
    return Config(str(path))


@pytest.fixture
def client(app_config, monkeypatch):
    """A container with one drive, a disc in it, and everything healthy."""
    monkeypatch.setattr(
        "adr.disc.media_status",
        lambda d, display=None: {"ready": True, "state": "ready", "detail": ""},
    )
    monkeypatch.setattr("adr.disc.get_drive_models", lambda: {DEVICE: "ASUS BW-16"})
    manager = MagicMock(spec=pipeline_mod.PipelineManager)
    manager.config = app_config
    manager.drive_pipelines = {DEVICE: FakeDrivePipeline()}
    manager.all_drives = [DEVICE]
    manager.encode_queue = MagicMock()
    manager.encode_queue.qsize.return_value = 0
    init_db()
    app = create_app(app_config, pipeline_manager=manager)
    app.config["TESTING"] = True
    return app.test_client()


def _job(status: JobStatus) -> int:
    session = get_session()
    try:
        job = Job(disc_label="JUMANJI", title="Jumanji", year=1995,
                  drive=DEVICE, status=status, progress_rip=1.0,
                  progress_encode=0.62)
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


class TestTheDashboardDuringAnEncode:
    """The reported case: /dev/sr1 read "Encoding" while it stood empty."""

    def test_the_drive_does_not_claim_to_be_encoding(self, client):
        _job(JobStatus.ENCODING)
        page = client.get("/").get_data(as_text=True)
        drives = page.split('id="drivesRow"', 1)[1].split("Active Jobs", 1)[0]
        assert "Encoding" not in drives, "the drive card reported a job's state"
        assert "Disc in" in drives, "a disc is loaded and the card should say so"

    def test_and_keeps_the_button_that_starts_the_next_disc(self, client):
        """The functional half. This drive can rip right now."""
        _job(JobStatus.ENCODING)
        page = client.get("/").get_data(as_text=True)
        drives = page.split('id="drivesRow"', 1)[1].split("Active Jobs", 1)[0]
        assert f"ripNow('{DEVICE}')" in drives

    def test_a_rip_still_reads_as_a_rip(self, client):
        _job(JobStatus.RIPPING)
        page = client.get("/").get_data(as_text=True)
        drives = page.split('id="drivesRow"', 1)[1].split("Active Jobs", 1)[0]
        assert "Ripping" in drives
        assert f"ripNow('{DEVICE}')" not in drives, (
            "a drive mid-rip must not offer to start another one"
        )

    def test_an_empty_drive_says_empty(self, client, monkeypatch):
        monkeypatch.setattr(
            "adr.disc.media_status",
            lambda d, display=None: {"ready": False, "state": "empty",
                                     "detail": ""},
        )
        page = client.get("/").get_data(as_text=True)
        drives = page.split('id="drivesRow"', 1)[1].split("Active Jobs", 1)[0]
        assert "Empty" in drives

    def test_the_encoding_job_is_still_on_the_page(self, client):
        """Moved, not deleted: it belongs to Active Jobs, where it says which
        drive it came off."""
        _job(JobStatus.ENCODING)
        page = client.get("/").get_data(as_text=True)
        assert "Jumanji" in page.split("Active Jobs", 1)[1]


class TestTheDrivesEndpoint:
    """Same fault, same fix, in the JSON an outside caller reads."""

    def test_it_reports_the_drive_not_the_job(self, client):
        _job(JobStatus.ENCODING)
        body = client.get("/api/drives").get_json()
        assert body[0]["status"] == "loaded"
        assert body[0]["job"] is None, "an encoding job is not holding the drive"

    def test_a_ripping_job_is_reported_with_the_drive(self, client):
        _job(JobStatus.RIPPING)
        body = client.get("/api/drives").get_json()
        assert body[0]["status"] == "ripping"
        assert body[0]["job"]["title"] == "Jumanji"


class TestTheDashboardSurvivesADriveThatWillNotAnswer:
    def test_an_exception_reading_the_drive_does_not_take_the_page(
            self, client, monkeypatch):
        def explode(device, display=None):
            raise OSError("the bus went away")

        monkeypatch.setattr("adr.disc.media_status", explode)
        assert client.get("/").status_code == 200


class TestTheTrayIsShownWhenItIsOut:
    """Both drives reported "Empty" with their trays hanging open.

    The information was read and thrown away one line short of the screen:
    media_status has always separated tray_open from empty, and drive_state
    collapsed the two. Ejecting a disc and seeing the drive still described as
    empty is the machine failing to admit it just did something.
    """

    def test_the_label_says_which(self):
        from adr.disc import DRIVE_STATE_LABELS

        assert DRIVE_STATE_LABELS["tray_open"] == "Tray open"
        assert DRIVE_STATE_LABELS["empty"] == "Empty"

    def test_the_dashboard_renders_it(self, client, monkeypatch):
        monkeypatch.setattr(
            "adr.disc.media_status",
            lambda d, display=None: {"ready": False, "state": "tray_open",
                                     "detail": ""},
        )
        page = client.get("/").get_data(as_text=True)
        drives = page.split('id="drivesRow"', 1)[1].split("Active Jobs", 1)[0]
        assert "Tray open" in drives
        assert 'data-drive-status="tray_open"' in drives

    def test_it_has_a_glyph_of_its_own(self):
        """Colour alone cannot carry it: an open tray is neither activity nor
        a fault, so it shares the neutral treatment with empty."""
        index = Path("web/templates/index.html").read_text()
        assert "'tray_open': 'bi-eject-fill'" in index

    def test_the_rip_button_stays(self, client, monkeypatch):
        """Pressing it on an open tray answers "the tray of X is open, close
        it with a disc in it", which is a better outcome than a missing button
        on a drive whose ioctl was wrong."""
        monkeypatch.setattr(
            "adr.disc.media_status",
            lambda d, display=None: {"ready": False, "state": "tray_open",
                                     "detail": ""},
        )
        page = client.get("/").get_data(as_text=True)
        assert f"ripNow('{DEVICE}')" in page

    def test_the_endpoint_reports_it_too(self, client, monkeypatch):
        monkeypatch.setattr(
            "adr.disc.media_status",
            lambda d, display=None: {"ready": False, "state": "tray_open",
                                     "detail": ""},
        )
        body = client.get("/api/drives").get_json()
        assert body[0]["status"] == "tray_open"
        assert body[0]["state_label"] == "Tray open"
