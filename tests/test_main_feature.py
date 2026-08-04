"""A disc with extras on it must not become a sixteen-part film.

The report this file exists for: "Main feature only" was on, Dinosaur (2000)
was ripped, and sixteen files came out named ``Dinosaur (2000) - pt1`` through
``pt16`` — the film, the commentary version of the film, and fourteen
featurettes, all stacked by Plex into one movie.

Two separate failures produced that, and both are covered here:

* the pre-rip scan came back empty, so every title was ripped and nothing said
  why anywhere the person who put the disc in could read it;
* and afterwards, ``pick_main_feature`` declined — the commentary title is
  exactly as long as the film — so nothing was called the feature and
  everything was numbered.
"""

import subprocess
import types

from adr.naming import (
    EXTRAS_FOLDER,
    MAX_STACKED_PARTS,
    longest_title,
    only_the_feature,
    plan_output,
    resolve_main_feature,
)
from adr.ripper import MakeMKVRipper


# ------------------------------------------------------------------ #
# Which title is the film
# ------------------------------------------------------------------ #

class TestLongestTitle:
    def test_the_longest_one(self):
        assert longest_title([180, 6000, 240]) == 1

    def test_unknown_durations_are_skipped_not_fatal(self):
        """Unlike pick_main_feature this is only asked once the question of
        *whether* to choose has been settled, so one unreadable title must not
        take the answer away."""
        assert longest_title([None, 6000, 0]) == 1

    def test_nothing_known_has_no_answer(self):
        assert longest_title([None, 0, None]) is None
        assert longest_title([]) is None

    def test_the_first_of_a_tie_wins(self):
        assert longest_title([6000, 6000]) == 0


class TestResolveMainFeature:
    def test_a_clear_feature_is_still_the_clear_feature(self):
        assert resolve_main_feature([6000, 180, 240], False) == 0

    def test_two_similar_titles_stay_a_two_part_film(self):
        """Unchanged: calling half a film an extra is the worse mistake."""
        assert resolve_main_feature([3000, 3000], False) is None

    def test_main_feature_only_settles_a_tie(self):
        """The film and its commentary version are the same length. The user
        asked for one title; naming both as parts gives them neither."""
        assert resolve_main_feature([4920, 4920], True) == 0

    def test_main_feature_only_survives_an_unreadable_title(self):
        assert resolve_main_feature([4920, None, 300], True) == 0

    def test_too_many_titles_are_never_parts(self):
        """No real multi-part release has more than a handful of parts, so
        beyond that the longest one is the film whatever the setting says."""
        durations = [4920, 4920] + [300] * (MAX_STACKED_PARTS - 1)
        assert len(durations) > MAX_STACKED_PARTS
        assert resolve_main_feature(durations, False) == 0

    def test_the_boundary_still_allows_a_multi_part_film(self):
        durations = [3000] * MAX_STACKED_PARTS
        assert resolve_main_feature(durations, False) is None

    def test_a_single_title_needs_no_decision(self):
        assert resolve_main_feature([6000], True) is None

    def test_nothing_known_makes_no_claim(self):
        assert resolve_main_feature([None, None, None, None], True) is None


class TestTheReportedDisc:
    """Sixteen titles: the film, its commentary version, fourteen featurettes."""

    DURATIONS = [4920, 4920] + [180 + 30 * i for i in range(14)]

    def test_the_extras_no_longer_become_parts_of_the_film(self):
        """With 'main feature only' off, the extras are extras — not pt2…pt16,
        which Plex stacks onto the end of the film."""
        main = resolve_main_feature(self.DURATIONS, False)
        assert main == 0
        job = types.SimpleNamespace(
            title="Dinosaur", year=2000, content_type="movie",
            series_season=None, series_first_episode=None,
        )
        plan = plan_output(job, len(self.DURATIONS), main_index=main)
        assert plan.filenames[0] == "Dinosaur (2000)"
        assert all(name.startswith(EXTRAS_FOLDER + "/") for name in plan.filenames[1:])
        assert not any(" - pt" in name for name in plan.filenames)

    def test_main_feature_only_encodes_the_film_and_nothing_else(self):
        files = [f"title{i:02d}.mkv" for i in range(len(self.DURATIONS))]
        main = resolve_main_feature(self.DURATIONS, True)
        kept, lengths, reduced = only_the_feature(files, self.DURATIONS, main)
        assert kept == ["title00.mkv"]
        assert lengths == [4920]
        assert reduced is None


