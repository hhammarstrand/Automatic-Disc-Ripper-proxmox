"""History is paged and filtered on the server.

It used to fetch every job ever run and let the template ask each row for its
tracks — one query per row. A machine that has worked through a shelf of discs
has thousands of rows, so the page got slower every week and the browser-side
status filter could only hide rows that had already been sent.
"""

from pathlib import Path

import pytest

from adr.config import Config
from adr.models import Job, JobStatus, Track, get_session, init_db
from web import app as app_module
from web.app import create_app


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "adr.yaml"
    path.write_text(
        f"raw_path: {tmp_path / 'raw'}\n"
        f"completed_path: {tmp_path / 'completed'}\n"
        f"staging_path: {tmp_path / 'staging'}\n",
    )
    init_db()
    app = create_app(Config(str(path)), pipeline_manager=None)
    app.config["TESTING"] = True
    return app.test_client()


def _jobs(count, status=JobStatus.DONE, title="Film"):
    session = get_session()
    try:
        for i in range(count):
            job = Job(drive="/dev/sr0", status=status, title=f"{title} {i}", year=2000 + i)
            session.add(job)
            session.commit()
            session.add(Track(job_id=job.id, track_number=1, filename=f"{i}.mp4"))
        session.commit()
    finally:
        session.close()


class TestPaging:
    def test_a_page_holds_at_most_the_page_size(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "HISTORY_PAGE_SIZE", 5)
        _jobs(12)
        html = client.get("/history").data.decode()
        assert html.count('data-status="done"') == 5
        assert "Page 1 of 3" in html

    def test_the_second_page_holds_different_jobs(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "HISTORY_PAGE_SIZE", 5)
        _jobs(12)
        first = client.get("/history?page=1").data.decode()
        second = client.get("/history?page=2").data.decode()
        assert first != second
        assert second.count('data-status="done"') == 5

    def test_the_last_page_holds_the_remainder(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "HISTORY_PAGE_SIZE", 5)
        _jobs(12)
        html = client.get("/history?page=3").data.decode()
        assert html.count('data-status="done"') == 2

    def test_a_page_past_the_end_shows_the_last_one(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "HISTORY_PAGE_SIZE", 5)
        _jobs(12)
        html = client.get("/history?page=99").data.decode()
        assert "Page 3 of 3" in html

    def test_a_nonsense_page_does_not_500(self, client):
        _jobs(3)
        assert client.get("/history?page=banana").status_code == 200
        assert client.get("/history?page=-4").status_code == 200

    def test_no_navigation_when_everything_fits(self, client):
        _jobs(3)
        html = client.get("/history").data.decode()
        assert "Page 1 of" not in html

    def test_an_empty_history_still_renders(self, client):
        response = client.get("/history")
        assert response.status_code == 200
        assert "No jobs recorded yet" in response.data.decode()


class TestFiltering:
    def test_a_status_filter_is_applied_by_the_database(self, client):
        _jobs(3, status=JobStatus.DONE)
        _jobs(2, status=JobStatus.ERROR, title="Broken")
        html = client.get("/history?status=error").data.decode()
        assert html.count('data-status="error"') == 2
        assert 'data-status="done"' not in html

    def test_the_total_reflects_the_filter(self, client):
        _jobs(3, status=JobStatus.DONE)
        _jobs(2, status=JobStatus.ERROR, title="Broken")
        assert "2 jobs" in client.get("/history?status=error").data.decode()

    def test_the_chosen_filter_stays_selected(self, client):
        _jobs(1, status=JobStatus.ERROR)
        html = client.get("/history?status=error").data.decode()
        assert '<option value="error" selected>' in html

    def test_an_unknown_status_shows_everything_rather_than_failing(self, client):
        _jobs(3)
        response = client.get("/history?status=exploded")
        assert response.status_code == 200
        assert response.data.decode().count('data-status="done"') == 3

    def test_the_filter_survives_paging(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "HISTORY_PAGE_SIZE", 2)
        _jobs(5, status=JobStatus.ERROR)
        html = client.get("/history?status=error&page=2").data.decode()
        assert "status=error" in html, "the next-page link dropped the filter"


