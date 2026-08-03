"""Extras go in Other/, not on the end of the film.

With main-feature selection off, a disc's trailers and featurettes used to be
named "Film (1999) - pt2", "pt3" and so on. Plex *stacks* numbered parts: it
treats them as one film split across files, so a two-minute trailer became the
back half of the movie. Plex recognises eight extras folder names, and Other is
the one that does not claim to know what the extra is — which is all MakeMKV
can tell us, since it reports a duration and nothing else.
"""

import types

from adr.naming import EXTRAS_FOLDER, pick_main_feature, plan_output


def _film(title="The Film", year=1999):
    return types.SimpleNamespace(
        title=title, year=year, content_type="movie",
        series_season=None, series_first_episode=None,
    )


def _series():
    return types.SimpleNamespace(
        title="The Show", year=2019, content_type="series",
        series_season=2, series_first_episode=1,
    )


# ------------------------------------------------------------------ #
# Picking the feature
# ------------------------------------------------------------------ #

class TestPickMainFeature:
    def test_a_clear_feature_is_found(self):
        # 100 minutes against 3 minutes of trailer.
        assert pick_main_feature([6000, 180, 240]) == 0

    def test_the_feature_need_not_be_first(self):
        assert pick_main_feature([180, 6000, 240]) == 1

    def test_similar_lengths_are_parts_not_extras(self):
        """Two 50-minute titles are a film split across the disc. Calling one
        of them an extra hides half the film."""
        assert pick_main_feature([3000, 3000]) is None

    def test_the_ratio_is_the_boundary(self):
        assert pick_main_feature([3000, 2000]) == 0        # exactly 1.5x
        assert pick_main_feature([2999, 2000]) is None

    def test_a_single_title_has_nothing_to_compare_against(self):
        assert pick_main_feature([6000]) is None

    def test_nothing_at_all(self):
        assert pick_main_feature([]) is None

    def test_a_missing_duration_makes_the_whole_thing_a_guess(self):
        """Partial information is how a trailer ends up named as the film."""
        assert pick_main_feature([6000, None, 240]) is None
        assert pick_main_feature([6000, 0, 240]) is None


# ------------------------------------------------------------------ #
# Naming
# ------------------------------------------------------------------ #

class TestPlanOutput:
    def test_extras_go_to_the_plex_folder(self):
        plan = plan_output(_film(), 3, main_index=0)
        assert plan.filenames == [
            "The Film (1999)",
            f"{EXTRAS_FOLDER}/Extra 1",
            f"{EXTRAS_FOLDER}/Extra 2",
        ]
        assert plan.folder == "The Film (1999)"

    def test_the_feature_keeps_the_film_name_wherever_it_is(self):
        plan = plan_output(_film(), 3, main_index=1)
        assert plan.filenames == [
            f"{EXTRAS_FOLDER}/Extra 1",
            "The Film (1999)",
            f"{EXTRAS_FOLDER}/Extra 2",
        ]

    def test_without_a_main_index_they_are_parts(self):
        """Unchanged behaviour: a genuinely multi-part film."""
        plan = plan_output(_film(), 2)
        assert plan.filenames == ["The Film (1999) - pt1", "The Film (1999) - pt2"]

    def test_a_single_file_is_just_the_film(self):
        assert plan_output(_film(), 1, main_index=0).filenames == ["The Film (1999)"]

    def test_an_out_of_range_index_is_ignored_rather_than_trusted(self):
        plan = plan_output(_film(), 2, main_index=5)
        assert plan.filenames == ["The Film (1999) - pt1", "The Film (1999) - pt2"]

    def test_a_series_is_never_given_extras(self):
        """Every title on a box-set disc is an episode. One of them being
        longer does not make the others bonus material."""
        plan = plan_output(_series(), 3, main_index=0)
        assert plan.is_series
        assert all(EXTRAS_FOLDER not in name for name in plan.filenames)
        assert plan.filenames == [
            "The Show (2019) - S02E01",
            "The Show (2019) - S02E02",
            "The Show (2019) - S02E03",
        ]

    def test_the_extras_folder_is_one_plex_recognises(self):
        """Plex only reads eight folder names; anything else is ignored."""
        assert EXTRAS_FOLDER in (
            "Behind The Scenes", "Deleted Scenes", "Featurettes", "Interviews",
            "Scenes", "Shorts", "Trailers", "Other",
        )

    def test_no_files_plans_nothing(self):
        assert plan_output(_film(), 0).filenames == ["The Film (1999)"]


# ------------------------------------------------------------------ #
# The encoder has to create the subfolder
# ------------------------------------------------------------------ #

def test_the_encoder_creates_the_extras_subfolder(tmp_path, monkeypatch):
    """HandBrake will not create a directory for its output file."""
    from adr.encoder import HandBrakeEncoder

    source = tmp_path / "title.mkv"
    source.write_bytes(b"x")
    exe = tmp_path / "HandBrakeCLI"
    exe.write_text("#!/bin/sh\nexit 1\n")
    exe.chmod(0o755)

    config = types.SimpleNamespace(
        handbrake_path=str(exe), handbrake_preset="Fast 1080p30",
        handbrake_preset_file="", handbrake_extra_args="",
        completed_path=tmp_path / "out",
    )
    encoder = HandBrakeEncoder(config)
    result = encoder.encode(
        input_path=source,
        output_dir=tmp_path / "out" / "The Film (1999)",
        output_filename=f"{EXTRAS_FOLDER}/Extra 1",
    )
    # The encode fails — the stub exits 1 — but the directory must exist by
    # then, because that is the part HandBrake cannot do for itself.
    assert (tmp_path / "out" / "The Film (1999)" / EXTRAS_FOLDER).is_dir()
    assert result.output_path.name == "Extra 1.mp4"
