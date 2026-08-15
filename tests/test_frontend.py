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


class TestOnePageReadsLikeTheNext:
    """Written over months, the pages drifted apart: "Job History" beside
    "Service log", "Save Settings" beside "Send a test notification". None of
    it is a bug and all of it makes an application feel unfinished.
    """

    #: Words that keep their capitals wherever they land.
    PROPER = {
        "Plex", "MakeMKV", "HandBrake", "HandBrakeCLI", "TMDb", "TV", "GPU",
        "MP3", "URL", "ISO", "NAS", "GitHub", "Proxmox", "Doctor", "Audio",
        "CDs", "Settings", "Dashboard", "History", "Storage", "Logs",
        "Encoding", "Discs", "Library", "Integrations", "Advanced",
    }

    def _sentence_case_offenders(self, phrases):
        wrong = []
        for phrase in phrases:
            words = phrase.strip().split()
            for word in words[1:]:
                bare = word.strip("()/,.—-")
                if bare and bare[0].isupper() and bare not in self.PROPER:
                    wrong.append(phrase.strip())
        return sorted(set(wrong))

    @pytest.mark.parametrize("path", PAGES)
    def test_every_page_names_itself(self, client, path):
        """Otherwise the browser tab is the only thing saying where you are."""
        html = client.get(path).get_data(as_text=True)
        assert re.search(r"<h4[^>]*>", html), f"{path} has no heading"

    @pytest.mark.parametrize("path", PAGES)
    def test_the_headings_are_sentence_case(self, client, path):
        html = client.get(path).get_data(as_text=True)
        headings = re.findall(r"<h4[^>]*>(?:<i[^>]*></i>)?\s*([^<]+)", html)
        assert not self._sentence_case_offenders(headings)

    @pytest.mark.parametrize("path", PAGES)
    def test_the_buttons_are_sentence_case(self, client, path):
        html = client.get(path).get_data(as_text=True)
        labels = re.findall(
            r"<button[^>]*>\s*(?:<i[^>]*></i>)?\s*([A-Za-z][^<]{2,40}?)\s*<", html)
        assert not self._sentence_case_offenders(labels)

    def test_no_heading_merely_repeats_the_navigation(self, client):
        """"Job History" under a nav item called History is a line of text
        that tells you nothing you did not already know."""
        html = client.get("/history").get_data(as_text=True)
        heading = re.search(r"<h4[^>]*>(?:<i[^>]*></i>)?\s*([^<]+)", html).group(1)
        assert heading.strip() == "History"


