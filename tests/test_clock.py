"""Times on screen have to match the clock on the wall.

A fresh LXC is Etc/UTC and nothing set it otherwise, so every timestamp the
application wrote read two hours behind the person looking at it — job start
times, the service log, the per-job logs. Nothing was *broken* by that, which
is why it survived: it just made every time quietly wrong, and made the log
impossible to line up against when something actually happened.

Two fixes, and both are needed. The container's clock is set from the host, so
the log file reads correctly. And the browser re-renders each timestamp in its
own zone, because the container's clock and the reader's are not necessarily
the same one.
"""

import re
from pathlib import Path

import pytest

INSTALL = Path("scripts/install.sh")
DOCTOR = Path("scripts/adr-doctor.sh")
APP_JS = Path("web/static/js/app.js")


class TestTheContainerGetsAClock:
    def test_the_installer_copies_the_hosts_timezone(self):
        """The host already knows the right answer, so it is copied rather
        than asked for — one fewer question in an installer that has enough."""
        source = INSTALL.read_text()
        assert "/etc/localtime" in source
        assert "timedatectl show -p Timezone" in source

    def test_it_can_be_overridden(self):
        """Someone may deliberately want the container elsewhere."""
        assert "CT_TIMEZONE" in INSTALL.read_text()

    def test_the_doctor_fixes_containers_that_already_exist(self):
        """The installer only helps new installs, and every existing one is
        on UTC."""
        source = DOCTOR.read_text()
        assert "/etc/localtime" in source
        block = source.split("3c. The container's clock")[1].split("# 4.")[0]
        assert "would_fix" in block, "it must offer the repair, not just report"

    def test_it_restarts_the_service_after_changing_the_zone(self):
        """A running process holds its own idea of the zone, so new log lines
        would keep the old offset — the fix would look like it had not worked."""
        block = DOCTOR.read_text().split("3c. The container's clock")[1]
        assert "systemctl restart adr" in block.split("# 4.")[0]

    def test_it_checks_the_zone_exists_before_linking_it(self):
        """A symlink to a missing zoneinfo file leaves the container with no
        working clock at all, which is worse than UTC."""
        for source in (INSTALL.read_text(), DOCTOR.read_text()):
            assert "/usr/share/zoneinfo/" in source
            assert "test -f" in source


class TestTheBrowserRendersInItsOwnZone:
    def test_it_formats_marked_up_times(self):
        assert "time[data-iso]" in APP_JS.read_text()

    def test_it_runs_on_load(self):
        assert "formatLocalTimes()" in APP_JS.read_text()

    def test_an_unparseable_value_leaves_the_server_rendering_alone(self):
        """Better a time in the wrong zone than the word "Invalid Date"."""
        body = APP_JS.read_text().split("function formatLocalTimes")[1]
        assert "isNaN(when)" in body
        assert "return" in body.split("isNaN(when)")[1][:40]

    @pytest.mark.parametrize("template", ["history.html", "index.html"])
    def test_the_server_still_renders_something(self, template):
        """With no JavaScript the page must still show a time, not an empty
        cell — so the markup carries the server's rendering as its content."""
        source = (Path("web/templates") / template).read_text()
        for match in re.finditer(r"<time data-iso=\"[^\"]*\"[^>]*>(.*?)</time>", source, re.S):
            assert "strftime" in match.group(1), match.group(0)[:80]

    @pytest.mark.parametrize("template", ["history.html", "index.html"])
    def test_every_marked_up_time_carries_an_offset(self, template):
        """A zoneless date-time is read by the browser as its own local time,
        which is how a job that had just started showed 2:00:39 elapsed."""
        source = (Path("web/templates") / template).read_text()
        for match in re.finditer(r'<time data-iso="([^"]*)"', source):
            assert "isotime" in match.group(1), match.group(0)


class TestNoTimestampIsRenderedRaw:
    """A strftime with no <time> around it cannot be corrected by the browser
    and is stuck in whatever zone the container happens to be in."""

    @pytest.mark.parametrize("template", sorted(
        p.name for p in Path("web/templates").glob("*.html")))
    def test_every_strftime_is_inside_a_time_element(self, template):
        source = (Path("web/templates") / template).read_text()
        for match in re.finditer(r"\{\{[^}]*strftime[^}]*\}\}", source):
            before = source[:match.start()]
            opened = before.rfind("<time ")
            closed = before.rfind("</time>")
            assert opened > closed, f"{template}: {match.group(0)}"
