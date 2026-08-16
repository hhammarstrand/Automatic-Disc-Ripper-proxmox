"""Text has to be readable against what is behind it.

This is not a matter of taste. A contrast ratio is a number, WCAG AA asks for
4.5:1 on body text, and the failure mode is total: the active tab in this
application's own theme measured 1.18:1 — near-white text on Bootstrap's white
tab background — which meant the tab you were looking at was the one you could
not read.

Colours are read out of the stylesheet rather than restated here, so a palette
change is checked rather than merely permitted.
"""

import re
from pathlib import Path

import pytest

CSS = Path("web/static/css/style.css")

#: WCAG AA. 4.5:1 for body text, 3:1 for large or bold text.
AA_TEXT = 4.5
AA_LARGE = 3.0


def _luminance(colour: str) -> float:
    value = colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    channels = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _declared(selector: str) -> dict:
    """The declarations a rule makes for *selector*, as written."""
    text = re.sub(r"/\*.*?\*/", " ", CSS.read_text(), flags=re.S)
    out: dict[str, str] = {}
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        if selector not in [s.strip() for s in block.group(1).split(",")]:
            continue
        for declaration in block.group(2).split(";"):
            if ":" not in declaration:
                continue
            name, _, value = declaration.partition(":")
            out[name.strip()] = value.replace("!important", "").strip()
    return out


def _resolve(value: str, palette: dict) -> str:
    """A declared colour as a hex string, following one var() indirection."""
    match = re.fullmatch(r"var\(--(adr-[a-z-]+)\)", value.strip())
    if match:
        return palette[match.group(1)]
    return value.strip()


def _sets_colour(selector: str) -> bool:
    """Whether the stylesheet gives *selector* a ``color`` in its resting state.

    Resting state specifically: a ``:hover`` or ``:focus`` rule mentioning the
    same class does not make the button readable before anyone touches it, and
    a test that merely searched for the class name was satisfied by one.
    """
    text = re.sub(r"/\*.*?\*/", " ", CSS.read_text(), flags=re.S)
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        selectors = [s.strip() for s in block.group(1).split(",")]
        if selector not in selectors:
            continue
        if re.search(r"(^|[;\s])color\s*:", block.group(2)):
            return True
    return False


@pytest.fixture(scope="module")
def palette() -> dict:
    """The theme's colours, as the stylesheet defines them."""
    text = CSS.read_text()
    # Digits count: the surface ladder's upper rungs are --adr-card-2 and
    # --adr-border-2, and a name pattern that stopped at letters silently
    # dropped them from the palette rather than failing to find them.
    return dict(re.findall(r"--(adr-[a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", text))


class TestTheKnownRatios:
    def test_the_maths_is_right(self):
        """Anchored against the two ratios everyone knows, so a broken
        implementation cannot quietly pass everything."""
        assert contrast("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
        assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0, abs=0.01)

    def test_it_does_not_care_which_way_round(self):
        assert contrast("#e6edf3", "#0d1117") == pytest.approx(
            contrast("#0d1117", "#e6edf3"))


class TestBodyText:
    @pytest.mark.parametrize("surface", ["adr-bg", "adr-card"])
    def test_ordinary_text_is_readable(self, palette, surface):
        assert contrast(palette["adr-text"], palette[surface]) >= AA_TEXT

    @pytest.mark.parametrize("surface", ["adr-bg", "adr-card"])
    def test_muted_text_is_still_readable(self, palette, surface):
        """Muted is the colour every explanation under every field uses. If it
        fails here, the answers to people's questions are the unreadable part."""
        assert contrast(palette["adr-text-muted"], palette[surface]) >= AA_TEXT

    @pytest.mark.parametrize("role", ["adr-accent", "adr-success", "adr-warning", "adr-danger"])
    def test_the_status_colours_carry_meaning_only_if_they_are_visible(
        self, palette, role,
    ):
        assert contrast(palette[role], palette["adr-card"]) >= AA_LARGE


