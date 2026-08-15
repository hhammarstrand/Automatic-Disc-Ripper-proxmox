"""The watch folder had never been reviewed, and it showed.

Three findings from the first review to cover it: a same-named file silently
replaced the one being processed (POSIX rename overwrites), the crash-restore
path had the same clobber in reverse, and watch encodes never staged — so a
NAS output path had HandBrake writing across the network for the whole encode,
the very thing stage_locally exists to prevent.
"""

import queue
import types

import pytest

from adr.watcher import _PROCESSING_SUFFIX, MIN_FILE_AGE, FolderWatcher


@pytest.fixture
def watcher(tmp_path):
    config = types.SimpleNamespace(
        watch_path=str(tmp_path / "watch"),
        watch_output_path=str(tmp_path / "out"),
        completed_path=tmp_path / "out",
        staging_path=tmp_path / "staging",
        raw_path=tmp_path / "raw",
        stage_locally=True,
    )
    (tmp_path / "watch").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "staging").mkdir()
    return FolderWatcher(config, queue.Queue())


class TestASecondFileWithTheSameNameSurvives:
    def test_the_pickup_refuses_to_replace_an_in_flight_file(self, watcher, tmp_path):
        """Re-drops are likely, not exotic: the first file 'disappeared' from
        the folder with nothing visible to show for it yet."""
        watch = tmp_path / "watch"
        in_flight = watch / f"Movie.mkv{_PROCESSING_SUFFIX}"
        in_flight.write_bytes(b"the first film")
        newcomer = watch / "Movie.mkv"
        newcomer.write_bytes(b"the second film")

        watcher._process_file(newcomer)

        assert in_flight.read_bytes() == b"the first film", (
            "the in-flight file was replaced — one user file destroyed"
        )
        assert newcomer.exists(), "the new file must wait, not vanish"

    def test_restore_steps_aside_rather_than_clobbering(self, watcher, tmp_path):
        """The same overwrite in reverse: a file dropped after the crash must
        not be replaced by the older restored one."""
        watch = tmp_path / "watch"
        stale = watch / f"Movie.mkv{_PROCESSING_SUFFIX}"
        stale.write_bytes(b"from before the crash")
        newer = watch / "Movie.mkv"
        newer.write_bytes(b"dropped after the crash")

        watcher._restore_stale_processing_files(watch)

        assert newer.read_bytes() == b"dropped after the crash"
        assert (watch / "Movie (2).mkv").read_bytes() == b"from before the crash"


class TestWatchEncodesStageLikeEveryOtherEncode:
    def test_a_network_output_is_staged(self, watcher, monkeypatch, tmp_path):
        """The watcher predates staging and never learned it."""
        import adr.watcher as watcher_mod

        monkeypatch.setattr("adr.storage.should_stage", lambda dest, on: True)
        monkeypatch.setattr(watcher_mod, "get_session", _fake_session_factory())

        source = tmp_path / "watch" / "Film (1999).mkv"
        source.write_bytes(b"x" * 2048)
        watcher._process_file(source)

        task = watcher._encode_queue.get_nowait()
        assert task.final_dir is not None, (
            "no final_dir: HandBrake writes across the network for the whole encode"
        )
        assert str(tmp_path / "staging") in str(task.output_dir)

    def test_a_local_output_is_not(self, watcher, monkeypatch, tmp_path):
        import adr.watcher as watcher_mod

        monkeypatch.setattr("adr.storage.should_stage", lambda dest, on: False)
        monkeypatch.setattr(watcher_mod, "get_session", _fake_session_factory())

        source = tmp_path / "watch" / "Film (1999).mkv"
        source.write_bytes(b"x" * 2048)
        watcher._process_file(source)

        task = watcher._encode_queue.get_nowait()
        assert task.final_dir is None
        assert str(tmp_path / "out") in str(task.output_dir)


class TestStabilityMeansTheWriteClockStopped:
    def test_a_recently_modified_file_is_left_alone(self, watcher, tmp_path):
        """Size alone misses a preallocated file: a copy tool writing into a
        full-size file changes no size while it writes."""
        import os
        import time

        watch = tmp_path / "watch"
        busy = watch / "Copying.mkv"
        busy.write_bytes(b"x" * 4096)

        picked = []
        watcher._process_file = lambda p: picked.append(p)

        now = time.time()
        # Seen long ago with the same size — but written to a moment ago.
        watcher._file_sizes[str(busy)] = (4096, now - MIN_FILE_AGE - 5)
        os.utime(busy, (now, now))
        watcher._scan_once()
        assert picked == [], "a file still being written was picked up"

        # The write clock stops — now it is fair game.
        old = now - MIN_FILE_AGE - 5
        os.utime(busy, (old, old))
        watcher._scan_once()
        assert picked == [busy]


def _fake_session_factory():
    """A session whose add() hands out ids, enough for _process_file."""
    import itertools
    import types

    counter = itertools.count(1)

    def factory():
        def add(obj):
            obj.id = next(counter)

        return types.SimpleNamespace(
            add=add, commit=lambda: None, rollback=lambda: None,
            close=lambda: None,
        )

    return factory
