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
    def test_discs_are_numbered_by_their_place_in_the_box(self, config):
        audiobook.start(config, "Astrid Lindgren", "Bröderna Lejonhjärta", discs_total=3)
        assert audiobook.claim_disc(config, 2) == 2
        assert audiobook.claim_disc(config, 1) == 1
        assert not audiobook.complete(config)
        assert audiobook.claim_disc(config, 3) == 3
        assert audiobook.complete(config)

    def test_without_a_position_the_next_free_number_is_taken(self, config):
        audiobook.start(config, "", "A Book")
        assert [audiobook.claim_disc(config) for _ in range(3)] == [1, 2, 3]
        assert not audiobook.complete(config), "nobody said how many discs there are"

    def test_a_disc_that_failed_can_go_in_again(self, config):
        audiobook.start(config, "", "A Book")
        number = audiobook.claim_disc(config)
        audiobook.disc_dir(config, number).mkdir(parents=True)
        audiobook.release_disc(config, number)
        assert audiobook.state(config)["discs_done"] == []
        assert not audiobook.disc_dir(config, number).exists()

    def test_a_title_is_required(self, config):
        with pytest.raises(ValueError):
            audiobook.start(config, "Someone", "  ")

    def test_cancelling_throws_the_discs_away(self, config):
        audiobook.start(config, "A", "B")
        audiobook.disc_dir(config, 1).mkdir(parents=True)
        audiobook.stop(config, discard=True)
        assert not audiobook.is_active(config)
        assert not audiobook.work_dir(config).exists()

    def test_the_library_defaults_beside_the_films(self, config):
        assert audiobook.library_root(config) == Path(config.completed_path) / "Audiobooks"


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
