# Changelog

## 1.23.0

The general review reached the three modules nothing had ever reviewed —
audio CDs, disc images and the watch folder — and the fixes from it are here,
alongside the review of the review's own leftovers.

**A privilege hole reopened by its own fix, closed again.** The update's
busy-check ran the service user's Python as root — the venv and the adr
package are deliberately service-user-owned, so root importing them executes
service-user-writable code as uid 0, the exact hole the root-owned scripts/
directory exists to close, reopened one block above it. The check now runs as
the service user.

**Two ways to lose a file in the watch folder.** Dropping a second file with
the same name silently replaced the one being processed — POSIX rename
overwrites — so one user file was destroyed without ever being encoded; and
the crash-restore path had the same clobber in reverse. Both now step aside
instead. Watch encodes also never staged: a NAS output path had HandBrake
writing across the network for the whole encode, the very thing staging
exists to prevent, quietly absent from one of the two paths that encode.

**A crash mid-image left a truncated ISO that looked complete.** The image
was written straight to its final name; a process death — an update, an OOM
kill, a power cut — left it squatting the canonical name so the good re-image
landed at "(2)". Images are written as .part and renamed only on completion,
stale parts are swept, the destination is gated by the same checks every video
disc passes (an unmounted NAS used to send an 8 GB image onto the container's
root disk), and the disc must fit in the free space before hours of reading
rather than at ENOSPC after them.

**Every Enhanced CD identified as nothing.** An album with a bonus data
session — half the CDs pressed after the mid-nineties — hashes its disc ID
from the audio session alone, with the lead-out at the data session's start
minus 11400 frames. Hashing the whole TOC produced an ID no release in
MusicBrainz has, so the lookup that would have named the album never matched.

**Cancelling an audio CD now actually stops it**: killing the running
cdparanoia only failed that track, and the loop started a fresh one for the
next — a fifteen-track CD cancelled at track two ripped the other thirteen.
And an artist name of ".." arriving from MusicBrainz can no longer step out
of the music folder.

**Recovery agrees with Retry.** After a crash it tried the raw files first —
the opposite order to the Retry button — so a job that crashed during the
transfer re-encoded everything while a complete encode sat in staging. It now
demands the same evidence: every track saying DONE before finished files are
trusted. A retry also honours "main feature only" for the titles the setting
deliberately kept unencoded, and a partially-consumed series renumbers its
survivors from its own track rows rather than from one.

**And the browser stops lying.** The Online badge was a hardcoded string,
shown identically whether the last poll answered or the service had been down
an hour — it now reflects the last poll. Polling loops no longer stack
requests on a slow server. The re-encode confirmation is red, like every
other action that replaces a library file. Nine pieces of help text and
documentation that described last week's behaviour now describe this week's.

## 1.22.0

**The app now fits a phone, verified in a real browser rather than by eye.**
Every page was rendered in headless Chromium at iPhone widths (390px and
320px) and measured: the layout viewport stays at device width and nothing
escapes it. Before this, the history page forced the viewport to 1260px —
which on an iPhone is exactly the sideways panning that was reported.

The history table is the big change. Fourteen columns in a sideways-scrolling
strip is unusable with a thumb, so below tablet width it becomes **cards**:
title as the heading, status, completed time, total time and path as labelled
rows, the buttons in a row at the bottom, the select-checkbox in the corner.
The low-value columns — drive, FPS, per-phase times — are desktop-only.
Nothing changes on a desktop.

With it: tap targets grow to Apple's 44px floor on phones (the table's icon
buttons were 26px, and the delete button sits next to retry), the bulk-action
bar wraps, the dashboard's recent-jobs table is contained, and
`overflow-x: clip` on the root is the backstop so one regressed element can
never make the whole app pan again.

**And the UI no longer needs the internet.** Bootstrap was served from a CDN,
so the dashboard of a LAN appliance rendered as bare unstyled HTML at exactly
the moment the internet was down — while the ripper underneath kept working.
The assets are vendored and served from the container.

## 1.21.2

**Two films ripped, and then every disc after them was refused.**

A set-top DVD recorder writes its own name onto every disc it burns, so a whole
shelf of home recordings all carried the label `LG_COMBI_RECORDER`. The first
was ripped and named by hand — which is the proof the label never identified it
— and every disc after it matched that label and was cancelled as a duplicate.

The mechanism was doing what it was told; what it was told was wrong.
`find_previous_rip` has said since it was written that a disc label "is not a
unique identifier" and "only ever annotates a job, never blocks one", and
`skip_duplicates` blocked on it anyway. The code contradicted its own
documented design, and nothing noticed because the two live in different files.

A label match is now reported and the rip proceeds. The two checks that compare
the *film* — the library already holding it, and a finished job with the same
TMDb id — still skip, which is what "skip discs already ripped" is for.

Separately, labels that name the equipment rather than the film are ignored
outright. Listing brands would never keep up, so the word is what gives it
away: `RECORDER`, `COMBI`, `DVDR`, `RECORDING`, `CAMCORDER`.

## 1.21.1

The thirty fixes in 1.21.0 were reviewed the same way they were found, and
thirteen of them had problems — three that would have cost files. **Do not run
1.21.0.** This is the version to update to.

**Every button in the history and dashboard was dead.** 1.21.0 replaced
`'{{ x | e }}'` with `{{ x | tojson }}` inside double-quoted `onclick`
attributes. tojson escapes `<`, `>`, `&` and `'` — but not the double quote,
and its output *begins* with one, so the attribute ended at `copyPath(` and
every handler was a truncated syntax error for every job. Strictly worse than
what it replaced. The attributes are single-quoted now, which is the one
quoting tojson is safe inside.

**Deleting one disc of a box set deleted the season.** The merge means every
disc's job points at the same season folder, and `cleanup.job_files` falls back
to listing that folder — so "delete this job's files" offered, and removed,
every episode six jobs had produced. A series job's files are now exactly its
track rows, which the transfer keeps accurate. The same widening fed "Encode
again" and the file listing.

**Cancelling a job with transcoding off destroyed the rip.** The cancel branch
unlinked the encoder's output to clear a truncated file — but with transcoding
off there is no encode: the ripped MKV is *moved* into place, so that path is
the complete film and its source no longer exists. It now removes only a
genuinely partial encode, and a cancel that arrives after the file is finished
leaves the track DONE.

**And the season merge silently overwrote episodes.** The comment claimed a
collision would be "visible per file"; nothing implemented it. The incumbent is
kept and the arrival set aside beside it, named and logged.

Also: `setup-nas.sh` aborted on every run — a `sed` address delimited with `#`
on a pattern beginning with `#` — after writing the credentials file and before
touching fstab. The new language rule rejected valid ISO codes like `hun` and
`ces`, and because the settings form posts every field on every save, an
install with one could change no setting at all; only a two-letter code that
cannot be expanded is refused now. `_cleanup_raw`'s arithmetic did not hold
with transcoding off, where the encoded file has already left the raw
directory. The quality warning called a function that was never added. And the
two copy buttons defined in page-local scripts never got the plain-http guard.

## 1.21.0

Every remaining finding from the review, fixed. Thirty of them, across the
naming rules, the encoder settings, the device probes, the templates, the
shell scripts and the data layer.

**Television took the worst of it.** Disc 2 of a season landed in
`Season 02 (2)` — the collision rule is a film rule, and a folder already
taken means *the previous disc of this season*, which is exactly where the
episodes belong. A six-disc box set produced six season folders with four
episodes in each. Season 0 became season 1, so a specials disc was named into
the real first season where the numbers collide with actual episodes. And two
drives fed discs a minute apart both produced S02E01–E04: the numbers were
read when the disc went in and only spent once the rip finished, so the read
and the write were minutes apart with another drive in between. They are
claimed atomically now, before the plan is built.

**Extras were flattened out of `Other/` by every path that moved a folder**,
so the row named a file that did not exist — the delete preview could not see
them and Play pointed at nothing. With transcoding off they were never created
at all: the passthrough path made the output directory and not the file's
parent, so the move failed for every extra and took the job with it.