class TestTheEmptyPagesStillHelp:
    """The first thing a new install shows is every empty state at once. They
    are the only instructions most people will read, and until this pass two
    of them named DVDs — in an application that also rips Blu-rays, audio CDs
    and data discs — while a third blamed a disconnected drive for what is
    almost always passthrough."""

    def test_the_dashboard_says_what_a_disc_will_do(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "Nothing running" in html
        for kind in ("Blu-ray", "audio CD", "data disc"):
            assert kind in html, f"the empty state never mentions a {kind}"

    def test_a_missing_drive_points_at_the_usual_cause(self, client):
        """"Make sure you have a DVD drive connected" is almost never it: the
        drive is connected and not passed into the container, and the fix is a
        command nobody guesses."""
        html = client.get("/").get_data(as_text=True)
        assert "adr-doctor --fix" in html
        assert "passed into" in html

    @pytest.mark.parametrize("path", PAGES)
    def test_no_page_promises_only_dvds(self, client, path):
        html = client.get(path).get_data(as_text=True)
        assert "DVD disc" not in html
        assert "DVD drive" not in html


class TestTheToastsSayWhatTheyMean:
    """The colour is what gets read, and a green toast carrying a failure is
    read as a success. Worth pinning: the first sweep across sixty-four call
    sites inferred each kind from the message, and inference is exactly what
    goes wrong quietly."""

    SOURCES = [Path("web/static/js/app.js"), *TEMPLATES]

    @pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
    def test_no_failure_is_dressed_as_a_success(self, source):
        text = re.sub(r"//.*", "", source.read_text())
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        wrong = [
            message for message, kind in re.findall(
                r"notify\(\s*['\"`]([^'\"`]{4,120})['\"`][^,]*,\s*'(\w+)'", text)
            if kind == "success" and re.search(
                r"could not|cannot|failed|error|unable|no such|invalid", message, re.I)
        ]
        assert not wrong, f"{source.name}: {wrong}"

    @staticmethod
    def _calls(text: str) -> list[str]:
        """Every notify(...) argument list, with nested parens intact.

        A regex cannot do this: `notify('x: ' + (reasonFrom(d)), 'danger')`
        ends at the first close paren and the kind falls off the end, so a
        naive pattern reports correct code as broken.
        """
        found, index = [], 0
        while True:
            start = text.find("notify(", index)
            if start == -1:
                return found
            if start and (text[start - 1].isalnum() or text[start - 1] in "_.$"):
                index = start + 7
                continue
            depth, cursor = 0, start + 6
            while cursor < len(text):
                if text[cursor] == "(":
                    depth += 1
                elif text[cursor] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                cursor += 1
            found.append(text[start + 7:cursor])
            index = cursor + 1

    @pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
    def test_every_toast_names_its_kind(self, source):
        """The default is 'info', which is never the right answer for a
        message about something that just happened."""
        text = re.sub(r"//.*", "", source.read_text())
        unlabelled = [
            call for call in self._calls(text)
            if "kind" not in call
            and not re.search(r"'(danger|success|warning|info)'", call)
        ]
        assert not unlabelled, f"{source.name}: {unlabelled}"

    def test_problems_stay_on_screen(self):
        """Five seconds is long enough for "saved" and not long enough for a
        list of files that could not be deleted."""
        body = Path("web/static/js/app.js").read_text()
        autohide = re.search(r"autohide:\s*(.+)", body).group(1)
        assert "'danger'" in autohide and "'warning'" in autohide


class TestValuesFromDataNeverBreakTheHandler:
    """Four separate places built JavaScript by string-concatenating data.

    A film called "Ocean's Eleven" was enough to break each of them: Jinja's
    |e emits &#39;, which the browser turns back into an apostrophe *before*
    the JS parser sees it, so the string literal ended early and the whole
    onclick was a syntax error. The button then did nothing, with an error
    only visible in the console.
    """

    TEMPLATES = ("web/templates/history.html", "web/templates/index.html")

    def test_no_quoted_escape_filter_survives_in_a_handler(self):
        for path in self.TEMPLATES:
            text = Path(path).read_text()
            assert "| e }}'" not in text, (
                f"{path}: a value is quoted into JS with |e again — use |tojson"
            )

    def test_the_handlers_use_tojson(self):
        for path in self.TEMPLATES:
            assert "| tojson }}" in Path(path).read_text()

    def test_the_show_search_builds_elements_not_markup(self):
        """JSON.stringify wraps its result in double quotes, which terminated
        the double-quoted attribute it was being written into."""
        source = Path("web/static/js/app.js").read_text()
        start = source.index("function searchSeriesShow")
        body = source[start:start + 3000]
        assert "onclick=" not in body, (
            "the show list builds inline handlers from data again"
        )
        assert "addEventListener('click'" in body

    def test_the_gpu_pairing_is_not_json_in_an_attribute(self):
        """escapeHtml does not escape the double quote, so JSON in a value
        attribute ended at its first key."""
        source = Path("web/templates/doctor.html").read_text()
        assert "escapeHtml(JSON.stringify(" not in source
        assert "hbGpuPairings[" in source

    def test_copying_works_without_a_secure_context(self):
        """navigator.clipboard is undefined over plain http, which is how this
        application is served by design — every copy button did nothing."""
        source = Path("web/static/js/app.js").read_text()
        assert "window.isSecureContext" in source
        assert "function copyToClipboard" in source
        assert "execCommand" in source

    def test_a_retry_refusal_is_not_reported_as_success(self):
        source = Path("web/static/js/app.js").read_text()
        start = source.index("function retryJob")
        body = source[start:start + 800]
        assert "notify(plan.reason, 'success')" not in body


class TestTojsonNeedsASingleQuotedAttribute:
    """The 1.21.0 fix replaced `'{{ x | e }}'` with `{{ x | tojson }}` and left
    the attribute double-quoted. tojson escapes `<`, `>`, `&` and `'` — but not
    the double quote, and its own output *starts* with one. So the attribute
    ended at `copyPath(` and every handler in history.html and index.html was a
    truncated syntax error for every job, not only ones with odd characters.

    Strictly worse than what it replaced. The fix for the fix is the quoting.
    """

    TEMPLATES = ("web/templates/history.html", "web/templates/index.html")

    def test_no_tojson_sits_in_a_double_quoted_attribute(self):
        for path in self.TEMPLATES:
            text = Path(path).read_text()
            for line in text.splitlines():
                if "tojson" not in line or "onclick" not in line:
                    continue
                assert 'onclick="' not in line, (
                    f"{path}: tojson inside a double-quoted attribute again:\n{line}"
                )

    def test_every_awkward_title_survives_a_render(self):
        """The characters that actually appear in film titles and paths, plus
        the ones that would be an injection if they got out."""
        from flask import Flask, render_template_string

        app = Flask(__name__)
        template = "<button onclick='copyPath({{ p | tojson }})'>x</button>"
        with app.test_request_context():
            for value in ("Ocean's Eleven (2001)",
                          'The "Burbs (1989)',
                          r"C:\temp\film",
                          "line one\nline two",
                          "</script><img src=x onerror=alert(1)>"):
                out = render_template_string(template, p=value)
                assert out.count("'") == 2, f"attribute broken by {value!r}: {out}"
                assert "</script>" not in out
                assert "onerror=" not in out.split("copyPath(")[0]

    def test_the_quality_warning_function_exists(self):
        """oninput named a function that was never added, so every keystroke in
        the field threw a ReferenceError and the warning never appeared."""
        text = Path("web/templates/settings.html").read_text()
        if "warnAboutQualityGap(this)" in text:
            assert "function warnAboutQualityGap" in text

    def test_every_copy_button_goes_through_the_shared_helper(self):
        """The plain-http guard landed in app.js only; the two buttons defined
        in page-local scripts kept calling navigator.clipboard directly."""
        for path in ("web/templates/doctor.html", "web/templates/storage.html"):
            text = Path(path).read_text()
            # Comments stripped: both files explain the guard at length, and an
            # explanation naming the API is not a call to it.
            code = "\n".join(
                line for line in text.splitlines()
                if not line.lstrip().startswith("//")
            )
            assert "navigator.clipboard" not in code, (
                f"{path}: a copy button bypasses the secure-context guard"
            )
            assert "copyToClipboard(" in code


class TestThePageFitsAPhone:
    """The reported failure: the app pans sideways on an iPhone and is hard
    to steer with a thumb.

    Verified for real with headless Chromium at 390px and 320px — layout
    viewport stayed at device width and nothing escaped it. These pin the
    structural half of that result, the part a template edit can regress:
    every wide table is either contained or carded, and the assets the layout
    depends on are served locally.
    """

    def test_the_viewport_is_declared(self):
        text = Path("web/templates/base.html").read_text()
        assert 'name="viewport"' in text
        assert "width=device-width" in text

    def test_every_table_is_contained_or_carded(self):
        """A bare <table> outside .table-responsive forces the layout viewport
        wide, which is exactly the sideways pan. The history table is exempt
        by name: below md it is display:block cards, and above md it sits in
        its own responsive wrapper."""
        for path in Path("web/templates").glob("*.html"):
            text = path.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                if "<table" not in line:
                    continue
                before = text[:text.index(line)]
                assert "table-responsive" in before[-600:] or "historyTable" in line, (
                    f"{path.name}:{i}: a table outside .table-responsive"
                )

    def test_the_history_table_cards_itself_on_phones(self):
        css = Path("web/static/css/style.css").read_text()
        assert "#historyTable tr" in css
        assert "attr(data-label)" in css
        text = Path("web/templates/history.html").read_text()
        assert 'data-label="Status"' in text
        assert 'data-label="Path"' in text

    def test_the_desktop_only_columns_are_marked(self):
        """Fourteen columns cannot card; the low-value ones are desktop-only.
        Bootstrap's d-none wins below md with !important, which is the lever
        the card transform stands on."""
        text = Path("web/templates/history.html").read_text()
        assert text.count("d-none d-md-table-cell") >= 8

    def test_sideways_panning_has_a_backstop(self):
        """clip, not hidden: hidden makes body a scroll container and quietly
        breaks position:sticky inside it."""
        css = Path("web/static/css/style.css").read_text()
        assert "overflow-x: clip" in css

    def test_tap_targets_grow_on_phones(self):
        css = Path("web/static/css/style.css").read_text()
        mobile = css[css.index("max-width: 767.98px"):]
        assert "min-height" in mobile and "min-width" in mobile

    def test_the_ui_does_not_need_the_internet(self):
        """The ripper keeps working when the internet is down; a dashboard
        that renders as bare HTML at exactly that moment reads as the whole
        appliance being broken. Every asset is served from /static."""
        text = Path("web/templates/base.html").read_text()
        assert "cdn.jsdelivr.net" not in text
        for line in text.splitlines():
            if "<link" in line or "<script src" in line:
                assert "https://" not in line, f"an external asset: {line.strip()}"
        for asset in ("web/static/vendor/bootstrap.min.css",
                      "web/static/vendor/bootstrap.bundle.min.js",
                      "web/static/vendor/bootstrap-icons.min.css",
                      "web/static/vendor/fonts/bootstrap-icons.woff2"):
            assert Path(asset).stat().st_size > 10_000, f"{asset} missing or truncated"

    def test_the_bulk_bar_wraps(self):
        """Five controls in one non-wrapping row is wider than any phone."""
        assert "flex-wrap" in Path("web/templates/history.html").read_text()

    def test_a_tap_is_not_treated_as_half_a_double_tap(self):
        """Without touch-action the browser holds every tap for 300ms in case
        a second one follows, and the whole application feels slow to answer
        on the device it is mostly used from."""
        css = Path("web/static/css/style.css").read_text()
        assert "touch-action: manipulation" in css

    def test_a_field_does_not_zoom_the_page_in_when_tapped(self):
        """iOS zooms in on any input under 16px and does not zoom back out, so
        one tap on the season number leaves the dashboard magnified and panned
        for the rest of the session."""
        css = Path("web/static/css/style.css").read_text()
        mobile = css[css.index("max-width: 767.98px"):]
        block = mobile[mobile.index(".form-control,"):]
        assert "font-size: 1rem" in block[:400]


class TestARefreshDoesNotInterruptTyping:
    """The dashboard reloads itself when the server's answer stops matching
    the page — a new job, a phase change, the preflight banner going stale.

    That is correct for a page nobody is touching and wrong at the moment it
    fires most often: inserting the next disc is what creates the new job, and
    it happens while the previous disc is being named in the series dialog. The
    reload took the half-typed show name with it. So the poller-driven reloads
    wait for the dialog to close; the ones the user asked for by pressing a
    button do not, because by then the dialog is gone and they are waiting for
    exactly that.
    """

    #: The four that fire from a timer rather than from a click.
    POLLERS = ["refreshDashboard", "updateActiveJobs", "checkPreflight"]

    @staticmethod
    def _body(name: str) -> str:
        source = Path("web/static/js/app.js").read_text()
        start = source.index(f"function {name}(")
        # To the next top-level function, which is where the body ends.
        end = source.find("\nfunction ", start + 1)
        return source[start:end if end != -1 else len(source)]

    def test_the_guard_exists(self):
        source = Path("web/static/js/app.js").read_text()
        assert "function uiIsBusy(" in source
        assert "function safeReload(" in source

    def test_it_rechecks_rather_than_remembering(self):
        """Busy ends without an event to listen for, so the pending reload has
        to keep asking. Caching the answer at scheduling time would reload the
        moment the interval fired, dialog or not."""
        body = self._body("safeReload")
        assert "setInterval" in body
        assert body.count("uiIsBusy()") >= 2

    def test_one_pending_reload_however_many_pollers_ask(self):
        """Three loops on a five-second tick would otherwise leave a queue of
        timers all racing to reload the same page."""
        body = self._body("safeReload")
        assert "_reloadPending" in body

    def test_it_reloads_at_once_when_nobody_is_doing_anything(self, guard):
        assert guard["idle"] == 1

    def test_an_open_dialog_holds_the_reload(self, guard):
        """The whole point. Disc 2 going into the drive is what creates the
        new job, and the dialog naming disc 1 is open when it happens."""
        assert guard["whileModalOpen"] == 0
        assert guard["whileModalOpenAfterTicks"] == 0

    def test_a_focused_field_holds_it_too(self, guard):
        """The show name is typed into a field on a page whose poller is
        running the whole time."""
        assert guard["whileTyping"] == 0
        assert guard["afterTyping"] == 1

    def test_an_open_sheet_holds_it(self, guard):
        assert guard["whileSheetOpen"] == 0
        assert guard["afterSheetClosed"] == 1

    def test_the_reload_still_happens_once_the_dialog_closes(self, guard):
        """Deferred, not cancelled: a dashboard that never catches up is its
        own bug, and a quieter one."""
        assert guard["afterModalClosed"] == 1

    def test_it_reloads_once_and_stops_asking(self, guard):
        """A pending reload that leaves its interval running reloads the page
        again every second after it fires."""
        assert guard["afterMoreTicks"] == 1

    @pytest.mark.parametrize("name", POLLERS)
    def test_no_poller_reloads_behind_the_users_back(self, name):
        body = self._body(name)
        assert "location.reload()" not in body, (
            f"{name} reloads the page directly again — it has to go through "
            "safeReload() or it will do it mid-dialog"
        )
        assert "safeReload" in body

    def test_the_reloads_the_user_asked_for_are_left_alone(self):
        """cancelJob and friends run after the dialog closed and after the
        user pressed the button. Deferring those would look like the button
        did nothing."""
        source = Path("web/static/js/app.js").read_text()
        assert "location.reload()" in source


#: The script is run rather than read, because reading it is what a reviewer
#: already did. It is loaded into a context with just enough of a browser to
#: answer the three questions uiIsBusy asks, and then the reload is actually
#: requested and the reloads counted.
RELOAD_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');

const state = {modal: false, sheet: false, focus: null};
let reloads = 0;
let ticks = [];

const context = {
    console,
    setTimeout: (fn) => fn,
    clearTimeout: () => {},
    setInterval: (fn) => { ticks.push(fn); return ticks.length; },
    clearInterval: (id) => { ticks[id - 1] = null; },
    fetch: () => new Promise(() => {}),
    navigator: {},
    location: {pathname: '/x', reload: () => { reloads += 1; }},
    document: {
        body: {classList: {contains: (name) =>
            name === 'modal-open' && state.modal}},
        querySelector: (sel) =>
            (sel === '.offcanvas.show' && state.sheet) ? {} : null,
        querySelectorAll: () => [],
        getElementById: () => null,
        addEventListener: () => {},
        get activeElement() {
            return state.focus === null ? null
                : {matches: (sel) => sel.includes(state.focus)};
        },
    },
};
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);

