"""What a setting is allowed to be.

The API validated five settings out of forty-nine, in a ladder of
`if "web_port" in data:` blocks — and the shape was the problem. Adding a rule
meant adding a branch, so every setting added since simply never got one. Type
"abc" into the quality box and it was accepted, stored, and silently discarded
at the moment of use: the value gone, nothing said, and the encode running on
the old one.
"""

import types

import pytest

from adr import settingsrules
from adr.config import _DEFAULTS


class TestTheTableItself:
    def test_every_default_is_a_real_setting(self):
        """A rule for a setting that does not exist can never fire, and reads
        as coverage that is not there."""
        unknown = sorted(set(settingsrules.RULES) - set(_DEFAULTS))
        assert not unknown, unknown

    def test_every_default_that_takes_a_bounded_value_has_a_rule(self):
        """Anything with a fixed set of answers or a numeric range should be
        checked. Free text — a preset name, a token, a label — should not."""
        needs_one = {
            key for key, value in _DEFAULTS.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        missing = sorted(needs_one - set(settingsrules.RULES))
        assert not missing, f"numeric settings with no bounds: {missing}"

    def test_a_rule_that_explodes_does_not_take_the_save_with_it(self):
        """A broken rule must report, not raise: someone saving their
        settings should not get a 500 because a validator has a bug."""
        settingsrules.RULES["web_port"] = lambda v: 1 / 0
        try:
            problems = settingsrules.check({"web_port": 8080})
        finally:
            settingsrules.RULES["web_port"] = settingsrules._port
        assert problems == ["web_port could not be checked"]


class TestEveryComplaintIsUseful:
    CASES = [
        ("web_port", "nonsense"),
        ("web_port", 99999),
        ("max_encode_jobs", 0),
        ("video_quality", "abc"),
        ("video_quality", 7),
        ("max_height", -1),
        ("audio_language", "swedish"),
        ("encoder_backend", "gpu"),
        ("vaapi_codec", "av1"),
        ("libva_driver", "nouveau"),
        ("log_level", "LOUD"),
        ("audio_cd_format", "wav"),
        ("notify_provider", "carrier pigeon"),
        ("completed_path", "media/films"),
    ]

    @pytest.mark.parametrize("name,value", CASES)
    def test_it_is_rejected(self, name, value):
        assert settingsrules.check({name: value}), f"{name}={value!r} was accepted"

    @pytest.mark.parametrize("name,value", CASES)
    def test_the_message_names_the_setting_and_what_would_be_right(self, name, value):
        """The person reading it is looking at a box they have just typed
        into. "Invalid value" tells them neither which box nor what to do."""
        message = settingsrules.check({name: value})[0]
        assert message.startswith(name)
        assert len(message) > len(name) + 12, message

    def test_every_problem_is_reported_at_once(self):
        """Someone who has just filled in a form wants to fix everything they
        got wrong in one pass, not discover the next mistake each time they
        press save."""
        problems = settingsrules.check({
            "web_port": "x", "video_quality": 99, "encoder_backend": "gpu"})
        assert len(problems) == 3


class TestWhatIsAccepted:
    GOOD = [
        ("web_port", 8080), ("max_encode_jobs", 1),
        ("video_quality", 0), ("video_quality", 22),
        ("max_height", 0), ("max_height", 1080),
        ("audio_language", ""), ("audio_language", "sv"), ("audio_language", "swe"),
        ("encoder_backend", "handbrake"), ("encoder_backend", "vaapi"),
        ("libva_driver", ""), ("libva_driver", "i965"),
        ("completed_path", "/mnt/media"), ("plex_path", ""),
        ("audio_cd_mp3_bitrate", "320k"),
    ]

    @pytest.mark.parametrize("name,value", GOOD)
    def test_it_passes(self, name, value):
        assert settingsrules.check({name: value}) == [], f"{name}={value!r}"

    def test_settings_with_no_rule_are_left_alone(self):
        """A preset name, a token, a drive label — free text, and inventing a
        rule for it would reject something someone legitimately wants."""
        assert settingsrules.check({"handbrake_preset": "Anything At All"}) == []

    def test_zero_means_leave_it_alone_not_out_of_range(self):
        """Quality has two valid shapes and a rule that knows only the range
        would reject the default."""
        assert settingsrules.check({"video_quality": 0}) == []


class TestTheRulesThatNeedTwoSettings:
    def _config(self, **values):
        base = {"series_min_minutes": 15, "series_max_minutes": 75}
        base.update(values)
        return types.SimpleNamespace(**base)

    def test_a_shortest_longer_than_the_longest_is_caught(self):
        """Each value is fine on its own, and together they make series
        detection match nothing at all — silently."""
        problems = settingsrules.cross_check(
            {"series_min_minutes": 90}, self._config())
        assert problems and "no episode can ever match" in problems[0]

    def test_the_sensible_pair_passes(self):
        assert settingsrules.cross_check(
            {"series_min_minutes": 20}, self._config()) == []

    def test_it_reads_the_other_value_from_the_saved_config(self):
        """A form that only sends the field that changed still has to be
        checked against the one that did not."""
        problems = settingsrules.cross_check(
            {"series_max_minutes": 5}, self._config(series_min_minutes=15))
        assert problems

    def test_a_value_that_is_not_a_number_is_left_to_the_field_rule(self):
        """Two complaints about one mistake is one complaint too many."""
        assert settingsrules.cross_check(
            {"series_min_minutes": "soon"}, self._config()) == []


class TestTheDrivesFieldCannotBeCorrupted:
    """The field round-tripped a Python list through the template, so a saved
    list came back as "['/dev/sr0', '/dev/sr1']" and the next save split *that*
    on commas. The watcher then had device paths made of brackets and quotes,
    and simply never saw a disc — with nothing on any page saying why."""

    def test_auto_is_fine(self):
        assert settingsrules.check({"drives": "auto"}) == []

    def test_empty_is_fine(self):
        assert settingsrules.check({"drives": ""}) == []

    def test_a_list_of_device_paths_is_fine(self):
        assert settingsrules.check({"drives": ["/dev/sr0", "/dev/sr1"]}) == []

    def test_the_corrupted_shape_is_refused(self):
        assert settingsrules.check({"drives": ["['/dev/sr0',", "'/dev/sr1']"]})

    def test_a_bare_string_that_is_not_auto_is_refused(self):
        assert settingsrules.check({"drives": "/dev/sr0"})

    def test_an_empty_list_is_refused(self):
        """"Watch nothing" is what "auto" with no drives means; an empty list
        is a form that lost its value."""
        assert settingsrules.check({"drives": []})


class TestALanguageBothEncodersCanHonour:
    """HandBrake resolves any ISO code; the ffmpeg backend matches against a
    table. A code outside it produced a Swedish film under one encoder and an
    English one under the other, with nothing saying why."""

    def test_the_common_codes_pass(self):
        for code in ("swe", "eng", "sv", "en", "ger", "fre", "nor"):
            assert settingsrules.check({"audio_language": code}) == [], code

    def test_empty_passes(self):
        assert settingsrules.check({"audio_language": ""}) == []

    def test_any_three_letter_code_is_accepted(self):
        """normalise_language passes an unknown three-letter code through, so
        the ffmpeg backend matches it against the disc's tag perfectly well.
        Rejecting these locked people out of the whole settings page — the form
        posts every field on every save."""
        for code in ("hun", "ces", "tur", "ell", "heb", "ara", "hin", "vie"):
            assert settingsrules.check({"audio_language": code}) == [], code

    def test_a_two_letter_code_that_cannot_be_expanded_is_refused(self):
        """This is the one case that genuinely diverges: the matcher would
        compare 'hu' against a disc's 'hun' and find nothing."""
        problems = settingsrules.check({"audio_language": "hu"})
        assert problems
        assert "three-letter" in problems[0]

    def test_malformed_is_still_malformed(self):
        assert settingsrules.check({"audio_language": "swedish"})
        assert settingsrules.check({"audio_language": "s1"})