**`_cleanup_raw` deleted the titles the job log had just promised would
stay.** When "main feature only" cannot scan the disc it rips everything and
encodes the film, and says the rest are still in `raw/`. They were gone by the
time anyone looked. The evidence that titles were kept on purpose is
arithmetic — more files on disk than the job has tracks — so it needs no
schema change and cannot drift from the decision that produced it.

**"Encode again" left the previous copy in the library**, referenced by no job
row and therefore invisible to the delete preview. The finished-file path now
removes each superseded file once its replacement exists and has bytes in it.

**Four separate places built JavaScript by concatenating data**, and a film
called *Ocean's Eleven* broke each of them. Jinja's `|e` emits `&#39;`, which
the browser turns back into an apostrophe before the JS parser sees it. And
every copy button did nothing at all over plain http, which is how this
application is served by design.

Also: `PRAGMA busy_timeout` silently overrode the configured lock timeout with
one six times shorter; a cancelled attempt's leftovers counted as a finished
film, so the disc that failed once could never be ripped again; the drive
probe called "the drive did not answer" the same as "no disc" while the
watcher did not; a language code outside the ffmpeg backend's table was
accepted and then matched nothing on one of the two encoders; cancelling an
audio CD was recorded as a failure and sent a notification; and the fstab
marker the uninstaller searches for was never written by the installer.

## 1.20.1

The rest of the review finished — 63 agents, 32 findings that survived a
skeptic. Both criticals are fixed here; the remaining thirty are triaged in
the commit message and will be worked through in order.

**The guard that stops an update killing a running rip had never once fired.**
It grepped `/api/status` for a `"status"` field. That endpoint returns drives,
queue size, worker count and watch-folder state — a job status has never been
among them — so the pattern could not match, and every update since the guard
was written was free to stop the service on top of a rip. Which is what kept
happening: `MakeMKV exited with code -15`, twice, on the same disc.

It now asks `updater._job_in_progress()`, the same check the Update button uses
to withhold itself. One question, one answer, no serialisation format in
between. When the check cannot run it still proceeds — a broken venv is itself
a reason to update — but it says so, because failing open in silence is the
bug being fixed.

**And the encoder test ran HandBrake in a different environment from a real
encode.** `encoder.encode()` sets `LIBVA_DRIVER_NAME` from the config; the
probe did not. So on the machines where the driver name is the entire reason
Quick Sync starts — the case this application has a setting for — the test
vouched for a setup it had not run. Worse: *Use HandBrake with the GPU* pins a
driver and then re-runs this test to prove it, so the test ran without the
thing just pinned, failed, and put every setting back.

**One test was passing over a run that had crashed.** A stub whose signature
had drifted from the real method raised `TypeError`; the pipeline recorded
"Pipeline error" and the test, which only asserted that an early line appeared
in the log, went green. The helper every test in that file uses now asserts
that line's absence, which closes the class rather than the instance.

## 1.20.0

A second critical review, this time by nine independent reviewers with a
skeptic behind each finding whose job was to refute it. Five defects survived
and are fixed here; two more are confirmed and named at the end.

**A privilege escalation.** `adr-update.service` runs
`/opt/adr/scripts/update.sh` as root, and the installer's recursive `chown`
walked into `scripts/`, so the file was owned by the unprivileged service user.
`ProtectSystem=full` does not cover `/opt`. Anything running as `adr` — which
includes the deliberately unauthenticated web UI — could have rewritten that
file and then asked for an update, and systemd would have executed the new
bytes as uid 0. That is the whole privilege separation this application claims
to have. `scripts/` and `systemd/` are now root-owned, in the installer and in
the update, because fixing only one of the two would have re-opened it on the
next update.

**A half-written encode was published to the library as a success.** HandBrake
writes straight to the final name with no temp file, so a job killed at 60% —
Cancel, a full disk, a source read error — leaves a truncated MP4 that a
directory listing cannot tell from a finished one. Retry called that "intact",
moved it into Plex, marked the job DONE and cleared the error, while the raw
MKVs that would have re-encoded perfectly sat unread. The rip branch beside it
has always refused to trust a directory listing for a process killed part-way;
the encoded branch now gets the same evidence test — every track saying DONE.
A cancel during encoding also marks the track and removes the stub instead of
leaving both behind.

**A cancellation could be silently overwritten.** The pipeline holds a job
object for the length of a rip while the cancel endpoint writes to the same row
from another thread. Between the last check and the write of ENCODING sit the
RIPPED write, a synchronous eject that blocks while the drive spins down, and a
commit per track. Cancel in that window and the job encoded, transferred and
announced itself as done.

**The television library was never preflighted.** Every series job goes to
`tv_path`, and `tv_path` appeared nowhere in the checks — so a box set passed
against a healthy film library and was then written onto the container's own
disk, or failed on `mkdir` after the whole rip.

**And "delete the files" deleted nothing for an audio CD or a data disc**,
while telling the user no files were found. Both created their track rows
without a path, so the preview fell back to looking for `.mp4` and `.mkv`. The
only way forward then dropped the row that was the sole record of where three
gigabytes had gone.

Two confirmed defects are **not** fixed here, because both change where files
land and deserve their own change: the second disc of a season lands in
`Season 02 (2)` instead of merging into the existing folder, and re-encoding
from the finished file leaves the old copy behind and permanently renames the
job's folder.

## 1.19.0

A critical read-through of everything the last week added, and the worst bug
this repository has had.

**A box set could be reduced to one episode.** "Main feature only" is on by
default. A TV disc is detected as a series and every episode is ripped —
correctly. But the main-feature choice then ran anyway, and six titles of 42
minutes have no 1.5× gap between them, so the timid rule declined and every
fallback added behind it picked the longest *episode* and called the other
five extras. They were dropped, the survivor was named `S02E01`, and nothing
said a word. `plan_output` has guarded this since it was written; the two
functions in front of it did not, and nothing checked that the guard was
reached.

The rule now lives in one function, `naming.feature_index`, which declines
outright for a series — and all three callers go through it.

**Which is the second finding: there were three callers, and two of them had
been left behind.** A retry and an encode-again both named their files
`pt1`…`pt16`, exactly the naming a fresh rip had stopped producing. They work
from files on disk and have no durations to read, so size decides, and
`main_feature_only` is deliberately not consulted there: that setting decides
what gets *ripped*, and these files are already ripped.

**A scan stopped by a service restart is no longer called a cancellation.** A
signal is a signal; only the database says which of the two it was, and
claiming a restart was a cancellation hid the restart from the recovery pass.

Also: four harmless settings were being redacted from the diagnostics bundle
for no reason, and the README described the audio layout, the HandBrake
arguments and the Media SDK as they were three versions ago.

## 1.18.3

**The update took the GPU away again, and it was the update's fault.**

`intel-media-va-driver-non-free` and `intel-media-va-driver` Conflict with and
Replace one another: installing the second removes the first. The GPU install
added in 1.17.2 listed both and installed whatever `dpkg` reported missing —
so on a container that already had the non-free driver, the free one counted
as absent, apt swapped them, and the update silently downgraded the machine.

What that cost, visible in one diagnostics bundle before and after:

    hb encoders  qsv_h264, qsv_h265, qsv_h265_10bit   ->  none (software-only build)
    encode profs H264 x3, HEVC Main, HEVC Main10,     ->  H264 x3, JPEG
                 JPEG, MPEG2 x2, VP8
    ffmpeg gpu   h264, hevc                           ->  h264

Quick Sync stopped starting, HEVC encode disappeared, and the next encode
failed with `encqsvInit: qsv is not available on the system` — on a machine
where it had been working the day before.

The driver is now a choice satisfied by any one member, tried best first and
stopped at the first that installs, in all three places that install it: the
container installer, the update, and `adr-doctor --fix`. `i965-va-driver` is
last and only reached when neither iHD build exists; beside iHD it gives libva
two drivers to choose between for the same chip. The Quick Sync runtimes stay
a genuine list — `libmfx1` and `libmfxgen1` cover different silicon and do not
conflict.

**To recover a container this already hit**, reinstall the good driver:

    pct exec <CTID> -- apt-get install -y intel-media-va-driver-non-free

## 1.18.2

Two ways the naming could still land on `Unknown - pt1.mp4`, closed.

