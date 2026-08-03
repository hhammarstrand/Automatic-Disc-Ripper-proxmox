"""Identify an audio CD from its table of contents.

An audio CD carries no title, no artist and no track names — nothing but
tracks and their positions. What makes lookup possible is that the *positions*
are effectively a fingerprint: no two pressings share a track layout down to
the frame. MusicBrainz hashes that layout into a disc ID and indexes releases
by it.

The disc ID algorithm is implemented here rather than pulled in as a
dependency (libdiscid). It is thirty lines of SHA-1 over a fixed-width text
representation of the TOC, it never changes, and a C library with a ctypes
binding is a poor trade for that inside an LXC where every extra package is
another thing that can fail to install.

Lookup is best-effort throughout. An unidentified CD still rips; it just ends
up filed under its disc ID instead of an artist and album.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass, field

import requests

from adr.disctype import Toc

logger = logging.getLogger(__name__)

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/discid/{discid}"

# MusicBrainz requires a User-Agent that identifies the application and gives
# them somewhere to complain to. A request without one is refused.
USER_AGENT = "AutomaticDiscRipper/1.0 (https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox)"

LOOKUP_TIMEOUT = 15

# Base64 with the three characters that are awkward in a URL swapped out.
_DISCID_ALPHABET = str.maketrans("+/=", "._-")


def compute_disc_id(toc: Toc) -> str:
    """Return the MusicBrainz disc ID for *toc*.

    The hashed string is: the first track number and the last track number as
    two-digit hex, then one hundred eight-digit hex offsets — the lead-out
    first, then tracks 1 to 99, with zero for every track the disc does not
    have. SHA-1 of that, base64-encoded, with ``+/=`` rewritten as ``._-``.
    """
    offsets = [0] * 100
    offsets[0] = toc.leadout_frame_offset
    for track in toc.tracks:
        if 1 <= track.number <= 99:
            offsets[track.number] = track.frame_offset

    payload = f"{toc.first:02X}{toc.last:02X}" + "".join(f"{o:08X}" for o in offsets)
    digest = hashlib.sha1(payload.encode("ascii")).digest()  # noqa: S324 - not security
    return base64.b64encode(digest).decode("ascii").translate(_DISCID_ALPHABET)


@dataclass
class AlbumTrack:
    """One track of an identified release."""

    number: int
    title: str
    artist: str = ""


@dataclass
class AlbumInfo:
    """What MusicBrainz knows about the disc, as far as we need it."""

    disc_id: str
    artist: str = ""
    album: str = ""
    year: int | None = None
    tracks: list[AlbumTrack] = field(default_factory=list)

    @property
    def identified(self) -> bool:
        return bool(self.album)

    @property
    def display(self) -> str:
        if not self.identified:
            return f"Unidentified CD ({self.disc_id})"
        who = self.artist or "Unknown Artist"
        when = f" ({self.year})" if self.year else ""
        return f"{who} — {self.album}{when}"

    def title_for(self, track_number: int) -> str:
        """The title of *track_number*, or a positional fallback."""
        for track in self.tracks:
            if track.number == track_number:
                return track.title
        return f"Track {track_number:02d}"


def lookup(toc: Toc, timeout: int = LOOKUP_TIMEOUT) -> AlbumInfo:
    """Look the disc up at MusicBrainz.

    Always returns an AlbumInfo. A network failure, a rate limit or a disc
    nobody has submitted all come back as an unidentified album carrying the
    disc ID, which is enough to file the rip under a stable name.
    """
    disc_id = compute_disc_id(toc)
    info = AlbumInfo(disc_id=disc_id)

    try:
        response = requests.get(
            MUSICBRAINZ_URL.format(discid=disc_id),
            params={"fmt": "json", "inc": "artists+recordings"},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException:
        logger.warning("MusicBrainz lookup failed for %s", disc_id, exc_info=True)
        return info

    if response.status_code == 404:
        logger.info("MusicBrainz has no release for disc ID %s", disc_id)
        return info
    if response.status_code != 200:
        logger.warning(
            "MusicBrainz returned HTTP %s for disc ID %s", response.status_code, disc_id,
        )
        return info

    try:
        payload = response.json()
    except ValueError:
        logger.warning("MusicBrainz returned something that was not JSON")
        return info

    return _parse(payload, disc_id) or info


def _parse(payload: dict, disc_id: str) -> AlbumInfo | None:
    """Turn a MusicBrainz disc lookup into an AlbumInfo.

    A disc ID can match several releases — the same album pressed in different
    countries. They share a track layout by definition, so any of them gives
    the right track titles; the first is taken.
    """
    releases = payload.get("releases") or []
    if not isinstance(releases, list) or not releases:
        return None
    release = releases[0]
    if not isinstance(release, dict):
        return None

    info = AlbumInfo(disc_id=disc_id)
    info.album = str(release.get("title") or "").strip()
    info.artist = _artist_credit(release.get("artist-credit"))

    date = str(release.get("date") or "")
    if len(date) >= 4 and date[:4].isdigit():
        info.year = int(date[:4])

    info.tracks = _tracks(release.get("media"), disc_id)
    return info if info.album else None


def _artist_credit(credit) -> str:
    """Flatten MusicBrainz's artist-credit list into one string.

    The list is deliberately ordered with join phrases between entries, so
    "Simon & Garfunkel" survives as written rather than becoming two artists.
    """
    if not isinstance(credit, list):
        return ""
    parts: list[str] = []
    for entry in credit:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or (entry.get("artist") or {}).get("name") or ""
        parts.append(str(name))
        join = entry.get("joinphrase")
        if join:
            parts.append(str(join))
    return "".join(parts).strip()


def _tracks(media, disc_id: str) -> list[AlbumTrack]:
    """Pull the track list from the medium that actually holds this disc.

    A box set is one release with several media, and only one of them is the
    disc in the drive. The medium carrying our disc ID is the right one; when
    the response does not say, the first medium is the only guess available.
    """
    if not isinstance(media, list) or not media:
        return []

    chosen = None
    for medium in media:
        if not isinstance(medium, dict):
            continue
        discs = medium.get("discs")
        if isinstance(discs, list) and any(
            isinstance(d, dict) and d.get("id") == disc_id for d in discs
        ):
            chosen = medium
            break
    if chosen is None:
        chosen = next((m for m in media if isinstance(m, dict)), None)
    if chosen is None:
        return []

    out: list[AlbumTrack] = []
    for entry in chosen.get("tracks") or []:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("position"))
        except (TypeError, ValueError):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            recording = entry.get("recording")
            if isinstance(recording, dict):
                title = str(recording.get("title") or "").strip()
        if title:
            out.append(AlbumTrack(number=number, title=title))
    return sorted(out, key=lambda t: t.number)
