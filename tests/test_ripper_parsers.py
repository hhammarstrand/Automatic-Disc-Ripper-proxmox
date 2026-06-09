"""Tests for MakeMKV robot-mode output parsers in adr.ripper."""

import pytest

from adr.ripper import MakeMKVRipper, RipResult

# ------------------------------------------------------------------ #
# _parse_csv_line
# ------------------------------------------------------------------ #

class TestParseCsvLine:
    def test_simple_values(self):
        parts = MakeMKVRipper._parse_csv_line('1,2,3,"hello"')
        assert parts == ["1", "2", "3", '"hello"']

    def test_quoted_comma(self):
        parts = MakeMKVRipper._parse_csv_line('1,"hello, world",3')
        assert parts == ["1", '"hello, world"', "3"]

    def test_empty_string(self):
        parts = MakeMKVRipper._parse_csv_line("")
        assert parts == [""]

    def test_single_value(self):
        parts = MakeMKVRipper._parse_csv_line("42")
        assert parts == ["42"]


# ------------------------------------------------------------------ #
# _parse_progress_rich (PRGV lines)
# ------------------------------------------------------------------ #

class TestParseProgressRich:
    def _make_state(self, **overrides):
        state = {
            "title_current": 1,
            "title_total": 1,
            "description": "Copying title 1 of 1",
            "is_copying_title": True,
            "is_rip_active": True,
        }
        state.update(overrides)
        return state

    def test_basic_progress(self):
        state = self._make_state()
        result = MakeMKVRipper._parse_progress_rich("PRGV:500,1000,1000", state)
        assert result is not None
        assert 0.49 < result["overall"] < 0.51
        assert result["title_progress"] == pytest.approx(0.5)

    def test_zero_max_returns_none(self):
        state = self._make_state()
        result = MakeMKVRipper._parse_progress_rich("PRGV:0,0,0", state)
        assert result is None

    def test_not_rip_active_returns_none(self):
        state = self._make_state(is_rip_active=False)
        result = MakeMKVRipper._parse_progress_rich("PRGV:500,1000,1000", state)
        assert result is None

    def test_multi_title_progress(self):
        state = self._make_state(title_current=2, title_total=4)
        result = MakeMKVRipper._parse_progress_rich("PRGV:500,1000,1000", state)
        assert result is not None
        # (2-1 + 0.5) / 4 = 0.375
        assert result["overall"] == pytest.approx(0.375)

    def test_overall_capped_below_one(self):
        state = self._make_state(title_current=1, title_total=1)
        result = MakeMKVRipper._parse_progress_rich("PRGV:1000,1000,1000", state)
        assert result is not None
        assert result["overall"] <= 0.995

    def test_invalid_line_returns_none(self):
        state = self._make_state()
        result = MakeMKVRipper._parse_progress_rich("PRGV:not,valid,data", state)
        assert result is None

    def test_description_passed_through(self):
        state = self._make_state(description="Saving title 1 of 3")
        result = MakeMKVRipper._parse_progress_rich("PRGV:100,1000,1000", state)
        assert result is not None
        assert result["description"] == "Saving title 1 of 3"

    def test_zero_title_current_clamped_to_1(self):
        state = self._make_state(title_current=0, title_total=2)
        result = MakeMKVRipper._parse_progress_rich("PRGV:500,1000,1000", state)
        assert result is not None
        assert result["title_current"] == 1

    def test_zero_title_total_clamped_to_1(self):
        state = self._make_state(title_current=1, title_total=0)
        result = MakeMKVRipper._parse_progress_rich("PRGV:500,1000,1000", state)
        assert result is not None
        assert result["title_total"] >= 1


# ------------------------------------------------------------------ #
# _parse_prgc (PRGC lines — current item)
# ------------------------------------------------------------------ #

class TestParsePrgc:
    def test_copying_title_detected(self):
        state = {"description": "", "is_copying_title": False, "is_rip_active": False,
                 "title_current": 0, "title_total": 0}
        MakeMKVRipper._parse_prgc('PRGC:5017,0,"Copying title 2 of 5"', state)
        assert state["is_copying_title"] is True
        assert state["is_rip_active"] is True
        assert state["title_current"] == 2
        assert state["title_total"] == 5

    def test_saving_to_mkv_detected(self):
        state = {"description": "", "is_copying_title": False, "is_rip_active": False,
                 "title_current": 0, "title_total": 0}
        MakeMKVRipper._parse_prgc('PRGC:5024,0,"Saving to MKV file"', state)
        assert state["is_rip_active"] is True

    def test_description_updated(self):
        state = {"description": "", "is_copying_title": False, "is_rip_active": False,
                 "title_current": 0, "title_total": 0}
        MakeMKVRipper._parse_prgc('PRGC:0,0,"Analyzing seamless segments"', state)
        assert state["description"] == "Analyzing seamless segments"

    def test_invalid_line_no_crash(self):
        state = {"description": "", "is_copying_title": False, "is_rip_active": False,
                 "title_current": 0, "title_total": 0}
        # Should not raise
        MakeMKVRipper._parse_prgc("PRGC:", state)


