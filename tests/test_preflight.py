"""Say once, up front, what would stop the next disc.

The pipeline has always refused a rip it knew would fail. It only ever said so
per disc, after the disc went in — so eleven discs produced eleven identical
red jobs and never once stated the one thing that was wrong.

The value of this module rests entirely on it giving the *same* answer the
pipeline gates on. A preflight that disagrees is worse than none: it either
promises a rip that then fails, or warns about one that would have worked. So
the first tests here are about that agreement, not about the wording.
"""

import pathlib
import queue
import types

import pytest

from adr import pipeline as pipeline_mod
from adr import preflight
from adr.config import Config
from adr.models import Job, JobStatus, get_session, init_db


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        "notify_enabled: false\n",
    )
    return Config(str(path))


@pytest.fixture
def no_drive_blockers(monkeypatch):
    """Drives are a separate concern; these tests are about the destination."""
    monkeypatch.setattr(
        "adr.disc.diagnose_passthrough",
        lambda: {"drives": [], "problems": [], "ok": True},
    )


# ------------------------------------------------------------------ #
# The gate itself
# ------------------------------------------------------------------ #

class TestDestinationBlocker:
    def test_a_working_destination_blocks_nothing(self, config):
        assert preflight.destination_blocker(config) is None

    def test_a_missing_destination_is_named(self, config, tmp_path):
        missing = tmp_path / "gone"
        config.update({"completed_path": str(missing)})
        # Config creates its directories on load, so remove it again — the
        # case under test is a destination that is not there when the disc is.
        if missing.exists():
            missing.rmdir()
        detail = preflight.destination_blocker(config)
        assert detail and "does not exist" in detail

    def test_an_unmounted_destination_is_named(self, config):
        """The user's actual failure: the share is not attached, so finished
        films would quietly fill the container disk."""
        config.update({"require_completed_mount": True})
        detail = preflight.destination_blocker(config)
        assert detail and "on the container's own disk" in detail

    def test_a_broken_plex_library_is_named_as_such(self, config, tmp_path):
        config.update({"plex_path": str(tmp_path / "no-library")})
        detail = preflight.destination_blocker(config)
        assert detail and detail.startswith("Plex library unusable:")


class TestItAgreesWithThePipeline:
    """If these two ever disagree the warning is worse than no warning."""

    def _run_a_disc(self, config, monkeypatch):
        from adr import disctype
        from adr.disctype import DiscInfo

        init_db()
        monkeypatch.setattr(pipeline_mod.Notifier, "job_failed", lambda *a, **k: True)
        monkeypatch.setattr(
            disctype, "classify",
            lambda d: DiscInfo(kind=disctype.KIND_VIDEO, detail="Video."),
        )
        drive = pipeline_mod.DrivePipeline("/dev/sr0", config, queue.Queue())
        monkeypatch.setattr(drive._ripper, "scan_disc", lambda d, job_id=None: {})
        monkeypatch.setattr(drive._ripper, "rip", lambda **kw: _rip_failure())
        drive._run_pipeline("HAPPY_FEET_TWO")
        session = get_session()
        try:
            return session.query(Job).order_by(Job.id.desc()).first()
        finally:
            session.close()

    def test_the_pipeline_refuses_exactly_what_preflight_warns_about(
        self, config, monkeypatch,
    ):
        config.update({"require_completed_mount": True})
        warned = preflight.destination_blocker(config)
        assert warned

        job = self._run_a_disc(config, monkeypatch)
        assert job.status == JobStatus.ERROR
        assert job.error_message == warned, "the two descriptions must not drift"

    def test_the_pipeline_proceeds_when_preflight_is_happy(self, config, monkeypatch):
        assert preflight.destination_blocker(config) is None
        job = self._run_a_disc(config, monkeypatch)
        # It got past the gate and reached the ripper, which is stubbed to fail.
        assert "stubbed" in job.error_message


def _rip_failure():
    from adr.ripper import RipResult

    result = RipResult()
    result.success = False
    result.error = "stubbed"
    return result


# ------------------------------------------------------------------ #
# What the dashboard is handed
# ------------------------------------------------------------------ #

