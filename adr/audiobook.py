"""Audiobooks: a box of CDs, or a folder of files, made into one M4B.

An audiobook is not an album. It is one long work cut into a few hundred
CD-sized pieces across ten or fifteen discs, and a music library files it as
fifteen albums of tracks nobody wants to see separately. What a listener — and
Audiobookshelf — wants is a single file with chapters, a place to resume from,
and the author and the narrator on it.

So this module does two things:

* **Audiobook mode**, the audio counterpart of series mode. Name the book
  once, then feed the discs. Each is ripped losslessly into a working folder
  for the book; when the last disc is in (or someone presses Finish), the lot
  is joined into ``Author/Title/Title.m4b`` with one chapter per track. A disc
  MusicBrainz knows as an audiobook starts the mode on its own, numbers itself
  by its real position in the box, and knows how many discs there are.
* **Folders on the share**: a directory of MP3s or M4As that already exists
  is joined the same way, and the originals are left exactly where they are.

The library layout is Audiobookshelf's: ``Author/Title/``. Audiobookshelf reads
the author from the artist tags and the narrator from the composer tag, which
is what is written.
"""

from __future__ import annotations

import contextlib
import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

from adr.utils import sanitize_filename, utcnow

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".aac", ".wav", ".wma",
})
COVER_NAMES = ("cover", "folder", "front")

# Two discs of one book can be ripped at once, in two drives; numbering and the
# list of discs done are read-modify-written under this.
_lock = threading.RLock()
# One book is built at a time. An M4B is a single long ffmpeg run, and two of
# them at once only make both slower.
_build_slot = threading.Semaphore(1)


# ------------------------------------------------------------------ #
# Where books go
# ------------------------------------------------------------------ #

def library_root(config) -> Path:
    value = str(config.as_dict().get("audiobook_path") or "").strip()
    return Path(value) if value else Path(config.completed_path) / "Audiobooks"


def _part(name: str, fallback: str) -> str:
    """One path component. sanitize_filename keeps dots, and '..' as an
    author is a folder outside the library."""
    cleaned = sanitize_filename(name or "").strip(" .")
    return cleaned or fallback


def book_dir(config, author: str, title: str) -> Path:
    return (library_root(config)
            / _part(author, "Unknown Author")
            / _part(title, "Untitled"))


def _work_root(config) -> Path:
    return Path(config.staging_path) / "audiobooks"


# ------------------------------------------------------------------ #
# Audiobook mode
#
# A disc is *ripping* from the moment it is claimed until its rip ends, and
# *done* only once its folder is whole. It rips into discNN.part and is
# renamed to discNN on success, so two discs never share a folder and a failed
# re-rip never touches a good one. Each book has an id: a build in progress
# owns its folder outright, and nothing it does afterwards can reach a book
# started since.
# ------------------------------------------------------------------ #

def _get(config, key, default=None):
    return config.as_dict().get(key, default)


def _ints(values) -> list[int]:
    out = []
    for value in values or []:
        with contextlib.suppress(TypeError, ValueError):
            out.append(int(value))
    return sorted(set(out))


def is_active(config) -> bool:
    return bool(_get(config, "audiobook_mode") and _get(config, "audiobook_title"))


def state(config) -> dict:
    return {
        "active": is_active(config),
        "id": _get(config, "audiobook_id") or "",
        "author": _get(config, "audiobook_author") or "",
        "title": _get(config, "audiobook_title") or "",
        "narrator": _get(config, "audiobook_narrator") or "",
        "year": _get(config, "audiobook_year"),
        "discs_total": _get(config, "audiobook_discs_total"),
        "discs_done": _ints(_get(config, "audiobook_discs_done")),
        "discs_ripping": _ints(_get(config, "audiobook_discs_ripping")),
        "release_id": _get(config, "audiobook_release_id") or "",
    }


def work_dir(config) -> Path:
    return _work_root(config) / (_get(config, "audiobook_id") or "current")


_CLEARED = {
    "audiobook_mode": False, "audiobook_id": "", "audiobook_title": "",
    "audiobook_author": "", "audiobook_narrator": "", "audiobook_year": None,
    "audiobook_discs_total": None, "audiobook_discs_done": [],
    "audiobook_discs_ripping": [], "audiobook_release_id": "",
}


