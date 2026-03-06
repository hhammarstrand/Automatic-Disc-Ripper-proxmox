"""Tests for adr.encoder — HandBrake wrapper helpers."""

import pytest

from adr.encoder import HandBrakeEncoder


# ------------------------------------------------------------------ #
# _normalize_progress_value
# ------------------------------------------------------------------ #

class TestNormalizeProgressValue:
    def test_fraction(self):
        assert HandBrakeEncoder._normalize_progress_value(0.5) == pytest.approx(0.5)

    def test_percentage(self):
        """Values > 1.0 are treated as percentages and divided by 100."""
        assert HandBrakeEncoder._normalize_progress_value(75.0) == pytest.approx(0.75)

    def test_zero(self):
        assert HandBrakeEncoder._normalize_progress_value(0) == 0.0

    def test_one(self):
        assert HandBrakeEncoder._normalize_progress_value(1.0) == 1.0

    def test_none(self):
        assert HandBrakeEncoder._normalize_progress_value(None) == 0.0

    def test_string_number(self):
        assert HandBrakeEncoder._normalize_progress_value("0.25") == pytest.approx(0.25)

    def test_invalid_string(self):
        assert HandBrakeEncoder._normalize_progress_value("not_a_number") == 0.0

    def test_negative_clamped(self):
        assert HandBrakeEncoder._normalize_progress_value(-0.5) == 0.0

    def test_over_100_clamped(self):
        assert HandBrakeEncoder._normalize_progress_value(150.0) == 1.0


# ------------------------------------------------------------------ #
# _extract_preset_names
# ------------------------------------------------------------------ #

class TestExtractPresetNames:
    def test_simple_preset(self):
        names = []
        seen = set()
        entry = {"PresetName": "Fast 1080p30", "Folder": False}
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        assert names == ["Fast 1080p30"]

    def test_folder_skipped(self):
        names = []
        seen = set()
        entry = {"PresetName": "General", "Folder": True, "ChildrenArray": [
            {"PresetName": "Fast 1080p30", "Folder": False},
        ]}
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        assert "General" not in names
        assert "Fast 1080p30" in names

    def test_nested_children(self):
        names = []
        seen = set()
        entry = {
            "PresetName": "Root",
            "Folder": True,
            "ChildrenArray": [
                {"PresetName": "Child1", "Folder": False},
                {
                    "PresetName": "SubFolder",
                    "Folder": True,
                    "ChildrenArray": [
                        {"PresetName": "GrandChild", "Folder": False},
                    ],
                },
            ],
        }
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        assert "Child1" in names
        assert "GrandChild" in names
        assert "Root" not in names
        assert "SubFolder" not in names

    def test_deduplication(self):
        names = []
        seen = set()
        entry = {"PresetName": "Duplicate", "Folder": False}
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        assert names.count("Duplicate") == 1

    def test_non_dict_entry(self):
        names = []
        seen = set()
        # Should not crash
        HandBrakeEncoder._extract_preset_names("not_a_dict", names, seen)  # type: ignore
        assert names == []

    def test_no_preset_name(self):
        names = []
        seen = set()
        entry = {"SomeOtherKey": "value"}
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        assert names == []

    def test_empty_children_array(self):
        names = []
        seen = set()
        entry = {"PresetName": "Leaf", "Folder": False, "ChildrenArray": []}
        HandBrakeEncoder._extract_preset_names(entry, names, seen)
        assert names == ["Leaf"]
