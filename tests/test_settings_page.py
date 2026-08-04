"""The Settings page must keep every field it had.

Rearranging seventeen cards into tabs is the kind of change that loses one
input without anyone noticing until a setting mysteriously stops sticking. The
page is the only way most people configure this application, so a field that
quietly disappears is a feature that quietly disappears.
"""

import re
from pathlib import Path

import pytest

from adr.config import Config
from web.app import create_app

#: Every form field the page offered before the layout was reorganised. A
#: deliberate removal means editing this list — which is the point: it should
#: be a decision, not an accident.
EXPECTED_FIELDS = {
    "audio_cd_enabled", "audio_cd_format", "audio_language",
    "auto_move_to_plex", "cdparanoia_path", "completed_path",
    "data_disc_enabled", "data_disc_path", "drives", "encoder_backend",
    "ffmpeg_path", "handbrake_extra_args", "handbrake_path",
    "handbrake_preset", "handbrake_preset_file", "log_level",
    "main_feature_only", "makemkv_path", "max_encode_jobs", "max_height",
    "min_title_length", "music_path", "notify_enabled", "notify_provider",
    "notify_token", "notify_url", "plex_path", "plex_refresh_enabled",
    "plex_section", "plex_token", "plex_url", "raw_path", "series_detection",
    "series_max_minutes", "series_min_episodes", "series_min_minutes",
    "skip_duplicates", "tmdb_api_key", "transcode_enabled", "tv_path",
    "vaapi_codec", "vaapi_device", "video_quality", "watch_interval",
    "watch_output_path", "watch_path", "web_host", "web_port",
}


@pytest.fixture
def page(tmp_path):
    config = Config(str(tmp_path / "adr.yaml"))
    config.update({"completed_path": str(tmp_path)})
    return create_app(config).test_client().get("/settings").get_data(as_text=True)


def _fields(html: str) -> set[str]:
    return set(re.findall(r'name="([a-z_]+)"', html))


def test_every_field_is_still_on_the_page(page):
    missing = EXPECTED_FIELDS - _fields(page)
    assert not missing, f"fields lost in the layout: {sorted(missing)}"


def test_every_field_can_be_saved(page):
    """A field on the page that the API rejects is a control that does
    nothing — worse than one that is not there, because it looks like it
    worked."""
    import web.app as app_module

    source = Path(app_module.__file__).read_text()
    allowed = re.search(r"_ALLOWED_SETTINGS_KEYS = frozenset\(\{(.*?)\}\)", source, re.S)
    assert allowed, "the allowlist moved"
    names = set(re.findall(r'"([a-z_]+)"', allowed.group(1)))
    rejected = _fields(page) - names - {"viewport"}
    assert not rejected, f"on the page but rejected by the API: {sorted(rejected)}"


def test_the_form_still_posts_as_one(page):
    """Tabs are a display device. Splitting the form would mean saving one
    tab silently discarded the others."""
    assert page.count('<form id="settingsForm"') == 1


class TestTheMarkupHoldsTogether:
    """Moving seventeen cards into five panes is exactly the edit that leaves
    a stray </div> behind, and a browser will paper over it silently — the
    page renders, one section is nested inside another, and nobody sees it
    until a tab shows the wrong content."""

    def _parse(self, html):
        from html.parser import HTMLParser

        void = {"br", "hr", "img", "input", "link", "meta", "source", "area",
                "base", "col", "embed", "param", "track", "wbr"}

        class Checker(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.bad = [], []

            def handle_starttag(self, tag, attrs):
                if tag not in void:
                    self.stack.append((tag, self.getpos()[0]))

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack:
                    self.bad.append(f"stray </{tag}> on line {self.getpos()[0]}")
                    return
                if self.stack[-1][0] != tag:
                    self.bad.append(
                        f"</{tag}> on line {self.getpos()[0]} closes "
                        f"<{self.stack[-1][0]}> opened on line {self.stack[-1][1]}")
                self.stack.pop()

        checker = Checker()
        checker.feed(html)
        return checker

    def test_every_tag_closes_the_one_it_should(self, page):
        checker = self._parse(page)
        assert not checker.bad, checker.bad[:3]

    def test_nothing_is_left_open(self, page):
        checker = self._parse(page)
        assert not checker.stack, [t for t, _ in checker.stack][:5]

    def test_every_card_lives_inside_a_pane(self, page):
        """A card outside the panes shows on every tab at once, which is how
        the layout stops being a layout."""
        import re

        panes = re.findall(
            r'<div class="tab-pane.*?(?=<div class="tab-pane|<!-- Save -->)',
            page, re.S)
        assert len(panes) == 5
        inside = sum(p.count('class="card mb-4"') for p in panes)
        assert inside == page.count('class="card mb-4"')


class TestTheCopyReadsAsOneVoice:
    """Seventeen cards were written over months, and it showed: Title Case on
    some headers and sentence case on others, hints inside labels on two
    fields and underneath on the rest, config keys leaking into labels. None
    of it is a bug and all of it makes a page feel unfinished.
    """

    #: Words that keep their capitals wherever they appear.
    PROPER = {
        "Plex", "MakeMKV", "HandBrake", "HandBrakeCLI", "TMDb", "TV", "GPU",
        "MP3", "URL", "ISO", "cdparanoia", "ffmpeg", "Audio", "CDs",
    }

    def _headers(self, page):
        return re.findall(r'card-header"><i class="bi [^"]+"></i>([^<]+)</div>', page)

    def _labels(self, page):
        return re.findall(r'<label class="form-label">([^<]+)</label>', page)

    def test_the_headers_are_sentence_case(self, page):
        """"Drive Labels" next to "Data discs" reads as two people having
        written the page, which is what happened."""
        wrong = []
        for header in self._headers(page):
            words = header.strip().split()
            for word in words[1:]:
                bare = word.strip("()/,")
                if bare and bare[0].isupper() and bare not in self.PROPER:
                    wrong.append(header.strip())
        assert not wrong, f"Title Case headers: {sorted(set(wrong))}"

    def test_no_label_carries_its_own_hint(self, page):
        """"Quality (0 = leave it to the preset)" puts under the label what
        every other field puts beneath it."""
        offenders = [
            label for label in self._labels(page)
            if "=" in label or "(optional)" in label.lower()
        ]
        assert not offenders, offenders

    def test_no_label_is_a_config_key(self, page):
        """A field called "Watch folder output path" is named after the
        setting rather than after what it does."""
        offenders = [label for label in self._labels(page) if "_" in label]
        assert not offenders, offenders

    def test_no_label_ends_in_punctuation(self, page):
        offenders = [
            label for label in self._labels(page)
            if label.strip().endswith((":", ".", "…"))
        ]
        assert not offenders, offenders

    def test_every_card_has_a_header(self, page):
        """A card with no header is a group of settings with no name."""
        assert page.count('class="card mb-4"') == len(self._headers(page))

    def test_the_tool_paths_are_together(self, page):
        """Two of the four used to be filed under Audio CDs, because that is
        what needed them first. Where a program lives is not an audio-CD
        setting, and splitting them is how a page stops being findable."""
        section = page.split("Where the tools live")[1].split("</div>\n    </div>")[0]
        for tool in ("makemkv_path", "handbrake_path", "ffmpeg_path", "cdparanoia_path"):
            assert f'name="{tool}"' in section, f"{tool} is somewhere else"