def start(config, author: str, title: str, narrator: str = "",
          year: int | None = None, discs_total: int | None = None,
          release_id: str = "") -> dict:
    import uuid

    title = (title or "").strip()
    if not title:
        raise ValueError("A title is required.")
    with _lock:
        config.update({
            "audiobook_mode": True,
            "audiobook_id": uuid.uuid4().hex[:12],
            "audiobook_author": (author or "").strip(),
            "audiobook_title": title,
            "audiobook_narrator": (narrator or "").strip(),
            "audiobook_year": int(year) if year else None,
            "audiobook_discs_total": int(discs_total) if discs_total else None,
            "audiobook_discs_done": [],
            "audiobook_discs_ripping": [],
            "audiobook_release_id": release_id or "",
        })
        work_dir(config).mkdir(parents=True, exist_ok=True)
    logger.info("Audiobook mode on: %s by %s", title, author or "?")
    return state(config)


def ensure_started(config, **book) -> bool:
    """Start the book unless one is already going. True if this started it.

    Atomic, for two drives that each find disc 1 and disc 2 of a box
    MusicBrainz knows at the same moment: the second must join the book the
    first one started, not start it again over the first one's claim.
    """
    with _lock:
        if is_active(config):
            return False
        start(config, **book)
        return True


def stop(config, discard: bool = False) -> dict:
    """Turn the mode off. With *discard*, the discs ripped so far go too."""
    with _lock:
        if discard:
            with contextlib.suppress(OSError):
                shutil.rmtree(work_dir(config))
        config.update(dict(_CLEARED))
    return state(config)


def _on_disk(config) -> set[int]:
    numbers = set()
    with contextlib.suppress(OSError):
        for entry in work_dir(config).iterdir():
            match = re.fullmatch(r"disc(\d+)(\.part)?", entry.name)
            if match:
                numbers.add(int(match.group(1)))
    return numbers


def claim_disc(config, position: int | None = None) -> int:
    """The number a disc about to be ripped is filed under.

    MusicBrainz's position in the box when it knows it — the box comes out in
    any order — unless another drive is ripping that number right now; then,
    and when there is no position, the next number nobody has used.
    """
    with _lock:
        s = state(config)
        busy = set(s["discs_ripping"])
        if position and int(position) not in busy:
            number = int(position)
        else:
            number = max(busy | set(s["discs_done"]) | _on_disk(config), default=0) + 1
        part = disc_part_dir(config, number)
        with contextlib.suppress(OSError):
            shutil.rmtree(part)
        part.mkdir(parents=True, exist_ok=True)
        config.update({"audiobook_discs_ripping": sorted(busy | {number})})
    return number


def disc_done(config, number: int) -> None:
    """The disc's rip is whole: its folder takes the number."""
    with _lock:
        final, part = disc_dir(config, number), disc_part_dir(config, number)
        with contextlib.suppress(OSError):
            shutil.rmtree(final)
        part.rename(final)
        s = state(config)
        config.update({
            "audiobook_discs_ripping": [n for n in s["discs_ripping"] if n != number],
            "audiobook_discs_done": sorted(set(s["discs_done"]) | {number}),
        })


def release_disc(config, number: int) -> None:
    """A rip that did not finish: its partial folder goes, nothing else."""
    with _lock:
        with contextlib.suppress(OSError):
            shutil.rmtree(disc_part_dir(config, number))
        s = state(config)
        config.update({
            "audiobook_discs_ripping": [n for n in s["discs_ripping"] if n != number],
        })


def disc_dir(config, number: int) -> Path:
    return work_dir(config) / f"disc{number:02d}"


def disc_part_dir(config, number: int) -> Path:
    return work_dir(config) / f"disc{number:02d}.part"


def complete(config) -> bool:
    s = state(config)
    return (bool(s["discs_total"]) and not s["discs_ripping"]
            and len(s["discs_done"]) >= int(s["discs_total"]))


# ------------------------------------------------------------------ #
# Joining files into one book
# ------------------------------------------------------------------ #

