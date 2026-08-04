"""Can this container use hardware video encoding?

A preset exported from HandBrake on a desktop asks for that desktop's encoder.
Inside an LXC it does not exist unless the GPU was passed through, and
HandBrake fails identically on every title: "encqsvInit: qsv is not available
on the system", exit 3, forty minutes after the disc went in.

The distinction that matters is between "no GPU here" — which is one host-side
config line away from fixed — and "wrong preset". Telling someone to give up
the hardware they deliberately chose is the wrong answer to the first.
"""

import errno
import json
from pathlib import Path

import pytest

from adr import gpu


def _preset_file(tmp_path, name, encoder):
    path = tmp_path / "preset.json"
    path.write_text(json.dumps({
        "PresetList": [{"PresetName": name, "VideoEncoder": encoder}],
    }))
    return str(path)


class TestReadingThePreset:
    def test_a_hardware_encoder_is_found(self, tmp_path):
        path = _preset_file(tmp_path, "Super HQ", "qsv_h264")
        assert gpu.preset_wants_hardware(path, "Super HQ") == "qsv_h264"

    @pytest.mark.parametrize("encoder", ["qsv_h265", "nvenc_h264", "vce_h265", "vaapi_h264"])
    def test_every_hardware_family_is_recognised(self, tmp_path, encoder):
        path = _preset_file(tmp_path, "P", encoder)
        assert gpu.preset_wants_hardware(path, "P") == encoder

    def test_a_software_encoder_is_not_hardware(self, tmp_path):
        path = _preset_file(tmp_path, "P", "x264")
        assert gpu.preset_wants_hardware(path, "P") == ""

    def test_the_name_alone_says_nothing(self, tmp_path):
        """'Super HQ 1080p30 Surround' does not mention an encoder, and the
        encoder is the whole question — so the file has to be read."""
        path = _preset_file(tmp_path, "Super HQ 1080p30 Surround", "x265")
        assert gpu.preset_wants_hardware(path, "Super HQ 1080p30 Surround") == ""

    def test_only_the_named_preset_is_considered(self, tmp_path):
        path = tmp_path / "many.json"
        path.write_text(json.dumps({"PresetList": [
            {"PresetName": "Mine", "VideoEncoder": "x264"},
            {"PresetName": "Other", "VideoEncoder": "qsv_h264"},
        ]}))
        assert gpu.preset_wants_hardware(str(path), "Mine") == ""

    def test_a_nested_preset_is_found(self, tmp_path):
        path = tmp_path / "folders.json"
        path.write_text(json.dumps({"PresetList": [
            {"Folder": True, "ChildrenArray": [
                {"PresetName": "Deep", "VideoEncoder": "nvenc_h265"},
            ]},
        ]}))
        assert gpu.preset_wants_hardware(str(path), "Deep") == "nvenc_h265"

    def test_a_missing_file_says_nothing_rather_than_raising(self, tmp_path):
        assert gpu.preset_wants_hardware(str(tmp_path / "gone.json"), "P") == ""

    def test_broken_json_says_nothing(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert gpu.preset_wants_hardware(str(path), "P") == ""

    def test_no_file_at_all(self):
        assert gpu.preset_wants_hardware("", "P") == ""


class TestReadingHandBrakesComplaint:
    @pytest.mark.parametrize("said", [
        "ERROR: encqsvInit: qsv is not available on the system",
        "Failure to initialise thread 'Quick Sync Video encoder (Intel Media SDK)'",
        "Unknown video encoder nvenc_h265",
        "vaapi: no device found",
    ])
    def test_hardware_failures_are_recognised(self, said):
        assert gpu.mentions_hardware(said) is True

    def test_an_unrelated_failure_is_not(self):
        assert gpu.mentions_hardware("No space left on device") is False

    def test_nothing_at_all(self):
        assert gpu.mentions_hardware("") is False
        assert gpu.mentions_hardware(None) is False


@pytest.fixture(autouse=True)
def _isolate_the_system(monkeypatch, tmp_path_factory):
    """Never let a test read the machine it happens to run on.

    The driver and library directories are real system paths, so without this
    a test asking "what happens when no driver is installed?" passes on a bare
    CI box and fails on a developer laptop with Mesa. Every test starts from
    empty and says so explicitly when it wants something installed.
    """
    empty = tmp_path_factory.mktemp("nothing-installed")
    monkeypatch.setattr(gpu, "VA_DRIVER_DIRS", (empty,))
    monkeypatch.setattr(gpu, "LIB_DIRS", (empty,))
    # An empty /sys means gpu_vendor() finds nothing, which is the neutral
    # starting point — patched here rather than the function itself, so the
    # tests of gpu_vendor() exercise the real one.
    monkeypatch.setattr(gpu, "DRM_CLASS_DIR", empty)


def _with_driver(monkeypatch, tmp_path, name="iHD_drv_video.so"):
    """A driver directory holding one VA-API driver."""
    directory = tmp_path / "dri"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text("")
    monkeypatch.setattr(gpu, "VA_DRIVER_DIRS", (directory,))
    return directory


def _with_libs(monkeypatch, tmp_path, *names):
    """A library directory holding the Quick Sync runtime."""
    directory = tmp_path / "lib"
    directory.mkdir(exist_ok=True)
    for name in names or ("libmfx-gen.so.1.2",):
        (directory / name).write_text("")
    monkeypatch.setattr(gpu, "LIB_DIRS", (directory,))
    return directory


def _intel_stack(monkeypatch, tmp_path):
    """An Intel GPU with everything it needs."""
    monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
    _with_driver(monkeypatch, tmp_path, "iHD_drv_video.so")
    _with_libs(monkeypatch, tmp_path)


class TestTheDriverOnTopOfTheNode:
    """Passing the render node through is only half the job.

    /dev/dri/renderD128 present and openable, and HandBrake still says
    "qsv is not available on the system" — because Quick Sync reaches the
    hardware through a VA-API driver, and a minimal container ships none.
    """

    def test_no_driver_installed_is_not_ok(self):
        state = gpu.runtime_state()
        assert state["ok"] is False
        assert state["drivers"] == []
        assert "adr-doctor --fix" in state["fix"]

    def test_the_intel_stack_counts(self, monkeypatch, tmp_path):
        _intel_stack(monkeypatch, tmp_path)
        state = gpu.runtime_state()
        assert state["ok"] is True
        assert state["drivers"] == ["iHD_drv_video.so"]
        assert state["fix"] == ""

    def test_the_older_intel_driver_counts_too(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "i965_drv_video.so")
        _with_libs(monkeypatch, tmp_path, "libmfxhw64.so.1")
        assert gpu.runtime_state()["ok"] is True

    def test_an_amd_driver_counts(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_AMD)
        _with_driver(monkeypatch, tmp_path, "radeonsi_drv_video.so")
        assert gpu.runtime_state()["ok"] is True

    def test_amd_is_not_asked_for_quick_syncs_library(self, monkeypatch, tmp_path):
        """libmfx is Quick Sync's alone. Demanding it on an AMD box would
        invent a problem and then fail to install a fix for it."""
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_AMD)
        _with_driver(monkeypatch, tmp_path, "radeonsi_drv_video.so")
        assert gpu.runtime_state()["libs"] == []
        assert gpu.runtime_state()["ok"] is True