**Size decides when duration cannot.** Duration is read out of MakeMKV's TINFO
records and matched to files by name, and every step of that can come back
empty — at which point the feature could not be identified and sixteen files
were numbered `pt1` to `pt16`, which Plex stacks into one movie. A 968 MB file
beside fifteen between 9 and 83 MB is not ambiguous about which one is the
film. Duration still outranks size, because length is what "the feature"
means; size is the last resort rather than a second opinion.

**And MakeMKV knew what the disc was called.** `RipResult.disc_name` has been
parsed out of the CINFO records since the first version of the ripper and read
by nothing. So a disc whose label blkid missed came out as "Unknown" — and
blkid times out on a busy drive, which the drive is for the whole rip. The
label still wins where there is one; MakeMKV's name is the fallback, and the
job log says which was used.

## 1.18.1

**"/dev/sr0 is already ripping" after cancelling a rip.** It was not wrong,
which was the problem: the drive really was still held, by a scan that Cancel
could not reach.

Two faults, both about killing the wrong thing.

The disc scan ran as a plain `subprocess.run`, invisible to the process
registry — so Cancel killed nothing at all and the drive's lock stayed held
until the scan finished on its own. That was five minutes before 1.18.0 and
fifteen after it, twice over with the retry. The scan is now spawned,
registered and killable like every other tool this application runs.

And cancelling used `proc.kill()`, which kills the one process we hold a
handle to. MakeMKV and HandBrake are both started in their own session
precisely so the whole tree can be killed; the stall watchdog has always done
that, cancelling never did. A surviving child holds the stdout pipe open, the
reader waits on it for ever, and the drive is unusable until the service
restarts.

A cancelled scan is also no longer read as a disc with no titles. Those are
the same value — an empty result — and opposite meanings, and the answer to
"no titles" is to rip all of them. So Cancel during the scan started a full
rip of the disc that had just been cancelled.

The refusal itself now names the job holding the drive and says Cancel is the
way out, rather than "already ripping", which reads as the application being
wrong when it is merely being slow.

## 1.18.0

**A diagnostics bundle went out with a live TMDb key in it.** The settings
section had redacted secrets since the day it was written. The service log
had not — because nothing put a secret in a log, until `log_level: DEBUG`
turned on urllib3, which writes every request URL in full:

    https://api.themoviedb.org:443 "GET /3/search/movie?api_key=<the key>"

Both ends are closed. The bundle is now scrubbed as a whole rather than
section by section, because redacting per section is a rule someone has to
remember at the moment they add one. Two passes: configured values matched
exactly, and credentials nobody configured matched by the name beside them —
`api_key=`, `token=`, `X-Plex-Token`, `Authorization:` — since there the value
is precisely what is unknown. And urllib3 is held at INFO however verbose the
application is set to be, so the key never reaches the log for a redactor to
miss. **Anyone who has pasted a bundle from an install with DEBUG logging
should rotate their TMDb key.**

**The disc scan gave up two minutes before it would have answered.** Five
minutes was a guess; a Disney DVD disproved it. The scan ran past the limit
twice, so "main feature only" fell back to ripping all sixteen titles on a
disc it could have read properly. A protected DVD with hundreds of dummy
titles legitimately takes that long — MakeMKV opens every one of them. The
limit is now fifteen minutes, because the cost of being wrong is asymmetric:
too short rips the whole disc and encodes it, too long delays one rip.

**And HandBrake reached the GPU.** With `libmfx1` installed, HandBrake now
offers `qsv_h264`, `qsv_h265` and `qsv_h265_10bit` on Gen 9.5 hardware. The
Media SDK path was never dead — the runtime was simply never installed,
behind a build everyone had assumed lacked Quick Sync entirely.

## 1.17.4

**Asking the dispatcher instead of asking the user.** Every hardware check in
this application reasons from file names, and on one machine they all ran out
at the same place: the iHD driver loads and offers four encode profiles,
`libmfxhw64.so` is installed, ffmpeg encodes h264 on the GPU — and HandBrake
still says `encqsvInit: qsv is not available on the system`.

That message covers three different problems with three different answers: no
runtime, a runtime that refuses the chip, and a runtime the driver will not
talk to. Nothing that reads a directory listing can tell them apart.

The oneVPL dispatcher can, and will if asked. `ONEVPL_DISPATCHER_LOG=ON`
makes it name every library it opened and why it turned each one down, and
HandBrake builds its encoder list at startup, so `--help` is enough to make
the attempt happen. The diagnostics bundle now runs that and includes both a
one-line verdict and the log's tail — but only when there is something to
explain: a hardware preset that has no hardware encoder. A working HandBrake
gets no post-mortem.

## 1.17.3

**The GPU packages were skipped by the check that was meant to install
them.** 1.17.2 gated the install on `gpu.runtime_state()["ok"]`, which answers
"is a Quick Sync runtime installed" — true the moment *either* of the two is.
A container carrying `libmfxgen1` from an earlier repair, on a processor that
needs `libmfx1`, therefore reported the stack fine and skipped the install
that would have fixed it. The update ran, said nothing, and changed nothing.

That is the same false green this repository has now produced three times: a
check one level too loose, reporting a state that is technically true and
practically wrong. The install is now decided per package with `dpkg-query`,
which has no opinion about what the packages are for, and an already-installed
package is simply not reinstalled.

## 1.17.2

**The GPU fix now arrives through the update button.** 1.17.1 made a fresh
container install the media stack, which does nothing for a container that
already exists — and the fix for "HandBrake cannot see the GPU" was a command
on the Proxmox host. Someone updating from their phone has no host.

`update.sh` runs as root, and an update is the only moment this application
can reach apt at all: the service itself is unprivileged with
`NoNewPrivileges`, deliberately, because the web UI is unauthenticated on the
LAN. So the update now installs the media stack too — but only when a render
node is actually present, and only when the runtime is actually missing. An
install that already works does not run apt on every update, and a container
with no GPU has no use for Intel's libraries.

It asks the application's own `gpu.runtime_state()` rather than probing in
shell. A second, looser check is how this went wrong once already: it accepted
any VA-API driver at all, reported the stack installed, and the web UI then
told the user the encoder was missing from their HandBrake build.

## 1.17.1

**The container never installed the GPU's userspace half.** Ubuntu's
`handbrake-cli` *is* built with Quick Sync — the source package build-depends
on `libvpl-dev`, the oneVPL dispatcher — so the long-standing belief that
HandBrake simply cannot use the GPU on this platform was wrong. What ships is
a loader, and a loader needs a runtime to load. The installer put in
HandBrake, MakeMKV, ffmpeg and cdparanoia, and nothing at all for the GPU, so
HandBrake said "encqsvInit: qsv is not available on the system" — which is
also exactly what it says on a machine with no GPU.

The installer now installs the media stack: the Intel VA-API driver, both
Quick Sync runtimes, and the AMD equivalent. Best-effort, one package at a
time, because the names differ across releases and one unavailable name must
not take the others with it.

**And having a runtime is not the same as having the right one.** There are
two, covering different silicon: `libmfx1` (`libmfxhw64.so`) for Gen 9 through
Gen 11 — Skylake to Comet Lake and Ice Lake — and `libmfxgen1`
(`libmfx-gen.so`) for Alder Lake and later. Either one alone satisfied the
"is a runtime installed" check while being refused by the actual chip, and
HandBrake reports that identically to having neither. The Doctor page now
names which one is installed, what it covers, and which to add if the encoder
test still finds nothing.

## 1.17.0

**The service ripped an empty drive every time it started.** Container comes
up with nothing in the tray, a job appears, and forty seconds later it is red
with a MakeMKV exit code in it. Pressing Rip on an empty drive did the same.

One line caused it. `open(device, O_RDONLY | O_NONBLOCK)` on an optical drive
is *specified* to succeed with an empty tray — that is how you are meant to
open one in order to ask what is in it — and the media check read a successful
open as "a disc is loaded". So the drive was reported loaded from the moment
the service came up, the watcher fired its startup event, and a job started on
an empty drive. Everything downstream was working correctly on a false
premise.

