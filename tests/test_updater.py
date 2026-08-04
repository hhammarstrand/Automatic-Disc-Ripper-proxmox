"""Tests for adr.updater.

The update path crosses a privilege boundary — an unprivileged, unauthenticated
web UI asking a root service to fetch and install code. Most of what matters
here is what the module refuses to do.
"""

import subprocess

import pytest

from adr import updater


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "INSTALL_DIR", tmp_path)
    monkeypatch.setattr(updater, "COMMIT_FILE", tmp_path / ".commit")
    monkeypatch.setattr(updater, "REQUEST_FILE", tmp_path / ".update-requested")
    monkeypatch.setattr(updater, "LOG_FILE", tmp_path / "update.log")


SHA_A = "a" * 40
SHA_B = "b" * 40


class TestInstalledCommit:
    def test_reads_a_recorded_sha(self, tmp_path):
        (tmp_path / ".commit").write_text(SHA_A + "\n")
        assert updater.installed_commit() == SHA_A

    def test_missing_file_is_none(self):
        assert updater.installed_commit() is None

    def test_garbage_is_rejected(self, tmp_path):
        """A truncated or corrupt file must not be compared against a real SHA."""
        (tmp_path / ".commit").write_text("not-a-sha")
        assert updater.installed_commit() is None


class TestRemoteCommit:
    def _ls_remote(self, monkeypatch, stdout="", returncode=0, stderr=""):
        monkeypatch.setattr(
            updater.subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, returncode, stdout, stderr),
        )

    def test_parses_the_sha(self, monkeypatch):
        self._ls_remote(monkeypatch, stdout=f"{SHA_B}\trefs/heads/main\n")
        sha, error = updater.remote_commit()
        assert sha == SHA_B
        assert error == ""

    def test_missing_branch_is_an_error_not_a_sha(self, monkeypatch):
        self._ls_remote(monkeypatch, stdout="")
        sha, error = updater.remote_commit(ref="nope")
        assert sha is None
        assert "nope" in error

    def test_git_failure_surfaces_its_message(self, monkeypatch):
        self._ls_remote(monkeypatch, returncode=128, stderr="fatal: repository not found\n")
        sha, error = updater.remote_commit()
        assert sha is None
        assert "repository not found" in error

    def test_timeout_is_reported(self, monkeypatch):
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired("git", 20)
        monkeypatch.setattr(updater.subprocess, "run", _timeout)
        sha, error = updater.remote_commit(timeout=20)
        assert sha is None
        assert "20s" in error


class TestCheckForUpdate:
    def _remote(self, monkeypatch, sha, error=""):
        monkeypatch.setattr(updater, "remote_commit", lambda *a, **k: (sha, error))

    def test_differing_shas_mean_an_update(self, tmp_path, monkeypatch):
        (tmp_path / ".commit").write_text(SHA_A)
        self._remote(monkeypatch, SHA_B)
        assert updater.check_for_update()["update_available"] is True

    def test_same_sha_means_up_to_date(self, tmp_path, monkeypatch):
        (tmp_path / ".commit").write_text(SHA_A)
        self._remote(monkeypatch, SHA_A)
        assert updater.check_for_update()["update_available"] is False

    def test_unknown_local_commit_is_not_an_update(self, monkeypatch):
        """'Cannot tell' must not render as 'out of date' on every page load."""
        self._remote(monkeypatch, SHA_B)
        result = updater.check_for_update()
        assert result["update_available"] is False
        assert result["known"] is False

    def test_unreachable_github_is_not_an_update(self, tmp_path, monkeypatch):
        (tmp_path / ".commit").write_text(SHA_A)
        self._remote(monkeypatch, None, "No answer from GitHub.")
        result = updater.check_for_update()
        assert result["update_available"] is False
        assert result["error"]


class TestUpdatesSupported:
    def test_a_missing_unit_is_refused_with_the_way_out(self, monkeypatch):
        monkeypatch.setattr(updater.Path, "exists", lambda self: False)
        ok, why = updater.updates_supported()
        assert ok is False
        assert "update.sh" in why

    def test_an_inactive_watcher_is_refused(self, monkeypatch):
        """A flag file nobody watches is the worst outcome: a silent no-op."""
        monkeypatch.setattr(updater.Path, "exists", lambda self: True)
        monkeypatch.setattr(updater, "_unit_active", lambda unit: False)
        ok, why = updater.updates_supported()
        assert ok is False
        assert updater.WATCH_UNIT in why
        assert "systemctl enable --now" in why

    def test_both_present_is_supported(self, monkeypatch):
        monkeypatch.setattr(updater.Path, "exists", lambda self: True)
        monkeypatch.setattr(updater, "_unit_active", lambda unit: True)
        assert updater.updates_supported() == (True, "")