const tick = () => ticks.filter(Boolean).forEach(fn => fn());
const answers = {};

// Nobody is doing anything: reload straight away.
context.safeReload();
answers.idle = reloads;

// A modal is open. It must wait, and keep waiting, however many ticks pass.
reloads = 0; ticks = []; state.modal = true;
context.safeReload();
answers.whileModalOpen = reloads;
tick(); tick();
answers.whileModalOpenAfterTicks = reloads;

// It closes. The next tick reloads, once, and stops asking.
state.modal = false;
tick();
answers.afterModalClosed = reloads;
tick(); tick();
answers.afterMoreTicks = reloads;

// A field has focus — the case the dashboard poller kept interrupting.
reloads = 0; ticks = []; state.focus = 'input';
context.safeReload();
answers.whileTyping = reloads;
state.focus = null;
tick();
answers.afterTyping = reloads;

// A bottom sheet is open.
reloads = 0; ticks = []; state.sheet = true;
context.safeReload();
answers.whileSheetOpen = reloads;
state.sheet = false;
tick();
answers.afterSheetClosed = reloads;

console.log(JSON.stringify(answers));
"""


@pytest.fixture(scope="module")
def guard():
    """What safeReload actually does, run in node against a stub browser."""
    import json

    if not shutil.which("node"):
        pytest.skip("node is not installed")
    with tempfile.TemporaryDirectory() as folder:
        harness = Path(folder) / "harness.js"
        harness.write_text(RELOAD_HARNESS)
        result = subprocess.run(  # noqa: S603
            ["node", str(harness), "web/static/js/app.js"],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
