"""Every failure answers in one shape.

The API grew two conventions. Some routes reported a failure as
``{"error": …}`` and some as ``{"ok": false, "message": …}``, and each button
in the front-end read whichever its author knew about. Where the two
disagreed, the person got "unknown error" on screen while the real reason sat
in the response, in a key nobody was reading.

Redundant on the wire, free at the point of use, and the right trade for a
message whose entire job is to be read.
"""

import re
from pathlib import Path

import pytest

from adr.config import Config
from adr.models import init_db
from web.app import create_app, fail


@pytest.fixture
def client(tmp_path):
    for name in ("raw", "completed", "staging"):
        (tmp_path / name).mkdir()
    config = Config(str(tmp_path / "adr.yaml"))
    config.update({
        "completed_path": str(tmp_path / "completed"),
        "raw_path": str(tmp_path / "raw"),
        "staging_path": str(tmp_path / "staging"),
    })
    init_db()
    return create_app(config).test_client()


class TestTheHelper:
    def test_it_carries_all_three_keys(self):
        from flask import Flask

        with Flask(__name__).app_context():
            body, status = fail("the disc is upside down")
            payload = body.get_json()
        assert payload["ok"] is False
        assert payload["error"] == "the disc is upside down"
        assert payload["message"] == payload["error"]
        assert status == 400

    def test_the_status_can_be_chosen(self):
        from flask import Flask

        with Flask(__name__).app_context():
            _, status = fail("gone", 404)
        assert status == 404


class TestRealFailures:
    """Routes that can be made to fail without a disc, a GPU or a NAS."""

    CASES = [
        ("get", "/api/jobs/999999", None, 404),
        ("post", "/api/jobs/999999/cancel", None, 404),
        ("delete", "/api/jobs/999999", None, 404),
        ("post", "/api/jobs/delete", {"ids": []}, 400),
        ("post", "/api/settings", {"not_a_setting": 1}, 400),
    ]

    @pytest.mark.parametrize("method,path,body,status", CASES)
    def test_the_status_is_what_it_should_be(self, client, method, path, body, status):
        response = getattr(client, method)(path, json=body)
        assert response.status_code == status

    @pytest.mark.parametrize("method,path,body,status", CASES)
    def test_every_failure_says_why_under_both_names(
        self, client, method, path, body, status,
    ):
        payload = getattr(client, method)(path, json=body).get_json()
        assert payload.get("ok") is False, path
        assert payload.get("error"), f"{path} sent no error"
        assert payload.get("message") == payload.get("error"), path

    @pytest.mark.parametrize("method,path,body,status", CASES)
    def test_the_reason_is_a_sentence_not_a_code(self, client, method, path, body, status):
        message = getattr(client, method)(path, json=body).get_json()["error"]
        assert len(message) > 5, f"{path}: {message!r}"
        assert message.strip()[0].isupper() or message.startswith("'"), message


class TestNothingSlipsBack:
    def test_no_route_answers_with_a_bare_error_key(self):
        """The old shape. jsonify({"error": …}) has no ok and no message, so
        half the front-end reads nothing from it."""
        source = Path("web/app.py").read_text()
        assert 'jsonify({"error"' not in source

    def test_no_route_answers_with_ok_false_and_no_error(self):
        source = Path("web/app.py").read_text()
        for block in re.findall(r'jsonify\(\{[^}]*"ok": False[^}]*\}\)', source, re.S):
            assert '"error"' in block, block[:120]


class TestTheFrontEndReadsItProperly:
    """The other half of the same bug. The server sending both keys is only
    useful if the browser stops reading whichever one its author knew about."""

    SOURCES = [Path("web/static/js/app.js"), *sorted(Path("web/templates").glob("*.html"))]

    @pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
    def test_nothing_reads_one_key_and_gives_up(self, source):
        """`data.error || 'Unknown error'` shows "Unknown error" for every
        route that answered under the other name — with the real reason sat
        in the response the whole time."""
        text = re.sub(r"//.*", "", source.read_text())
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        # The helper itself is the one place allowed to read the keys.
        text = text.replace("return payload.error || payload.message || fallback;", "")
        offenders = re.findall(r"\w+\.(?:error|message)\s*\|\|\s*['\"]", text)
        assert not offenders, f"{source.name}: {offenders}"

    def test_the_helper_reads_both(self):
        text = Path("web/static/js/app.js").read_text()
        body = text.split("function reasonFrom")[1].split("}")[0]
        assert "payload.error" in body and "payload.message" in body

    def test_the_helper_has_something_to_say_when_both_are_missing(self):
        text = Path("web/static/js/app.js").read_text()
        signature = text.split("function reasonFrom(")[1].split(")")[0]
        assert "fallback =" in signature, "a bare 'undefined' on screen helps nobody"
