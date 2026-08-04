"""One block of text that answers the questions a screenshot cannot.

The most important property is not what it contains but what it does not: a
bundle is made to be pasted somewhere public, so nothing that could
authenticate as the user may survive in it. Those tests come first.
"""

import pytest

from adr import applog, bundle
from adr.config import Config
from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db

SECRETS = {
    "tmdb_api_key": "tmdb-0123456789abcdef",
    "plex_token": "plex-abcdefghijklmnop",
    "notify_token": "ntfy-secret-token-here",
    "notify_url": "https://ntfy.sh/my-private-topic-nobody-should-know",
    "plex_url": "http://10.10.0.5:32400",
}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n"
        f"log_path: {tmp_path / 'logs'}\n"
        "handbrake_preset: Fast 1080p30\n"
        + "".join(f"{k}: {v}\n" for k, v in SECRETS.items()),
    )
    init_db()
    return Config(str(path))


@pytest.fixture
def quiet_drives(monkeypatch):
    monkeypatch.setattr("adr.disc.diagnose_passthrough", lambda: {
        "drives": [{"device": "/dev/sr0", "node_present": True,
                    "openable": True, "has_media": True}],
        "problems": [], "ok": True,
    })


# ------------------------------------------------------------------ #
# What must never leave
# ------------------------------------------------------------------ #

class TestNothingSecretSurvives:
    @pytest.mark.parametrize("name,value", sorted(SECRETS.items()))
    def test_the_value_is_not_in_the_bundle(self, config, quiet_drives, name, value):
        """A bundle is made to be pasted in public."""
        text = bundle.build(config)
        assert value not in text, f"{name} leaked into the bundle"

    def test_but_it_still_says_whether_they_are_set(self, config, quiet_drives):
        """Whether a key exists is half the diagnosis, and is not a secret."""
        text = bundle.build(config)
        assert "tmdb_api_key = <set, redacted>" in text

    def test_an_unset_secret_reads_as_empty(self, tmp_path):
        path = tmp_path / "adr.yaml"
        path.write_text(
            f"raw_path: {tmp_path / 'raw'}\n"
            f"completed_path: {tmp_path / 'completed'}\n"
            f"staging_path: {tmp_path / 'staging'}\n",
        )
        init_db()
        assert "tmdb_api_key = <empty>" in bundle.build(Config(str(path)))

    def test_a_new_setting_is_redacted_until_someone_says_otherwise(self, config,
                                                                    quiet_drives):
        """The list is a whitelist on purpose: a missing setting costs a
        follow-up question, a leaked token costs the user their account."""
        config.update({"tmdb_api_key": "should-not-appear"})
        assert "should-not-appear" not in bundle.build(config)

    def test_the_whitelist_holds_no_secrets(self):
        for name in bundle.SAFE_KEYS:
            assert not any(word in name for word in ("token", "key", "password", "secret")), \
                f"{name} is on the safe list but looks like a credential"


# ------------------------------------------------------------------ #
# What must be there
# ------------------------------------------------------------------ #

class TestItAnswersTheUsualQuestions:
    def test_the_version(self, config, quiet_drives):
        from adr import __version__

        assert __version__ in bundle.build(config)

    def test_whether_a_rip_would_work(self, config, quiet_drives):
        assert "Will a rip work right now" in bundle.build(config)

    def test_the_self_checks(self, config, quiet_drives):
        text = bundle.build(config)
        assert "Self-checks" in text
        assert "Optical drives" in text

    def test_where_the_files_go_and_whether_it_is_a_mount(self, config, quiet_drives):
        text = bundle.build(config)
        assert "completed" in text
        assert "container disk" in text or "network" in text

    def test_the_safe_settings_are_shown_in_full(self, config, quiet_drives):
        assert "handbrake_preset = 'Fast 1080p30'" in bundle.build(config)

    def test_the_service_log(self, config, quiet_drives):
        path = applog.log_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("2026-08-04 07:26:01 [ERROR] adr.pipeline: the thing broke\n")
        assert "the thing broke" in bundle.build(config)

    def test_it_says_so_when_there_is_no_log_yet(self, config, quiet_drives):
        assert "no log file" in bundle.build(config)

    def test_the_whole_hardware_encoding_stack(self, config, quiet_drives, monkeypatch):
        """Diagnosing this from a distance took several rounds of "run this
        and paste the output" — the node, the driver, the runtime, and what
        the stack itself says. None of it can authenticate anything, so it
        belongs here rather than in a conversation."""
        from adr import gpu

        monkeypatch.setattr(gpu, "describe", lambda: {
            "available": True, "nodes": ["/dev/dri/renderD128"],
            "detail": "the node is there.", "fix": "",
            "runtime": {
                "ok": False, "vendor": "0x8086",
                "drivers": ["iHD_drv_video.so"], "libs": [],
                "dispatchers": ["libvpl.so.2"], "detail": "", "fix": "",
            },
        })
        monkeypatch.setattr(gpu, "vainfo", lambda: {
            "ran": True, "ok": False, "driver": "Intel iHD driver",
            "encoders": [], "output": "",
        })
        text = bundle.build(config)
        assert "Hardware encoding" in text
        assert "0x8086" in text
        assert "iHD_drv_video.so" in text
        assert "libvpl.so.2" in text
        assert "stack ok     NO" in text
        assert "NONE — cannot encode" in text