class TestCheck:
    def test_a_healthy_setup_has_nothing_to_say(self, config, no_drive_blockers):
        result = preflight.check(config)
        assert result.ok
        assert result.blockers == []

    def test_a_blocker_carries_something_to_do_about_it(self, config, no_drive_blockers):
        config.update({"require_completed_mount": True})
        result = preflight.check(config)
        assert not result.ok
        blocker = result.blockers[0]
        assert "nowhere to go" in blocker.title
        assert blocker.fix, "a blocker nobody can act on is just bad news"

    def test_the_container_id_is_substituted_so_commands_are_runnable(
        self, config, no_drive_blockers,
    ):
        config.update({"require_completed_mount": True})
        result = preflight.with_ctid(preflight.check(config), "108")
        assert "pct reboot 108" in result.blockers[0].fix
        assert "{ctid}" not in result.blockers[0].fix

    def test_without_a_container_id_the_placeholder_is_obvious(
        self, config, no_drive_blockers,
    ):
        config.update({"require_completed_mount": True})
        result = preflight.with_ctid(preflight.check(config), None)
        assert "<CTID>" in result.blockers[0].fix

    def test_an_unreachable_drive_is_its_own_blocker(self, config, monkeypatch):
        monkeypatch.setattr("adr.disc.diagnose_passthrough", lambda: {
            "drives": [{"device": "/dev/sr0", "node_present": False}],
            "problems": ["the passthrough did not apply"],
            "ok": False,
        })
        result = preflight.check(config)
        assert not result.ok
        assert "cannot reach the drive" in result.blockers[0].title
        assert "adr-doctor --fix" in result.blockers[0].fix

    def test_a_check_that_explodes_does_not_take_the_dashboard_with_it(
        self, config, monkeypatch,
    ):
        monkeypatch.setattr(
            "adr.disc.diagnose_passthrough",
            lambda: (_ for _ in ()).throw(RuntimeError("sysfs went away")),
        )
        result = preflight.check(config)          # must not raise
        assert isinstance(result.ok, bool)

    def test_it_serialises_for_the_api(self, config, no_drive_blockers):
        config.update({"require_completed_mount": True})
        payload = preflight.check(config).as_dict()
        assert payload["ok"] is False
        assert payload["blockers"][0]["detail"]
        assert payload["blockers"][0]["fix"]


# ------------------------------------------------------------------ #
# Advice that matches the fault
# ------------------------------------------------------------------ #

class TestTheFixMatchesTheFault:
    """"Not mounted" and "not writable" need opposite actions."""

    def test_an_unmounted_share_says_restart_the_container(self):
        fix = preflight._destination_fix(
            "Destination /mnt/media is on the container's own disk, not on attached storage.",
        )
        assert "pct reboot" in fix
        assert "captured when the container starts" in fix

    def test_an_unwritable_share_says_re_run_the_nas_setup(self):
        fix = preflight._destination_fix("Destination /mnt/media is not writable by uid 8420.")
        assert "adr-setup-nas" in fix
        assert "pct reboot" not in fix

    def test_a_missing_folder_points_at_settings(self):
        assert "Settings" in preflight._destination_fix("Destination /x does not exist.")

    def test_a_full_disk_says_free_space(self):
        fix = preflight._destination_fix("Only 2 GB free on /x — not enough for a rip.")
        assert "Free some space" in fix

    def test_anything_else_still_gets_somewhere_to_look(self):
        assert preflight._destination_fix("something new went wrong")


# ------------------------------------------------------------------ #
# The Rip button
# ------------------------------------------------------------------ #

def test_rip_now_refuses_instead_of_making_another_red_job(config, monkeypatch):
    """Pressing Rip against a broken destination made a job that failed with a
    message it could have given immediately — and pressing it again made
    another one."""
    from unittest.mock import MagicMock

    config.update({"require_completed_mount": True})
    pipeline = types.SimpleNamespace(
        is_busy=False, calls=[],
        handle_disc_inserted=lambda *a, **k: pytest.fail("the rip must not start"),
    )
    mgr = MagicMock(spec=pipeline_mod.PipelineManager)
    mgr.config = config
    mgr.drive_pipelines = {"/dev/sr0": pipeline}
    mgr.rip_now = pipeline_mod.PipelineManager.rip_now.__get__(mgr)

    monkeypatch.setattr(
        "adr.disc.media_status",
        lambda d: {"ready": True, "state": "ready", "detail": ""},
    )
    monkeypatch.setattr("adr.disc._blkid_label", lambda d: "HAPPY_FEET_TWO")

    ok, message = mgr.rip_now("/dev/sr0")
    assert ok is False
    assert "Ripping would fail" in message
    assert "on the container's own disk" in message