class TestFailureIsReadable:
    """The reason a job failed belongs in the first column.

    The status column sits several columns to the right, which on a phone is
    off the side of the screen — so the one thing worth reading on this page
    was the one thing you could not see without scrolling sideways.
    """

    def _failed(self, message, status=JobStatus.ERROR):
        session = get_session()
        try:
            session.add(Job(drive="/dev/sr0", disc_label="HAPPY_FEET_TWO",
                            status=status, error_message=message))
            session.commit()
        finally:
            session.close()

    def test_the_reason_is_shown_without_scrolling(self, client):
        self._failed("Destination /mnt/media/Filmer is not writable by uid 8420.")
        html = client.get("/history").data.decode()
        assert "job-reason" in html
        assert "not writable" in html

    def test_a_cancelled_job_says_why_too(self, client):
        self._failed("Skipped as a duplicate.", status=JobStatus.CANCELLED)
        assert "Skipped as a duplicate." in client.get("/history").data.decode()

    def test_only_the_first_line_is_shown(self, client):
        """A pipeline error carries its whole traceback; the row must stay a row.

        The full text is still in the onclick, because the modal shows all of
        it — so this checks the text that is actually rendered, between the
        icon and the end of the element.
        """
        self._failed("The real reason\n\nTraceback (most recent call last):\n  File ...")
        html = client.get("/history").data.decode()
        after_icon = html.split("job-reason")[1].split("</i>")[1]
        visible = after_icon.split("</div>")[0].strip()
        assert visible == "The real reason"

    def test_a_successful_job_has_no_reason_line(self, client):
        _jobs(1, status=JobStatus.DONE)
        assert "job-reason" not in client.get("/history").data.decode()

    def test_a_job_with_no_message_has_no_reason_line(self, client):
        self._failed(None)
        assert "job-reason" not in client.get("/history").data.decode()


class TestThePhaseStrip:
    """A single bar says how far along the current step is, not which step.

    Sixty percent could be sixty percent of a rip with an encode still to come,
    or sixty percent of the last thing left to do. The strip shows the run.
    """

    def _card(self, client, status):
        session = get_session()
        try:
            session.add(Job(drive="/dev/sr0", status=status, title="The Film", year=1999))
            session.commit()
        finally:
            session.close()
        return client.get("/").data.decode()

    @pytest.mark.parametrize("status", [
        JobStatus.IDENTIFYING, JobStatus.RIPPING, JobStatus.RIPPED, JobStatus.ENCODING,
    ])
    def test_every_active_phase_renders(self, client, status):
        """Jinja indexes a phase list here; an unexpected status must not 500."""
        html = self._card(client, status)
        assert "job-phases" in html
        assert "Identify" in html and "Rip" in html and "Encode" in html

    def test_a_pending_job_does_not_break_it(self, client):
        # data-job-phases, not "job-phases": the latter also appears in the
        # class attribute, so it is two per card and counts nothing useful.
        assert self._card(client, JobStatus.PENDING).count("data-job-phases") == 1

    def test_earlier_steps_are_marked_done(self, client):
        """While encoding, the rip is behind you — and should look it."""
        html = self._card(client, JobStatus.ENCODING)
        # The strip holds only spans, so the first </div> after it closes it.
        strip = html.split("data-job-phases")[1].split("</div>")[0]
        assert "bg-success" in strip, "completed steps should read as completed"
        assert "Identify" in strip and "Rip" in strip

    def test_there_is_somewhere_for_the_tool_to_speak(self, client):
        assert "job-saying" in self._card(client, JobStatus.RIPPING)


