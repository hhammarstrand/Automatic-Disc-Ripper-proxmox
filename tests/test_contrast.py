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


@pytest.fixture(scope="module")
def palette() -> dict:
    """The theme's colours, as the stylesheet defines them."""
    text = CSS.read_text()
    return dict(re.findall(r"--(adr-[a-z-]+):\s*(#[0-9a-fA-F]{6})", text))


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
        belongs to — and the text on it has to clear AA against that."""
        assert contrast(palette["adr-text"], palette["adr-card"]) >= AA_TEXT

    def test_an_inactive_tab_is_readable_too(self, palette):
        assert contrast(palette["adr-text-muted"], palette["adr-bg"]) >= AA_TEXT

    def test_the_selected_tab_is_marked_by_more_than_a_border(self):
        """Which tab is selected has to survive being read quickly, and a
        one-pixel border shift does not."""
        text = CSS.read_text()
        block = text.split(".nav-tabs .nav-link.active")[-1]
        assert "box-shadow" in block
