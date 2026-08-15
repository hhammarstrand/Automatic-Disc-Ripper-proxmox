"""What the installer promises a new machine.

The installer is the only part of this application most people run exactly
once, which is precisely why it drifts: a capability gets added to the Doctor
or to adr-doctor, and the fresh-install path never learns about it. That
happened with hardware encoding — a machine with perfectly good Intel graphics
finished the installer with software encoding and no hint that anything else
was available, because the passthrough lived only in a repair tool nobody had
been told to run.

These read the script rather than run it: it needs a Proxmox host, and the
things worth pinning are what it *reaches for*, not the shell mechanics.
"""

import re
from pathlib import Path

import pytest

INSTALL = Path("scripts/install.sh")
DOCTOR = Path("scripts/adr-doctor.sh")


@pytest.fixture(scope="module")
def install() -> str:
    return INSTALL.read_text()


class TestOpticalPassthrough:
    def test_the_block_major_is_allowed(self, install):
        """/dev/sr* are block devices. A 'c' rule alone leaves the node
        visible and every open() denied, which reads as a broken drive."""
        assert "lxc.cgroup2.devices.allow: b 11:* rwm" in install

    def test_the_generic_scsi_node_comes_along(self, install):
        """MakeMKV talks to the drive through SG_IO."""
        assert "c 21:* rwm" in install

    def test_it_never_binds_every_sg_node(self, install):
        """Those also address the system's SATA disks, and handing raw SG_IO
        on the boot disk to a privileged container is dangerous. Checked
        against the code rather than the file, since the comment right above
        it says "/dev/sg*" precisely to explain why it must not."""
        code = "\n".join(
            line for line in install.splitlines() if not line.lstrip().startswith("#"))
        assert "scsi_generic" in code, "the sg node is resolved per drive"
        assert "lxc.mount.entry: /dev/sg*" not in code

    def test_guest_autostart_waits_for_the_drive(self, install):
        """'optional' skips a device that does not exist yet, silently, and a
        node cannot be bind-mounted into a running container. Without the
        ordering, passthrough works after installing and breaks on reboot."""
        assert "pve-guests.service.d" in install
        assert "systemd-escape" in install


class TestItFinishesWhatTheHostCanDo:
    def test_it_runs_the_repair_tool(self, install):
        """adr-doctor knows about the GPU, the render node's group and the
        driver stack. None of it was happening on a fresh install."""
        assert re.search(r"adr-doctor --fix --yes", install)

    def test_the_repair_tool_is_installed_before_it_is_used(self, install):
        assert install.index("/usr/local/sbin/adr-doctor\n") < install.index(
            "adr-doctor --fix --yes")

    def test_a_failure_there_does_not_fail_the_install(self, install):
        """The summary below it is the only copy of the generated root
        password, and a GPU that will not pass through is no reason to lose
        an install that otherwise worked."""
        section = install.split("Everything the host can still do")[1]
        assert "msg_warn" in section.split("# Done")[0]

    def test_it_waits_for_the_container_before_printing_the_summary(self, install):
        """--yes lets the repair restart the container, and a container three
        seconds into booting has no IP address yet. The install would end by
        printing "<container-ip>" for a machine that is perfectly fine."""
        section = install.split("Everything the host can still do")[1].split("# Done")[0]
        assert "hostname -I" in section

    def test_the_gpu_work_is_not_duplicated_here(self, install):
        """Two implementations of "pass the GPU through" would drift. The one
        in adr-doctor is the tested one and knows how to do nothing when
        there is nothing to do."""
        assert "226" not in install, "the DRM major belongs in adr-doctor"
        assert "/dev/dri" not in install


class TestTheSummaryTellsTheTruth:
    def test_it_prints_where_things_live(self, install):
        for path in ("/opt/adr/raw", "/opt/adr/staging"):
            assert path in install

    def test_it_names_the_repair_command_for_later(self, install):
        """The drive classically stops being seen after a host reboot, and
        the fix is not guessable."""
        assert "adr-doctor --fix ${CT_ID}" in install

    def test_the_password_is_printed_after_the_trap_is_cleared(self, install):
        """An error while merely looking up the IP must not abort before the
        user's only copy of the generated password has been shown."""
        assert install.index("trap - EXIT") < install.index("Root pw:")


