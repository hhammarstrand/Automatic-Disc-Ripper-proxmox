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

    def _after_the_drivers(self, code: str, lines: int = 25) -> str:
        """The window of code that decides which VA-API driver gets installed.

        Crude on purpose. The three scripts express the same choice three
        ways — a bash array, a plain word list, and a quoted `sh -c` body —
        and a parser clever enough for all three would be a third thing to
        keep correct.
        """
        rows = code.splitlines()
        # The *last* mention, not the first: adr-doctor names the packages in
        # a variable well above the loop that installs them.
        first = max(
            i for i, row in enumerate(rows)
            if "intel-media-va-driver-non-free" in row
            or 'in $1' in row or "gpu_driver_choices" in row
        )
        return "\n".join(rows[first:first + lines])

    def test_the_driver_choice_stops_at_the_first_that_installs(self):
        """Without a break the second install removes the first — and the
        second is the stripped-down build."""
        for path in self.SCRIPTS:
            window = self._after_the_drivers(self._code(path))
            assert "break" in window, f"{path}: the driver choice runs to the end"

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
