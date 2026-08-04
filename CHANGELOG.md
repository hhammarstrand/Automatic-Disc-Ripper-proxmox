# Changelog

## 1.6.0

**Every diagnosis of this application ended with "paste me the output of
journalctl".** That needs a shell on the Proxmox host. The person looking at
the dashboard on their phone does not have one. That is a design failure, not
a support process — and it is the reason the last week of problems each took
several days instead of one message.

**A Logs page.** The application keeps its own rotating log beside the
database, so the web UI reads it in-process with no privileges and no journald
group membership. Filter by level — asking for WARNING keeps the ERRORs, which
is the point of asking — or search for a device path or a job number.
Tracebacks are kept whole with the line they belong to, because a traceback
filtered down to its first line is not a traceback. The tail is read backwards
from the end of the file, so a log from a month of uptime still opens
instantly.

**Copy diagnostics**, one button, on the Logs page and linked from Doctor. It
produces the whole picture as one block of plain text: version and host, every
setting, all the self-checks with their fixes, where each storage path really
points and whether it is on the share or the container disk, the drives, the
last three failures *with the tool output that explains them*, and the end of
the service log. Plain text because it is meant to be read by a person in a
message, not parsed.

Nothing that could authenticate as you survives in it. The redaction is a
whitelist of setting names known to be harmless rather than a blacklist of
suspicious ones, because the two mistakes are not symmetric: a setting left
out costs a follow-up question, a leaked token costs you the account. Tests
assert that each of the TMDb key, the Plex token, the notification token and
the notification URL — which for several providers *is* the credential —
cannot be found anywhere in the output.

---

## 1.5.1

**"One or more tracks failed to encode" is true of every encode failure there
has ever been.** It names the symptom, does not say which track on a
multi-title disc, and sends you to the log to find the one line that mattered
— which is exactly what the message in the history is for.

HandBrake says why it is unhappy on stderr and then exits with a number. Its
last meaningful line is now kept and put in the error itself, so the history
reads `Encoding failed: HandBrake exited with code 1. HandBrake said: Invalid
preset` instead of a sentence that fits every failure equally.

The reason is also stored per track, so a disc where one title of six failed
says which one. Several distinct failures are listed; identical ones are not
repeated. A failure HandBrake gave no reason for says exactly that, rather
than implying there is nothing to see.

**The Doctor page disagreed with the encoder about the preset.** With no
preset file configured it reported "using HandBrake's built-in preset", while
the encoder goes looking in `presets/` and imports whatever it finds. Two
answers to one question, and the wrong one was on the page precisely when a
preset is the suspect. The check now calls the encoder's own discovery, and
distinguishes a file you named — where a missing preset name is a real failure
— from one merely found, where HandBrake still resolves a built-in name and
the setup works.

---

## 1.5.0

**The rip showed a percentage and nothing else.** That tells you where you are
but not whether to wait or come back after dinner, and it cannot tell a slow
disc from a stuck one — which is the question that actually gets asked. The
encode has always had an ETA because HandBrake reports one; MakeMKV reports a
position and never a rate, so nobody derived it.

A rip now shows **time remaining, read speed and elapsed time**:

    Title 1/2 · 40% · 39m left · 6.9 MB/s · 15m elapsed

The speed is measured from bytes actually landing on disk, not from MakeMKV's
percentage. They are different questions, and only the first distinguishes a
healthy read from a drive re-reading one bad sector for twenty minutes — which
now looks like `2 KB/s · 30m elapsed`, with no estimate offered, because there
is nothing honest to estimate from.

Two decisions in `adr/progress.py` matter more than the arithmetic. The rate is
measured over a recent window rather than the whole run, so the scan and
spin-up a rip opens with do not drag the estimate up for the next forty
minutes. And nothing is reported until the samples support it: "4 hours
remaining" on a disc that finishes in twenty is worse than no estimate at all,
because someone walks away on the strength of it.

**A phase strip** — Identify · Rip · Encode · Library — sits above the bar.
One bar says how far along the current step is but not which step that is, so
sixty percent could mean nearly finished or barely started.

**What the tool is saying** gets its own line, so "Saving title 2 to file"
is visible without opening the log.

Also: the rip's timings now use a monotonic clock. They are only ever used as
differences, and an NTP step during a forty-minute rip would otherwise have
produced a negative elapsed time and a nonsense estimate.

