"""Tests for adr.utils — pure helper functions."""

import pytest
from unittest.mock import patch

from adr.utils import (
    sanitize_filename,
    parse_disc_label,
    format_duration,
    parse_duration,
    normalize_drive,
    extract_tmdb_year,
    make_plex_folder_name,
    get_lan_ip,
    BYTES_PER_MB,
)


class TestSanitizeFilename:
    def test_removes_illegal_chars(self):
        assert sanitize_filename('my<>:"/\\|?*file') == "myfile"

    def test_preserves_unicode_letters(self):
        assert sanitize_filename("A unicode film with äöü") == "A unicode film with äöü"

    def test_collapses_whitespace(self):
        assert sanitize_filename("too   many    spaces") == "too many spaces"

    def test_strips_leading_trailing_whitespace(self):
        assert sanitize_filename("  padded  ") == "padded"

    def test_truncates_long_names(self):
        long_name = "A" * 250
        result = sanitize_filename(long_name)
        assert len(result) == 200

    def test_empty_string(self):
        assert sanitize_filename("") == ""

    def test_control_characters_removed(self):
        assert sanitize_filename("hello\x00\x01\x1fworld") == "helloworld"

    def test_normal_filename_unchanged(self):
        assert sanitize_filename("The Matrix (1999)") == "The Matrix (1999)"


class TestParseDiscLabel:
    def test_underscores_to_title_case(self):
        assert parse_disc_label("THE_MATRIX") == ("The Matrix", None)

    def test_extracts_trailing_year(self):
        assert parse_disc_label("THE_MATRIX_1999") == ("The Matrix", 1999)

    def test_removes_disc_suffix(self):
        assert parse_disc_label("Inception_2010_DISC1") == ("Inception", 2010)

    def test_removes_disk_suffix(self):
        assert parse_disc_label("MY_MOVIE_DISK2") == ("My Movie", None)

    def test_removes_title_marker(self):
        assert parse_disc_label("BEAUTY_AND_BEAST_T01") == ("Beauty And Beast", None)

    def test_removes_region_code(self):
        assert parse_disc_label("DEADPOOL_2_R1") == ("Deadpool 2", None)

    def test_removes_pal_ntsc(self):
        assert parse_disc_label("MOVIE_PAL") == ("Movie", None)

    def test_empty_label(self):
        assert parse_disc_label("") == ("Unknown", None)

    def test_whitespace_only(self):
        assert parse_disc_label("   ") == ("Unknown", None)

    def test_year_at_end_with_hyphen(self):
        assert parse_disc_label("Some-Movie-2023") == ("Some Movie", 2023)

    def test_mid_string_year(self):
        _, year = parse_disc_label("MOVIE_2020_SPECIAL")
        assert year == 2020

    def test_no_false_year_from_short_numbers(self):
        _, year = parse_disc_label("TRACK_42")
        assert year is None


class TestFormatDuration:
    def test_seconds_only(self):
        assert format_duration(45) == "45s"

    def test_minutes_and_seconds(self):
        assert format_duration(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert format_duration(3725) == "1h 2m"

    def test_zero(self):
        assert format_duration(0) == "0s"

    def test_negative(self):
        assert format_duration(-10) == "0s"

    def test_exact_hour(self):
        assert format_duration(3600) == "1h 0m"

    def test_exact_minute(self):
        assert format_duration(60) == "1m 0s"


class TestParseDuration:
    def test_hms_format(self):
        assert parse_duration("1:23:45") == 5025

    def test_hms_zero(self):
        assert parse_duration("0:00:00") == 0

    def test_invalid_format(self):
        assert parse_duration("not_a_time") == 0

    def test_two_parts(self):
        assert parse_duration("23:45") == 0

    def test_empty(self):
        assert parse_duration("") == 0

    def test_with_whitespace(self):
        assert parse_duration("  2:05:30  ") == 7530


class TestNormalizeDrive:
    """Linux device-path normalisation: strip trailing slashes only."""

    def test_strips_trailing_slash(self):
        assert normalize_drive("/dev/sr0/") == "/dev/sr0"

    def test_no_change_when_no_slash(self):
        assert normalize_drive("/dev/sr0") == "/dev/sr0"

    def test_preserves_case(self):
        assert normalize_drive("/dev/Sr0") == "/dev/Sr0"

    def test_strips_multiple_slashes(self):
        assert normalize_drive("/dev/sr0//") == "/dev/sr0"

    def test_empty_string(self):
        assert normalize_drive("") == ""


class TestExtractTmdbYear:
    def test_valid_release_date(self):
        assert extract_tmdb_year("2023-05-15") == 2023

    def test_short_date(self):
        assert extract_tmdb_year("2023") == 2023

    def test_none_returns_fallback(self):
        assert extract_tmdb_year(None, fallback=2000) == 2000

    def test_empty_string_returns_fallback(self):
        assert extract_tmdb_year("", fallback=1999) == 1999

    def test_too_short_returns_fallback(self):
        assert extract_tmdb_year("20", fallback=2010) == 2010

    def test_non_numeric_year_returns_fallback(self):
        assert extract_tmdb_year("abcd-01-01", fallback=None) is None

    def test_no_fallback_returns_none(self):
        assert extract_tmdb_year(None) is None


class TestMakePlexFolderName:
    def test_title_with_year(self):
        assert make_plex_folder_name("The Matrix", 1999) == "The Matrix (1999)"

    def test_title_without_year(self):
        assert make_plex_folder_name("Unknown Movie", None) == "Unknown Movie"

    def test_title_is_sanitized(self):
        result = make_plex_folder_name('Movie: The "Sequel"', 2023)
        assert ":" not in result
        assert '"' not in result
        assert "(2023)" in result

    def test_zero_year_treated_as_no_year(self):
        assert make_plex_folder_name("Title", 0) == "Title"


class TestConstants:
    def test_bytes_per_mb(self):
        assert BYTES_PER_MB == 1_048_576


class TestGetLanIp:
    def test_returns_string(self):
        result = get_lan_ip()
        assert isinstance(result, str)

    def test_fallback_on_error(self):
        with patch("socket.socket") as mock_sock:
            mock_sock.side_effect = OSError("no network")
            assert get_lan_ip() == "127.0.0.1"