class TestAlerts:
    """The second bug this file caught, and the one it should have caught first.

    Only ``.alert-secondary`` was ever restated for the dark theme. The other
    four kept Bootstrap's light-theme colours — pale background, dark text —
    which on its own merely looks out of place. What it did in practice was
    hide things: ``.btn-outline-light`` is themed here to near-white for the
    dark ground it is normally on, and the TV-disc banner puts two of them
    inside an ``.alert-info``. Near-white on Bootstrap's ``#cff4fc`` measures
    1.01:1. The "Change" and "Not a series" buttons were rendered, focusable
    and clickable, and could not be read at all.
    """

    VARIANTS = ["info", "success", "warning", "danger"]

    def test_every_variant_has_a_dark_background_of_its_own(self, palette):
        """Not just the one that had the bug. A variant left at Bootstrap's
        default is a pale box that anything light placed on it disappears
        into, and nothing in the markup would say so."""
        for name in self.VARIANTS:
            assert f"adr-{name}-bg" in palette, (
                f"alert-{name} has no dark background and will inherit "
                "Bootstrap's light-theme one"
            )

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_ordinary_text_on_an_alert_is_readable(self, palette, variant):
        assert contrast(palette["adr-text"], palette[f"adr-{variant}-bg"]) >= AA_TEXT

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_a_button_inside_an_alert_is_readable(self, palette, variant):
        """The actual failure. .btn-outline-light takes --adr-text, so this is
        the ratio those two buttons were rendered at."""
        assert contrast(palette["adr-text"], palette[f"adr-{variant}-bg"]) >= AA_TEXT

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_a_link_inside_an_alert_is_readable(self, palette, variant):
        assert contrast(palette["adr-accent"], palette[f"adr-{variant}-bg"]) >= AA_TEXT

    @pytest.mark.parametrize("variant", VARIANTS)
    def test_the_icon_that_carries_the_variant_is_visible(self, palette, variant):
        """The left bar and the icon are what is left of the variant once the
        background stops being the thing that signals it."""
        role = "adr-accent" if variant == "info" else f"adr-{variant}"
        assert contrast(palette[role], palette[f"adr-{variant}-bg"]) >= AA_LARGE

    def test_muted_text_in_an_alert_is_lifted_off_the_lighter_ground(self):
        """.text-muted resolves against the page, and every alert ground is
        lighter than the page, so muted text sits closer to its background
        inside an alert than anywhere else."""
        text = CSS.read_text()
        assert ".alert .text-muted" in text

    def test_the_alerts_are_restated_at_all(self):
        text = CSS.read_text()
        for name in self.VARIANTS:
            assert f".alert-{name}" in text, f"alert-{name} is still Bootstrap's"

    def test_the_variant_survives_as_something_other_than_the_background(self):
        """Four alerts on one dark background would otherwise be four
        identical grey boxes, and which one is the warning would be a matter
        of reading it."""
        text = CSS.read_text()
        block = text[text.index("/* ---- Alerts ----"):]
        block = block[:block.index("/* ---- Progress")]
        assert "border-left-color" in block


class TestButtonsOnAlerts:
    """Restating the alerts broke a button, in exactly the way the alerts had
    been broken.

    The series-mode banner is an ``.alert-warning`` holding a
    ``.btn-outline-dark``, and ``.btn-outline-dark`` is Bootstrap's #212529 —
    near-black, correct against the pale yellow it used to sit on, and
    **1.00:1** against the dark ground that replaced it. "Fix episode number"
    was on every page in the application and could not be seen at all.

    It was found by rendering the banner in a browser, not by reading the
    diff, because the banner only exists while series mode is switched on and
    no default render produces it. These tests are the cheap half of that: an
    alert ground is a place buttons get put, so every button colour has to
    clear AA against every one of them.
    """

    GROUNDS = ["adr-info-bg", "adr-success-bg", "adr-warning-bg",
               "adr-danger-bg", "adr-card"]

    #: Bootstrap classes that are near-black or near-white by default and
    #: therefore have to be restated to survive on a dark ground.
    MUST_BE_RESTATED = [
        ".btn-outline-dark", ".btn-dark", ".btn-secondary",
        ".btn-outline-light", ".btn-outline-secondary", ".btn-outline-primary",
    ]

    @pytest.mark.parametrize("ground", GROUNDS)
    def test_a_button_label_is_readable_on_every_alert(self, palette, ground):
        """--adr-text is what every restated outline button resolves to."""
        assert contrast(palette["adr-text"], palette[ground]) >= AA_TEXT

    @pytest.mark.parametrize("selector", MUST_BE_RESTATED)
    def test_the_near_black_and_near_white_buttons_are_restated(self, selector):
        """Bootstrap picked these colours for a white page. Left alone, each
        one is invisible somewhere in this application.

        The declaration, not the name. Asking only whether the selector
        appears anywhere in the file passes on the ``:hover`` rule alone —
        which is how the first version of this test watched the very
        regression it was written for go straight past it.
        """
        assert _sets_colour(selector), (
            f"{selector} has no rule setting its colour, so it still carries "
            "Bootstrap's light-theme one in its resting state"
        )

    def test_bootstraps_own_dark_button_colour_would_fail(self, palette):
        """The number this class exists for. If this ever passes, the premise
        is wrong and the rest of these tests are worthless."""
        assert contrast("#212529", palette["adr-warning-bg"]) < AA_TEXT

    def test_the_subtle_border_tokens_are_restated_too(self):
        """--bs-danger-border-subtle is #f1aeb5, a pale pink hairline on a
        dark alert. Not text, so no contrast sweep would ever mention it."""
        assert ".border-danger-subtle" in CSS.read_text()