---

## 1.4.2

The Storage page had the last copy of the same mistake. A folder inside a
mount is not a mount point, so a library at `/mnt/media/Filmer` fell through
to the final branch and was labelled **container disk — "this is local storage
inside the container"**, which is flatly untrue: it is on the share, which is
the whole reason it is there. A network share was saved from this by a
separate check for its filesystem type; a plain bind-mount was not.

It now says *inside a mount*, and names the filesystem it is actually on.

---

## 1.4.1

**A film library inside the share was refused, even with the share mounted,
writable and eight terabytes free.**

`require_completed_mount` asks one question: is my NAS actually attached, or am
I about to fill the container disk with films while believing otherwise. The
check answered a narrower one — `os.path.ismount`, which is true only of the
mount point itself. A share mounted at `/mnt/media` with the library at
`/mnt/media/Filmer` therefore failed, because a folder inside a mount is never
a mount point. Putting the library in a subfolder of the share is the ordinary
way to arrange one.

It now compares the filesystem the path is on against the container's root.
That accepts the subfolder, and still refuses a directory that merely has the
right name on the container's own disk — which is the whole reason the check
exists. The Storage page used the same narrow test and had the same blind spot;
both now agree, and say so in the same words.

---

## 1.4.0

**The app knew why every rip would fail and never said so until each one had.**

The pipeline has always refused to start a rip it knew was doomed — a
destination that is missing, read-only, or an unmounted NAS costs forty minutes
and several GB to find out the hard way. But it said so per disc, after the
disc went in, in a job that then sat red in the history. Eleven discs produced
eleven identical failures and not one statement of the single thing wrong.
Worse, the Doctor page had the answer the whole time, on a different page,
with no reason to look at it.

The dashboard now runs the same check with no disc in the drive and states it
at the top of the page: what is broken, and what to do about it. Pressing Rip
against a broken destination refuses on the spot with the same reason rather
than producing another red job.

It is the *same function* the pipeline gates on, not a second opinion. A
warning that disagreed would be worse than none — it would either promise a rip
that then fails or complain about one that would have worked — so a test
asserts the two produce identical text.

The advice now matches the fault. "Not a mounted filesystem" and "not writable"
have nothing in common but the word destination and need opposite actions: one
is a container restart, the other is re-running the NAS setup. Both carry the
command with the container id already filled in.

---

## 1.3.3

**A job that failed before ripping left an empty log.** The terminal icon in
the history is where you look to answer "why did this fail". Two failures wrote
nothing to it: the destination check, which runs before any tool does, and an
unhandled error in the pipeline itself. Those are the two most likely early
failures there are, so a run of red jobs could be entirely undiagnosable from
the interface — precisely when the interface is needed.

Both now write what happened, traceback included.

**The disc-type decision is recorded for every disc**, not only for audio CDs
and data discs. A video disc logged nothing, which left no way to tell
"classification ran and chose video" from "classification never ran" — the
first question worth asking when a disc that used to rip stops.

Also fixed: job logs default to a `logs/` folder beside the database, which in
the test suite meant the checkout. Tests wrote there and read each other's
files back, so an assertion about a log could pass on stale content from an
earlier run. Two of the tests added here did exactly that until the isolation
was fixed — the same mistake the database made before it was given a temporary
directory per test.

---

## 1.3.2

**On a phone the history page could not tell you why anything failed.** The
status column sits several columns to the right of the title, which on a narrow
screen is off the side of the screen entirely — so eleven red jobs looked
identical and unexplained, and the one piece of information on the page worth
reading was the one piece you had to scroll sideways to find.

The reason now sits under the title, in the first column, in red. Only the
first line, so a row stays a row; tapping it still opens the full text,
traceback and all.

---

## 1.3.1

**Every per-drive button was broken, and Rip said so in a way that sounded
like the drive was gone.** Pressing it answered *"DEV/SR0 is not a drive this
instance watches"* — a name that exists nowhere.

A Linux optical drive is identified by a device path, and the buttons put that
path into the URL, which means percent-encoding its slashes. Werkzeug does two
different things with that. For Rip it 308-redirected to the same path with the
leading slash removed, so the handler received `dev/sr0`, treated it as a
Windows drive letter, and upper-cased it into `DEV/SR0`. For auto-eject and
hide-drive the URL simply did not match any route: 404, the handler never
reached, the button silently doing nothing at all.