It now asks the drive. `CDROM_DRIVE_STATUS` answers exactly this question and
outranks everything else, including the host's sysfs — which, inside an LXC,
is what `/sys/block/sr0/size` reads from and can be stale. Drives that do not
implement the ioctl (some USB enclosures) still fall back to the old
inference. A disc that is spinning up still counts as present, because the
watcher fires on the *transition* and dropping that event would lose the
insertion entirely.

**And the errors are sentences now.** "No readable disc in /dev/sr0" used to
cover four different problems — an empty tray, an open tray, a device node the
passthrough never created, and a cgroup denial — of which exactly one is fixed
by putting a disc in. Each has its own message, saying what to do:

* *There is no disc in /dev/sr0. Put one in and try again.*
* *The tray of /dev/sr0 is open. Close it with a disc in it.*
* *There is no /dev/sr0 in this container. The drive was not passed through
  when the container started — run 'adr-doctor --fix <CTID>' on the Proxmox
  host, or restart the container.*
* *[…] is still reading the disc. Give it a few seconds and try again.*

A rip that fails for real is no longer reported as an exit code either.
MakeMKV's own last error leads, because it is nearly always the actual answer;
where it said nothing, the message names the usual causes instead of the
number. A rip killed by a signal — a restart or an update mid-rip — says so,
and says the files are incomplete.

## 1.16.5

**"Och den blev på engelska."** The spoken language was set — in the HandBrake
preset, which names `swe` and always has. Then HandBrake turned out to be
unable to reach the GPU, the encoder was switched to ffmpeg, and ffmpeg read
only the *setting*, which was blank. Blank meant "keep every track in the
disc's own order", so the English track led and the player picked it.

The setting now falls back to the language the preset names. Someone who typed
one still wins, because they typed it; someone who never touched the field gets
what their preset asks for, which is the only reading of "use my preset as the
template" that survives changing encoder. The job log says which of the two
the language came from, because "Swedish, from a preset nobody remembers
writing" is otherwise indistinguishable from a guess.

## 1.16.4

**A film that came off the disc as sixteen parts.** "Main feature only" was
on. Dinosaur (2000) was ripped, and out came `Dinosaur (2000) - pt1` through
`pt16` — the film, the commentary version of the film, and fourteen
featurettes. Plex *stacks* numbered parts, so all sixteen became one movie
with the trailers on the end of it.

Two failures had to line up, and both are fixed.

The pre-rip scan came back with nothing, so every title was ripped. That is
the only thing "main feature only" does, and when it fails it failed silently:
the exception was swallowed inside the scan, the caller saw an empty result,
and the job log said `Ripping from /dev/sr0 (title None)` and nothing else.
The scan now says what went wrong in MakeMKV's own words, in the job log where
the person who put the disc in can read it, and tries once more before giving
up — a drive that has only just been handed a disc answers the first `info`
with nothing often enough to be worth five seconds.

Then the naming declined to choose. The rule was that the longest title has to
stand half again clear of the next one before the rest are called extras,
which is the right caution for a two-part film — but a commentary version is
exactly as long as the film, and one title MakeMKV gave no duration for made
it decline outright. Now the question is only left open where it is genuinely
open. Past three titles there is no multi-part release to protect, and someone
who asked for the main feature has already said which title they want; in both
cases the longest one is the film and the rest go to `Other/`.

And "main feature only" now holds even when the scan could not run: the film
is encoded and the other titles are not. They stay in the job's raw directory
as MKV, so nothing is lost and nothing is re-ripped — the job log names them
and says where they are.

**The tray stopped opening.** `eject failed for /dev/sr0: eject: udev: not
found mountpoint or device with the given name`. There is no udev in an LXC
container, and `eject` consults it before doing anything, so it never reached
the ioctl — on a drive that ejects perfectly well. The kernel is now asked
first and the command kept as the fallback, because it knows how to unmount,
which matters on a host where someone has mounted the disc.

## 1.16.3

Two things a healthy diagnostics bundle made visible — neither of which had
broken anything, which is why both had survived.

**The service log was almost entirely the dashboard talking to itself.** A
real 120-line tail held six lines about a disc; the rest was `GET /api/system`,
`GET /api/status`, `GET /api/preflight` every five seconds per open tab, plus
twenty-five range requests for one film someone watched in the browser. The
log exists to answer "what happened", and the answer was in the file, pushed
off the end of it by polling that was meant to be invisible.

Those requests no longer get a line. A page someone opened, a change they
made, and anything that failed all still do — including a polling endpoint
returning 500, which is the most interesting line in the file. The filter is
on werkzeug's logger alone, so nothing it does can touch a line about a rip.

**And a renamed setting had quietly become two settings.** 1.10.0 renamed
`vaapi_quality` to `video_quality` and kept reading the old name as a
fallback, which was enough to keep existing installs working and not enough to
stop it being confusing: the file still held `vaapi_quality: 22`, the
diagnostics still listed it, and it sat beside `video_quality: 0` with nothing
saying which of the two the encoder was using.

The rename now happens once, on load, and the old name goes. A value set since
through the settings page is never overwritten by the historical one — someone
who changed it meant it.

## 1.16.2

**"Still in English" now says why.** That symptom has four causes which look
identical from outside: no spoken language is set, the disc carries no language
tags so nothing can be matched, the language asked for is not on this disc, or
HandBrake's preset made the decision. They need four different things done
about them, and there was nothing on screen to tell them apart.

Each encode now writes one line into the job's own log — on the History page,
beside the film it is about, not one `pct exec` away:

```
Audio: 2 track(s) — 0:eng (ac3), 1:swe (ac3). Track 1 matches 'swe' and leads;
it is kept as a stereo downmix and as the surround track.

Audio: 2 track(s) — 0:untagged (ac3), 1:untagged (ac3). None of them carries a
language tag, so 'swe' cannot be matched against anything and track 0 leads.
That is how the disc was authored, not a setting.
```

The HandBrake path says what it passed and what the preset still decides,
because its language rules live in a JSON file nobody opens mid-encode — so
when a film came out in the wrong language, nothing anywhere said what had
been asked for.

## 1.16.1

**Every time on screen was in UTC.** A fresh LXC is `Etc/UTC` and nothing ever
set it otherwise, so every timestamp the application wrote read two hours
behind the wall clock of the person looking at it — job start times, the
service log, the per-job logs.

Nothing was *broken* by that, which is exactly why it survived a whole
evening's troubleshooting: it simply made every time quietly wrong, and made
the log impossible to line up against when something actually happened.

Two fixes, and both are needed:

- **The container gets the host's clock.** The installer copies it, and
  `adr-doctor --fix` sets it on containers that already exist — then restarts
  the service, because a running process holds its own idea of the zone and new
  log lines would otherwise keep the old offset, making the fix look like it had
  not worked. `CT_TIMEZONE` overrides it. Both check the zone exists before
  linking it: a symlink to a missing zoneinfo file is worse than UTC.
- **The browser renders each timestamp in its own zone.** Because the
  container's clock and the reader's are not necessarily the same one — a phone
  on holiday should still show the time the disc finished, in the zone the
  person holding it is standing in. The server keeps rendering a value too, so
  the page reads correctly with no JavaScript at all.

## 1.16.0

**Updating from the web UI now refuses while a job is running, instead of
failing.** 1.8.2 taught `update.sh` to protect a rip in progress — stopping the
service takes MakeMKV with it, and MakeMKV writes each title as it goes, so the
rip dies part-way and leaves files that look ordinary and are truncated
mid-frame. But the Doctor page went on offering the button, so the click would
be accepted, the unit would run, and the script would decline. A button offered
and then declined teaches people the button is unreliable.

It is withheld now, and says when it will come back.

**And the page says that updating happens there at all.** The Version card had
a "Check GitHub" button and no sentence explaining that this is how you update
— which is worth stating, because the alternative people fall back to is a
shell they should never need to open.

## 1.15.2

**The README was describing an older application.** It promised a button
called "Test the preset" that had been renamed, said hardware encoding was
something `adr-doctor` did afterwards rather than something the installer now
does, and listed none of what has been added since: one set of encoding
settings for both encoders, encoding a job again, selecting and deleting
several at once.

A README that describes a capability the software does not have is worse than
one that omits it — somebody reads it, does not check, and concludes the thing
is broken. Tests now compare the two: the installer steps against the script,
and the feature list against the names the interface actually uses.