class TestTheDriverHasToBeForThisGPU:
    """The false green one level up.

    Asking only "is any VA driver installed?" passes on a container that has
    Mesa and an Intel chip — a combination that cannot encode a single frame,
    and which then gets told the encoder is missing from its HandBrake build.
    """

    def test_somebody_elses_driver_does_not_count(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "radeonsi_drv_video.so")
        _with_libs(monkeypatch, tmp_path)
        state = gpu.runtime_state()
        assert state["ok"] is False
        assert "iHD_drv_video.so" in state["detail"]

    def test_it_says_what_is_installed_instead(self, monkeypatch, tmp_path):
        """Otherwise "no driver installed" contradicts the ls they will run
        to check, and the message loses its credibility."""
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "radeonsi_drv_video.so")
        _with_libs(monkeypatch, tmp_path)
        assert "radeonsi_drv_video.so" in gpu.runtime_state()["detail"]

    def test_the_intel_driver_without_quick_syncs_runtime_is_not_enough(
        self, monkeypatch, tmp_path,
    ):
        """The exact state that produced 'encqsvInit: qsv is not available'
        on a container whose render node was passed through correctly."""
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "iHD_drv_video.so")
        state = gpu.runtime_state()
        assert state["ok"] is False
        assert "libmfx" in state["detail"]
        assert "adr-doctor --fix" in state["fix"]

    def test_an_unknown_vendor_accepts_any_driver(self, monkeypatch, tmp_path):
        """A confident wrong answer about hardware this code has never heard
        of is worse than a guess."""
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: "0xdead")
        _with_driver(monkeypatch, tmp_path, "something_drv_video.so")
        assert gpu.runtime_state()["ok"] is True

    def test_the_vendor_is_read_from_the_node(self, monkeypatch, tmp_path):
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        vendor = tmp_path / "sys" / "renderD128" / "device"
        vendor.mkdir(parents=True)
        (vendor / "vendor").write_text("0x8086\n")
        monkeypatch.setattr(gpu, "DRM_CLASS_DIR", tmp_path / "sys")
        assert gpu.gpu_vendor() == gpu.VENDOR_INTEL

    def test_a_vendor_that_cannot_be_read_is_empty(self, monkeypatch, tmp_path):
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        monkeypatch.setattr(gpu, "DRM_CLASS_DIR", tmp_path / "absent")
        assert gpu.gpu_vendor() == ""

    @pytest.mark.parametrize("lib", ["libmfxhw64.so.1", "libmfx-gen.so.1.2"])
    def test_every_shape_of_the_runtime_is_recognised(
        self, monkeypatch, tmp_path, lib,
    ):
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "iHD_drv_video.so")
        _with_libs(monkeypatch, tmp_path, lib)
        assert gpu.runtime_state()["ok"] is True

    def test_an_unrelated_library_is_not_the_runtime(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "iHD_drv_video.so")
        _with_libs(monkeypatch, tmp_path, "libavcodec.so.60")
        assert gpu.runtime_state()["ok"] is False