class TestTheDocumentationMatchesTheScript:
    """A README describing a capability the installer does not have is worse
    than one that omits it: someone reads it, does not check, and concludes
    the thing is broken."""

    @pytest.fixture
    def readme(self):
        return Path("README.md").read_text()

    def test_the_installer_steps_mention_the_repair_run(self, readme, install):
        assert "adr-doctor --fix" in readme
        assert "adr-doctor --fix --yes" in install

    def test_the_boot_ordering_is_documented(self, readme):
        """It is the fix for "the drive worked until I rebooted", which is the
        single most confusing failure this project has."""
        assert "guest autostart" in readme.lower()

    def test_nothing_promises_a_test_of_the_preset_alone(self, readme):
        """The encoder test covers whichever encoder is configured, and the
        GPU path has no preset."""
        assert "Test the preset" not in readme


# ------------------------------------------------------------------ #
# The GPU's userspace half reaches every path that can install it
# ------------------------------------------------------------------ #

CONTAINER = Path("scripts/install-container.sh")
UPDATE = Path("scripts/update.sh")


@pytest.fixture(scope="module")
def container() -> str:
    return CONTAINER.read_text()


@pytest.fixture(scope="module")
def update() -> str:
    return UPDATE.read_text()


class TestTheMediaStackIsInstalledEverywhere:
    """Ubuntu's handbrake-cli *is* built with Quick Sync — the source package
    build-depends on libvpl-dev. What it ships is the dispatcher, and a
    dispatcher with no runtime fails exactly like a machine with no GPU. The
    installer put in HandBrake and nothing for the GPU at all, so that state
    was the default on every fresh container.
    """

    #: Both, because they cover different silicon: libmfx1 is Gen 9 to Gen 11
    #: (Skylake through Comet Lake), libmfxgen1 is Alder Lake and later.
    #: Either alone is refused by half the hardware in the world.
    RUNTIMES = ("libmfx1", "libmfxgen1")

    def test_a_fresh_container_gets_both_quick_sync_runtimes(self, container):
        for package in self.RUNTIMES:
            assert package in container, f"{package} is not installed at first install"

    def test_a_fresh_container_gets_the_intel_driver(self, container):
        assert "intel-media-va-driver" in container

    def test_an_existing_container_gets_them_on_update(self, update):
        """An install made before this existed has no host to run adr-doctor
        from when the person updating is holding a phone."""
        for package in self.RUNTIMES:
            assert package in update, f"{package} never reaches an existing install"

    def test_the_update_asks_per_package_not_whether_a_runtime_exists(self, update):
        """Gating on gpu.runtime_state() was a bug, and shipped as one.

        runtime_state() answers "is a Quick Sync runtime installed", which is
        true the moment *either* is — so a container carrying libmfxgen1 from
        an earlier repair, on a processor that needs libmfx1, reported the
        stack fine and skipped the very install that would have fixed it. The
        question is per package, and dpkg-query answers it without opinion.
        """
        assert "dpkg-query" in update
        # Comments stripped: this file explains the bug at length, and the
        # explanation naming the function is not the function being called.
        code = "\n".join(
            line for line in update.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "runtime_state()" not in code, (
            "the install is gated on 'a runtime exists' again"
        )

    def test_an_already_installed_package_is_not_reinstalled(self, update):
        assert "install ok installed" in update

    def test_the_update_does_not_run_apt_without_a_gpu(self, update):
        assert "/dev/dri/renderD" in update

    def test_amd_is_not_given_intels_media_stack(self, update):
        assert "mesa-va-drivers" in update
        assert "0x1002" in update


class TestTheDriverIsAChoiceNotAList:
    """intel-media-va-driver-non-free and intel-media-va-driver Conflict with
    and Replace one another.

    Listing both and installing whatever dpkg reported missing left the
    *worse* one: the non-free driver was already present, the free one counted
    as absent, apt swapped them, and a routine update took HEVC encode,
    MPEG-2, VP8 and Quick Sync off a container where HandBrake had just
    started using the GPU. vainfo went from nine encode profiles to four.
    """

    SCRIPTS = ("scripts/update.sh", "scripts/install-container.sh",
               "scripts/adr-doctor.sh")

    def _code(self, path: str) -> str:
        """The script with its comments stripped — they name the packages at
        length, and a comment is not an install."""
        return "\n".join(
            line for line in Path(path).read_text().splitlines()
            if not line.lstrip().startswith("#")
        )

    #: Where each script decides which VA-API driver to install, and how far
    #: that decision reaches. Spelled out per file rather than found by a
    #: parser: the three express the same choice three ways — a bash array, a
    #: plain word list, and a quoted `sh -c` body — and a parser clever enough
    #: for all three would be a fourth thing to keep correct.
    DRIVER_LOOP = {
        "scripts/update.sh": ('for pkg in "${gpu_driver_choices[@]}"', "done"),
        "scripts/install-container.sh": (
            "for pkg in intel-media-va-driver-non-free", "done"),
        "scripts/adr-doctor.sh": ("for pkg in $1", "done"),
    }

    def _driver_loop(self, path: str) -> str:
        code = self._code(path)
        opener, closer = self.DRIVER_LOOP[path]
        start = code.index(opener)
        end = code.index(closer, start)
        return code[start:end]

    def test_the_driver_choice_stops_at_the_first_that_installs(self):
        """Without a break the second install removes the first — and the
        second is the stripped-down build."""
        for path in self.SCRIPTS:
            assert "break" in self._driver_loop(path), (
                f"{path}: the driver choice runs to the end"
            )

    def test_the_runtimes_are_not_inside_that_loop(self):
        """They do not conflict, so stopping at the first would install one of
        the two Quick Sync runtimes and skip the other."""
        for path in self.SCRIPTS:
            assert "libmfx1" not in self._driver_loop(path), (
                f"{path}: a runtime is inside the driver choice"
            )

    def test_i965_is_the_last_choice_not_the_first(self):
        """It is for pre-Broadwell hardware. Beside iHD it only gives libva
        two drivers to choose between for the same chip."""
        for path in self.SCRIPTS:
            code = self._code(path)
            if "i965-va-driver" not in code:
                continue
            assert code.index("intel-media-va-driver-non-free") < code.index("i965-va-driver")

    def test_the_runtimes_are_still_a_list(self):
        """libmfx1 and libmfxgen1 cover different silicon and do not conflict;
        installing only the first would be the opposite mistake."""
        for path in self.SCRIPTS:
            code = self._code(path)
            assert "libmfx1" in code and "libmfxgen1" in code, path


class TestTheServiceUserCannotBecomeRoot:
    """Root-owning scripts/ and systemd/ was necessary and not sufficient.

    /opt/adr itself is owned by the service user, and a directory's owner may
    rename or replace the entries inside it however those entries are owned.
    So the service user could move scripts/ aside, drop its own update.sh, ask
    for an update — POST /api/update/start does exactly that — and systemd
    would execute the new bytes as uid 0. The web UI is unauthenticated by
    design, and the settings page lets anyone point handbrake_path at another
    binary, so "code execution as the service user" is not a high bar.

    The fix is that nothing systemd runs as root comes from under /opt/adr.
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def update() -> str:
        return Path("scripts/update.sh").read_text()

    @staticmethod
    @pytest.fixture(scope="class")
    def unit() -> str:
        return Path("systemd/adr-update.service").read_text()

    def test_the_unit_does_not_execute_from_the_writable_directory(self, unit):
        assert "ExecStart=/opt/adr/scripts/update.sh" not in unit
        assert "ExecStart=/usr/local/lib/adr/update.sh" in unit

    def test_the_update_log_is_not_written_into_the_writable_directory(self, unit):
        """systemd opens it as root, so a symlink planted there would have
        root truncate whatever it points at."""
        assert "/opt/adr/update.log" not in unit
        assert "StandardOutput=truncate:/var/log/adr/update.log" in unit

    @pytest.mark.parametrize("script", ["scripts/install-container.sh", "scripts/update.sh"])
    def test_the_root_only_copy_is_installed(self, script):
        text = Path(script).read_text()
        assert "/usr/local/lib/adr" in text, (
            f"{script} never installs the copy systemd executes"
        )
        assert "install -d -o root -g root -m 0755 /usr/local/lib/adr" in text

    def test_the_update_refreshes_that_copy_from_fetched_source(self, update):
        """Not from $INSTALL_DIR/scripts, which is the copy an attacker can
        replace — refreshing from there would launder the replacement into the
        root-only location on the next update."""
        assert 'install -o root -g root -m 0755 "$TMP/src/scripts/update.sh"' in update
        assert '"$INSTALL_DIR/scripts/update.sh" \\\n    /usr/local/lib' not in update

    def test_the_units_are_installed_from_fetched_source(self, update):
        assert 'src="$TMP/src/systemd/$unit"' in update

    def test_the_commit_file_is_not_chowned_through_a_symlink(self, update):
        """chown follows symlinks without -h, so chowning a path the service
        user controls hands it ownership of whatever that path points at."""
        assert 'chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR/.commit"' not in update
        assert 'rm -f "$INSTALL_DIR/.commit"' in update


class TestTheUpdateMechanismSurvivesItsOwnMigration:
    """1.31 moved ExecStart to /usr/local/lib/adr/update.sh and had update.sh
    create it — but the update.sh that *performs* that upgrade is the previous
    version, which knows nothing about the path. So updating from 1.30 or
    earlier installed this unit with its ExecStart absent, and in-app updates
    stopped working entirely:

        adr-update.service: Command /usr/local/lib/adr/update.sh is not
        executable: No such file or directory

    which is the worst possible thing to break, because it is the mechanism
    that delivers every other fix. A unit that systemd cannot start also
    leaves the Doctor reporting "adr-update.path is not running, so an update
    request would go unnoticed".
    """

    @staticmethod
    @pytest.fixture(scope="class")
    def unit() -> str:
        return Path("systemd/adr-update.service").read_text()

    def test_it_seeds_its_own_executable_when_missing(self, unit):
        assert "test -e /usr/local/lib/adr/update.sh ||" in unit, (
            "a machine whose unit arrived before the file has no way back"
        )
        assert "install -D -o root -g root -m 0755" in unit

    def test_the_seed_runs_before_the_thing_it_seeds(self, unit):
        """ExecStartPre, not ExecStartPost, and above ExecStart in the file."""
        seed = unit.index("ExecStartPre=-/bin/sh -c 'test -e")
        start = unit.index("ExecStart=/usr/local/lib/adr/update.sh")
        assert seed < start, "the seed runs after the thing it seeds"

    def test_the_seed_only_fires_when_the_file_is_absent(self, unit):
        """On a migrated machine /usr/local/lib/adr is root-owned and the
        service user cannot remove what is in it, so this branch is dead —
        which is what keeps the seed from reopening the hole the move
        closed."""
        assert "||" in unit
        assert "test -e" in unit

    def test_systemd_owns_the_log_directory(self, unit):
        """StandardOutput pointed at a directory nothing created, so the
        service failed before it ran. LogsDirectory makes systemd create it,
        and takes the job away from every installer at once."""
        assert "LogsDirectory=adr" in unit
        assert "StandardOutput=truncate:/var/log/adr/update.log" in unit

    def test_the_installers_still_create_both_for_a_fresh_machine(self):
        for script in ("scripts/install-container.sh", "scripts/update.sh"):
            text = Path(script).read_text()
            assert "/usr/local/lib/adr" in text, script

    def test_the_watch_unit_is_enabled_even_when_no_unit_changed(self):
        """Running the script from the host is what someone does *because* the
        Doctor said the path unit is not running — and that can be true with
        the unit files already byte-identical. Gating the enable on a file
        change meant the repair did nothing in exactly that case."""
        text = Path("scripts/update.sh").read_text()
        gated = text.index('if [[ "$units_changed" -eq 1 ]]; then')
        enable = text.index("systemctl enable --now adr-update.path")
        closing = text.index("fi", gated)
        assert enable > closing, "the enable is still inside the changed-only branch"