def natural_key(path: Path):
    """'Track 2' before 'Track 10', and disc02 after disc01 at every depth."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", str(path))]


def collect_files(folder: Path) -> list[Path]:
    files = [p for p in folder.rglob("*")
             if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
             and not p.name.startswith(".")
             and not any(part.endswith(".part") for part in p.relative_to(folder).parts)]
    return sorted(files, key=natural_key)


def find_cover(folder: Path) -> Path | None:
    images = [p for p in folder.rglob("*")
              if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    for name in COVER_NAMES:
        for image in images:
            if image.stem.lower() == name:
                return image
    return images[0] if images else None


def _ffprobe(config) -> str:
    return str(Path(config.ffmpeg_path).with_name("ffprobe"))


def probe(config, path: Path) -> tuple[float, dict]:
    """(duration in seconds, format tags in lower case) of one file."""
    import json

    try:
        out = subprocess.run(
            [_ffprobe(config), "-v", "error", "-print_format", "json",
             "-show_entries", "format=duration:format_tags", str(path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        data = json.loads(out.stdout or "{}").get("format", {})
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0, {}
    tags = {str(k).lower(): str(v) for k, v in (data.get("tags") or {}).items()}
    try:
        return float(data.get("duration") or 0), tags
    except ValueError:
        return 0.0, tags


def chapter_title(path: Path, tags: dict, disc_count: int) -> str:
    """The track's own title when it has one worth reading.

    'Track 07' is not; across fifteen discs there are fifteen of them, so a
    generic name says which disc it came from.
    """
    title = (tags.get("title") or "").strip()
    if not title:
        title = re.sub(r"^\d+\s*[-._ ]\s*", "", path.stem).strip() or path.stem
    if re.fullmatch(r"(?i)track\s*\d+", title) and disc_count > 1:
        disc = re.search(r"disc(\d+)", str(path.parent), re.I)
        if disc:
            return f"Disc {int(disc.group(1))}, {title.lower()}"
    return title


def _meta_escape(value: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", str(value))


def build_m4b(config, files: list[Path], meta: dict, destination: Path,
              progress=None, cover: Path | None = None) -> tuple[bool, str]:
    """Join *files* into one AAC M4B with a chapter per file.

    Written beside the destination under a temporary name and renamed when
    whole, so a half-built book never appears in the library.
    """
    if not files:
        return False, "There were no audio files to join."
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Built on local disk, like every encode here: an hour of ffmpeg writing
    # across the network is what staging exists to avoid. One copy at the end.
    import tempfile
    Path(config.staging_path).mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="audiobook-", dir=config.staging_path))
    try:
        disc_count = len({p.parent for p in files})
        chapters, position = [], 0.0
        for path in files:
            duration, tags = probe(config, path)
            if duration <= 0:
                return False, f"Could not read the length of {path.name}."
            chapters.append((position, position + duration, chapter_title(path, tags, disc_count)))
            position += duration
        total = position

        listing = scratch / "files.txt"
        listing.write_text("".join(
            "file '" + str(p).replace("'", "'\\''") + "'\n" for p in files), encoding="utf-8")
        lines = [";FFMETADATA1"]
        for key, value in (
            ("title", meta.get("title")), ("album", meta.get("title")),
            ("artist", meta.get("author")), ("album_artist", meta.get("author")),
            ("composer", meta.get("narrator")), ("date", meta.get("year")),
            ("genre", "Audiobook"),
        ):
            if value:
                lines.append(f"{key}={_meta_escape(value)}")
        for start, end, title in chapters:
            lines += ["[CHAPTER]", "TIMEBASE=1/1000",
                      f"START={int(start * 1000)}", f"END={int(end * 1000)}",
                      f"title={_meta_escape(title)}"]
        metadata = scratch / "metadata.txt"
        metadata.write_text("\n".join(lines) + "\n", encoding="utf-8")

        partial = scratch / "book.m4b"
        cmd = [config.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listing),
               "-i", str(metadata)]
        if cover:
            cmd += ["-i", str(cover)]
        cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if cover:
            cmd += ["-map", "2:v", "-c:v", "mjpeg" if cover.suffix.lower() != ".png" else "png",
                    "-disposition:v:0", "attached_pic"]
        bitrate = str(_get(config, "audiobook_bitrate") or "64k")
        cmd += ["-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats", "-f", "mp4", str(partial)]

        logger.info("Building audiobook %s from %d file(s)", destination.name, len(files))
        # stderr to a file, not a pipe: progress is read from stdout until it
        # ends, and an unread stderr pipe that fills up stops ffmpeg dead.
        errors = scratch / "ffmpeg.err"
        with open(errors, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
            for line in proc.stdout:
                if line.startswith("out_time_us=") and progress and total:
                    with contextlib.suppress(ValueError):
                        progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / total))
            code = proc.wait()
        error = errors.read_text(encoding="utf-8", errors="replace")
        if code != 0 or not partial.exists():
            return False, f"ffmpeg could not build the book: {error.strip()[-400:] or 'no output'}"

        final = destination
        counter = 2
        while final.exists():
            final = destination.with_name(f"{destination.stem} ({counter}){destination.suffix}")
            counter += 1
        # Copied under a name nobody's player will read, then renamed: a
        # half-copied book never appears in the library.
        arriving = final.with_name(final.name + ".part")
        shutil.copyfile(partial, arriving)
        arriving.rename(final)
        return True, str(final)
    finally:
        with contextlib.suppress(OSError):
            shutil.rmtree(scratch)


# ------------------------------------------------------------------ #
# A build, as a job the dashboard shows
# ------------------------------------------------------------------ #

def _make_job(author: str, title: str, label: str):
    from adr.models import Job, JobStatus, get_session

    session = get_session()
    try:
        job = Job(
            disc_label=label,
            title=f"{author} — {title}" if author else title,
            drive="audiobook",
            content_type="audiobook",
            status=JobStatus.ENCODING,
            progress_rip=1.0,
            started_at=utcnow(),
        )
        session.add(job)
        session.commit()
        return job.id
    finally:
        session.close()


def _run_build(config, job_id: int, files: list[Path], meta: dict,
               cover: Path | None, after_success=None, after_failure=None) -> None:
    from adr.joblog import JobLog
    from adr.models import Job, JobStatus, get_session
    from adr.notify import Notifier

    log = JobLog(config, job_id)
    session = get_session()
    job = None
    try:
        job = session.get(Job, job_id)
        last = {"value": 0.0}

        def report(fraction: float) -> None:
            if fraction - last["value"] < 0.01:
                return
            last["value"] = fraction
            job.progress_encode = fraction
            with contextlib.suppress(Exception):
                session.commit()

        with _build_slot:
            log.append("encode", f"Joining {len(files)} file(s) into one book.")
            destination = book_dir(config, meta.get("author", ""), meta["title"]) \
                / f"{_part(meta['title'], 'Untitled')}.m4b"
            ok, detail = build_m4b(config, files, meta, destination, report, cover)

        job.completed_at = utcnow()
        if ok:
            job.status = JobStatus.DONE
            job.progress_encode = 1.0
            job.output_path = detail
            session.commit()
            log.append("done", f"Written to {detail}")
            if after_success:
                after_success()
            Notifier(config).job_done(job, detail)
        else:
            job.status = JobStatus.ERROR
            job.error_message = detail
            session.commit()
            log.append("encode", detail)
            if after_failure:
                after_failure()
            Notifier(config).job_failed(job)
    except Exception as exc:                            # noqa: BLE001 - recorded
        logger.exception("Audiobook build %s failed", job_id)
        with contextlib.suppress(Exception):
            if job is None:
                raise LookupError(job_id)
            job.status = JobStatus.ERROR
            job.error_message = f"The book could not be built: {exc}"
            job.completed_at = utcnow()
            session.commit()
        if after_failure:
            with contextlib.suppress(Exception):
                after_failure()
    finally:
        session.close()


def _start(target, *args) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, daemon=True, name="AudiobookBuild")
    thread.start()
    return thread


def _building_dir(config, book_id: str) -> Path:
    return _work_root(config) / f".building-{book_id}"


def _launch(config, build_dir: Path, meta: dict) -> int:
    """Build the book whose discs are in *build_dir*. Returns the job id.

    The folder belongs to this build alone: on success it goes, on failure
    the book is handed back to audiobook mode — unless another book has been
    started since, in which case it waits beside it under .failed- with the
    reason in the job.
    """
    files = collect_files(build_dir)
    label = f"{meta['title']} ({len({p.parent for p in files})} discs)"
    job_id = _make_job(meta.get("author", ""), meta["title"], label)

    def tidy():
        with contextlib.suppress(OSError):
            shutil.rmtree(build_dir)

    def give_back():
        with _lock:
            if is_active(config):
                build_dir.rename(_work_root(config) / f".failed-{meta['id']}")
                return
            build_dir.rename(_work_root(config) / meta["id"])
            numbers = sorted(int(m.group(1)) for m in (
                re.fullmatch(r"disc(\d+)", d.name) for d in (_work_root(config) / meta["id"]).iterdir())
                if m)
            config.update({
                "audiobook_mode": True, "audiobook_id": meta["id"],
                "audiobook_title": meta["title"], "audiobook_author": meta.get("author", ""),
                "audiobook_narrator": meta.get("narrator", ""), "audiobook_year": meta.get("year"),
                "audiobook_discs_total": meta.get("discs_total"),
                "audiobook_discs_done": numbers, "audiobook_discs_ripping": [],
                "audiobook_release_id": meta.get("release_id", ""),
            })

    _start(_run_build, config, job_id, files, meta, find_cover(build_dir), tidy, give_back)
    return job_id


def finish(config) -> int:
    """Join the discs ripped in audiobook mode. Returns the build's job id.

    Refused while a disc is still ripping — a book built then is a book
    without that disc. Otherwise, under the lock, the book's folder becomes
    the build's (.building-<id>, with the book described in book.json beside
    the discs) and the mode is cleared: two drives finishing the last discs
    together cannot both build it, a book started during the build is a new
    one, and a restart mid-build finds the folder and builds it again.
    """
    import json

    with _lock:
        s = state(config)
        if not s["active"]:
            raise ValueError("No audiobook is being ripped.")
        if s["discs_ripping"]:
            raise ValueError(
                f"Disc {', '.join(map(str, s['discs_ripping']))} is still ripping; "
                "finish the book when it is done.")
        work = work_dir(config)
        if not (work.is_dir() and collect_files(work)):
            raise ValueError("No disc of this book has been ripped yet.")
        meta = {k: s[k] for k in ("id", "author", "title", "narrator", "year",
                                  "discs_total", "release_id")}
        (work / "book.json").write_text(json.dumps(meta), encoding="utf-8")
        build_dir = _building_dir(config, s["id"])
        work.rename(build_dir)
        config.update(dict(_CLEARED))
    return _launch(config, build_dir, meta)


def resume_builds(config) -> list[int]:
    """Start again every build a restart interrupted. Returns their job ids."""
    import json

    started = []
    root = _work_root(config)
    if not root.is_dir():
        return started
    for build_dir in sorted(root.glob(".building-*")):
        try:
            meta = json.loads((build_dir / "book.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Audiobook build folder %s has no book.json; left alone", build_dir)
            continue
        logger.info("Resuming the build of %s", meta.get("title"))
        started.append(_launch(config, build_dir, meta))
    return started


def guess_meta(config, folder: Path, files: list[Path]) -> dict:
    """Author and title for a folder nobody described.

    The files' own tags first — an album and an artist were put there by
    whoever made them — then the folder: 'Author - Title', or the usual
    Author/Title nesting.
    """
    _, tags = probe(config, files[0]) if files else (0.0, {})
    title = tags.get("album") or ""
    author = tags.get("album_artist") or tags.get("artist") or ""
    narrator = tags.get("composer") or ""
    year = (tags.get("date") or "")[:4]
    if not title:
        name = folder.name
        if " - " in name:
            left, right = name.split(" - ", 1)
            author, title = author or left.strip(), right.strip()
        else:
            title = name
            author = author or folder.parent.name
    return {"title": title, "author": author, "narrator": narrator,
            "year": int(year) if year.isdigit() else None}


def import_folder(config, folder: Path) -> int:
    """Build a book from a folder of audio files where it lies."""
    files = collect_files(folder) if folder.is_dir() else (
        [folder] if folder.suffix.lower() in AUDIO_EXTENSIONS else [])
    if not files:
        raise ValueError("There are no audio files in that folder.")
    base = folder if folder.is_dir() else folder.parent
    meta = guess_meta(config, base, files)
    job_id = _make_job(meta["author"], meta["title"], base.name)
    _start(_run_build, config, job_id, files, meta, find_cover(base) if base.is_dir() else None)
    return job_id


def fetch_cover(release_id: str, destination: Path) -> bool:
    """The front cover from the Cover Art Archive, for a MusicBrainz release."""
    import requests

    if not release_id or destination.exists():
        return destination.exists()
    try:
        response = requests.get(
            f"https://coverartarchive.org/release/{release_id}/front-500", timeout=15)
    except requests.RequestException:
        return False
    if response.status_code != 200 or not response.content:
        return False
    destination.write_bytes(response.content)
    return True