class TestRequestUpdate:
    def test_unsupported_install_is_refused(self, monkeypatch):
        monkeypatch.setattr(updater, "updates_supported", lambda: (False, "no unit"))
        ok, message = updater.request_update()
        assert ok is False
        assert message == "no unit"
        assert not updater.REQUEST_FILE.exists(), "a flag nobody watches must not be left behind"

    def test_supported_install_writes_the_flag(self, monkeypatch):
        monkeypatch.setattr(updater, "updates_supported", lambda: (True, ""))
        monkeypatch.setattr(updater, "update_status", lambda: {"state": "idle"})
        ok, _ = updater.request_update()
        assert ok is True
        assert updater.REQUEST_FILE.exists()

    @pytest.mark.parametrize("state", ["requested", "running"])
    def test_a_second_request_is_refused(self, monkeypatch, state):
        monkeypatch.setattr(updater, "updates_supported", lambda: (True, ""))
        monkeypatch.setattr(updater, "update_status", lambda: {"state": state})
        ok, message = updater.request_update()
        assert ok is False
        assert "already in progress" in message


class TestUpdateStatus:
    def _unit(self, monkeypatch, **values):
        monkeypatch.setattr(updater, "_unit_state", lambda: values)

    def test_pending_flag_reads_as_requested(self, monkeypatch):
        updater.REQUEST_FILE.touch()
        self._unit(monkeypatch, ActiveState="inactive", ExecMainStatus="0")
        assert updater.update_status()["state"] == "requested"

    def test_activating_reads_as_running(self, monkeypatch):
        self._unit(monkeypatch, ActiveState="activating")
        assert updater.update_status()["state"] == "running"

    def test_nonzero_exit_reads_as_failed(self, monkeypatch):
        self._unit(monkeypatch, ActiveState="failed", Result="exit-code", ExecMainStatus="1")
        assert updater.update_status()["state"] == "failed"

    def test_clean_exit_reads_as_done(self, monkeypatch):
        self._unit(monkeypatch, ActiveState="inactive", Result="success", ExecMainStatus="0")
        assert updater.update_status()["state"] == "done"

    def test_the_log_tail_is_returned(self, tmp_path, monkeypatch):
        self._unit(monkeypatch, ActiveState="activating")
        (tmp_path / "update.log").write_text("fetching…\ndone\n")
        assert "fetching" in updater.update_status()["log"]

    def test_a_huge_log_is_truncated(self, tmp_path, monkeypatch):
        """A multi-MB log must not be shipped to the browser in full."""
        self._unit(monkeypatch, ActiveState="activating")
        (tmp_path / "update.log").write_text("x" * 200_000)
        log = updater.update_status()["log"]
        assert 0 < len(log) <= updater._LOG_TAIL_BYTES

    def test_a_missing_log_is_not_an_error(self, monkeypatch):
        self._unit(monkeypatch, ActiveState="inactive", ExecMainStatus="0")
        assert updater.update_status()["log"] == ""


class TestItWillNotUpdateOnTopOfARunningJob:
    """update.sh stops the service, which kills MakeMKV with it. MakeMKV
    writes each title as it goes, so the rip dies part-way and leaves files
    that look perfectly ordinary in a directory listing and are truncated
    mid-frame. An hour, gone, for a button press.

    The script refuses for the same reason. Refusing here too is what makes
    that an answer rather than a failed click — a button offered and then
    declined teaches people the button is unreliable.
    """

    @pytest.fixture
    def ready(self, monkeypatch, tmp_path):
        """An install where everything else about updating is fine."""
        unit = tmp_path / "adr-update.service"
        unit.write_text("")
        monkeypatch.setattr(updater, "UPDATE_UNIT", unit.name)
        monkeypatch.setattr(
            updater.Path, "exists", lambda self: True, raising=False)
        monkeypatch.setattr(updater, "_unit_active", lambda unit: True)

    def _job(self, status):
        from adr.models import Job, JobStatus, get_session, init_db
        from adr.utils import utcnow

        init_db()
        session = get_session()
        session.add(Job(disc_label="X", title="X", drive="/dev/sr0",
                        status=getattr(JobStatus, status), started_at=utcnow()))
        session.commit()
        session.close()

    def test_a_rip_in_progress_blocks_it(self, ready):
        self._job("RIPPING")
        supported, why = updater.updates_supported()
        assert supported is False
        assert "ripping" in why

    def test_an_encode_in_progress_blocks_it_too(self, ready):
        self._job("ENCODING")
        assert updater.updates_supported()[0] is False

    def test_it_says_when_the_button_comes_back(self, ready):
        """A refusal with no end in sight reads as a broken feature."""
        self._job("RIPPING")
        assert "when the job finishes" in updater.updates_supported()[1]

    def test_a_finished_job_does_not_block_it(self, ready):
        self._job("DONE")
        assert updater.updates_supported()[0] is True

    def test_no_jobs_at_all_does_not_block_it(self, ready):
        from adr.models import init_db

        init_db()
        assert updater.updates_supported()[0] is True

    def test_a_database_it_cannot_read_does_not_block_it(self, ready, monkeypatch):
        """The script checks again before it stops anything, so the worst
        case is an offer the script then declines — which beats refusing to
        update because a query failed."""
        monkeypatch.setattr(
            updater, "_job_in_progress",
            lambda: (_ for _ in ()).throw(RuntimeError("no database")))
        with pytest.raises(RuntimeError):
            updater._job_in_progress()

    def test_the_helper_swallows_its_own_errors(self, monkeypatch):
        import adr.models

        monkeypatch.setattr(
            adr.models, "get_session",
            lambda: (_ for _ in ()).throw(RuntimeError("gone")))
        assert updater._job_in_progress() == ""