The drive now travels in the request body, where a slash is just a character.
The old URLs still work — a page already open in a browser goes on using them —
because a device path that has lost its leading slash is now recognised and
repaired rather than shouted back in capitals.

**"Test with MakeMKV" reported "Test failed: Load failed" on a phone.** The
probe allows five minutes, which is what a Blu-ray with many playlists needs,
and it held the HTTP request open for all of it. A phone browser gives up long
before that, and the page could then only say the request failed — which reads
as a broken drive when the drive is fine and still reading. The test now starts
in the background and the page asks every three seconds how it is getting on,
showing the elapsed time while it waits. Asking twice joins the probe already
running rather than starting a second one, because two MakeMKV processes on one
drive is how a working drive is made to fail.

---

## 1.3.0

**Every disc went to MakeMKV, including the ones with no video on them.** An
audio CD came back as "no titles found". So did a data disc. So does a drive
the container cannot open — and that is the problem: three unrelated causes
produced one indistinguishable failure, and the one people acted on was the
hardware one, which was fine.

A disc is now classified before anything touches it, from its table of contents
and its ISO 9660 root directory. Nothing is mounted; no extra privileges are
needed. A disc that cannot be read at all is still treated as video, because
that is exactly what the application did before this existed — a pure-UDF
Blu-ray with no ISO 9660 descriptor takes that path and keeps working.

**Audio CDs** are extracted with cdparanoia and encoded to FLAC or MP3 with
ffmpeg, tagged from MusicBrainz. cdparanoia rather than a plain read because CD
audio has no error correction worth the name: a scratch does not produce a read
error, it produces a click, and cdparanoia re-reads until the samples agree.
The MusicBrainz disc ID is computed here from the track layout rather than
pulled in as a C dependency — it is a published, fixed algorithm, and it is
checked against MusicBrainz's own worked example in the tests.

Output is `Artist/Album (Year)/01 - Title.flac`. A CD nobody has submitted
still rips, filed under its disc ID, which is stable — so the same disc always
lands in the same folder instead of accumulating "Unknown Album (2), (3), (4)".
One unreadable track costs that track, not the album.

**Data discs** are kept as byte-for-byte ISO images. Only the recorded area is
read: a drive routinely claims more capacity than the disc holds, and reading
past the end produces I/O errors that look like failure and are not. Read
errors are retried before being believed, and a copy that fails is deleted —
a half-written ISO that looks complete is worse than none.

Both can be turned off under Settings, in which case the disc is left alone and
the job is closed as cancelled rather than failed. Nothing went wrong; a red
job and a failure notification would be lying about a setting you chose.

**Transcoding can be turned off**, under Settings → Encoding. The MKV is kept
exactly as MakeMKV produced it: lossless, and minutes rather than hours, at
several times the size. Everything after the encode is unchanged, because the
task still goes through the same worker — the same folder, the same rename, the
same transfer, the same notification. Three places assumed the finished file
was an MP4 and would have reported a folder full of films as empty: renaming,
retrying, and the collision check that stops two films sharing a folder.

**Extras no longer land on the end of the film.** With main-feature selection
off, a disc's trailers were named `Film (1999) - pt2` — and Plex *stacks*
numbered parts, so a two-minute trailer became the back half of the movie. When
one title is at least 1.5× longer than the next it is the feature, and the rest
go to `Other/`, one of the eight folder names Plex recognises and the only one
that does not claim to know what the extra is. Titles of similar length are
still numbered parts: that is what a two-part film looks like, and calling half
a film an extra is the worse mistake.

**A restart mid-job left it stranded for ever.** A job's progress is in the
database; the thread doing the work is not. Press Update with a disc in — a
normal thing to do — and the job said RIPPING until someone noticed, with the
drive it named permanently busy so the card offered no way to start again.

Every job that was running is now dealt with at the next start, according to
what is actually still on disk. Mid-rip is failed, because a killed rip leaves
a truncated file and nothing else. Mid-encode is *resumed*: the raw MKVs are
intact and the expensive part is done, so it simply goes back on the queue —
asking someone to press Retry after every update, for work the machine can
obviously pick up itself, is not a reasonable thing to ask. A job waiting to be
moved is failed pointing at Retry, which re-checks the destination first, since
the destination may well be why the service was restarted. No notifications for
any of it: a burst of failures after every update trains you to ignore them.

