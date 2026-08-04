"""The pages have to hold together as documents and as programs.

Templates are not compiled and JavaScript is not linted by anything else here,
so a stray brace or an unbalanced div ships. Both have happened: a `</div>`
that put a card outside its tab, and a rewrite that wrote the characters
backslash-n instead of a newline and left a whole page's script unparseable.
Neither showed up in any other test, and a browser reports the first as
nothing at all.
"""

import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import pytest

from adr.config import Config
from web.app import create_app

TEMPLATES = sorted(Path("web/templates").glob("*.html"))
PAGES = ["/", "/history", "/settings", "/storage", "/doctor", "/logs"]

#: Tags that never close.
VOID = frozenset({
    "br", "hr", "img", "input", "link", "meta", "source",
    "area", "base", "col", "embed", "param", "track", "wbr",
})


@pytest.fixture
def client(tmp_path):
    """Function-scoped on purpose: conftest gives every test its own database
    file, and a module-scoped app would hold a connection to the first one
    while the rest of the tests were pointed somewhere else."""
    from adr.models import init_db

    root = tmp_path / "app"
    root.mkdir()
    for name in ("raw", "completed", "staging"):
        (root / name).mkdir()
    config = Config(str(root / "adr.yaml"))
    config.update({
        "completed_path": str(root / "completed"),
        "raw_path": str(root / "raw"),
        "staging_path": str(root / "staging"),
    })
    init_db()
    return create_app(config).test_client()


def _tag_errors(html: str) -> list[str]:
    problems: list[str] = []

    class Checker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack = []

        def handle_starttag(self, tag, attrs):
            if tag not in VOID:
                self.stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):
            if tag in VOID:
                return
            if not self.stack:
                problems.append(f"stray </{tag}> on line {self.getpos()[0]}")
                return
            if self.stack[-1][0] != tag:
                problems.append(
                    f"</{tag}> on line {self.getpos()[0]} closes <{self.stack[-1][0]}> "
                    f"opened on line {self.stack[-1][1]}")
            self.stack.pop()

    checker = Checker()
    checker.feed(html)
    problems += [f"<{tag}> never closed (line {line})" for tag, line in checker.stack]
    return problems


class TestEveryPageIsAWellFormedDocument:
    @pytest.mark.parametrize("path", PAGES)
    def test_the_tags_balance(self, client, path):
        assert _tag_errors(client.get(path).get_data(as_text=True)) == []

    @pytest.mark.parametrize("path", PAGES)
    def test_it_renders_at_all(self, client, path):
        assert client.get(path).status_code == 200


class TestEveryScriptParses:
    """Node is not a dependency of this application, so this skips without it
    rather than failing — but where it is available it is the only thing that
    reads the JavaScript before a browser does."""

    def _check(self, source: str) -> str:
        if not shutil.which("node"):
            pytest.skip("node is not installed")
        # Jinja expressions are not JavaScript. Replaced with a literal so the
        # surrounding syntax is still checked.
        source = re.sub(r"\{\{.*?\}\}", "0", source)
        source = re.sub(r"\{%.*?%\}", "", source)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(source)
            path = handle.name
        result = subprocess.run(  # noqa: S603
            ["node", "--check", path], capture_output=True, text=True)
        return "" if result.returncode == 0 else result.stderr

    def test_the_shared_script(self):
        assert self._check(Path("web/static/js/app.js").read_text()) == ""

    @pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
    def test_the_inline_scripts(self, template):
        for index, body in enumerate(
                re.findall(r"<script>(.*?)</script>", template.read_text(), re.S)):
            error = self._check(body)
            assert not error, f"{template.name} script #{index}:\n{error}"


class TestNoBrowserDialogsAreLeft:
    """alert() and confirm() block the page, look nothing like the
    application, and on a phone are a full-screen system dialog for "3 jobs
    removed". Every one was replaced; this stops them coming back one commit
    at a time."""

    @pytest.mark.parametrize(
        "source",
        [Path("web/static/js/app.js"), *TEMPLATES],
        ids=lambda p: p.name,
    )
    def test_nothing_calls_them(self, source):
        text = source.read_text()
        # Strip comments and the deliberate fallback inside confirmAction,
        # which is what happens on a page that has no modal markup.
        text = re.sub(r"//.*", "", text)
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        text = text.replace("window.confirm(", "")
        leftovers = re.findall(r"(?<![\w.])(alert|confirm)\s*\(", text)
        assert not leftovers, f"{source.name} still calls {set(leftovers)}"


class TestTheEmptyStates:
    def test_the_history_placeholder_spans_the_whole_table(self, client):
        """A colspan that does not match the column count leaves the "no jobs
        yet" message wedged into part of a row with empty cells beside it."""
        html = Path("web/templates/history.html").read_text()
        columns = html.split("<thead>")[1].split("</thead>")[0].count("<th")
        spans = {int(s) for s in re.findall(r'colspan="(\d+)"', html)}
        assert spans == {columns}