class TestRecentFailures:
    def _failed_job(self, config, error, track_error=None):
        session = get_session()
        try:
            job = Job(drive="/dev/sr0", status=JobStatus.ERROR,
                      title="The Film", year=1999, error_message=error)
            session.add(job)
            session.commit()
            session.add(Track(job_id=job.id, track_number=1, filename="t.mkv",
                              status=TrackStatus.ERROR, error_message=track_error))
            session.commit()
            return job.id
        finally:
            session.close()

    def test_no_failures_says_so(self, config, quiet_drives):
        assert "No failed jobs." in bundle.build(config)

    def test_the_error_is_included(self, config, quiet_drives):
        self._failed_job(config, "HandBrake exited with code 1")
        assert "HandBrake exited with code 1" in bundle.build(config)

    def test_the_per_track_reason_is_included(self, config, quiet_drives):
        self._failed_job(config, "encode failed", track_error="Invalid preset")
        assert "Invalid preset" in bundle.build(config)

    def test_the_tool_output_is_included(self, config, quiet_drives):
        from adr.joblog import JobLog

        job_id = self._failed_job(config, "encode failed")
        JobLog(config, job_id).append("encode", "x264 could not open the file")
        assert "x264 could not open the file" in bundle.build(config)

    def test_only_the_most_recent_few(self, config, quiet_drives):
        for n in range(bundle.FAILED_JOBS + 3):
            self._failed_job(config, f"failure number {n}")
        text = bundle.build(config)
        assert f"failure number {bundle.FAILED_JOBS + 2}" in text
        assert "failure number 0" not in text


# ------------------------------------------------------------------ #
# It has to survive a broken installation, which is when it is used
# ------------------------------------------------------------------ #

class TestItSurvivesWhatItIsFor:
    def test_one_broken_section_does_not_lose_the_rest(self, config, monkeypatch):
        monkeypatch.setattr(
            "adr.disc.diagnose_passthrough",
            lambda: (_ for _ in ()).throw(RuntimeError("sysfs went away")),
        )
        text = bundle.build(config)
        assert "could not be gathered" in text
        assert "Self-checks" in text
        assert "Settings" in text

    def test_a_missing_destination_does_not_stop_it(self, config, quiet_drives,
                                                    tmp_path):
        # update() writes the setting without reloading, so the directory is
        # never created — which is exactly the case under test.
        config.update({"completed_path": str(tmp_path / "gone")})
        assert not (tmp_path / "gone").exists()
        text = bundle.build(config)
        assert "MISSING" in text

    def test_it_is_text_someone_can_read(self, config, quiet_drives):
        text = bundle.build(config)
        assert text.startswith("Automatic Disc Ripper")
        assert text.endswith("\n")
        assert text.count("=== ") >= 8


def test_every_fix_in_the_bundle_is_a_command_someone_can_run(config, quiet_drives,
                                                              monkeypatch):
    """A fix that still reads '{ctid}' is a placeholder, not an instruction."""
    monkeypatch.setenv("ADR_CTID", "108")
    monkeypatch.setattr("adr.disc.diagnose_passthrough", lambda: {
        "drives": [{"device": "/dev/sr0", "node_present": False,
                    "openable": False, "has_media": False}],
        "problems": ["the passthrough did not apply"], "ok": False,
    })
    text = bundle.build(config)
    assert "{ctid}" not in text
    assert "adr-doctor --fix 108" in text