**A MakeMKV that stopped talking held the drive for ever.** The read loop waits
on a pipe with no timeout, which is right — a Blu-ray with many playlists takes
hours and a slow disc must not be thrown away. But nothing distinguished slow
from stuck, and stuck meant the drive, the job and the thread were gone until
the service was restarted. Thirty minutes of *complete* silence — no progress,
no messages, nothing — now ends the rip, with an error that names the one test
that separates a bad disc from a bad drive: another disc in the same drive.

**Two background threads could be killed by one bad moment.** Both had the same
shape: a call outside the `try` block whose failure escaped past the cleanup.

In the drive pipeline that call was opening the database session, and the
cleanup it jumped over was releasing the drive's lock. A locked database for
one moment meant the drive was busy for the life of the service — no disc in
it, nothing running, and the dashboard offering no way to start again. In the
encoder worker it meant the thread simply died, and every later encode queued
up behind a consumer that no longer existed. Nothing said so: a dead daemon
thread is silent, and the queue just grows.

**The history page got slower every week.** It fetched every job ever run, and
the template then asked each row for its tracks — one query per row. Both the
paging and the status filter are now done by the database, so the page costs
the same on the thousandth disc as on the first.

Also fixed, found while testing the above: killing a timed-out tool killed only
the process we started. Anything *it* had started kept the output pipe open, so
the reader thread blocked on a read that would never return and closing the
pipe waited on that read — which turned the timeout, the mechanism that exists
to stop us hanging, into a hang. Tools now run in their own process group and
the whole group is signalled.

---

## 1.2.6

**A disc that failed had no way to be tried again without ejecting it.**
Ripping only ever started on the empty→loaded transition, so once a job failed
the disc sitting in the drive was inert: the only way to retry was to open the
tray and close it again. Every idle drive card now has a **Rip** button that
starts a rip on the disc already loaded.

It refuses rather than misbehaves — an unknown drive, a drive already ripping,
a drive disabled under Settings, and an empty drive each get a specific reason
instead of a silently queued job. The "disc inserted" notification is
suppressed for a manual start, since you are the one who started it.

---

## 1.2.5

**`adr-setup-nas` wrote an empty SMB password and let the mount fail.**
`NAS_USERNAME` was required for `smb://`, `NAS_PASSWORD` was not — so omitting
it produced `mount error(13): Permission denied` and the advice "check
credentials and share name", which reads as a *wrong* password rather than a
missing one.

It now prompts when there is a terminal, refuses with the exact re-run command
when there is not, and never writes a blank credential. If Proxmox already
stores a password for a CIFS storage on the same server it says so, with the
command to reuse it — the machine already knows the secret; there is no reason
to make someone find and retype it.

Mount failures also print what `mount` actually said, and error 13 in
particular is named for what it is: the server rejecting the credentials.

---

## 1.2.4

**The MakeMKV scan gave up sooner than a real rip does.** The diagnostic used a
90-second limit; `ripper.scan_disc`, which an actual rip uses, allows 300. A
Blu-ray with many playlists routinely needs minutes, so the check reported a
perfectly good disc as a failure — and a diagnostic that fails where the real
operation succeeds is worse than none, because it sends you off to debug
something that was never broken.

The limit now matches the real path, with a test that fails if the two drift
apart. More importantly, the output is read as it arrives, so a timeout can
tell the two cases apart: output still arriving is "slow, and answering" (a
warning, naming what it was last doing), while nothing at all in five minutes
is "the drive is not answering" (a failure). The old message admitted it could
not distinguish them; it did not have to.

Two test-hygiene bugs surfaced while fixing it, both the same shape — patching
a shared stdlib attribute reaches far outside the code under test. Stubbing
`os.close` broke `subprocess.Popen`'s pipe handling and hung the suite;
patching `subprocess.Popen` broke a type annotation evaluated by a later
import. Both now use narrow seams.

---

## 1.2.3

**`update.sh` overwrote itself while running.** Bash reads a script
incrementally by byte offset; the copy step replaces the whole of `/opt/adr`
including `update.sh`, so bash carried on reading at its old offset inside a
different file. The result was a syntax error on an arbitrary line — with the
service already stopped:

