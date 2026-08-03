"""Tests for audio CD identification."""

import requests

from adr import musicbrainz
from adr.disctype import FRAME_OFFSET, Toc, TocTrack
from adr.musicbrainz import AlbumInfo, AlbumTrack, compute_disc_id


def _toc_from_offsets(offsets, leadout, first=1):
    """Build a Toc from frame offsets, the units MusicBrainz documents."""
    tracks = [
        TocTrack(number=first + i, lba=offset - FRAME_OFFSET, is_audio=True)
        for i, offset in enumerate(offsets)
    ]
    return Toc(
        first=first,
        last=first + len(offsets) - 1,
        leadout_lba=leadout - FRAME_OFFSET,
        tracks=tracks,
    )


# ------------------------------------------------------------------ #
# Disc ID
# ------------------------------------------------------------------ #

def test_disc_id_matches_the_published_worked_example():
    """The example from MusicBrainz's own Disc ID Calculation page.

    This is the whole reason the algorithm can live here instead of behind
    libdiscid: it is fixed, published, and checkable against a known answer.
    """
    toc = _toc_from_offsets([150, 15363, 32314, 46592, 63414, 80489], leadout=95462)
    assert compute_disc_id(toc) == "49HHV7Eb8UKF3aQiNmu1GR8vKTY-"


def test_disc_id_is_the_url_safe_alphabet():
    toc = _toc_from_offsets([150, 15363], leadout=40000)
    disc_id = compute_disc_id(toc)
    assert len(disc_id) == 28
    assert not set(disc_id) & set("+/=")


def test_disc_id_depends_on_the_layout():
    a = compute_disc_id(_toc_from_offsets([150, 15363], leadout=40000))
    b = compute_disc_id(_toc_from_offsets([150, 15364], leadout=40000))
    assert a != b


def test_disc_id_ignores_track_numbers_above_99():
    """The hashed array only has room for 99 tracks; a stray number must not
    write past the end of it."""
    toc = _toc_from_offsets([150, 15363], leadout=40000)
    toc.tracks.append(TocTrack(number=120, lba=20000, is_audio=True))
    assert compute_disc_id(toc) == compute_disc_id(_toc_from_offsets([150, 15363], leadout=40000))


# ------------------------------------------------------------------ #
# AlbumInfo
# ------------------------------------------------------------------ #

def test_unidentified_album_still_has_a_name():
    info = AlbumInfo(disc_id="abc-")
    assert not info.identified
    assert info.display == "Unidentified CD (abc-)"


def test_display_includes_artist_and_year():
    info = AlbumInfo(disc_id="x", artist="Kent", album="Isola", year=1997)
    assert info.display == "Kent — Isola (1997)"


def test_display_without_artist_says_unknown():
    info = AlbumInfo(disc_id="x", album="Isola")
    assert info.display == "Unknown Artist — Isola"


def test_title_for_falls_back_to_the_position():
    info = AlbumInfo(disc_id="x", tracks=[AlbumTrack(number=1, title="747")])
    assert info.title_for(1) == "747"
    assert info.title_for(4) == "Track 04"


# ------------------------------------------------------------------ #
# Lookup
# ------------------------------------------------------------------ #

class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _patch_get(monkeypatch, response):
    calls = {}

    def fake_get(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(musicbrainz.requests, "get", fake_get)
    return calls


def test_lookup_sends_a_user_agent(monkeypatch):
    calls = _patch_get(monkeypatch, _Response(404))
    musicbrainz.lookup(_toc_from_offsets([150], leadout=40000))
    assert calls["kwargs"]["headers"]["User-Agent"].startswith("AutomaticDiscRipper/")


def test_unknown_disc_comes_back_unidentified(monkeypatch):
    _patch_get(monkeypatch, _Response(404))
    info = musicbrainz.lookup(_toc_from_offsets([150], leadout=40000))
    assert not info.identified
    assert info.disc_id


def test_network_failure_does_not_stop_the_rip(monkeypatch):
    _patch_get(monkeypatch, requests.ConnectionError("no route"))
    info = musicbrainz.lookup(_toc_from_offsets([150], leadout=40000))
    assert not info.identified


def test_rate_limit_comes_back_unidentified(monkeypatch):
    _patch_get(monkeypatch, _Response(503))
    assert not musicbrainz.lookup(_toc_from_offsets([150], leadout=40000)).identified


def test_non_json_response_comes_back_unidentified(monkeypatch):
    _patch_get(monkeypatch, _Response(200, payload=None, text="<html>"))
    assert not musicbrainz.lookup(_toc_from_offsets([150], leadout=40000)).identified


def test_a_release_is_parsed(monkeypatch):
    _patch_get(monkeypatch, _Response(200, payload={
        "releases": [{
            "title": "Isola",
            "date": "1997-10-06",
            "artist-credit": [{"name": "Kent"}],
            "media": [{"tracks": [
                {"position": 2, "title": "747"},
                {"position": 1, "title": "Om du var här"},
            ]}],
        }],
    }))
    info = musicbrainz.lookup(_toc_from_offsets([150, 15363], leadout=40000))
    assert info.identified
    assert info.album == "Isola"
    assert info.artist == "Kent"
    assert info.year == 1997
    assert [t.number for t in info.tracks] == [1, 2]
    assert info.title_for(2) == "747"


def test_join_phrases_keep_a_collaboration_readable():
    credit = [
        {"name": "Simon", "joinphrase": " & "},
        {"name": "Garfunkel"},
    ]
    assert musicbrainz._artist_credit(credit) == "Simon & Garfunkel"


def test_artist_falls_back_to_the_nested_artist_name():
    assert musicbrainz._artist_credit([{"artist": {"name": "Kent"}}]) == "Kent"


def test_the_medium_carrying_our_disc_is_the_one_used():
    """A box set is one release with several discs; only one is in the drive."""
    media = [
        {"discs": [{"id": "other"}], "tracks": [{"position": 1, "title": "Wrong"}]},
        {"discs": [{"id": "ours"}], "tracks": [{"position": 1, "title": "Right"}]},
    ]
    tracks = musicbrainz._tracks(media, "ours")
    assert [t.title for t in tracks] == ["Right"]


def test_without_disc_ids_the_first_medium_is_used():
    media = [{"tracks": [{"position": 1, "title": "Only guess"}]}]
    assert musicbrainz._tracks(media, "ours")[0].title == "Only guess"


def test_track_title_falls_back_to_the_recording():
    media = [{"tracks": [{"position": 1, "recording": {"title": "From recording"}}]}]
    assert musicbrainz._tracks(media, "x")[0].title == "From recording"


def test_tracks_without_a_position_are_dropped():
    media = [{"tracks": [{"title": "No position"}, {"position": 1, "title": "Fine"}]}]
    assert [t.title for t in musicbrainz._tracks(media, "x")] == ["Fine"]


def test_release_without_a_title_is_not_an_identification(monkeypatch):
    _patch_get(monkeypatch, _Response(200, payload={"releases": [{"date": "1997"}]}))
    assert not musicbrainz.lookup(_toc_from_offsets([150], leadout=40000)).identified


def test_empty_release_list_is_not_an_identification(monkeypatch):
    _patch_get(monkeypatch, _Response(200, payload={"releases": []}))
    assert not musicbrainz.lookup(_toc_from_offsets([150], leadout=40000)).identified


def test_garbage_shapes_do_not_raise(monkeypatch):
    _patch_get(monkeypatch, _Response(200, payload={"releases": "not a list"}))
    assert not musicbrainz.lookup(_toc_from_offsets([150], leadout=40000)).identified
