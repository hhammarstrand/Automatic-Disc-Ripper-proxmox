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
