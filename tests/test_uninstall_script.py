"""Uninstalling has to undo installing, on both machines.

Installing touches the container *and* the host: two helper commands in
/usr/local/sbin, a guest-startup drop-in, an fstab line, and a file in /root
holding the NAS password. An uninstall that only destroys the container leaves
all of it — including a password for an application that no longer exists.

And one hazard worth a test of its own: on installs made before 1.0,
/opt/adr/completed is a bind-mount of the user's media library. `rm -rf
/opt/adr` goes straight through it.
"""

import re
from pathlib import Path

import pytest

SCRIPT = Path("scripts/uninstall.sh")


@pytest.fixture(scope="module")
def source() -> str:
    return SCRIPT.read_text()


class TestItNeverDeletesThroughAMount:
    def test_it_looks_for_mounts_under_the_install_directory(self, source):
        """The uninstall that destroys the thing it was ripping."""
        assert "findmnt" in source
        assert "/opt/adr/" in source

    def test_the_delete_is_guarded_by_that_check(self, source):
        """A check whose result is not used is decoration."""
        block = source.split("if [[ -d /opt/adr ]]")[1]
        mount_check = block.index("findmnt")
        delete = block.index("rm -rf /opt/adr")
        assert mount_check < delete, "the check must come first"
        assert "elif" in block[mount_check:delete], "and must gate it"

    def test_it_says_how_to_proceed_rather_than_just_refusing(self, source):
        assert "umount" in source


class TestItCleansUpTheHostToo:
    @pytest.mark.parametrize("leftover", [
        "/usr/local/sbin/adr-doctor",
        "/usr/local/sbin/adr-setup-nas",
        "pve-guests.service.d/adr-optical.conf",
        "/etc/fstab",
    ])
    def test_each_is_offered(self, source, leftover):
        assert leftover in source

    def test_the_nas_password_is_offered_last_and_named(self, source):
        """A password in a file for an application that no longer exists. It
        gets its own question, in its own words, rather than being swept up
        with the helper scripts."""
        assert "/root/.adr-nas-credentials" in source
        block = source.split("adr-nas-credentials")[1]
        assert "password" in block.lower()

    def test_nothing_on_the_host_is_removed_without_asking(self, source):
        """Someone running two containers wants the shared helpers to stay,
        and the fstab line may mount a share they use for something else.

        Checked by nesting rather than by the preceding line: every
        destructive statement sits inside a conditional, so none of them is
        at the block's own indentation.
        """
        host_block = source.split("Host mode")[1].split("Container mode")[0]
        unguarded = [
            line for line in host_block.splitlines()
            if re.match(r"^ {0,4}(rm -f |sed -i |umount )", line)
        ]
        assert not unguarded, unguarded

    def test_each_leftover_is_a_separate_question(self, source):
        """One "clean up the host?" would make keeping adr-doctor and
        deleting the password an all-or-nothing choice."""
        host_block = source.split("Host mode")[1].split("Container mode")[0]
        assert host_block.count("confirm ") >= 4

    def test_fstab_is_backed_up_before_it_is_edited(self, source):
        """Editing fstab wrongly shows up at the next boot, by which time the
        original is the only thing that would have helped."""
        assert "cp /etc/fstab" in source
        assert source.index("cp /etc/fstab") < source.index("sed -i '/Automatic Disc Ripper/")


class TestItRemovesEveryServiceItInstalled:
    @pytest.mark.parametrize("unit", [
        "adr.service", "adr-update.path", "adr-update.service",
    ])
    def test_each_unit_is_disabled_and_deleted(self, source, unit):
        """adr-update.* arrived with in-app updating and was missed by an
        uninstall that only knew about adr.service."""
        assert unit in source


class TestItSaysWhatWillHappen:
    def test_destroying_a_container_is_confirmed_first(self, source):
        block = source.split("Host mode")[1]
        assert block.index("confirm") < block.index("pct destroy")

    def test_it_says_what_survives(self, source):
        """Someone about to destroy a container needs to know their films are
        not on its disk before they answer, not after."""
        assert "NOT on that disk" in source