## 1.15.1

**Uninstalling now undoes installing, on both machines.** Installing touches
the container *and* the host — two helper commands in `/usr/local/sbin`, a
guest-startup drop-in, an `/etc/fstab` line, and a file in `/root` holding the
NAS password. The uninstaller destroyed the container and left every bit of
it, including a password for an application that no longer exists.

Each is offered separately rather than as one "clean up the host?", because
someone running two containers wants the shared helpers to stay while nobody
wants the password to. `/etc/fstab` is copied before it is edited: getting
that wrong shows up at the next boot, by which time the original is the only
thing that would have helped.

**And one that could have cost someone their films.** On installs made before
1.0, `/opt/adr/completed` is a bind-mount of the media library — and the
container-side uninstall offered `rm -rf /opt/adr`, which goes straight
through it. It now looks for mounts under that directory first, and when it
finds one it says which, says how to unmount it, and stops.

It also removes `adr-update.path` and `adr-update.service`, which arrived with
in-app updating and were missed by an uninstall that only knew about
`adr.service`.

## 1.15.0

**Settings were validated five out of forty-nine, and the shape was why.** The
checks were a ladder of `if "web_port" in data:` blocks, so adding a rule meant
adding a branch — and every setting added since simply never got one. Typing
"abc" into the quality box was accepted, stored, and silently discarded at the
moment of use: the value gone, nothing said, and the encode running on the old
number.

It is a table now. Twenty-nine settings are checked, every complaint is
reported at once rather than one press of Save at a time, and each says what
would be right — the person reading it is looking at the box they just typed
into, and "Invalid value" tells them neither which box nor what to do.

A test asks a question the table cannot: *is every numeric setting bounded?* It
found three that were not. A negative episode counter does not fail — it names
the next file `S01E-1` and puts it somewhere nobody looks.

Some rules need two settings to see. A shortest episode of 90 minutes and a
longest of 75 are each fine alone and together make series detection match
nothing at all, silently.

**And the restart notice was wrong about most of them.** Changing the spoken
language, the quality, the encoder — none of those need a restart; the worker
rebuilds its encoder when the backend changes and reads the rest per job.
Asking for a restart that changes nothing trains people to ignore the notice
for the settings that genuinely need one.

**Toasts that carry a failure are no longer green.** A bulk delete that could
not remove two files reported "3 job(s) removed" in success colours, and the
colour is what gets read. Problems now stay on screen until dismissed —
warnings as well as failures, since five seconds is long enough for "saved"
and not long enough for a list of paths.

## 1.14.0

The polish pass, continued — the example config, the Doctor page, and the
first thing a new install shows.

**`config/adr.yaml.example` is grouped the way the Settings page is.** It had
been three themed sections followed by an alphabetical blob of forty keys,
which told a reader nothing about what belonged with what. It now has the same
five sections as the Settings tabs, in the same order, each with a paragraph
saying why the settings under it exist. Two ways of configuring one
application should agree on how it is organised.

**The Doctor's encoding card describes the encoder that will actually run.**
It said "does the preset resolve, and can HandBrake encode with it" regardless
of what was configured — so with the GPU backend selected it described a
program that was not going to run and a preset nothing was going to read. It
now states what is configured, in a line above the test, and the button says
"Test encoding" rather than "Test the preset", which was only ever half true.

**The empty states are the only instructions most people read.** Two of them
named DVDs, in an application that also rips Blu-rays, audio CDs and data
discs. A third blamed a disconnected drive for what is almost always
passthrough — the drive is connected, the container cannot see it, and the fix
is a command nobody guesses. All three say something useful now.

## 1.13.1

**"Unknown error" was usually the API answering in the wrong key.** Routes had
grown two conventions for reporting a failure — `{"error": …}` in some,
`{"ok": false, "message": …}` in others — and each button in the front-end read
whichever one its author knew about. Where the two disagreed the person got
"Unknown error" on screen while the real reason sat in the response, in a key
nobody was reading.

Every failure now carries `ok`, `error` and `message`, the last two holding the
same sentence. Redundant on the wire, free at the point of use, and the right
trade for a message whose entire job is to be read.

The browser side stopped guessing too: twenty-three copies of `data.error ||
'Unknown error'` became one `reasonFrom()` that reads either. The specific
fallbacks were kept — "Could not start the rip." says what was being attempted
in a way no generic sentence can — they simply read both keys now.

Tests check both halves, and that neither convention can come back.

## 1.13.0

A polish pass over the whole application: every menu, every setting, and the
install itself.

**No more browser dialogs.** `alert()` and `confirm()` were used sixty-four
times. They block the page, they look nothing like it, and on a phone they are
a full-screen system dialog announcing "3 jobs removed". Messages are toasts
now — failures stay until dismissed, everything else says its piece and goes.
Confirmations are a real modal with a title and a verb on the button instead of
"OK", and a detail pane, which is what pays for the change: the delete
confirmation lists the actual file paths rather than cramming them into a
string.

**A fresh install now finishes what the host can do for the container.**
`adr-doctor` knows about passing a GPU through, joining the service user to
the group that owns the render node, and installing the VA-API driver stack —
and none of it was happening on a new install, so a machine with perfectly good
Intel graphics finished the installer with software encoding and no hint that
anything else was available. The installer runs it, non-fatally, and waits for
the container to come back before printing the summary, because `--yes` lets it
restart and a container three seconds into booting has no IP address yet.

**The settings page reads as one voice.** Title Case on some headers and
sentence case on others, hints inside two labels and underneath the rest,
config keys leaking into labels ("Watch folder output path"), and two of the
four tool paths filed under Audio CDs because that is what needed them first.
All four now sit together in Advanced, each saying what its program is for.

**And so do the other pages.** "Job History" under a nav item called History
told you nothing you did not know; "Save Settings" sat beside "Send a test
notification". The dashboard was the only page with no heading of its own.

Tests hold all of it: contrast ratios computed from the stylesheet, every page
parsed as a document, every script parsed by node, headings and buttons checked
for sentence case, and the installer checked for what it reaches for. Two of
those found something the moment they were written — a template whose
JavaScript contained the characters backslash-n instead of newlines, and the
history table's empty-state row spanning twelve of fourteen columns.

## 1.12.0

**The active tab was unreadable, and that one is mine.** Bootstrap's active tab
is dark text on a *white* background — it assumes a light page. Against this
theme's near-white link colour that came out white on white: **1.18:1**, which
is not poor contrast, it is none. The tab you were looking at was the one you
could not read.

The tab strip is now restated for the dark theme — background, borders and text
together, because leaving any of the three to Bootstrap reintroduces the
light-page assumption — and the selected tab carries an accent bar, since a
one-pixel border shift does not survive being read quickly.

The rest of the palette measured fine: every text-on-surface pair is between
5.2:1 and 16:1. Tests compute those ratios from the stylesheet's own variables,
so a palette change is checked rather than merely allowed.

**Select several jobs in the history and delete them.** Checkboxes on every
finished row, select-all in the header, and a bar that appears only when
something is selected.

**And optionally delete what they produced.** That is a separate button, never
the default, and it has no undo — so it asks the server what it would remove
and shows you the list of paths before asking for a yes. Naming the files
rather than counting them: "12 files" is not something anyone can check.

The deletion is deliberately narrow. Only what the job recorded producing —
its tracks' output paths, its own folders, its raw rip — is a candidate.
Nothing walks a tree looking for likely video, nothing removes a directory
that still holds something else, and the configured library roots are never
removed for being empty. A film's own folder goes once its film has; a library
folder shared with other films comes out with those films intact.

**Encode a job again with the current settings.** The reason to want it is that
the settings changed — a different encoder, a different language, a different
quality — and the film on disk was made under the old ones.

Where it encodes *from* changes what you get, so it says which before it
starts. The raw rip when that survives: the same source a first encode used,
and the disc is not needed. The finished file otherwise, which works and is
second-generation — encoding an encode loses a little more each time. "Re-
encode" sounds free and that version of it is not, so it is said plainly. A rip
that never finished is refused outright: its files are truncated mid-frame and
an encoder can do nothing with them but waste an hour.

## 1.11.0

