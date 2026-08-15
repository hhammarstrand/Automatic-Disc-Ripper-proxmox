"""Tests for the Storage and drive-health API endpoints.

These pin down two judgements the UI makes, both of which are easy to get
subtly wrong:

* keeping films on the container's own disk is a valid setup, not a warning —
  a banner that fires on a correct configuration teaches people to ignore
  banners;
* a disc reported by sysfs does not mean the container can open the drive,
  because inside an LXC /sys is the *host's* sysfs.
"""

import os
import pathlib

import pytest
import yaml

from adr.config import Config
from web.app import create_app


def _make_config(tmp_path, **overrides):
    """A Config whose paths all live under tmp_path."""
    data = {
        "raw_path": str(tmp_path / "raw"),
        "completed_path": str(tmp_path / "completed"),
        "staging_path": str(tmp_path / "staging"),
        "plex_path": "",
        "watch_path": "",
        "watch_output_path": "",
    }
    data.update(overrides)
    path = tmp_path / "adr.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return Config(path)


def _client(config):
    app = create_app(config)
    app.config["TESTING"] = True
    return app.test_client()


class TestStorageWarnings:
    def test_local_only_setup_is_not_a_warning(self, tmp_path):
        """No NAS configured: a plain local directory is exactly right."""
        config = _make_config(tmp_path, require_completed_mount=False)
        data = _client(config).get("/api/storage").get_json()

        assert data["warnings"] == [], (
            "a valid local-only install must not be flagged; "
            f"got {data['warnings']}"
        )
        assert data["paths"]["completed"]["is_mount"] is False
        assert data["require_mount"] is False

    def test_detached_share_is_a_warning(self, tmp_path):
        """require_completed_mount records 'the user attached storage'."""
        config = _make_config(tmp_path, require_completed_mount=True)
        data = _client(config).get("/api/storage").get_json()

        assert len(data["warnings"]) == 1
        message = data["warnings"][0]
        assert "on the container's own disk" in message
        assert "restart the container" in message.lower(), (
            "a bind-mount is captured at container start — the fix must be stated"
        )

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_unwritable_destination_is_always_a_warning(self, tmp_path):
        completed = tmp_path / "completed"
        completed.mkdir()
        completed.chmod(0o500)
        try:
            config = _make_config(tmp_path, require_completed_mount=False)
            data = _client(config).get("/api/storage").get_json()
            assert any("cannot write" in w for w in data["warnings"])
        finally:
            completed.chmod(0o700)

    def test_staging_is_reported_only_when_it_applies(self, tmp_path):
        """A local destination needs no staging — it would be a pointless copy."""
        config = _make_config(tmp_path, stage_locally=True)
        data = _client(config).get("/api/storage").get_json()
        assert data["staging"] is False
        assert "staging" not in data["paths"]


class TestEffectiveDestination:
    """Which path the page judges — the one films actually land in."""

    def test_plex_library_is_the_destination_when_auto_move_is_on(self, tmp_path):
        plex = tmp_path / "plex"
        config = _make_config(tmp_path, plex_path=str(plex), auto_move_to_plex=True)
        data = _client(config).get("/api/storage").get_json()

        assert data["destination_is_plex"] is True
        assert data["destination"] == str(plex)

    def test_completed_path_is_the_destination_without_a_library(self, tmp_path):
        config = _make_config(tmp_path)
        data = _client(config).get("/api/storage").get_json()

        assert data["destination_is_plex"] is False
        assert data["destination"] == str(tmp_path / "completed")

    def test_auto_move_off_leaves_completed_in_charge(self, tmp_path):
        config = _make_config(
            tmp_path, plex_path=str(tmp_path / "plex"), auto_move_to_plex=False,
        )
        data = _client(config).get("/api/storage").get_json()
        assert data["destination_is_plex"] is False

    def test_a_broken_library_is_warned_about_not_a_healthy_completed_path(self, tmp_path):
        """The trap: completed_path looks fine, but nothing is written there."""
        plex = tmp_path / "plex"
        plex.mkdir()
        config = _make_config(
            tmp_path, plex_path=str(plex), auto_move_to_plex=True,
            require_completed_mount=True,
        )
        data = _client(config).get("/api/storage").get_json()

        assert len(data["warnings"]) == 1
        assert str(plex) in data["warnings"][0], (
            "the warning must name the path that actually decides the outcome"
        )


