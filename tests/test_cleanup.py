"""Deleting a job's files.

Removing a row from the history and removing a film from the library are two
different acts, and the second one has no undo. So this module is deliberately
narrow: it deletes what the job recorded producing and nothing else, it never
takes a directory that still holds something, and it can say what it would do
before it does it.

These tests are mostly about what must *survive* a delete.
"""

import types

import pytest

from adr import cleanup


@pytest.fixture
def config(tmp_path):
    for name in ("raw", "completed", "staging", "plex"):
        (tmp_path / name).mkdir()
    return types.SimpleNamespace(
        raw_path=tmp_path / "raw",
        completed_path=tmp_path / "completed",
        staging_path=tmp_path / "staging",
        plex_path=str(tmp_path / "plex"),
        tv_path="",
    )


def _job(job_id=1, output_path=None, plex_path=None, tracks=()):
    return types.SimpleNamespace(
        id=job_id,
        output_path=str(output_path) if output_path else None,
        plex_path=str(plex_path) if plex_path else None,
        tracks=[types.SimpleNamespace(output_path=str(p)) for p in tracks],
    )


class TestWhatItWouldRemove:
    def test_the_tracks_own_files(self, config, tmp_path):
        film = tmp_path / "completed" / "The Matrix (1999)"
        film.mkdir()
        movie = film / "The Matrix (1999).mp4"
        movie.write_bytes(b"X" * 1024)
        job = _job(tracks=[movie])
        assert cleanup.job_files(job, config) == [movie]

    def test_the_output_folder_when_the_tracks_say_nothing(self, config, tmp_path):
        """An older job, or one interrupted before its rows were written."""
        film = tmp_path / "completed" / "The Matrix (1999)"
        film.mkdir()
        (film / "The Matrix (1999).mp4").write_bytes(b"X")
        job = _job(output_path=film)
        assert [p.name for p in cleanup.job_files(job, config)] == ["The Matrix (1999).mp4"]

    def test_a_file_is_only_listed_once(self, config, tmp_path):
        film = tmp_path / "completed" / "The Matrix (1999)"
        film.mkdir()
        movie = film / "The Matrix (1999).mp4"
        movie.write_bytes(b"X")
        job = _job(output_path=film, tracks=[movie])
        assert cleanup.job_files(job, config) == [movie]

    def test_the_raw_rip_is_reported_separately(self, config, tmp_path):
        raw = tmp_path / "raw" / "1"
        raw.mkdir()
        (raw / "title_t00.mkv").write_bytes(b"M" * 2048)
        described = cleanup.describe(_job(), config)
        assert len(described["raw"]) == 1
        assert described["bytes"] == 2048

    def test_describing_deletes_nothing(self, config, tmp_path):
        raw = tmp_path / "raw" / "1"
        raw.mkdir()
        source = raw / "title_t00.mkv"
        source.write_bytes(b"M")
        cleanup.describe(_job(), config)
        assert source.exists(), "a preview that deletes is not a preview"

    def test_a_job_with_nothing_on_disk(self, config):
        described = cleanup.describe(_job(), config)
        assert described["files"] == [] and described["raw"] == []
        assert described["bytes"] == 0

    @pytest.mark.parametrize("count,expected", [
        (0, "0 B"), (900, "900 B"), (4096, "4.0 KB"),
        (5 * 1024 * 1024, "5.0 MB"), (4_800_000_000, "4.47 GB"),
    ])
    def test_a_size_someone_can_judge_at_a_glance(self, count, expected):
        """Megabytes throughout makes a film read as "4823.7 MB" and a stray
        subtitle as "0.0 MB" — the first is hard to weigh and the second looks
        like nothing worth keeping, in a delete confirmation."""
        assert cleanup.human_size(count) == expected


class TestWhatMustSurvive:
    def test_a_file_this_job_did_not_produce(self, config, tmp_path):
        """The one that matters. A library folder shared with other films
        comes out of a delete with those films intact."""
        shared = tmp_path / "completed"
        mine = shared / "Mine.mp4"
        mine.write_bytes(b"X")
        someone_elses = shared / "Someone Else's Film.mp4"
        someone_elses.write_bytes(b"X")

        cleanup.delete_job_files(_job(tracks=[mine]), config)
        assert not mine.exists()
        assert someone_elses.exists(), "only what the job produced"

    def test_the_library_folder_itself(self, config, tmp_path):
        """Emptying a configured root is not a reason to remove it."""
        movie = tmp_path / "completed" / "Only.mp4"
        movie.write_bytes(b"X")
        cleanup.delete_job_files(_job(tracks=[movie]), config)
        assert (tmp_path / "completed").is_dir()

    def test_a_folder_that_still_holds_something(self, config, tmp_path):
        film = tmp_path / "completed" / "The Matrix (1999)"
        film.mkdir()
        movie = film / "The Matrix (1999).mp4"
        movie.write_bytes(b"X")
        (film / "poster.jpg").write_bytes(b"X")
        cleanup.delete_job_files(_job(tracks=[movie]), config)
        assert film.is_dir(), "something else lives there"

    def test_the_films_own_empty_folder_is_taken_away(self, config, tmp_path):
        """Litter, once its film is gone."""
        film = tmp_path / "completed" / "The Matrix (1999)"
        film.mkdir()
        movie = film / "The Matrix (1999).mp4"
        movie.write_bytes(b"X")
        cleanup.delete_job_files(_job(tracks=[movie]), config)
        assert not film.exists()