class TestTheElapsedTimer:
    """The browser must not have to guess which zone a timestamp is in.

    Times are stored naive, in the container's zone. Sent without an offset,
    JavaScript reads a date-time with no zone as *its own* local time — so a
    container on UTC and a phone on CEST disagreed by two hours, and a job that
    had just started showed an elapsed time of 2:00:39.
    """

    def _running_job(self):
        session = get_session()
        try:
            session.add(Job(drive="/dev/sr0", status=JobStatus.RIPPING,
                            title="The Film", year=1999))
            session.commit()
        finally:
            session.close()

    def test_the_start_time_carries_an_offset(self, client):
        import re

        self._running_job()
        html = client.get("/").data.decode()
        match = re.search(r'data-start="([^"]+)"', html)
        assert match, "no start time was rendered"
        stamp = match.group(1)
        assert re.search(r"[+-]\d\d:\d\d$|Z$", stamp), (
            f"{stamp!r} has no timezone, so the browser will guess"
        )

    def test_a_job_without_a_start_time_renders_empty(self, client):
        session = get_session()
        try:
            job = Job(drive="/dev/sr0", status=JobStatus.RIPPING, title="X")
            session.add(job)
            session.commit()
            job.started_at = None
            session.commit()
        finally:
            session.close()
        assert client.get("/").status_code == 200

    def test_the_status_is_stated_once(self, client):
        """There used to be a status badge here as well as the phase strip.

        It caused its own bug — the refresh, looking for "the first badge on
        the card", rewrote the leading phase pill instead — which was fixed by
        giving it a class. The badge is gone now: the strip says which phase
        this is, the counters say how far, and a third statement of the same
        fact was the loudest thing on the card. This test is what is left of
        that one, and it fails if the duplicate ever comes back.
        """
        self._running_job()
        html = client.get("/").data.decode()
        assert "data-job-phases" in html, "the phase strip is the statement"
        assert "job-status-badge" not in html
        assert "job-status-badge" not in Path("web/static/js/app.js").read_text(), (
            "the refresh still writes a badge the page no longer has"
        )


class TestASeriesExtraCanActuallyBePlayed:
    """The file list returned an extra by its bare name — "Extra 1.mkv" — and
    the stream route joined that to the season folder, where it is not. Every
    extra offered a Play button that answered 404."""

    def _series_job(self, tmp_path):
        from adr.models import Job, Track, get_session

        season = tmp_path / "tv" / "Show (1964)" / "Season 01"
        (season / "Other").mkdir(parents=True)
        episode = season / "Show (1964) - S01E01.mkv"
        episode.write_bytes(b"x" * 2048)
        extra = season / "Other" / "Extra 1.mkv"
        extra.write_bytes(b"y" * 1024)

        session = get_session()
        job = Job(disc_label="SHOW", title="Show", year=1964, drive="/dev/sr0",
                  content_type="series", series_season=1,
                  output_path=str(season))
        session.add(job)
        session.commit()
        session.add(Track(job_id=job.id, track_number=1, filename="t0.mkv",
                          output_path=str(episode), episode_number=1))
        session.add(Track(job_id=job.id, track_number=2, filename="t1.mkv",
                          output_path=str(extra)))
        session.commit()
        job_id = job.id
        session.close()
        return job_id

    def test_the_extra_is_listed_with_its_folder(self, client, tmp_path):
        job_id = self._series_job(tmp_path)
        files = client.get(
            f"/api/jobs/{job_id}/files").get_json()["files"]
        names = [f["name"] for f in files]
        assert "Other/Extra 1.mkv" in names, names

    def test_the_extra_streams(self, client, tmp_path):
        job_id = self._series_job(tmp_path)
        response = client.get(
            f"/api/jobs/{job_id}/stream/Other/Extra 1.mkv")
        assert response.status_code == 200

    def test_an_episode_still_streams(self, client, tmp_path):
        job_id = self._series_job(tmp_path)
        response = client.get(
            f"/api/jobs/{job_id}/stream/Show (1964) - S01E01.mkv")
        assert response.status_code == 200

    def test_climbing_out_of_the_job_folder_is_refused(self, client, tmp_path):
        """The basename flattening was also the traversal guard, so replacing
        it has to keep that job."""
        job_id = self._series_job(tmp_path)
        response = client.get(
            f"/api/jobs/{job_id}/stream/../../../etc/passwd")
        assert response.status_code in (403, 404)
