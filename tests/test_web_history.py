"""History is paged and filtered on the server.

It used to fetch every job ever run and let the template ask each row for its
tracks — one query per row. A machine that has worked through a shelf of discs
has thousands of rows, so the page got slower every week and the browser-side
status filter could only hide rows that had already been sent.
"""

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