**The Settings page was seventeen cards in one column.** Forty-eight fields,
nine hundred lines of scrolling, and no way to tell the two settings you change
while setting up from the twelve you will never touch. Everything was equally
prominent, which is the same as nothing being prominent.

It is five tabs now:

| | |
|---|---|
| **Library** | where films go, Plex, duplicates |
| **Encoding** | the encoder and what you want out of it |
| **Discs** | ripping, drives, television, audio CDs, data discs |
| **Integrations** | TMDb, MakeMKV key, notifications, watch folder |
| **Advanced** | web interface, logging, where the tools live |

Nothing was removed and nothing was reworded away — the cards moved intact,
help text and all. The **Paths** card was the one real edit: where films go is
a Library concern and stays there, while where MakeMKV and HandBrake are
installed went to Advanced, which is where a setting the installer gets right
belongs.

It is still one form, so saving saves everything regardless of which tab is
open, and the tab you were on is remembered between visits.

Tests hold the shape: every field that existed still renders, every field on
the page is one the API accepts, every card sits inside a pane, and the markup
parses with nothing left open. That last one earned its place immediately — it
caught a stray `</div>` that put a whole card outside its tab, which a browser
would have papered over silently.

## 1.10.1

**The audio layout now mirrors a real HandBrake preset instead of a guess at
one.** Read out of `Super HQ 1080p30 Surround (Svenska)`:

```json
"AudioLanguageList": ["swe"],
"AudioTrackSelectionBehavior": "first",
"AudioCopyMask": ["copy:aac", "copy:ac3"],
"AudioEncoderFallback": "av_aac",
"AudioList": [
  {"AudioEncoder": "av_aac",   "AudioMixdown": "stereo",  "AudioBitrate": 160},
  {"AudioEncoder": "copy:ac3", "AudioMixdown": "7point1", "AudioBitrate": 640}
]
```

Four things were wrong against it, and one of them shipped an hour earlier:

- **`--all-audio` overrode the preset.** `AudioTrackSelectionBehavior` is
  `"first"` — one track. Forcing every language on it was overriding a
  deliberate choice with one nobody made. The override now sets the language
  list and leaves the count to the preset.
- **One language means one source track, twice.** Ask for Swedish and you get
  the Swedish track as an AAC stereo downmix and as the surround track, and
  the other languages are not carried. Ask for nothing and every track is kept
  as before, because choosing for someone who has not said would be guessing
  at the thing they care most about.
- **160k, not 192k**, for the stereo track — the number the preset actually
  specifies.
- **The fallback is AAC, not AC-3.** `AudioEncoderFallback: "av_aac"`, which
  keeps the channel count without AC-3's 640k ceiling. And passthrough is
  narrowed to AAC and AC-3, matching `AudioCopyMask` rather than everything
  MP4 can technically hold.

Tests read the preset file and check the constants against it, so a model that
drifts from the thing it models fails rather than quietly misleading.

## 1.10.0

**One set of encoding settings, for both encoders.**

Two encoders do the transcoding now, and they were configured in two unrelated
ways: a HandBrake preset on one side, a handful of `vaapi_*` values on the
other. So "I want Swedish audio" was a preset property in one and a setting in
the other, and switching encoders silently changed what came out. That is a
bad way to arrange an application — settings should describe the *result*, not
the tool.

Three now do. **Spoken language**, **quality** and a **height cap** are told
to whichever encoder runs. For HandBrake they become `--audio-lang-list`, `-q`
and `--maxHeight`, applied after the preset, so each replaces exactly one
preset value and leaves the rest of that preset intact.

Every one has a "leave it alone" default, and that is what ships. An
installation that never opens this page produces exactly the command it
produced before — a preset someone spent an evening tuning is not overridden
by a setting they never set. An older config keeps the numbers it was given:
`vaapi_quality` and `vaapi_max_height` are still read when the shared keys are
unset, because a migration that silently resets someone's quality is worse
than one that never happened.

The encoder test runs the same overrides a real encode would, and tags its
generated sample with the configured language so a language filter is actually
exercised. A flag HandBrake rejects now shows up in two seconds rather than
forty minutes into a rip.

What stays backend-specific is genuinely specific: the render node and GPU
codec on one side, the preset file on the other.

## 1.9.2

**Choose the language you want to hear.** The GPU encoder took whatever track
the disc listed first and made that the default. On a Swedish disc that is
often the English one, and a rip you cannot understand is wrong in a way no
amount of encoding quality makes up for.

**Settings → Encoding → Spoken language** takes a code as a disc spells it —
`swe`, `eng`, `nor`. Two-letter codes work too, which needs saying because
"sv" and "swe" share exactly one letter and comparing them directly fails; so
do the alternate spellings discs use for German, French, Dutch and Chinese.
The chosen track becomes the stereo default and is tagged with its language,
so a player does not list it as "Undetermined". **Every other track is still
kept** — choosing a default is not the same as throwing the rest away. A
language the disc does not carry falls back to the disc's own order rather
than picking something at random.

To be explicit, because it is a reasonable thing to assume otherwise: **the
HandBrake preset does not apply to the ffmpeg path.** They are two different
encoders. HandBrake takes its audio and language rules from the preset; the
GPU path takes them from these settings.

## 1.9.1

**Films encoded on the GPU played silently.** The ffmpeg path copied the
disc's AC-3 into the MP4 and stopped there. That is legal, and it is a track
plenty of hardware will not decode from an MP4 — a TV, a phone, a browser. The
film plays with no sound, and nothing anywhere says why.

HandBrake's "Surround" presets put an **AAC stereo track first** for exactly
this reason, and that is now the shape: a stereo downmix as track one, marked
default, with every source track behind it. The stereo track is the guarantee
that something comes out of the speakers; the surround track is there for
whatever can use it. Marking it default matters too — otherwise the player
picks whichever track the file lists first, which on a multi-language disc is
a coin toss over the language.

Every source track is kept rather than one being chosen. A Swedish disc
carries Swedish and English, and picking would be deciding which language the
user is allowed. Each is judged on its own: one DTS track is no reason to
re-encode the AC-3 next to it.

## 1.9.0

**HandBrake on the GPU: the variable nobody could have guessed.**

Every check said this container's hardware encoding should work. The render
node is passed through, the service user can open it, `vainfo` loads the Intel
driver and lists encode profiles, the Media SDK runtime is installed, and
HandBrake has Quick Sync compiled in. And Quick Sync still reported the
hardware as absent.

Quick Sync does not open the GPU itself. It goes through whichever VA-API
driver **libva** loads, and the Media SDK is built against a particular one. A
container with both `iHD` and `i965` installed can therefore have a working
GPU, a working Media SDK, and no way to connect them — because libva picked
the other driver. The symptom is `qsv is not available on the system`, which
is also exactly what it says when there is no GPU at all.

`LIBVA_DRIVER_NAME` decides it, and nothing on the system says which value is
right. So the encoder test tries them: every hardware encoder against every
candidate driver, each one a real two-second encode, stopping at the first
that works. When one does, **Use HandBrake with the GPU** appears — it pins
the driver, overrides only the preset's video encoder (everything else in the
preset survives), and re-runs the test before committing. If the combination
fails on the second look, every setting goes back.

A note on what was removed: the probe used to try `vaapi_h264` and
`vaapi_h265`. HandBrake has no VA-API encoder on any platform — Intel goes
through Quick Sync, AMD through VCE — so those attempts could never have
succeeded. It now tries `qsv_h265` and `qsv_h264` as the alternatives, which
are encoders HandBrake actually has.

## 1.8.2

**Updating in the middle of a rip destroyed the rip, quietly.** `update.sh`
runs `systemctl stop adr`, which kills the whole control group — MakeMKV
included. MakeMKV writes each title as it goes, so the rip died with `exited
with code -15` and left MKVs in `raw/` that look perfectly ordinary in a
directory listing and are truncated mid-frame. An hour of ripping, gone, at
exactly the moment someone is sitting there waiting for it.

The updater now refuses to stop the service while a job is ripping, encoding
or identifying, and says why. `ADR_UPDATE_FORCE=1` overrides it.