class TestTheBottomBarAndSheets:
    """The phone's navigation is a bar of 11px labels on the darkest surface
    in the application, which is the combination least likely to survive being
    read at arm's length — and the one nobody would think to measure, because
    the colours are the theme's own and each is correct somewhere else.

    The sheet behind More is the first offcanvas here, and Bootstrap ships it
    white for the same reason it shipped the modals and the active tab white:
    it assumes a light page. That assumption is the single most repeated bug
    in this file's history.
    """

    def test_the_bars_share_a_colour_that_can_be_checked(self, palette):
        """It was a literal inside .navbar, which no test could read and the
        second bar would have copied by hand."""
        assert "adr-nav-bg" in palette
        assert _declared(".navbar").get("background-color") == "var(--adr-nav-bg)"

    def test_an_unselected_label_is_readable(self, palette):
        """Muted grey is what four of the five items are, all of the time."""
        assert contrast(palette["adr-text-muted"], palette["adr-nav-bg"]) >= AA_TEXT

    def test_the_selected_label_is_readable(self, palette):
        assert contrast(palette["adr-accent"], palette["adr-nav-bg"]) >= AA_TEXT

    def test_ordinary_text_survives_on_the_bar_too(self, palette):
        """The brand and the Online badge sit on the same ground up top."""
        assert contrast(palette["adr-text"], palette["adr-nav-bg"]) >= AA_TEXT

    def test_which_item_is_selected_is_not_only_a_colour(self):
        """Blue against grey at 11px, on a screen held at arm's length in a
        room with a disc drive in it."""
        assert ".adr-bottomnav-item.active::before" in CSS.read_text()

    def test_the_sheet_is_not_bootstraps_white_panel(self, palette):
        declared = _declared(".offcanvas")
        assert declared.get("background-color"), (
            "the offcanvas keeps --bs-body-bg, which is #fff — a white card "
            "sliding up over a dark application"
        )
        assert declared.get("color"), "the sheet sets no text colour"
        assert contrast(
            _resolve(declared["color"], palette),
            _resolve(declared["background-color"], palette),
        ) >= AA_TEXT

    def test_the_bar_sits_under_every_overlay(self):
        """Bootstrap's offcanvas is 1045 and its modal backdrop 1050. A bar
        above either is live navigation drawn on top of a dialog that is in
        the middle of asking a question."""
        z = _declared(".adr-bottomnav").get("z-index")
        assert z and int(z) < 1045


class TestTheActiveTab:
    """The bug this file was written for."""

    def test_the_tab_strip_is_restated_for_the_dark_theme(self):
        """Bootstrap's active tab is dark text on white. Inheriting any part
        of that reintroduces a light-page assumption on a dark page."""
        text = CSS.read_text()
        assert ".nav-tabs .nav-link.active" in text
        block = text.split(".nav-tabs .nav-link.active")[1]
        assert "background-color" in block, "the white background must be replaced"

    def test_the_active_tab_is_readable(self, palette):
        """Its background is the card colour, so it reads like the panel it
        belongs to — and the text on it has to clear AA against that.

        Against the colour the rule actually sets, not against a pair of
        palette entries that are correct by construction. The first version
        asserted --adr-text over --adr-card and would have passed with the
        active tab back on Bootstrap's white, which is the bug this whole
        file was written for.
        """
        declared = _declared(".nav-tabs .nav-link.active")
        assert declared.get("background-color"), "the tab sets no background"
        assert declared.get("color"), "the tab sets no text colour"
        assert contrast(
            _resolve(declared["color"], palette),
            _resolve(declared["background-color"], palette),
        ) >= AA_TEXT

    def test_an_inactive_tab_is_readable_too(self, palette):
        assert contrast(palette["adr-text-muted"], palette["adr-bg"]) >= AA_TEXT

    def test_the_selected_tab_is_marked_by_more_than_a_border(self):
        """Which tab is selected has to survive being read quickly, and a
        one-pixel border shift does not."""
        text = CSS.read_text()
        block = text.split(".nav-tabs .nav-link.active")[-1]
        assert "box-shadow" in block