# ------------------------------------------------------------------ #
# _parse_prgt (PRGT lines — phase description)
# ------------------------------------------------------------------ #

class TestParsePrgt:
    def test_saving_phase_sets_active(self):
        state = {"description": "", "is_rip_active": False, "title_current": 0, "title_total": 0}
        MakeMKVRipper._parse_prgt('PRGT:5024,0,"Saving to MKV file"', state)
        assert state["is_rip_active"] is True

    def test_extracts_title_count(self):
        state = {"description": "", "is_rip_active": False, "title_current": 0, "title_total": 0}
        MakeMKVRipper._parse_prgt('PRGT:0,0,"Saving 3 titles to MKV files"', state)
        assert state["title_total"] == 3

    def test_description_updated(self):
        state = {"description": "", "is_rip_active": False, "title_current": 0, "title_total": 0}
        MakeMKVRipper._parse_prgt('PRGT:0,0,"Reading disc structure"', state)
        assert state["description"] == "Reading disc structure"


# ------------------------------------------------------------------ #
# _parse_tinfo (TINFO lines — title metadata)
# ------------------------------------------------------------------ #

class TestParseTinfo:
    def test_parses_title_name(self):
        titles = {}
        MakeMKVRipper._parse_tinfo('TINFO:0,2,0,"Main Feature"', titles)
        assert titles[0]["name"] == "Main Feature"

    def test_parses_duration(self):
        titles = {}
        MakeMKVRipper._parse_tinfo('TINFO:0,9,0,"1:45:30"', titles)
        assert titles[0]["duration"] == "1:45:30"

    def test_parses_filename(self):
        titles = {}
        MakeMKVRipper._parse_tinfo('TINFO:0,27,0,"title_t00.mkv"', titles)
        assert titles[0]["filename"] == "title_t00.mkv"

    def test_parses_size_bytes(self):
        titles = {}
        MakeMKVRipper._parse_tinfo('TINFO:0,10,0,"1234567890"', titles)
        assert titles[0]["size_bytes"] == "1234567890"

    def test_multiple_titles(self):
        titles = {}
        MakeMKVRipper._parse_tinfo('TINFO:0,2,0,"Title 1"', titles)
        MakeMKVRipper._parse_tinfo('TINFO:1,2,0,"Title 2"', titles)
        assert 0 in titles
        assert 1 in titles
        assert titles[0]["name"] == "Title 1"
        assert titles[1]["name"] == "Title 2"

    def test_unknown_code_ignored(self):
        titles = {}
        MakeMKVRipper._parse_tinfo('TINFO:0,99,0,"ignored"', titles)
        assert 0 in titles  # index is created
        assert len(titles[0]) == 0  # but no mapped field

    def test_invalid_line_no_crash(self):
        titles = {}
        MakeMKVRipper._parse_tinfo("TINFO:baddata", titles)
        # Should not raise


# ------------------------------------------------------------------ #
# _parse_cinfo (CINFO lines — disc metadata)
# ------------------------------------------------------------------ #

class TestParseCinfo:
    def test_parses_disc_name(self):
        result = RipResult()
        MakeMKVRipper._parse_cinfo('CINFO:0,2,0,"MY_MOVIE_DISC"', result)
        assert result.disc_name == "MY_MOVIE_DISC"

    def test_ignores_non_name_codes(self):
        result = RipResult()
        MakeMKVRipper._parse_cinfo('CINFO:0,1,0,"some_value"', result)
        assert result.disc_name is None

    def test_invalid_line_no_crash(self):
        result = RipResult()
        MakeMKVRipper._parse_cinfo("CINFO:", result)


# ------------------------------------------------------------------ #
# _log_message (MSG lines)
# ------------------------------------------------------------------ #

class TestLogMessage:
    def test_does_not_crash_on_valid_msg(self):
        # Should not raise
        MakeMKVRipper._log_message('MSG:1000,0,1,"Normal message"')

    def test_does_not_crash_on_warning(self):
        MakeMKVRipper._log_message('MSG:2001,0,1,"Warning message"')

    def test_does_not_crash_on_empty(self):
        MakeMKVRipper._log_message("MSG:")


# ------------------------------------------------------------------ #
# _make_dev_source
# ------------------------------------------------------------------ #

class TestMakeDevSource:
    def test_linux_device_path(self):
        assert MakeMKVRipper._make_dev_source("/dev/sr0") == "dev:/dev/sr0"

    def test_linux_device_path_second_drive(self):
        assert MakeMKVRipper._make_dev_source("/dev/sr1") == "dev:/dev/sr1"

    def test_linux_device_path_with_whitespace(self):
        assert MakeMKVRipper._make_dev_source("  /dev/sr0  ") == "dev:/dev/sr0"

    # Legacy Windows drive-letter forms are still accepted.
    def test_drive_letter_with_colon(self):
        assert MakeMKVRipper._make_dev_source("G:") == "dev:G:"

    def test_drive_letter_with_backslash(self):
        assert MakeMKVRipper._make_dev_source("G:\\") == "dev:G:"

    def test_drive_letter_without_colon(self):
        assert MakeMKVRipper._make_dev_source("G") == "dev:G:"