class TestTheDispatcherIsNotTheRuntime:
    """The most confusing shape of a broken Quick Sync stack.

    libvpl.so.2 sits there in the library directory, so `ls` says the stack is
    installed. It is a loader with nothing to load: it finds a runtime and
    hands over, and encodes nothing itself. HandBrake then fails exactly as it
    would with neither installed.
    """

    def _intel_with(self, monkeypatch, tmp_path, *libs):
        monkeypatch.setattr(gpu, "gpu_vendor", lambda: gpu.VENDOR_INTEL)
        _with_driver(monkeypatch, tmp_path, "iHD_drv_video.so")
        _with_libs(monkeypatch, tmp_path, *libs)

    def test_the_dispatcher_alone_is_not_enough(self, monkeypatch, tmp_path):
        self._intel_with(monkeypatch, tmp_path, "libvpl.so.2", "libvpl.so.2.9")
        state = gpu.runtime_state()
        assert state["ok"] is False
        assert state["libs"] == [], "no runtime is installed"
        assert state["dispatchers"] == ["libvpl.so.2", "libvpl.so.2.9"]

    def test_it_explains_why_the_library_it_can_see_does_not_count(
        self, monkeypatch, tmp_path,
    ):
        """Otherwise the message contradicts the listing in front of them."""
        self._intel_with(monkeypatch, tmp_path, "libvpl.so.2")
        detail = gpu.runtime_state()["detail"]
        assert "libvpl.so.2" in detail
        assert "dispatcher" in detail

    def test_the_old_msdk_dispatcher_is_also_only_a_dispatcher(
        self, monkeypatch, tmp_path,
    ):
        self._intel_with(monkeypatch, tmp_path, "libmfx.so.1")
        assert gpu.runtime_state()["ok"] is False

    def test_a_dispatcher_with_a_runtime_is_fine(self, monkeypatch, tmp_path):
        self._intel_with(monkeypatch, tmp_path, "libvpl.so.2", "libmfx-gen.so.1.2")
        assert gpu.runtime_state()["ok"] is True