class TestWhenItCannotDelete:
    def test_a_file_that_has_already_gone_is_not_an_error(self, config, tmp_path):
        movie = tmp_path / "completed" / "Gone.mp4"
        job = _job(tracks=[movie])
        result = cleanup.delete_job_files(job, config)
        assert result["deleted"] == [] and result["failed"] == []

    def test_a_failure_is_reported_rather_than_raised(self, config, tmp_path, monkeypatch):
        """A partial delete reported honestly is more use than an exception
        halfway through, which leaves the caller unable to say what happened."""
        first = tmp_path / "completed" / "a.mp4"
        second = tmp_path / "completed" / "b.mp4"
        for path in (first, second):
            path.write_bytes(b"X")

        real = type(first).unlink

        def stubborn(self, *args, **kwargs):
            if self.name == "a.mp4":
                raise OSError("read-only file system")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(type(first), "unlink", stubborn)
        result = cleanup.delete_job_files(_job(tracks=[first, second]), config)
        assert len(result["failed"]) == 1
        assert "read-only" in result["failed"][0]
        assert len(result["deleted"]) == 1, "the other one still went"


class TestDiscsThatAreNotFilms:
    """"Remove and delete the files" deleted nothing for an audio CD or a data
    disc, and said so in a sentence that was not true.

    Both create their Track rows without an output_path, so job_files fell
    through to finished_files(), which accepts .mp4 and .mkv — an album of
    FLACs matched nothing, and an ISO job's output_path is the image *file*,
    of which finished_files() is empty by definition. The dialog then said "No
    files were found for these 1 job(s) — they may already be gone", and the
    only way forward dropped the row that was the sole record of where three
    gigabytes had gone.
    """

    def _job(self, tracks, output_path=None):
        import types

        return types.SimpleNamespace(
            tracks=[types.SimpleNamespace(output_path=p) for p in tracks],
            output_path=str(output_path) if output_path else None,
            plex_path=None,
        )

    def _config(self, tmp_path):
        import types

        return types.SimpleNamespace(
            completed_path=tmp_path, raw_path=tmp_path / "raw",
            staging_path=tmp_path / "staging", plex_path="", tv_path="",
            music_path=str(tmp_path / "Music"),
            data_disc_path=str(tmp_path / "ISO"),
        )

    def test_an_album_of_flacs_is_listed(self, tmp_path):
        album = tmp_path / "Music" / "Artist" / "Album (1999)"
        album.mkdir(parents=True)
        paths = []
        for n in range(1, 4):
            track = album / f"0{n} - Song.flac"
            track.write_bytes(b"x" * 1024)
            paths.append(str(track))

        found = cleanup.job_files(self._job(paths, album), self._config(tmp_path))
        assert len(found) == 3, "an album was reported as no files at all"

    def test_an_iso_image_is_listed(self, tmp_path):
        iso_dir = tmp_path / "ISO"
        iso_dir.mkdir()
        image = iso_dir / "SOME_DISC.iso"
        image.write_bytes(b"x" * 4096)

        found = cleanup.job_files(self._job([str(image)], image), self._config(tmp_path))
        assert found == [image], "an ISO image was invisible to the delete preview"

    def test_the_music_root_is_never_removed(self, tmp_path):
        """It is a library root, like the film and television ones."""
        config = self._config(tmp_path)
        music = tmp_path / "Music"
        album = music / "Artist" / "Album (1999)"
        album.mkdir(parents=True)

        cleanup._remove_empty([album, album.parent, music], config)
        assert music.is_dir(), "the music library root was deleted"

    def test_the_iso_root_is_never_removed(self, tmp_path):
        config = self._config(tmp_path)
        iso_dir = tmp_path / "ISO"
        iso_dir.mkdir()

        cleanup._remove_empty([iso_dir], config)
        assert iso_dir.is_dir()

    def test_the_pipeline_records_the_path_on_both(self):
        """The fix is at the source: both Track rows now carry the path they
        wrote, so job_files' precise pass covers them and nothing has to be
        inferred from a directory listing."""
        import inspect

        from adr.pipeline import DrivePipeline

        for method in (DrivePipeline._run_audio_cd, DrivePipeline._run_data_disc):
            assert "output_path=" in inspect.getsource(method), method.__name__
