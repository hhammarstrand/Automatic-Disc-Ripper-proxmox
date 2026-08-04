"""A MakeMKV that stops talking must not hold the drive for ever.

There is deliberately no overall time limit on a rip — a Blu-ray with many
playlists legitimately takes hours, and a disc that is merely slow must not be
thrown away. But the read loop blocks on a pipe with no timeout, so a MakeMKV
that has stopped entirely holds the drive, the job and the thread for as long
as the service runs. The watchdog only fires on *complete* silence.
"""

import subprocess
import time

import pytest

from adr import ripper
from adr.ripper import MakeMKVRipper
from adr.utils import kill_process_tree


@pytest.fixture
def fast_watchdog(monkeypatch):
    """Shrink the timeouts so the behaviour is testable in under a second."""
    monkeypatch.setattr(ripper, "STALL_TIMEOUT", 0.4)
    monkeypatch.setattr(ripper, "STALL_CHECK_INTERVAL", 0.05)


def _ripper(tmp_path, script: str):
    """A MakeMKVRipper whose makemkvcon is *script*."""
    import types

    exe = tmp_path / "makemkvcon"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    return MakeMKVRipper(types.SimpleNamespace(
        makemkv_path=str(exe),
        min_title_length=120,
        raw_path=tmp_path / "raw",
    ))


# ------------------------------------------------------------------ #
# The watchdog
# ------------------------------------------------------------------ #

def test_a_silent_tool_is_abandoned(tmp_path, fast_watchdog):
    started = time.monotonic()
    result = _ripper(tmp_path, "sleep 30\n").rip("/dev/sr0", job_id=1)
    elapsed = time.monotonic() - started

    assert not result.success
    assert "stopped responding" in result.error
    assert "no output at all" in result.error
    # Abandoned near the timeout, not after the full sleep.
    assert elapsed < 10, "the watchdog did not stop the rip"


def test_the_message_helps_tell_the_disc_from_the_drive(tmp_path, fast_watchdog):
    """'MakeMKV is stuck' has two very different causes and only one is fixable
    by the person reading it."""
    result = _ripper(tmp_path, "sleep 30\n").rip("/dev/sr0", job_id=1)
    assert "another disc in the same drive" in result.error


def test_a_tool_that_keeps_talking_is_left_alone(tmp_path, fast_watchdog):
    """Output resets the clock, so a slow-but-working rip survives."""
    script = """
i=0
while [ $i -lt 12 ]; do
    echo "MSG:1005,0,1,\\"working\\",\\"working\\""
    sleep 0.1
    i=$((i + 1))
done
exit 3
"""
    result = _ripper(tmp_path, script).rip("/dev/sr0", job_id=1)
    assert not result.success
    # Exit code, not a stall: it was talking the whole time.
    assert "exit code 3" in result.error
    assert "stopped responding" not in result.error


def test_a_quick_failure_is_reported_as_itself(tmp_path, fast_watchdog):
    """The code is still named — it is the only handle for a web search — but
    it no longer stands alone as the whole explanation."""
    result = _ripper(tmp_path, "exit 7\n").rip("/dev/sr0", job_id=1)
    assert "exit code 7" in result.error
    assert "dirty or scratched disc" in result.error


def test_a_missing_binary_is_reported(tmp_path, fast_watchdog):
    import types

    r = MakeMKVRipper(types.SimpleNamespace(
        makemkv_path=str(tmp_path / "not-installed"),
        min_title_length=120,
        raw_path=tmp_path / "raw",
    ))
    result = r.rip("/dev/sr0", job_id=1)
    assert not result.success
    assert "not installed" in result.error
    assert "Settings" in result.error


def test_the_timeout_is_generous_by_default():
    """Killing a working rip costs forty minutes, so the default has to be far
    beyond anything a healthy rip does."""
    assert ripper.STALL_TIMEOUT >= 900


# ------------------------------------------------------------------ #
# kill_process_tree
# ------------------------------------------------------------------ #

class TestKillProcessTree:
    def test_it_kills_a_child_the_tool_started(self):
        """The reason the group is signalled rather than the leader: a shell
        that forks leaves the grandchild holding the output pipe, and then the
        reader thread blocks on a read that never returns."""
        proc = subprocess.Popen(  # noqa: S603
            ["/bin/sh", "-c", "sleep 30"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        assert kill_process_tree(proc) is True
        proc.wait(timeout=5)
        # The pipe is closed because everything holding it is gone.
        started = time.monotonic()
        assert proc.stdout.read() == b""
        assert time.monotonic() - started < 5
        proc.stdout.close()

    def test_an_already_dead_process_is_not_an_error(self):
        proc = subprocess.Popen(["/bin/true"], start_new_session=True)  # noqa: S603
        proc.wait()
        # Either path is fine; what matters is that it does not raise.
        assert kill_process_tree(proc) in (True, False)

    def test_none_is_handled(self):
        assert kill_process_tree(None) is False
