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

    @pytest.mark.parametrize("lib", [
        "libmfxhw64.so.1", "libmfx-gen.so.1.2", "libvpl.so.2", "libmfx.so.1",
    ])
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