class TestOnlyTheFeature:
    def test_nothing_is_dropped_without_a_feature(self):
        files, lengths = ["a.mkv", "b.mkv"], [3000, 3000]
        assert only_the_feature(files, lengths, None) == (files, lengths, None)

    def test_a_lone_file_is_left_alone(self):
        assert only_the_feature(["a.mkv"], [3000], 0) == (["a.mkv"], [3000], 0)

    def test_an_out_of_range_index_changes_nothing(self):
        files, lengths = ["a.mkv", "b.mkv"], [3000, 300]
        assert only_the_feature(files, lengths, 7) == (files, lengths, 7)

    def test_the_caller_s_lists_are_not_mutated(self):
        """The raw files stay on disk, and so does the record of them."""
        files, lengths = ["a.mkv", "b.mkv", "c.mkv"], [6000, 300, 200]
        only_the_feature(files, lengths, 0)
        assert files == ["a.mkv", "b.mkv", "c.mkv"]
        assert lengths == [6000, 300, 200]


# ------------------------------------------------------------------ #
# The scan that came back empty
# ------------------------------------------------------------------ #

def _ripper(tmp_path):
    config = types.SimpleNamespace(
        makemkv_path=str(tmp_path / "makemkvcon"),
        min_title_length=120,
        raw_path=tmp_path,
    )
    (tmp_path / "makemkvcon").write_text("#!/bin/sh\nexit 0\n")
    return MakeMKVRipper(config)


def _stub_run(monkeypatch, outputs):
    """Feed scan_disc a canned stdout per attempt, and count the attempts."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        text = outputs[min(len(calls) - 1, len(outputs) - 1)]
        return subprocess.CompletedProcess(cmd, 0, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("adr.ripper.time.sleep", lambda _s: None)
    return calls


_ONE_TITLE = (
    'TINFO:0,2,0,"Dinosaur"\n'
    'TINFO:0,9,0,"1:22:00"\n'
    'TINFO:0,27,0,"title00.mkv"\n'
)


class TestScanDisc:
    def test_a_good_scan_is_not_repeated(self, tmp_path, monkeypatch):
        calls = _stub_run(monkeypatch, [_ONE_TITLE])
        titles = _ripper(tmp_path).scan_disc("/dev/sr0")
        assert list(titles) == [0]
        assert len(calls) == 1

    def test_an_empty_scan_is_tried_once_more(self, tmp_path, monkeypatch):
        """A drive that has only just been given a disc answers the first
        `info` with nothing often enough to be worth five seconds."""
        calls = _stub_run(monkeypatch, ["", _ONE_TITLE])
        ripper = _ripper(tmp_path)
        assert list(ripper.scan_disc("/dev/sr0")) == [0]
        assert len(calls) == 2
        assert ripper.last_scan_error == ""

    def test_two_empty_scans_give_up_and_say_why(self, tmp_path, monkeypatch):
        calls = _stub_run(monkeypatch, ['MSG:5010,0,1,"Failed to open disc","x"\n'])
        ripper = _ripper(tmp_path)
        assert ripper.scan_disc("/dev/sr0") == {}
        assert len(calls) == 2
        assert "Failed to open disc" in ripper.last_scan_error

    def test_makemkvs_own_words_reach_the_job_log(self, tmp_path, monkeypatch):
        """The one thing that explains why the whole disc got ripped."""
        _stub_run(monkeypatch, ['MSG:5010,0,1,"Failed to open disc","x"\n'])
        seen = []
        ripper = _ripper(tmp_path)
        ripper.log_sink = seen.append
        ripper.scan_disc("/dev/sr0")
        assert any("Failed to open disc" in line for line in seen)

    def test_chatter_stays_out_of_the_job_log(self, tmp_path, monkeypatch):
        """Codes below 2000 are progress, not problems."""
        _stub_run(monkeypatch, ['MSG:1005,0,1,"Opening files on harddrive","x"\n'])
        seen = []
        ripper = _ripper(tmp_path)
        ripper.log_sink = seen.append
        ripper.scan_disc("/dev/sr0")
        assert seen == []

    def test_a_timeout_is_reported_rather_than_swallowed(self, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 300)

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr("adr.ripper.time.sleep", lambda _s: None)
        ripper = _ripper(tmp_path)
        assert ripper.scan_disc("/dev/sr0") == {}
        assert "gave up" in ripper.last_scan_error

    def test_a_later_good_scan_clears_the_previous_reason(self, tmp_path, monkeypatch):
        ripper = _ripper(tmp_path)
        _stub_run(monkeypatch, [""])
        ripper.scan_disc("/dev/sr0")
        assert ripper.last_scan_error
        _stub_run(monkeypatch, [_ONE_TITLE])
        ripper.scan_disc("/dev/sr0")
        assert ripper.last_scan_error == ""