```
/opt/adr/scripts/update.sh: line 81: syntax error near unexpected token `('
```

It had survived on luck: as long as the file's length happened not to shift the
following bytes into something unparseable, nothing went wrong. Any change to
the script could break the *previous* version's update.

The script now re-executes from a private copy in `/tmp` before doing anything,
so the bytes bash is reading can never change underneath it.

---

## 1.2.2

- **A failed update no longer leaves the application stopped.** `update.sh`
  stops the service partway through, and everything between then and the
  restart runs under `set -e` — so a failing `pip install`, a full disk or an
  unwritable file aborted the script with nothing running. That presents as
  "the web UI is gone" with no clue why. The exit trap now always brings the
  service back: a failed update that leaves the previous version running is a
  bad afternoon, one that leaves nothing running is a broken appliance.
- **pip's output is no longer swallowed.** It was run with `--quiet`, which
  hid the reason for the step most likely to fail. Its output is captured and
  shown.
- **The startup banner reports the real version.** It was hardcoded to
  `v1.0.0`, so the log disagreed with what was installed — which makes every
  "which version is this?" question worse.

---

## 1.2.1

### "Have I already ripped this?"

There was a duplicate check, but a weak one: it matched only the disc label,
ran *before* identification (when the label was all that was known), and did
nothing but put a badge in the history afterwards. It missed a different
pressing of the same film, a cleared history, and anything already in the
library that this application had not put there.

It now runs after identification and asks three questions in descending order
of authority: does the film's folder already hold video at the destination
(the only check that is true rather than remembered), is there a completed job
with the same TMDb id, and finally the disc label. Better evidence overrules
worse — a shared label between two discs TMDb has called different films is a
coincidence, not a duplicate.

**Settings → Duplicates → Skip discs already ripped** makes it actually stop
the rip, before MakeMKV starts. Off by default: re-ripping is legitimate when
the first attempt came from a scratched disc, and a false positive that
silently cancels a disc is harder to notice than one that only warns.

Series discs are exempt — every disc of a box set writes into the same show
folder, which is the normal case rather than a warning.

---

## 1.2.0

### Series mode

Marking one disc as television was already possible. Doing it for six discs of
a season meant re-entering the show, the season and the starting episode each
time — and that last number is both easy to get wrong and expensive to get
wrong, since a third of the season then carries the wrong episode numbers.

**Rip a TV series** makes those answers sticky and adds a counter. Set the show
and season once, then feed discs: each takes the next block of episode numbers
and the counter advances by however many titles that disc actually produced.
Disc 2 starts at episode 5 because disc 1 yielded four, not because anyone said
so.

The counter advances when the tracks are queued, which is the moment the
numbers are spent — advancing at insert time would mean guessing the count, and
advancing at completion would let a second drive hand out the same numbers
first. It is lock-guarded for exactly that reason, and lives in the config file
so a restart mid-box-set resumes where it left off.

While the mode is on it overrides both the duration heuristic (someone has said
what these discs are) and the film identification. That last one was a bug
found while wiring it up: TMDb identification runs *after* job creation and
would have overwritten the show with whatever film the disc label resembled.

It does not expire, and every page carries a banner saying it is on — a mode
that renames every disc it sees has to be visible from wherever you are
looking, and one that switched itself off on a timer would silently number
discs 4-6 of a season from 1 again.

---

## 1.1.2

- **The HandBrake preset is now a Doctor check.** A preset name missing from
  the file it is supposed to come from makes every encode fail identically,
  with HandBrake's output as the only clue. The logic already existed behind
  `/api/preset-check`, which nothing called; the route and the check now share
  one implementation rather than two that can disagree.
- **The TV thresholds are settings.** What counts as an episode — shortest,
  longest, how many — is a judgement, not a fact, and someone's box set will
  always sit outside the default. **Settings → Television**, plus a switch to
  turn detection off entirely.
- **A verdict now says what it saw.** "Not enough similar titles" said nothing
  about which titles there were. The reason lists every title length on the
  disc, so a wrong guess is correctable. Writing that test found the formatter
  rendering a 2:16:00 feature as `136:00`.

---

## 1.1.1

Three things shipped in 1.1 that were built but not connected, found by
sweeping every route and public function for callers rather than by assuming:

- **Retrying a series brought it back as a film.** `adr.retry` predates
  television and rolled its own filenames, so a retried season landed as
  `Show (2002)/Show (2002) - pt1` — wrong folder, wrong names, wrong library.
  It now asks `adr.naming` like everything else does.
- **The TV show could not actually be corrected.** Identification runs TMDb's
  *movie* search, which for a box set returns a confident-looking film; the TV
  search endpoint existed but nothing called it, so a season would be named
  after whatever film the disc label resembled. The dashboard now looks the
  show up in the TV namespace, previews the numbering with real episode titles,
  and clears the film's poster and TMDb id when the show is replaced.
- **Two dead functions** written and never called: one removed as redundant,
  one wired in so the SG_IO failure names the actual `/dev/sg` node instead of
  saying "the drive's sg node".

---

## 1.1.0

Everything from the Windows version was already here — verified module by
module, route by route, and setting by setting; the only Windows-exclusive file
was a Tkinter setup wizard whose job the installer and Settings page already
do. So this release is about what a disc ripper should have and did not.

### Television discs

A box-set disc is six episodes of similar length with no "main feature", and
main-feature selection would have ripped the longest and silently discarded the
rest. Discs are now recognised as television from their title durations, and
named `Show (2019)/Season 02/Show (2019) - S02E05.mp4` under a separate
`tv_path`. Detection only annotates — the user confirms the season and starting
episode before encoding, since calling a film a series renames it into a season
folder and that is much worse to undo than the reverse.

### Notifications, and telling Plex

ntfy, Gotify, Discord or a raw JSON webhook, on a disc finishing, a rip
failing, or a disc being inserted. Plex is told to scan the moment a film lands
instead of it staying invisible until the next scheduled scan. Both are
best-effort: a service being down never fails a rip.

### Diagnosing and recovering

Per-job logs capture what MakeMKV and HandBrake actually said, shown in the UI
rather than living in `journalctl` behind `pct exec`.

Retry resumes a failed job from the furthest point that still has its files —
the encoded files intact means redo only the transfer, raw MKVs intact means
redo only the encode, neither means say so. A rip is forty minutes; losing all
of it to an unmounted NAS was the difference between an annoyance and finding
the disc again.

Discs that were ripped before are flagged in the history, without blocking.

### Also

- `tests/conftest.py`: every test that touched the database shared one
  `adr.db` in the checkout, so rows leaked between tests and the suite failed
  for anyone who could not write there. Each test now gets its own.
- `adr.yaml.example` is generated from the defaults, with a test that they
  cannot drift — the example is what a fresh install copies, so a key missing
  from it is a setting no new user can discover.

---

## 1.0.0

The first release meant to be run by someone other than its author. Two things
drove it: optical passthrough that survives a host reboot, and a folder layout
you can explain to yourself six months later.

### Optical drives

- **Guest autostart now waits for the drive.** Passthrough entries are written
  with `optional`, so a device that does not exist *yet* is skipped silently —
  and a device node cannot be added to a running container. If
  `pve-guests.service` beat udev at boot, the container came up with no drive
  and stayed that way. This is why passthrough worked right after installing
  (the container is started by hand, long after udev) and broke on the next
  reboot. `pve-guests.service` is now ordered after the drive's `.device` unit.
- **A disc the container cannot open is no longer reported as a disc.** Inside
  an LXC `/sys` is the *host's* sysfs, so `/sys/block/sr0/size` shows the disc
  in the host's drive whether or not this container may touch it. Media
  detection trusted that first, so a cgroup denial produced a dashboard with a
  loaded disc, a rip job that started, and a MakeMKV failure a minute later
  saying nothing useful. Access is confirmed before capacity is believed.
- **The dashboard says what is wrong**, distinguishing "the node never arrived"
  (restart the container) from "the node is there but the device cgroup denies
  it" (`lxc.cgroup2.devices.allow: b 11:* rwm` — block, not char).
- **A denial is logged once per device**, with the command that fixes it,
  instead of once every three-second poll.

### New: `adr-doctor`

A host-side check-and-repair, because everything above is invisible from inside
the container:

```bash
adr-doctor <CTID>          # report only
adr-doctor --fix <CTID>    # repair
```

It checks the device cgroup rules, a passthrough entry per drive plus its
`/dev/sg` node, guest-autostart ordering, and the storage layout — then asks
the container itself whether it can open each drive, using the same code the
dashboard does.

### Folder layout

**Breaking for existing installs.** A NAS share used to be bind-mounted over
`/opt/adr/completed`. It worked, but it made half the application's own
directory somebody else's filesystem: a routine `chown -R /opt/adr` walked into
the user's film library, and an offline share was indistinguishable from an
empty app directory.

```
/opt/adr/            the application — code, database, scratch space
  ├─ raw/            raw MKVs off the disc   (local, deleted after encoding)
  ├─ staging/        HandBrake writes here   (local, always)
  └─ completed/      finished films, if you have no NAS
/mnt/media/          finished films, if you do
```

Nothing is ever mounted inside `/opt/adr`. `completed_path` points at whichever
of the two you use.

**To migrate**, on the Proxmox host:

```bash
adr-doctor --fix <CTID>
```

It moves the mount to `/mnt/media`, updates `completed_path`, and restarts the
container. Your files are not moved or touched — they are already on the NAS.

### Films go straight to the Plex library

A job bound for Plex was written to `completed_path` first and moved
afterwards. With the library on a NAS that is a multi-GB network write into a
folder nothing reads — and if the two paths are on different mounts, a second
full copy on top. The finished folder now crosses the network **once**, into
the folder it will live in:

```
/dev/sr0 → /opt/adr/raw → /opt/adr/staging → <plex_path>
           (local)        (local)            (the only network write)
```

The pre-rip check now verifies `plex_path` too, and the Storage page judges
free space, writability and mount state on the path films actually land in —
previously it could show a healthy `completed_path` while the library it never
looked at was full.

### Testing a drive, instead of inferring that it works

Everything in this app observed the drive passively: sysfs says a disc is
loaded, the node exists, `open()` succeeded. Enough to run on, not enough to
answer "is my drive working?" — which is the question you have when nothing is
happening and you cannot tell whose fault it is.

**Doctor → Optical drives** now pokes it: device node → open → drive status
(tray state and disc type, straight from the drive) → generic SCSI → read a
sector. Each step is reported separately and the chain stops at the first
failure, since everything after it is a consequence.

The SG_IO probe is the one that earns its place. A drive can open cleanly,
report a disc, and still refuse SG_IO — and because MakeMKV is the only thing
that uses that interface, the dashboard looks healthy right up until every rip
fails. **Test with MakeMKV** goes further and opens the disc for real, the only
check that exercises the registration key end to end.

**Scan for drives** re-reads sysfs and hot-adds anything new instead of waiting
out the watcher's 30-second cache. A drive the host has but the container did
not get is reported as its own thing, pointing at the host — scanning cannot
fix it, because a device node cannot be added to a running container.

### Doctor page, and updating from the browser

**Doctor** in the web UI runs everything the container can check about itself —
optical drives, MakeMKV and HandBrake, the registration key, the destination
path, scratch space, the database — and says what to do about each failure. A
count of failures rides along in the navbar on every page, so a problem finds
you before the failed rip does. The checks the container *cannot* run need
`pct`; the page hands over the `adr-doctor --fix <CTID>` command rather than
guessing.

The same page checks GitHub for a newer commit and applies it with one button,
streaming the log. The app runs unprivileged with `NoNewPrivileges=yes` and
cannot install its own update, which is deliberate: it *requests* one by
touching a flag file, and `adr-update.path` starts the root-side
`adr-update.service`. Repository and branch live in that unit, never in the HTTP
request, so an unauthenticated caller on the LAN can only ask for the update the
machine's owner already configured — not point it somewhere else.

The installed commit is recorded in `/opt/adr/.commit`. An install made before
1.0 has no such file; the page says "cannot tell" rather than offering a phantom
update on every load.

### Other

- `update.sh` no longer recursively chowns `$INSTALL_DIR`, which on a pre-1.0
  install would have rewritten the ownership of every film in the library.
- The Storage page no longer warns about writing to the container's own disk
  when that is the configured setup. It warns once network storage has been
  attached and is no longer mounted — the case where rips would silently fill
  the container disk.
- Container disk default raised to 100 GB.
