"""Audiobooks: discs or a folder of files, joined into one M4B with chapters."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from adr import audiobook
from adr.config import Config

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(yaml.safe_dump({
        "raw_path": str(tmp_path / "raw"),
        "completed_path": str(tmp_path / "completed"),
        "staging_path": str(tmp_path / "staging"),
        "ffmpeg_path": shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
        "notify_enabled": False,
    }))
    (tmp_path / "completed").mkdir()
    return Config(str(path))


def _tone(path: Path, seconds: float, title: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if title:
        cmd += ["-metadata", f"title={title}"]
    subprocess.run(cmd + [str(path)], check=True)
    return path


def _chapters(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters",
         "-show_entries", "format_tags", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


class TestTheMode:
    def _rip(self, config, position=None):
        number = audiobook.claim_disc(config, position)
        (audiobook.disc_part_dir(config, number) / "01 - Track 01.flac").write_bytes(b"x")
        audiobook.disc_done(config, number)
        return number

    def test_discs_are_numbered_by_their_place_in_the_box(self, config):
        audiobook.start(config, "Astrid Lindgren", "Bröderna Lejonhjärta", discs_total=3)
        assert self._rip(config, 2) == 2
        assert self._rip(config, 1) == 1
        assert not audiobook.complete(config)
        assert self._rip(config, 3) == 3
        assert audiobook.complete(config)

    def test_without_a_position_the_next_free_number_is_taken(self, config):
        audiobook.start(config, "", "A Book")
        assert [self._rip(config) for _ in range(3)] == [1, 2, 3]
        assert not audiobook.complete(config), "nobody said how many discs there are"

    def test_a_disc_still_ripping_is_not_done(self, config):
        """The last disc to *start* is not the last to finish."""
        audiobook.start(config, "", "Book", discs_total=2)
        self._rip(config, 1)
        audiobook.claim_disc(config, 2)
        assert not audiobook.complete(config)
        with pytest.raises(ValueError, match="still ripping"):
            audiobook.finish(config)

    def test_two_drives_never_share_a_number(self, config):
        audiobook.start(config, "", "Book")
        first = audiobook.claim_disc(config, 2)
        second = audiobook.claim_disc(config, 2)
        assert first != second

    def test_a_failed_rerip_leaves_the_good_disc_alone(self, config):
        audiobook.start(config, "", "Book")
        self._rip(config, 1)
        audiobook.claim_disc(config, 1)            # the same disc, in again
        audiobook.release_disc(config, 1)          # ... and it failed
        assert (audiobook.disc_dir(config, 1) / "01 - Track 01.flac").exists()
        assert audiobook.state(config)["discs_done"] == [1]

    def test_a_title_is_required(self, config):
        with pytest.raises(ValueError):
            audiobook.start(config, "Someone", "  ")

    def test_only_one_of_two_drives_starts_the_book(self, config):
        assert audiobook.ensure_started(config, author="A", title="Book")
        book = audiobook.state(config)["id"]
        assert not audiobook.ensure_started(config, author="A", title="Book")
        assert audiobook.state(config)["id"] == book

    def test_cancelling_throws_the_discs_away(self, config):
        audiobook.start(config, "A", "B")
        self._rip(config)
        work = audiobook.work_dir(config)
        audiobook.stop(config, discard=True)
        assert not audiobook.is_active(config)
        assert not work.exists()

    def test_the_library_defaults_beside_the_films(self, config):
        assert audiobook.library_root(config) == Path(config.completed_path) / "Audiobooks"

    def test_an_author_cannot_climb_out_of_the_library(self, config):
        assert audiobook.book_dir(config, "..", "..").parent.parent == audiobook.library_root(config)


class TestChapters:
    def test_a_track_title_is_used_when_it_has_one(self):
        assert audiobook.chapter_title(Path("disc01/03 - Kapitel 3.flac"), {}, 2) == "Kapitel 3"

    def test_a_generic_track_says_which_disc(self):
        assert audiobook.chapter_title(
            Path("work/disc02/07 - Track 07.flac"), {}, 3) == "Disc 2, track 07"

    def test_tags_win_over_the_filename(self):
        assert audiobook.chapter_title(Path("01.mp3"), {"title": "Prologue"}, 1) == "Prologue"

    def test_files_sort_like_people_number_them(self, tmp_path):
        for name in ("CD2/1.mp3", "CD1/10.mp3", "CD1/2.mp3", "CD10/1.mp3", "notes.txt"):
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_bytes(b"x")
        order = [str(p.relative_to(tmp_path)) for p in audiobook.collect_files(tmp_path)]
        assert order == ["CD1/2.mp3", "CD1/10.mp3", "CD2/1.mp3", "CD10/1.mp3"]


@needs_ffmpeg
class TestBuilding:
    def test_files_become_one_m4b_with_a_chapter_each(self, config, tmp_path):
        files = [_tone(tmp_path / "src" / f"{i:02d} - Part {i}.mp3", 1.0) for i in (1, 2, 3)]
        destination = tmp_path / "lib" / "Book" / "Book.m4b"

        ok, written = audiobook.build_m4b(
            config, files,
            {"title": "Book", "author": "Author", "narrator": "Reader", "year": 2001},
            destination)

        assert ok, written
        info = _chapters(Path(written))
        assert [c["tags"]["title"] for c in info["chapters"]] == ["Part 1", "Part 2", "Part 3"]
        tags = {k.lower(): v for k, v in info["format"]["tags"].items()}
        assert tags["artist"] == "Author"
        assert tags["composer"] == "Reader", "Audiobookshelf reads the narrator from composer"
        assert not list(destination.parent.glob("*.part"))
        assert not list(Path(config.staging_path).glob("audiobook-*")), "scratch is cleared"

    def test_an_existing_book_is_not_overwritten(self, config, tmp_path):
        files = [_tone(tmp_path / "a.mp3", 0.5)]
        destination = tmp_path / "lib" / "Book.m4b"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"somebody's book")

        ok, written = audiobook.build_m4b(config, files, {"title": "Book"}, destination)

        assert ok
        assert written.endswith("Book (2).m4b")
        assert destination.read_bytes() == b"somebody's book"

    def test_a_folder_is_named_from_its_tags_or_its_name(self, config, tmp_path):
        folder = tmp_path / "Selma Lagerlöf - Nils Holgersson"
        files = [_tone(folder / "01.mp3", 0.5)]
        meta = audiobook.guess_meta(config, folder, files)
        assert meta["author"] == "Selma Lagerlöf"
        assert meta["title"] == "Nils Holgersson"


def test_musicbrainz_says_which_disc_of_an_audiobook_this_is():
    from adr.musicbrainz import _parse

    payload = {"releases": [{
        "id": "rel-1", "title": "Mio, min Mio", "date": "2004",
        "artist-credit": [{"name": "Astrid Lindgren"}],
        "release-group": {"secondary-types": ["Audiobook"]},
        "media": [
            {"position": 1, "discs": [{"id": "disc-a"}], "tracks": []},
            {"position": 2, "discs": [{"id": "disc-b"}], "tracks": []},
            {"position": 3, "discs": [{"id": "disc-c"}], "tracks": []},
        ],
    }]}
    info = _parse(payload, "disc-b")
    assert info.is_audiobook
    assert (info.disc_position, info.disc_count) == (2, 3)
    assert info.release_id == "rel-1"


def test_a_music_album_is_not_an_audiobook():
    from adr.musicbrainz import _parse

    info = _parse({"releases": [{"title": "Abbey Road", "media": []}]}, "x")
    assert not info.is_audiobook


class TestTheApi:
    @pytest.fixture
    def http(self, config):
        from web.app import create_app

        app = create_app(config)
        app.config["TESTING"] = True
        return app.test_client()

    def test_start_and_cancel(self, http, config):
        state = http.post("/api/audiobook", json={
            "active": True, "title": "Mio, min Mio", "author": "Astrid Lindgren",
            "discs_total": "3"}).get_json()
        assert state["active"] and state["discs_total"] == 3
        assert http.post("/api/audiobook", json={"active": False, "discard": True}
                         ).get_json()["active"] is False

    def test_finishing_with_no_discs_says_so(self, http, config):
        http.post("/api/audiobook", json={"active": True, "title": "Empty"})
        response = http.post("/api/audiobook/finish")
        assert response.status_code == 400
        assert "No disc" in response.get_json()["error"]

    def test_a_folder_outside_the_share_is_refused(self, http):
        response = http.post("/api/audiobook/import", json={"path": "/etc"})
        assert response.status_code == 400

    def test_the_banner_shows_on_every_page(self, http):
        http.post("/api/audiobook", json={"active": True, "title": "Mio, min Mio"})
        page = http.get("/settings").get_data(as_text=True)
        assert "Audiobook mode" in page and "Mio, min Mio" in page


def _book_with_a_disc(config):
    from adr.models import init_db
    init_db()
    audiobook.start(config, "A", "Book", discs_total=1)
    number = audiobook.claim_disc(config, 1)
    (audiobook.disc_part_dir(config, number) / "01.flac").write_bytes(b"x")
    audiobook.disc_done(config, number)


def test_two_drives_finishing_together_build_the_book_once(config, monkeypatch):
    started = []
    monkeypatch.setattr(audiobook, "_start", lambda *a: started.append(a))
    _book_with_a_disc(config)

    audiobook.finish(config)
    with pytest.raises(ValueError):
        audiobook.finish(config)
    assert len(started) == 1


def test_a_book_started_during_a_build_is_left_alone(config, monkeypatch):
    held = []
    monkeypatch.setattr(audiobook, "_start", lambda target, *args: held.append((target, args)))
    monkeypatch.setattr(audiobook, "build_m4b", lambda *a, **k: (True, "/lib/Book.m4b"))
    monkeypatch.setattr("adr.notify.Notifier.job_done", lambda *a, **k: True)
    _book_with_a_disc(config)
    audiobook.finish(config)
    audiobook.start(config, "B", "Next Book")
    target, args = held[0]

    target(*args)                                  # the first build ends

    assert audiobook.state(config)["title"] == "Next Book"
    assert audiobook.is_active(config)


def test_a_failed_build_hands_the_book_back(config, monkeypatch):
    monkeypatch.setattr(audiobook, "build_m4b", lambda *a, **k: (False, "ffmpeg said no"))
    monkeypatch.setattr("adr.notify.Notifier.job_failed", lambda *a, **k: True)
    monkeypatch.setattr(audiobook, "_start", lambda target, *args: target(*args))
    _book_with_a_disc(config)

    audiobook.finish(config)

    state = audiobook.state(config)
    assert state["active"] and state["title"] == "Book", "Finish book is there again"
    assert state["discs_done"] == [1]
    assert audiobook.collect_files(audiobook.work_dir(config))


def test_a_build_a_restart_interrupted_starts_again(config, monkeypatch):
    held = []
    monkeypatch.setattr(audiobook, "_start", lambda target, *args: held.append(args))
    _book_with_a_disc(config)
    audiobook.finish(config)
    held.clear()                                   # ... and the service went down

    assert len(audiobook.resume_builds(config)) == 1
    files = held[0][2]
    assert [f.name for f in files] == ["01.flac"]