class TestTheInstrument:
    """Drive Bay's own decisions, checked as numbers rather than as taste.

    The direction's argument is that this screen is read standing at a machine
    in whatever light the room has, so its two riskiest choices are the ones
    below: a state colour that carries meaning on its own, and a ground lifted
    far enough that the surfaces stay distinguishable when a phone dims itself.
    """

    #: The state words and stage pills are 10-11px caps, so they are body text
    #: by WCAG's reckoning however bold they are.
    STATES = ["adr-accent", "adr-encode", "adr-link", "adr-danger", "adr-success"]

    @pytest.mark.parametrize("role", STATES)
    @pytest.mark.parametrize("surface", ["adr-bg", "adr-card"])
    def test_every_state_colour_is_readable_as_text(self, palette, role, surface):
        assert contrast(palette[role], palette[surface]) >= AA_TEXT

    def test_the_two_states_a_job_lives_in_are_told_apart(self, palette):
        """Ripping and encoding used to be one hue with a different word.

        Deliberately not a contrast ratio: amber and teal sit at almost the
        same luminance (1.13:1), which is what makes them both readable on the
        same dark ground, and a ratio between two foregrounds is not a WCAG
        measure of anything. What has to hold is that they are different, and
        that the difference is not carried by colour alone — the state word
        and its glyph do that, and test_frontend checks the glyph is there.
        """
        assert palette["adr-accent"] != palette["adr-encode"]

    def test_the_readout_labels_are_readable(self, palette):
        """ELAPSED / REMAINING / DONE are muted, 11px, on the card."""
        assert contrast(palette["adr-text-muted"], palette["adr-card"]) >= AA_TEXT

    def test_the_surfaces_are_far_enough_apart_to_survive_daylight(self, palette):
        """Four steps, and each has to be visible against the one below it.

        A tenth of a ratio point is what separates a surface ladder from a
        flat page on a phone that has dimmed itself in a bright room. These
        are not text ratios; they are the reason the ground was lifted from
        #0d1117 in the first place.
        """
        ladder = ["adr-bg-alt", "adr-bg", "adr-card", "adr-card-2"]
        for lower, upper in zip(ladder, ladder[1:]):
            assert contrast(palette[upper], palette[lower]) >= 1.09, (
                f"{upper} does not separate from {lower}"
            )

    def test_the_gauge_trough_is_distinct_from_its_ticks(self, palette):
        """The ticks are cut into the trough; if they match it there is no
        gauge, only a bar."""
        declared = _declared(".card[data-job-id] .progress")
        assert declared.get("box-shadow"), "the trough has no rim"
        assert "#2d343e" in CSS.read_text(), "the tick colour is gone"
        assert contrast("#2d343e", palette["adr-bg-alt"]) >= 1.1

    def test_a_drive_bay_carries_its_state_on_an_edge(self):
        declared = _declared("#drivesRow > .card")
        assert "var(--adr-state)" in declared.get("border-left", "")

    def test_every_job_state_has_a_colour(self):
        """A status with no entry inherits the grey meant for an idle drive
        edge, which as 10px text on a card measures 2.1:1 — the audit caught
        exactly that on a job that was still identifying its disc."""
        text = re.sub(r"/\*.*?\*/", " ", CSS.read_text(), flags=re.S)
        for status in ("pending", "identifying", "ripping", "ripped",
                       "encoding", "done", "error"):
            assert f'.card[data-job-status="{status}"]' in text, status


class TestTheAuditMeasuresAFixture:
    """The tool seeded its fixtures into the checkout's own adr.db.

    adr.config resolves DATABASE_PATH at import time, so an audit run wrote
    four jobs into whatever database the working copy was using and then
    measured them alongside whatever real jobs were already there. Its answer
    therefore depended on the machine it ran on — a leftover job in a state the
    fixtures never create is exactly how a 2.1:1 colour appears on one checkout
    and not another — and every run grew someone's real history.
    """

    TOOL = Path("tools/contrast_audit.py")

    def test_it_points_the_database_somewhere_disposable(self):
        source = self.TOOL.read_text()
        assert "models.DATABASE_PATH" in source
        assert "audit.db" in source

    def test_it_does_so_before_anything_opens_one(self):
        """The engine is cached on first use, so a redirect after seed() runs
        would change nothing."""
        # Inside main(): "seed(config)" also matches the function's own
        # definition, which is above everything main does.
        body = self.TOOL.read_text().split("def main(")[1]
        assert body.index("models.DATABASE_PATH") < body.index("seed(config)")

    def test_it_clears_the_cached_engine(self):
        source = self.TOOL.read_text()
        assert "models._engine = None" in source