class TestDriveHealth:
    def test_problems_are_surfaced(self, tmp_path, monkeypatch):
        import adr.disc as disc

        monkeypatch.setattr(
            disc, "diagnose_passthrough",
            lambda: {"drives": [], "problems": ["the drive is missing"], "ok": False},
        )
        data = _client(_make_config(tmp_path)).get("/api/drives/health").get_json()
        assert data["ok"] is False
        assert data["problems"] == ["the drive is missing"]

    def test_healthy_reports_ok(self, tmp_path, monkeypatch):
        import adr.disc as disc

        monkeypatch.setattr(
            disc, "diagnose_passthrough",
            lambda: {"drives": [{"device": "/dev/sr0"}], "problems": [], "ok": True},
        )
        data = _client(_make_config(tmp_path)).get("/api/drives/health").get_json()
        assert data["ok"] is True
        assert data["problems"] == []


class TestSeriesIdentification:
    """Correcting a TV disc that the movie search mis-identified.

    Identification runs TMDb's *movie* endpoint, which for a box set returns a
    confident-looking film. Naming a whole season after it is silently wrong,
    so the show is corrected against the TV namespace before encoding.
    """

    def _job(self, status=None):
        from adr.models import Job, JobStatus, Track, TrackStatus, get_session, init_db
        from adr.utils import utcnow

        init_db()
        session = get_session()
        job = Job(
            disc_label="THE_WIRE_S02_D3", title="The Wire (2008 film)", year=2008,
            drive="/dev/sr0", status=status or JobStatus.RIPPED,
            started_at=utcnow(), tmdb_id=999, poster_url="http://x/film.jpg",
        )
        session.add(job)
        session.commit()
        for i in range(3):
            session.add(Track(job_id=job.id, track_number=i + 1,
                              filename=f"t{i}.mkv", status=TrackStatus.PENDING))
        session.commit()
        job_id = job.id
        session.close()
        return job_id

    def test_the_show_replaces_the_film_it_was_mistaken_for(self, tmp_path):
        from adr.models import Job, get_session

        job_id = self._job()
        client = _client(_make_config(tmp_path))
        response = client.post(f"/api/jobs/{job_id}/content-type", json={
            "content_type": "series", "season": 2, "first_episode": 5,
            "show": "The Wire", "year": 2002, "tmdb_id": 1438,
        })
        assert response.status_code == 200

        session = get_session()
        job = session.get(Job, job_id)
        assert job.title == "The Wire"
        assert job.year == 2002
        assert job.tmdb_id == 1438
        assert job.poster_url is None, "the poster was the film's; it no longer applies"
        session.close()

    def test_the_preview_shows_what_the_files_will_be_called(self, tmp_path):
        job_id = self._job()
        data = _client(_make_config(tmp_path)).post(
            f"/api/jobs/{job_id}/content-type",
            json={"content_type": "series", "season": 2, "first_episode": 5,
                  "show": "The Wire", "year": 2002},
        ).get_json()
        assert data["preview"] == [
            "The Wire (2002) - S02E05",
            "The Wire (2002) - S02E06",
            "The Wire (2002) - S02E07",
        ]

    def test_omitting_the_show_leaves_the_existing_title_alone(self, tmp_path):
        from adr.models import Job, get_session

        job_id = self._job()
        _client(_make_config(tmp_path)).post(
            f"/api/jobs/{job_id}/content-type",
            json={"content_type": "series", "season": 1, "first_episode": 1},
        )
        session = get_session()
        assert session.get(Job, job_id).title == "The Wire (2008 film)"
        session.close()

    def test_it_is_refused_once_encoding_has_started(self, tmp_path):
        from adr.models import JobStatus

        job_id = self._job(status=JobStatus.ENCODING)
        response = _client(_make_config(tmp_path)).post(
            f"/api/jobs/{job_id}/content-type",
            json={"content_type": "series", "season": 1, "first_episode": 1},
        )
        assert response.status_code == 409
        assert "already started" in response.get_json()["error"]

    def test_marking_it_back_as_a_film_clears_the_season(self, tmp_path):
        from adr.models import Job, get_session

        job_id = self._job()
        client = _client(_make_config(tmp_path))
        client.post(f"/api/jobs/{job_id}/content-type",
                    json={"content_type": "series", "season": 2, "first_episode": 5})
        client.post(f"/api/jobs/{job_id}/content-type", json={"content_type": "movie"})

        session = get_session()
        job = session.get(Job, job_id)
        assert job.content_type == "movie"
        assert job.series_season is None
        assert job.series_first_episode is None
        session.close()

    def test_a_nonsense_content_type_is_refused(self, tmp_path):
        job_id = self._job()
        response = _client(_make_config(tmp_path)).post(
            f"/api/jobs/{job_id}/content-type", json={"content_type": "documentary"})
        assert response.status_code == 400


