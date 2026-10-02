"""adr-hookscript.sh: the container does not start before its share is mounted.

A bind-mount is captured when the container starts. Started before the NAS was
mounted, the container holds the bare directory underneath it until somebody
restarts it, and every disc is refused in the meantime.
"""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path("scripts/adr-hookscript.sh")
DOCTOR = Path("scripts/adr-doctor.sh")

FSTAB = """\
# a comment
UUID=abc  /  ext4  defaults  0  1
//nas/media  /mnt/adr-media  cifs  nofail  0  0
proc  /proc  proc  defaults  0  0
"""


@pytest.fixture
def host(tmp_path):
    (tmp_path / "fstab").write_text(FSTAB)

    def run(ctid, phase, conf):
        (tmp_path / f"{ctid}.conf").write_text(conf)
        env = dict(
            os.environ,
            ADR_LXC_CONF_DIR=str(tmp_path),
            ADR_FSTAB=str(tmp_path / "fstab"),
            ADR_SHARE_WAIT="0",
        )
        return subprocess.run(
            ["bash", str(HOOK), str(ctid), phase],
            env=env, capture_output=True, text=True, timeout=30,
        )
    return run


def test_an_unmounted_share_refuses_the_start(host):
    result = host(108, "pre-start", "mp0: /mnt/adr-media,mp=/mnt/media\n")
    assert result.returncode == 1
    assert "/mnt/adr-media" in result.stderr
    assert "pct start 108" in result.stderr


def test_a_folder_inside_the_share_waits_for_the_share(host):
    result = host(108, "pre-start", "mp0: /mnt/adr-media/films,mp=/mnt/media\n")
    assert result.returncode == 1
    assert "/mnt/adr-media " in result.stderr


def test_a_mounted_source_starts(host):
    """/proc is in the fstab and is mounted on any Linux machine."""
    assert host(108, "pre-start", "mp0: /proc/sys,mp=/mnt/media\n").returncode == 0


def test_a_plain_host_directory_is_not_waited_for(host):
    """"/" is in every fstab and covers every path; it must not count."""
    assert host(108, "pre-start", "mp0: /srv/films,mp=/mnt/media\n").returncode == 0


def test_only_pre_start_does_anything(host):
    conf = "mp0: /mnt/adr-media,mp=/mnt/media\n"
    for phase in ("post-start", "pre-stop", "post-stop"):
        assert host(108, phase, conf).returncode == 0


def test_snapshots_are_not_this_start(host):
    conf = "rootfs: local-lvm:vm-108-disk-0\n[before-update]\nmp0: /mnt/adr-media,mp=/mnt/media\n"
    assert host(108, "pre-start", conf).returncode == 0


def test_the_updater_ships_it_executable():
    assert os.access(HOOK, os.X_OK)


class TestTheDoctorInstallsIt:
    def test_it_pulls_the_hookscript_out_of_the_container(self):
        text = DOCTOR.read_text()
        assert "/opt/adr/scripts/${HOOK_NAME}" in text
        assert 'HOOK_NAME="adr-hookscript.sh"' in text
        assert "--hookscript" in text

    def test_it_leaves_somebody_elses_hookscript_alone(self):
        assert "its own hookscript" in DOCTOR.read_text()

    def test_it_restarts_a_container_holding_the_directory_underneath(self):
        text = DOCTOR.read_text()
        section = text[text.index("# 4b."):text.index("# 5. Folder layout")]
        assert "NEEDS_RESTART=1" in section
        assert 'mount "$MEDIA_FSTAB"' in section
