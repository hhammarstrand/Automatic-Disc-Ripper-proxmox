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