**And Retry then offered to encode the wreckage.** "2 raw MKV(s) from the rip
are still on disk. Retrying re-encodes them — the disc is not needed" is a
reasonable thing to say about files from a *finished* rip. It was being said
about files from a rip that never finished, and the encode ended in `Invalid
data found when processing input` — which reads as an encoder fault and is
nothing of the kind. Retry now checks whether the rip actually completed, and
sends you back to the disc when it did not.

The encoder says the same thing in its own words rather than passing ffmpeg's
message straight through: the file is named, its size is given, and the
sentence explains that this is what a half-finished rip leaves behind. The
rewrite is narrow — a genuine encoder failure is still reported as one, or
someone re-rips a disc for nothing.

**Smaller things.** `blkid` blocks for its full timeout on a busy optical
drive, and the dashboard polls every few seconds — so a working drive produced
a five-second stall and a full traceback in the log on a loop. It now backs off
for two minutes after a timeout and logs one line instead of a stack trace. And
the diagnostics bundle no longer redacts the encoder settings: a device path
and a quantiser authenticate nothing, and hiding them made the hardware section
unreadable in exactly the bundle someone pastes when hardware encoding is what
has gone wrong.

## 1.8.1

**The encoder test was still testing HandBrake after the switch.** 1.8.0 let
you move encoding to the GPU and then, if you pressed the test button, showed
a red cross about a HandBrake preset nothing was going to use. The same class
of wrong answer this page exists to prevent, introduced by the release that
fixed the last one.

The test now tests whatever will actually run: with the GPU backend it probes
ffmpeg and VA-API and encodes a sample through the real `VaapiEncoder` —
including the audio decision, which is the part that fails at the end of a
two-hour encode rather than at the start. HandBrake could be uninstalled
entirely and it would pass. The Doctor page's hardware check asks the same
question, and the summary names whichever encoder was tested rather than
always saying "HandBrake".

Switching to a software preset now switches the backend back to HandBrake too.
Otherwise the preset was written, the GPU kept encoding, and the test that
followed reported on something the setting had not touched.

## 1.8.0

**Encode on the GPU with ffmpeg, when HandBrake cannot.**

The last four releases chased a container whose Intel GPU worked perfectly and
whose HandBrake could not use it. Everything about the passthrough turned out
to be right: the render node is there, the service user can open it, the
driver stack is installed, and `vainfo` loads the driver and lists encode
profiles. HandBrake still would not start a single hardware encoder, its own
or any other — because its Quick Sync path goes through the Intel Media SDK,
which Intel deprecated in favour of oneVPL and which no longer initialises on
current drivers.

Giving up the hardware over that is the wrong answer when the hardware works.
VA-API is the same silicon by a different road, and ffmpeg drives it directly
— and ffmpeg is already installed, for audio CDs.

**Settings → Encoding → Encoder** now offers `ffmpeg on the GPU (VA-API)`
beside HandBrake, and the encoder test offers the switch when it applies —
having first encoded a test clip on the GPU, because a page that promises
hardware encoding and hands back a failed job would be the same mistake in a
new place. The change takes effect on the next job; the worker rebuilds its
encoder when the setting moves underneath it, rather than quietly waiting for
a service restart.

What the GPU path gives up is presets. HandBrake's are a large body of tuning
and none of it transfers, so this offers what VA-API actually exposes: a codec
(H.264 or HEVC), a quality number, a resolution cap, and the render node.

Two details worth knowing, both of which fail an hour into an encode rather
than at the start:

- **Audio** is copied when the container can hold it and re-encoded to AC-3 at
  640 kb/s when it cannot. MP4 cannot carry the TrueHD or DTS-HD MA a Blu-ray
  rip arrives with, and ffmpeg only discovers that when it writes the trailer.
  The fallback keeps 5.1 rather than downmixing — surround is usually the
  reason the disc was kept.
- **Disc subtitles** are bitmap (PGS, VOBSUB) and MP4 holds neither, so they
  are left out of an MP4 and copied into an MKV.

The diagnostics bundle now reports which encoder is configured and whether
ffmpeg can reach the GPU, because that single line is what four rounds of
troubleshooting were converging on.

## 1.7.8

**`--help` is not a list of what HandBrake was built with.** 1.7.7 read it as
one and concluded, on a machine whose GPU was working perfectly well, that the
build had no hardware encoder compiled in and the hardware should be given up.
HandBrake filters that list by what it can *start right now*, so a build whose
Quick Sync runtime fails to initialise lists nothing — indistinguishable from a
build that never had it. And a build that reaches `encqsvInit` at all plainly
has the code.

Inference was the mistake, three times over. So the Hardware step now *tries*:
it encodes two seconds of video with the encoder the preset asks for, and with
VAAPI, and reports which ones actually ran. That is the difference between
guessing at a cause and knowing one, and it opens the answer that had been
invisible — when Quick Sync will not start but VAAPI does, the GPU is fine and
the fix is one word in a preset file, not a retreat to software.

When nothing hardware-related will start and `vainfo` says the GPU encodes,
the page now says exactly that: an honest dead end pointing at HandBrake's own
runtime, rather than a confident wrong cause. The sample is made once and
shared between the two steps, so this costs no extra wait.

**`H.265 VCN 1080p` was being offered as a software preset.** VCN is AMD's
current name for the engine it used to call VCE, and HandBrake ships those
presets on every platform whether or not the hardware exists. The escape route
from a failing hardware preset was offering presets that fail the same way.

## 1.7.7

**The dispatcher is not the runtime.** 1.7.6 accepted `libvpl.so` as the
Quick Sync runtime. It is not one — it is the *dispatcher*, the library
HandBrake links against, whose entire job is to find a runtime at load time
and hand over. It encodes nothing. A container with `libvpl.so.2` and no
`libmfx-gen.so` has a loader with nothing to load, and HandBrake reports
exactly what it reports when neither is installed.

That is the most confusing shape this failure takes, because `ls` shows the
library sitting right there. So when the dispatcher is present without a
runtime, the message now says which one is which rather than claiming nothing
is installed — a message contradicted by the first listing anyone runs loses
its credibility. `adr-doctor` installs `libmfxgen1` and `libmfx1` first for
the same reason: the dispatcher is usually already there as somebody else's
dependency.

**And the stack is now asked whether it works, not just whether it exists.**
Every check up to here reasons from file names: the node is there, the driver
is there, therefore it should encode. `vainfo` does not reason — it opens the
device, loads the driver and lists what the hardware will actually do. A
driver too old for the chip, a chip with no encode engine, a render node
belonging to a different card: all of them look correct in a directory
listing and all of them fail here.

For a preset that asks for a GPU, the encoder test now shows the whole chain
before it tries the encode — which encoders the build has, whether the node
opens, what the driver stack is, and what `vainfo` makes of it. The
diagnostics bundle carries the same, because working this out from a distance
took several rounds of "run this and paste the output" and none of it can
authenticate anything.

A missing `vainfo` is reported, not treated as a failure. Absence of evidence
is not evidence, and the encode itself is still the verdict.

## 1.7.6

**The driver check from 1.7.5 was itself a false green.** It asked "is any
VA-API driver installed?" and a container with Mesa on it answered yes — while
the GPU was Intel and `radeonsi_drv_video.so` could not encode a single frame
on it. `adr-doctor` reported the stack installed and skipped the install; the
web UI went on blaming the HandBrake build. One layer down, the same mistake.

Both halves are now checked against the PCI vendor of the render node, read
from `/sys`. Intel needs `iHD` or `i965` *and* the Media SDK / oneVPL runtime
Quick Sync loads on top of it — the second thing is Quick Sync's alone, so an
AMD card is not asked for it. When drivers are installed but none of them
drives this GPU, the message says so rather than claiming none exist, because
otherwise it contradicts the first `ls` anyone runs to check.

`adr-doctor` no longer carries its own opinion about this. It asks the
container's own `gpu.runtime_state()` and prints what that says. A second,
looser implementation on the host is precisely how 1.7.5 went wrong.

**And HandBrake is now asked whether it has the encoder at all.** `--help`
lists every encoder the build was compiled with, which settles the one
question that was previously inferred: `qsv is not available on the system`
means either the build has no Quick Sync or the system has no runtime for it,
and those have opposite fixes. A build with no hardware encoder is told to
encode in software immediately, with no detour through GPU passthrough that
could never have helped. Encoder *names* are matched, not the substring `qsv`
— `--help` documents `--qsv-async-depth` whether or not the encoder is there.

