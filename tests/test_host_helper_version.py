"""adr-doctor lives on the host and is a copy, so it goes stale silently.

It is pulled out of the container at install time. The in-container updater
cannot write to the host, so updating the application does not update it — and
an old copy skips every check added since it was taken, then prints "nothing
wrong found". That is worse than failing: it is a clean bill of health from a
script that never looked.

The stamp it compares against is only useful if it is kept current, which is
what this file is for.
"""

import re
from pathlib import Path

import pytest

from adr import __version__

DOCTOR = Path("scripts/adr-doctor.sh")


def _stamp() -> str:
    match = re.search(r'^ADR_DOCTOR_VERSION="([^"]+)"', DOCTOR.read_text(), re.M)
    assert match, "adr-doctor has no version stamp"
    return match.group(1)


def test_the_stamp_matches_the_application():
    """A stamp that drifts makes the warning fire on a current copy, and
    people stop reading warnings that cry wolf."""
    assert _stamp() == __version__, (
        f"scripts/adr-doctor.sh says {_stamp()}, adr/__init__.py says "
        f"{__version__}. Update the stamp when releasing."
    )


def test_it_compares_against_the_container():
    text = DOCTOR.read_text()
    assert "CT_VERSION" in text
    assert "adr.__version__" in text


def test_it_prints_the_command_to_refresh_itself():
    """A warning nobody can act on is just noise."""
    text = DOCTOR.read_text()
    assert "pct pull" in text
    assert "/usr/local/sbin/adr-doctor" in text


def test_a_container_that_will_not_answer_does_not_block_the_run():
    """The check runs before anything else; a stopped container or a broken
    venv must not stop the diagnosis that would explain it."""
    text = DOCTOR.read_text()
    assert 'CT_VERSION=""' in text, "the version probe must tolerate failure"


@pytest.mark.parametrize("marker", [
    "3b. The GPU",
    "renderD128",
    "lxc.mount.entry: /dev/dri",
])
def test_the_gpu_section_is_present(marker):
    """The section whose absence from a stale copy started all this."""
    assert marker in DOCTOR.read_text()