# ------------------------------------------------------------------ #
# What the dashboard shows
# ------------------------------------------------------------------ #

@pytest.fixture
def client(config, no_drive_blockers):
    import web.app as app_module
    from web.app import create_app

    app_module._preflight_cache = None       # the cache is a module global
    init_db()
    app = create_app(config, pipeline_manager=None)
    app.config["TESTING"] = True
    yield app.test_client()
    app_module._preflight_cache = None


class TestTheBanner:
    def test_a_healthy_setup_shows_no_banner(self, client):
        assert "preflightBanner" not in client.get("/").data.decode()

    def test_a_broken_destination_is_stated_before_any_disc(self, client, config):
        config.update({"require_completed_mount": True})
        html = client.get("/").data.decode()
        assert "preflightBanner" in html
        assert "Ripping will fail" in html
        # No apostrophe in the match: Jinja escapes it to &#39;.
        assert "own disk, not on attached storage" in html

    def test_the_banner_carries_the_fix(self, client, config):
        config.update({"require_completed_mount": True})
        html = client.get("/").data.decode()
        assert "pct reboot" in html

    def test_the_api_answers_the_same_question(self, client, config):
        config.update({"require_completed_mount": True})
        payload = client.get("/api/preflight").get_json()
        assert payload["ok"] is False
        assert "on the container's own disk" in payload["blockers"][0]["detail"]

    def test_the_api_says_ok_when_it_is(self, client):
        assert client.get("/api/preflight").get_json() == {"ok": True, "blockers": []}

    def test_the_answer_is_cached_so_polling_is_cheap(self, client, monkeypatch):
        """The dashboard polls every five seconds and this stats filesystems —
        one of which may be the very network share it is complaining about."""
        from adr import preflight as preflight_mod

        calls = []
        real = preflight_mod.check
        monkeypatch.setattr(
            preflight_mod, "check",
            lambda cfg, mgr=None: (calls.append(1), real(cfg, mgr))[1],
        )
        for _ in range(4):
            client.get("/api/preflight")
        assert len(calls) == 1


# ------------------------------------------------------------------ #
# A library inside the share
# ------------------------------------------------------------------ #

class TestASubfolderOfTheShareIsFine:
    """The ordinary way to arrange a library, and it used to be refused.

    require_completed_mount asks "is my NAS actually attached". The old check
    answered a narrower question — os.path.ismount, true only for the mount
    point itself. A share mounted at /mnt/media with the film library at
    /mnt/media/Filmer therefore failed: a folder inside a mount is never a
    mount point. The share was attached, writable and had eight terabytes
    free, and every rip was refused anyway.
    """

    @pytest.fixture
    def share(self):
        """A real mount to put a subfolder in. /dev/shm is a tmpfs."""
        import os
        import shutil
        import uuid

        if not os.path.ismount("/dev/shm"):
            pytest.skip("no tmpfs at /dev/shm to stand in for the share")
        root = pathlib.Path("/dev/shm") / f"adr-test-{uuid.uuid4().hex[:8]}"
        (root / "Filmer").mkdir(parents=True)
        yield root
        shutil.rmtree(root, ignore_errors=True)

    def test_the_mount_point_itself_passes(self, share):
        from adr.storage import check_destination

        ok, message = check_destination("/dev/shm", require_mount=True)
        assert ok, message

    def test_a_subfolder_of_the_mount_passes_too(self, share):
        from adr.storage import check_destination

        ok, message = check_destination(share / "Filmer", require_mount=True)
        assert ok, f"a library inside the share must be accepted: {message}"

    def test_a_folder_on_the_container_disk_is_still_refused(self, tmp_path):
        """The check must still catch what it exists to catch: a directory
        with the right name on the wrong filesystem."""
        from adr.storage import check_destination

        ok, message = check_destination(tmp_path, require_mount=True)
        assert ok is False
        assert "own disk, not on attached storage" in message

    def test_the_reported_setup_end_to_end(self, config, share, no_drive_blockers):
        """Share mounted at one path, library in a subfolder of it."""
        config.update({
            "completed_path": str(share),
            "plex_path": str(share / "Filmer"),
            "require_completed_mount": True,
            "stage_locally": False,
        })
        assert preflight.destination_blocker(config) is None
        assert preflight.check(config).ok