## 1.7.5

**Passing the GPU through is only half of it.** 1.7.3 got the render node into
the container and the service user into the group that owns it. Every check
about hardware encoding then went green — and HandBrake still said
`encqsvInit: qsv is not available on the system`, because Quick Sync does not
talk to the kernel directly. It reaches the hardware through a VA-API driver
and a Media SDK / oneVPL runtime, and a minimal container image ships neither.

That failure looked solved, which made it worse than one that looks broken.
The Doctor page reported a working GPU, and the encoder test told you the
encoder was "missing from this HandBrake build" — advice that sends someone
who has just finished passing a GPU through back to software encoding, over a
missing driver.

So the two halves are now told apart. The Doctor page fails, in as many words,
when the node is present and the driver is not; the encoder test says which
one is missing and what installs it; and `adr-doctor --fix` installs it,
picking Intel's or AMD's stack from the PCI vendor of the render node rather
than guessing. Nothing inside the container could have done this itself — the
service runs unprivileged and apt needs root.

**The software-preset dropdown was full of prose.** "and Dolby Digital (AC-3)
surround audio, in an MP4" is not something you can encode with. HandBrake
lays `--preset-list` out by indentation, and accepting a *range* of indents
caught the wrapped description lines too. The name level is now measured — the
smallest indent any non-category line uses — which also survives HandBrake
changing its spacing.

## 1.7.4

**`adr-doctor` refreshes itself.** 1.7.3 taught it to notice it was an old
copy; it then left you to paste a `pct pull` command, which is the friction
that produced the stale copy to begin with.

Now it offers, fetches, and re-runs — carrying the arguments you gave it, so
`--fix` is still `--fix` on the second pass. It re-executes from a copy in
`/tmp` and lets that copy install itself, rather than overwriting the file it
is running from: bash reads a script incrementally by byte offset, and one
that replaces itself mid-run carries on reading at its old offset inside
different bytes. `update.sh` learned that the hard way, and the same pattern
is used here.

A refreshed run cannot decide it is stale and refresh again, and a fetch that
fails carries on with the old version rather than refusing to diagnose —
having no route to the container is not a reason to stop looking at the host.

---

## 1.7.3

**`adr-doctor` gave a clean bill of health from a script that never looked.**

It lives on the Proxmox host but is *copied* out of the container at install
time, and the in-container updater cannot write to the host. So updating the
application does not update it: an old copy silently skips every check added
since it was taken, and then prints **"Nothing wrong found."**

That is worse than failing. Someone updated to 1.7.2 for the GPU passthrough,
ran `adr-doctor --fix`, and was told everything was fine by a copy that had no
GPU section in it at all.

It now carries the version it was taken from, asks the container what version
it is running, and says plainly when the two differ — with the one command
that refreshes it, and a prompt before going on. A stale copy can still be
used deliberately; it can no longer pretend.

The updater's parting advice was part of the problem too: a two-line `for`
loop over a `name:name` mapping, easy to mistype and easy to skip. It is two
plain `pct pull` commands now, and it says why they matter rather than
mentioning them in passing.

---

## 1.7.2

**Passing the GPU through is only half of it.** `/dev/dri/renderD128` is
`crw-rw---- root:render`. The passthrough added in 1.7.0 makes the node
visible; opening it still needs the service user to be in the group that owns
it, so the next thing to happen would have been `permission denied` and
another round trip.

`adr-doctor --fix` now does both. It reads the gid that owns the node **on the
host** and joins the service user to the group carrying that number inside the
container, creating one if the container has no group with it.

By number, not by name, and that is the whole point: in a privileged container
gids map straight through, and the host's `render` gid is almost never the
container's — Proxmox is Debian, the container is Ubuntu, and they number
system groups differently. The kernel checks the number. Advice to run
`usermod -aG render adr`, which is what the app suggested before, would have
looked right and changed nothing; that advice is gone.

Also fixed: `adr-doctor` referred to a `RUN_USER` it never defined, so the
group step would have run against an empty user name.

---

## 1.7.1

**Passing a GPU through needs the Proxmox host. Changing the preset does not.**

For someone without host access, "pass the GPU through or pick a software
preset" is one instruction they cannot follow and one sentence telling them
where to go and what to type. The second is now a button on the encode test:
*Encode in software instead*.

It lists the software presets HandBrake actually has — asked of HandBrake, not
assumed — ordered by resemblance to the one configured. Someone who chose
"Super HQ 1080p30 Surround (Svenska)" wanted that quality, so the stock "Super
HQ 1080p30 Surround" is offered first rather than whatever comes first
alphabetically. A parenthesised suffix is ignored when matching, because a
localised copy is still the same preset.

Then it switches and **re-runs the test**, because the entire point of that
page is that a preset which cannot encode looks exactly like one that can
until something tries. If the replacement fails too, the previous preset is
restored: leaving a setting in place that has just been proven not to work is
a worse state than the one you started in. And only presets from that list are
accepted, since the endpoint exists to escape a hardware preset, not to set an
arbitrary string that fails the same way.

The button appears only when the failure was actually about hardware. A button
for a fix that does not apply is worse than no button.

---

## 1.7.0

**The preset wanted a GPU the container did not have.** The encoder test
added in 1.6.1 found it on its first run against a real installation:

```
ERROR: encqsvInit: qsv is not available on the system
ERROR: Failure to initialise thread 'Quick Sync Video encoder (Intel Media SDK)'
```

A preset exported from HandBrake on a desktop asks for that desktop's encoder.
Inside an LXC it does not exist unless the GPU was passed through, so every
title of every disc failed identically at initialisation.

The advice for this was half an answer: *pick a software preset*. That throws
away the hardware the person deliberately chose, when the fix is one host-side
config line — the same kind of line that already passes the optical drive
through.

**`adr-doctor --fix` now passes the GPU through too**: the DRM character major
and a bind of `/dev/dri`, offered only when the host actually has a render
node, because binding a device that is not there would be noise.

**Doctor gained a Hardware encoding check.** It reads the encoder out of the
preset file rather than guessing from its name — "Super HQ 1080p30 Surround"
says nothing about the encoder, and the encoder is the whole question — and it
stays quiet when the preset encodes in software, because a red cross about
hardware nobody asked for trains people to ignore the page.

The encode test now tells the two situations apart. No GPU in the container
names both ways out, with the command for each. A GPU that *is* there means
the build lacks the encoder instead, which is a different sentence and a
different fix. And a render node that exists but will not open is separated
again: permission denied is the service user's groups, anything else is the
device cgroup — three problems that look identical and need three different
answers.

---

## 1.6.1

**"HandBrake exited with code 3" ten times in a row is one problem, not ten.**
Exit 3 is an initialisation failure, and it is the same for every title of
every disc because it is decided before any video is touched: an encoder the
build was compiled without, a hardware encoder that is not in the container, a
preset name that does not resolve. Finding out which cost forty minutes and a
disc.

**Doctor → Encoding → Test the preset** answers it in seconds with no disc.
It checks that the preset resolves, then encodes two seconds of generated
video through the real preset — the step that matters, because everything
before it passes on a build with no usable encoder. When it fails it shows
what HandBrake actually said and turns it into something to do: a hardware
encoder means the container has no GPU, an unknown encoder means the build
lacks it, and both are one setting away from fixed.

**The elapsed timer was two hours out.** Times are stored naive, in the
container's zone, and sent to the browser without one. JavaScript reads a
date-time with no zone as *its own* local time, so a container on UTC and a
phone on CEST disagreed by exactly the offset — a job that had just started
read 2:00:39. Timestamps now carry the server's offset.

**The auto-refresh was rewriting the wrong badge.** The phase strip added in
1.5.0 put pills above the status badge, and the refresh took the first
`.badge` on the card — so every five seconds it overwrote "Identify" with the
job status. It targets the status badge by name now.

Also, on a phone: the completed-date column wrapped onto two lines and is now
hidden on the narrowest screens, and the drive heading no longer fights its
buttons for one line.

---

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