class TestAskingTheStackWhetherItWorks:
    """Everything else reasons from file names. vainfo opens the device."""

    def _vainfo(self, monkeypatch, returncode=0, stdout="", stderr=""):
        import shutil
        import subprocess
        import types

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vainfo")
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: types.SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr=stderr),
        )

    def test_without_vainfo_it_says_so_rather_than_guessing(self, monkeypatch):
        import shutil

        monkeypatch.setattr(shutil, "which", lambda name: None)
        probe = gpu.vainfo()
        assert probe["ran"] is False
        assert probe["ok"] is False
        assert "not installed" in probe["output"]

    def test_a_working_stack_lists_its_encode_profiles(self, monkeypatch):
        self._vainfo(monkeypatch, stdout=(
            "vainfo: VA-API version: 1.20 (libva 2.20.0)\n"
            "vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics\n"
            "      VAProfileH264High               : VAEntrypointVLD\n"
            "      VAProfileH264High               : VAEntrypointEncSlice\n"
            "      VAProfileHEVCMain               : VAEntrypointEncSlice\n"
        ))
        probe = gpu.vainfo()
        assert probe["ran"] is True
        assert probe["ok"] is True
        assert "iHD" in probe["driver"]
        assert probe["encoders"] == ["VAProfileH264High", "VAProfileHEVCMain"]

    def test_a_decode_only_stack_is_not_ok(self, monkeypatch):
        """A real configuration: the driver loads and the chip has no encode
        engine. Every file is in place and no preset that asks for hardware
        can ever work."""
        self._vainfo(monkeypatch, stdout=(
            "vainfo: Driver version: Mesa Gallium driver\n"
            "      VAProfileH264High               : VAEntrypointVLD\n"
        ))
        probe = gpu.vainfo()
        assert probe["ran"] is True
        assert probe["ok"] is False
        assert probe["encoders"] == []

    def test_a_stack_that_will_not_initialise_is_not_ok(self, monkeypatch):
        self._vainfo(monkeypatch, returncode=1, stderr=(
            "vaInitialize failed with error code -1 (unknown libva error)\n"
        ))
        probe = gpu.vainfo()
        assert probe["ok"] is False
        assert "vaInitialize failed" in probe["output"]

    def test_a_vainfo_that_explodes_is_not_an_exception(self, monkeypatch):
        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/vainfo")

        def boom(*args, **kwargs):
            raise OSError("no such thing")

        monkeypatch.setattr(subprocess, "run", boom)
        probe = gpu.vainfo()
        assert probe["ran"] is False
        assert "could not be run" in probe["output"]

    def test_unrelated_files_are_not_drivers(self, monkeypatch, tmp_path):
        directory = tmp_path / "dri"
        directory.mkdir()
        (directory / "README").write_text("")
        (directory / "libsomething.so").write_text("")
        monkeypatch.setattr(gpu, "VA_DRIVER_DIRS", (directory,))
        assert gpu.va_drivers() == []

    def test_a_missing_directory_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu, "VA_DRIVER_DIRS", (tmp_path / "absent",))
        assert gpu.va_drivers() == []

    def test_the_same_driver_in_two_places_is_listed_once(self, monkeypatch, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        for directory in (first, second):
            directory.mkdir()
            (directory / "iHD_drv_video.so").write_text("")
        monkeypatch.setattr(gpu, "VA_DRIVER_DIRS", (first, second))
        assert gpu.va_drivers() == ["iHD_drv_video.so"]

    def test_a_working_node_with_no_driver_still_asks_to_be_fixed(
        self, monkeypatch, tmp_path,
    ):
        """The failure that looks solved.

        Every check about the passthrough passes — the node is there and
        opens — and encoding fails anyway. If describe() reports this as
        clean, nothing anywhere tells the user what is actually wrong.
        """
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        state = gpu.describe()
        assert state["available"] is True, "the node genuinely is passed through"
        assert state["runtime"]["ok"] is False
        assert "driver" in state["detail"]
        assert state["fix"], "there is something to do about it"


class TestDescribingTheContainer:
    def test_no_dri_directory_at_all(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path / "absent")
        state = gpu.describe()
        assert state["available"] is False
        assert "does not exist" in state["detail"]
        assert "adr-doctor --fix" in state["fix"]

    def test_a_dri_directory_with_no_render_node(self, monkeypatch, tmp_path):
        (tmp_path / "card0").write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        state = gpu.describe()
        assert state["available"] is False
        assert "no render node" in state["detail"]

    def test_a_render_node_that_opens_is_available(self, monkeypatch, tmp_path):
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        _with_driver(monkeypatch, tmp_path)
        state = gpu.describe()
        assert state["available"] is True
        assert str(node) in state["detail"]
        assert state["fix"] == "", "nothing to fix when it works"

    def test_permission_denied_is_named_as_a_group_problem(
        self, monkeypatch, tmp_path,
    ):
        """A node that is there but unreadable is the service user's groups,
        not the passthrough — a different cause, though the same command
        fixes it, because only the host knows the gid that owns the node."""
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        monkeypatch.setattr(gpu, "_openable", lambda p: (False, errno.EACCES))
        state = gpu.describe()
        assert state["available"] is False
        assert "permission denied" in state["detail"]
        assert "adr-doctor --fix" in state["fix"]

    def test_the_advice_does_not_name_a_group_by_name(self, monkeypatch, tmp_path):
        """The container's 'render' group rarely carries the host's gid, and
        the kernel checks the number. Advice to join it by name would look
        right and change nothing."""
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        monkeypatch.setattr(gpu, "_openable", lambda p: (False, errno.EACCES))
        assert "usermod -aG render" not in gpu.describe()["fix"]

    def test_a_cgroup_denial_points_at_the_host(self, monkeypatch, tmp_path):
        node = tmp_path / "renderD128"
        node.write_text("")
        monkeypatch.setattr(gpu, "DRI_DIR", tmp_path)
        monkeypatch.setattr(gpu, "_openable", lambda p: (False, errno.EPERM))
        state = gpu.describe()
        assert state["available"] is False
        assert "cgroup" in state["detail"]
        assert "adr-doctor --fix" in state["fix"]

    def test_it_never_raises(self, monkeypatch):
        monkeypatch.setattr(gpu, "DRI_DIR", Path("/proc/self/mem"))
        state = gpu.describe()
        assert isinstance(state["available"], bool)


class TestTheRuntimeCanBeTheWrongOne:
    """Having a Quick Sync runtime is not the same as having the right one.

    The oneVPL GPU runtime refuses a Gen 9.5 chip outright; the older Media
    SDK stops at Gen 11. HandBrake's error is identical either way — "qsv is
    not available on the system" — so a stack that is present and wrong looks
    exactly like a stack that is present and right, and the encoder test is
    the only thing that can tell them apart. The note exists so its answer has
    a next step.
    """

    def test_one_runtime_names_what_it_covers_and_what_it_does_not(self):
        note = gpu._runtime_coverage_note(
            gpu.VENDOR_INTEL, ["/usr/lib/x86_64-linux-gnu/libmfxhw64.so.1"],
        )
        assert "libmfx1" in note
        assert "Comet Lake" in note
        assert "libmfxgen1" in note          # the one to try next

    def test_the_other_runtime_points_the_other_way(self):
        note = gpu._runtime_coverage_note(
            gpu.VENDOR_INTEL, ["/usr/lib/x86_64-linux-gnu/libmfx-gen.so.1.2"],
        )
        assert "libmfxgen1" in note
        assert "Alder Lake" in note
        assert "libmfx1" in note

    def test_both_installed_needs_no_warning(self):
        """The dispatcher picks. There is nothing to say."""
        assert gpu._runtime_coverage_note(
            gpu.VENDOR_INTEL, ["libmfxhw64.so.1", "libmfx-gen.so.1.2"],
        ) == ""

    def test_neither_installed_is_handled_elsewhere(self):
        assert gpu._runtime_coverage_note(gpu.VENDOR_INTEL, []) == ""

    def test_amd_is_not_told_about_quick_sync(self):
        assert gpu._runtime_coverage_note(
            gpu.VENDOR_AMD, ["libmfxhw64.so.1"],
        ) == ""

    def test_every_runtime_this_module_looks_for_is_described(self):
        """A runtime added to the probe without an entry here would produce a
        KeyError in the middle of a diagnostic."""
        assert set(gpu.RUNTIME_COVERAGE) == set(gpu.QSV_RUNTIME_LIBS)


class TestTheDispatcherIsAskedDirectly:
    """"The driver is installed, the runtime is installed, ffmpeg encodes on
    this GPU, and HandBrake says qsv is not available" is where every check
    based on file names runs out. Three problems share that one symptom: no
    runtime, a runtime that refuses the chip, and a runtime the driver will
    not talk to. The dispatcher knows which; it just has to be asked.
    """

    def test_a_loaded_runtime_is_reported_as_loaded(self):
        log = (
            "libvpl: loading library /usr/lib/x86_64-linux-gnu/libmfxhw64.so.1\n"
            "libvpl: library loaded successfully\n"
        )
        said = gpu.summarise_dispatcher_log(log)
        assert "loaded" in said
        assert "libmfxhw64.so" in said

    def test_a_rejected_runtime_is_the_interesting_case(self):
        """Installed and refused looks identical to missing from outside."""
        log = (
            "libvpl: loading library /usr/lib/x86_64-linux-gnu/libmfxhw64.so.1\n"
            "libvpl: unloading library, implementation not supported\n"
        )
        said = gpu.summarise_dispatcher_log(log)
        assert "turned it down" in said
        assert "will not serve this GPU" in said

    def test_nothing_found_says_so_plainly(self):
        said = gpu.summarise_dispatcher_log("libvpl: searching /usr/lib\n")
        assert "no Quick Sync runtime" in said

    def test_an_empty_log_is_not_a_crash(self):
        assert gpu.summarise_dispatcher_log("")

    def test_a_missing_handbrake_is_answered_not_raised(self, tmp_path):
        result = gpu.qsv_dispatcher_log(str(tmp_path / "nope"))
        assert result["ran"] is False
        assert "not installed" in result["summary"]

    def test_the_probe_sets_the_variables_that_make_it_talk(self, tmp_path, monkeypatch):
        exe = tmp_path / "HandBrakeCLI"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        seen = {}

        import subprocess

        def fake_run(cmd, **kwargs):
            seen.update(kwargs.get("env") or {})
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        gpu.qsv_dispatcher_log(str(exe), driver="iHD")
        assert seen.get("ONEVPL_DISPATCHER_LOG") == "ON"
        assert seen.get("ONEVPL_DISPATCHER_LOG_FILE")
        assert seen.get("LIBVA_DRIVER_NAME") == "iHD"

    def test_no_log_written_is_itself_an_answer(self, tmp_path, monkeypatch):
        """A build that talks to the old Media SDK directly never goes through
        the dispatcher, and writes nothing."""
        exe = tmp_path / "HandBrakeCLI"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)

        import subprocess

        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="x", stderr=""),
        )
        result = gpu.qsv_dispatcher_log(str(exe))
        assert result["ran"] is True
        assert "wrote no log" in result["summary"]