class TestASubfolderOfAMountIsNotTheContainerDisk:
    """A library inside the share is on the share, and must be labelled so.

    os.path.ismount is true only of the mount point itself, so a folder inside
    a mount fails it — which is why /mnt/media/Filmer was refused as a
    destination, and why the Storage page would have called it "container
    disk". Both are the same mistake: asking whether the path *is* a mount
    rather than which filesystem it is *on*.
    """

    @pytest.fixture
    def share(self):
        import shutil
        import uuid

        if not os.path.ismount("/dev/shm"):
            pytest.skip("no tmpfs at /dev/shm to stand in for the share")
        root = pathlib.Path("/dev/shm") / f"adr-web-{uuid.uuid4().hex[:8]}"
        (root / "Filmer").mkdir(parents=True)
        yield root
        shutil.rmtree(root, ignore_errors=True)

    def test_the_api_reports_it_as_being_on_its_own_filesystem(self, share):
        from adr.storage import describe_path

        info = describe_path(share / "Filmer")
        assert info["is_mount"] is False, "a folder inside a mount never is one"
        assert info["on_separate_filesystem"] is True

    def test_the_container_disk_is_still_reported_as_such(self, tmp_path):
        from adr.storage import describe_path

        assert describe_path(tmp_path)["on_separate_filesystem"] is False

    def test_a_library_inside_the_share_raises_no_warning(self, tmp_path, share):
        """The reported setup: share mounted at one path, library below it."""
        config = _make_config(
            tmp_path,
            completed_path=str(share),
            plex_path=str(share / "Filmer"),
            auto_move_to_plex=True,
            require_completed_mount=True,
        )
        data = _client(config).get("/api/storage").get_json()
        assert data["warnings"] == [], data["warnings"]

    def test_the_page_can_tell_the_two_apart(self, tmp_path, share):
        config = _make_config(
            tmp_path, completed_path=str(share), plex_path=str(share / "Filmer"),
        )
        paths = _client(config).get("/api/storage").get_json()["paths"]
        assert paths["plex"]["on_separate_filesystem"] is True
        assert paths["raw"]["on_separate_filesystem"] is False


class TestTheShowPosterReachesTheJob:
    """Naming a series by hand cleared the film's poster — correctly, it was a
    film's — and then stopped, so every hand-named series sat on the dashboard
    with no image at all while every film had one. The dialog has just been
    shown the show's own poster; it sends it back."""

    def _job(self):
        from adr.models import Job, JobStatus, get_session, init_db
        from adr.utils import utcnow

        init_db()
        session = get_session()
        job = Job(disc_label="THE_WIRE_S02_D3", title="The Wire (2008 film)",
                  year=2008, drive="/dev/sr0", status=JobStatus.RIPPED,
                  started_at=utcnow(), tmdb_id=999,
                  poster_url="https://image.tmdb.org/t/p/w300/film.jpg")
        session.add(job)
        session.commit()
        job_id = job.id
        session.close()
        return job_id

    def _post(self, tmp_path, job_id, poster):
        client = _client(_make_config(tmp_path))
        return client.post(f"/api/jobs/{job_id}/content-type", json={
            "content_type": "series", "season": 2, "first_episode": 5,
            "show": "The Wire", "year": 2002, "tmdb_id": 1438,
            "poster_url": poster,
        })

    def _stored(self, job_id):
        from adr.models import Job, get_session

        session = get_session()
        try:
            return session.get(Job, job_id).poster_url
        finally:
            session.close()

    def test_the_shows_own_poster_is_kept(self, tmp_path):
        job_id = self._job()
        wanted = "https://image.tmdb.org/t/p/w300/wire.jpg"
        assert self._post(tmp_path, job_id, wanted).status_code == 200
        assert self._stored(job_id) == wanted

    def test_a_url_from_anywhere_else_is_refused(self, tmp_path):
        """The dashboard renders this straight into an img src, so a stored
        URL is a request every later page load makes. A browser will send this
        POST on any page the owner happens to open."""
        job_id = self._job()
        assert self._post(
            tmp_path, job_id, "https://evil.example/pixel.gif").status_code == 200
        assert self._stored(job_id) is None, "a foreign host was stored"

    def test_the_films_poster_is_still_dropped_when_none_is_offered(self, tmp_path):
        """The old behaviour where there is nothing better: a film's cover
        does not describe a season."""
        job_id = self._job()
        assert self._post(tmp_path, job_id, None).status_code == 200
        assert self._stored(job_id) is None

    def test_a_newline_cannot_ride_along(self, tmp_path):
        job_id = self._job()
        self._post(tmp_path, job_id,
                   "https://image.tmdb.org/t/p/w300/a.jpg\nX-Evil: 1")
        assert self._stored(job_id) is None
