# Automatic Disc Ripper — Proxmox LXC edition

[![tests](https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/actions/workflows/tests.yml/badge.svg)](https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Automatic, hands-off DVD/Blu-ray ripping for your homelab. Insert a disc and it
is ripped with **MakeMKV**, transcoded to MP4 with **HandBrake**, identified and
named via **TMDb**, and dropped into a Plex-ready folder — all inside a single
**Proxmox LXC container** that installs itself with one command.

![ADR Dashboard](docs/adr_dashboard_screenshot.png)

> This is the Linux/Proxmox port of *Automatic Disc Ripper for Windows*. The disc
> layer was rewritten for Linux (`/dev/sr*`, `eject`); everything else — the
> pipeline, web UI, TMDb matching and Plex handling — is unchanged.

---

## Features

- **Zero-touch pipeline:** detect → identify (TMDb) → rip (MakeMKV) → eject → transcode (HandBrake) → Plex-ready output.
- **Every kind of disc**, not just films: DVDs and Blu-rays transcode, **audio CDs** become tagged FLAC or MP3, **data discs** become ISO images.
- **One-command install** on the Proxmox host: creates the container, passes the optical drive through, installs everything, starts the service.
- **No compilation:** MakeMKV from the `heyarje/makemkv-beta` PPA, HandBrakeCLI from Ubuntu universe.
- **Automatic MakeMKV key:** fetches the current free beta key, or accepts your own.
- **Web dashboard** (port 8080) with live progress, job history, a Storage page for NAS setup, settings, and in-browser playback.
- **Doctor page** that self-diagnoses drives, tools, keys and storage — and updates the app from GitHub with one button.
- **Logs page** with the service's own log, filterable by level and text — no `pct exec`, no journald, no shell.
- **Test the preset** without a disc: encodes two seconds of video with your real settings and says what HandBrake objected to.
- **Hardware encoding**: `adr-doctor --fix` passes the host's GPU into the container, so a Quick Sync or NVENC preset works instead of failing on every title.
- **Copy diagnostics**: one button produces everything needed to diagnose the install as a single paste, with keys and tokens removed.
- **Television discs**: box sets are recognised from title durations and named `Show (Year)/Season 02/Show (Year) - S02E05.mp4`.
- **Series mode**: set the show once, then feed a whole box set — the episode number carries across discs on its own.
- **Notifications** to ntfy, Gotify, Discord or a webhook when a disc finishes or fails — the pipeline is unattended, so it tells you.
- **Plex library refresh** the moment a film lands, instead of waiting for the next scheduled scan.
- **Per-job logs** in the UI: what MakeMKV and HandBrake actually said, without SSH.
- **Progress that answers the question**: time remaining, read speed and elapsed time for the rip, not just a percentage.
- **Duplicate detection** against the library itself, the TMDb id and the disc label — optionally skipping the rip entirely.
- **Retry** a failed job from whatever is still on disk — a broken NAS should not cost you a 40-minute rip.
- **Says what is broken before you insert a disc**, with the fix, instead of letting every disc fail separately with the same reason.
- **Survives a restart mid-job**: an interrupted encode picks itself up on the next start, and nothing is left saying "ripping" for ever.
- **Notices a drive that has stopped answering** instead of waiting on it for the rest of the service's life.
- **Multi-drive** support and a **watch folder** for batch encoding of existing video files.
- **Transcoding is optional** — keep the lossless MKV straight off the disc instead, if size is cheaper than time.
- **Extras kept apart** from the film, in a folder Plex actually recognises, so a trailer never becomes the second half of the movie.
- **Install via Claude for Chrome** — paste one prompt and it does the whole thing for you.

---

## Quick Start (one command)

On your **Proxmox host**, as root — via SSH or the node **Shell** in the Proxmox web UI.

### If this repository is public

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/scripts/install.sh)"
```

### If this repository is private

`raw.githubusercontent.com` returns **404** for private repositories unless the
request carries a token, so the bootstrap `curl` needs an auth header too — not
just the clone. Create a
[fine-grained token](https://github.com/settings/personal-access-tokens/new)
scoped to this repository with **Repository permissions → Contents: Read-only**,
then:

```bash
export GITHUB_TOKEN=github_pat_xxx
bash -c "$(curl -fsSL -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/scripts/install.sh)"
```

The exported `GITHUB_TOKEN` is picked up automatically by the installer for the
subsequent `git clone` and for the in-container fetch — you only set it once.

> **If the command returns instantly with no output**, the `curl` fetched
> nothing (bad token or wrong URL) and `bash -c ""` simply did nothing. That is
> a fetch failure, not a successful install. Check it with:
> ```bash
> curl -fsSI -H "Authorization: Bearer $GITHUB_TOKEN" \
>   https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/scripts/install.sh
> ```
> A working token prints `HTTP/2 200`.

> **Tip:** making the repo public (*Settings → General → Danger Zone → Change
> visibility*) lets you and anyone else use the short form above. The installer
> works either way.

### What you'll see

You'll be asked for a few values (container ID, disk size, the optical device,
an optional TMDb key) — press Enter to accept the sensible defaults. When it
finishes:

```
 ✓ Installation complete!
   ┌────────────────────────────────────────────────────────
   │  Web UI : http://192.168.1.42:8080
   │  SSH    : ssh root@192.168.1.42
   │  Root pw: ••••••••••••   (generated — save it now)
   │  CTID   : 200
   └────────────────────────────────────────────────────────
```

Open that URL and you're done. Insert a disc to start ripping.

### What the installer does

1. Downloads the Ubuntu 24.04 LXC template (if missing) and creates the container.
2. Adds optical-drive passthrough to the container config (`/dev/sr*`, `/dev/sg*`).
3. Installs MakeMKV, HandBrakeCLI, Python, the app, and a `systemd` service.
4. Fetches/stores the MakeMKV key and starts the service on boot.

### Useful environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CT_ID` | next free | Container ID |
| `CT_CORES` / `CT_RAM` / `CT_DISK` | `4` / `2048` / `100` | CPU cores / RAM (MiB) / disk (GiB) |
| `CT_STORAGE` / `CT_BRIDGE` | `local-lvm` / `vmbr0` | Container storage / network bridge |
| `CT_UNPRIVILEGED` | `0` | `0` = privileged (recommended for optical passthrough) |
| `DISC_DEVICE` | first `/dev/sr*` | Optical device to pass through |
| `MEDIA_HOST_PATH` | — | Host dir already mounted, bind-mounted into the container (e.g. `/mnt/pve/<storage-id>`) |
| `CT_MEDIA_PATH` | `/mnt/media` | Where that mount appears **inside** the container |
| `NAS_URL` | — | `nfs://host/export/path` or `smb://host/share` — mounts a NAS for the finished files |
| `NAS_USERNAME` / `NAS_PASSWORD` | — | SMB credentials (SMB only) |
| `NAS_MOUNTPOINT` | `/mnt/adr-media` | Where the share is mounted on the host |
| `TMDB_API_KEY` | — | TMDb API key |
| `MAKEMKV_KEY` | `auto` | `auto` (fetch free beta key), a `T-…` key, or empty to skip |
| `ADR_NONINTERACTIVE` | `0` | `1` = no prompts (use the `CT_*` defaults) |

---

## Install via Claude for Chrome

If you have the [Claude for Chrome](https://www.anthropic.com/news/claude-for-chrome)
extension enabled, you can install this in one conversation without touching a
terminal yourself.

**Step 1.** Open your Proxmox web UI (e.g. `https://proxmox.local:8006`) and log in.

**Step 2.** Click your Proxmox node in the left sidebar, then click **Shell**. A
root shell opens in your browser.

**Step 3.** Click the Claude extension icon and paste **exactly** this prompt:

```
You are controlling my Proxmox VE root shell that is currently visible
in this browser tab. Please install "Automatic Disc Ripper" for me by:

1. Verifying the active tab is a Proxmox node shell (it shows a prompt
   like root@pve:~#). If not, ask me to open one first.
2. Typing the following command into the shell and pressing Enter:

       bash -c "$(curl -fsSL https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/scripts/install.sh)"

3. Watching the installer output. When it prompts me for:
      - container ID (CTID)   -> suggest the number it shows as default
      - hostname              -> suggest "adr"
      - disk / RAM / cores    -> accept defaults (100 / 2048 / 4)
      - CT_UNPRIVILEGED       -> answer "0" (privileged)
      - DISC_DEVICE           -> accept the default it found (e.g. /dev/sr0)
      - TMDB_API_KEY          -> leave empty for now, I will add it later
      - MAKEMKV_KEY           -> type "auto" to auto-fetch the free beta key
   Answer each prompt accordingly.
4. When the installer prints "Installation complete!" and a URL like
   http://<ip>:8080, open that URL in a new tab and confirm the web UI
   loads. Then paste the container IP and the generated root password
   back to me.

Do not run any other commands, do not modify unrelated files, and stop
immediately if any step fails — report the error instead of retrying.
```

**Step 4.** Approve each command when Claude asks. When the web UI opens, you're done.

> If the repository is **private**, replace the command in step 2 of the prompt
> with the token form from [Quick Start](#if-this-repository-is-private) —
> the plain URL returns 404 and Claude will report the install as failed.

> The container web UI has **no authentication** and is reachable by anyone on
> your LAN. Keep it on a trusted network and don't port-forward it to the internet.

---

## Configuration

Settings live in `/opt/adr/config/adr.yaml` and can be edited live from the web
UI under **Settings**. Key options:

| Setting | Default | Notes |
|---------|---------|-------|
| `makemkv_path` | `/usr/bin/makemkvcon` | MakeMKV CLI |
| `handbrake_path` | `/usr/bin/HandBrakeCLI` | HandBrake CLI |
| `raw_path` | `/opt/adr/raw` | Raw MKVs from the disc — always local, deleted after encoding |
| `completed_path` | `/opt/adr/completed`, or `/mnt/media` with a library attached | Where finished films end up |
| `drives` | `auto` | Or a list like `["/dev/sr0", "/dev/sr1"]` |
| `handbrake_preset` | `Fast 1080p30` | Any built-in or custom preset |
| `tmdb_api_key` | — | Free key from [themoviedb.org](https://www.themoviedb.org/settings/api) |
| `plex_path` | — | Plex **movie** library. When set (with `auto_move_to_plex`) films are written **directly** here and never pass through `completed_path` |
| `tv_path` | — | Plex **TV** library. Series go here; they never go to `plex_path`, which has different naming rules |
| `notify_enabled` / `notify_provider` / `notify_url` / `notify_token` | off / `ntfy` | Where to send notifications |
| `notify_events` | `job_done`, `job_failed` | Which events to send |
| `plex_refresh_enabled` / `plex_url` / `plex_token` / `plex_section` | off | Ask Plex to scan when a film lands |
| `require_completed_mount` | `false` | Refuse to start a rip unless the destination is a real mount point — set automatically by `adr-setup-nas` |
| `stage_locally` | `true` | Encode to local disk and transfer the finished film in one copy. Only applies when `completed_path` is network storage |
| `staging_path` | `/opt/adr/staging` | Local scratch used while encoding to network storage |

### Where things live

```
/opt/adr/            the application — code, database, venv, scratch space
  ├─ raw/            raw MKVs straight off the disc   (local, deleted after encoding)
  ├─ staging/        HandBrake writes here            (local, always)
  └─ completed/      finished films, when you have no NAS
       ├─ Music/     albums from audio CDs            (music_path)
       └─ ISO/       images of data discs             (data_disc_path)
/mnt/media/          finished films, when you do      (your NAS or host storage)
```

One rule, and it is the whole design: **nothing is ever mounted inside
`/opt/adr`.** That directory belongs to the application. Your library is a
separate filesystem mounted beside it at `/mnt/media`, and `completed_path`
simply points at whichever of the two you are using.

This matters for more than tidiness. A share mounted over `/opt/adr/completed`
means a routine `chown -R /opt/adr` walks into your film library, and it means
that when the NAS is offline you cannot tell "empty share" from "empty app
directory". Installs made before 1.0 used that layout; `adr-doctor --fix
<CTID>` migrates them.

Ripping and encoding always happen on the container's own disk. Only the
finished MP4 crosses the network, as a single sequential transfer — see
[Local staging](#local-staging) below.

### Keeping the MKV instead of transcoding

**Settings → Encoding → Transcode with HandBrake.** Off keeps the file exactly
as MakeMKV produced it: lossless, and minutes rather than hours, at several
times the size. Nothing else changes — same folder, same name, same move into
the library, same notifications. The watch folder still transcodes, since
transcoding is the only reason it exists.

### Extras

With main-feature selection off, a disc's trailers and featurettes are ripped
too. They used to be named `Film (1999) - pt2`, `pt3` and so on, and Plex
*stacks* numbered parts — it treats them as one film split across files, so a
two-minute trailer became the back half of the movie.

When one title is at least 1.5× longer than the next, it is the feature and the
rest go to `Film (1999)/Other/Extra 1.mp4`. `Other` is one of the eight folder
names Plex recognises for local extras, and the only one that does not claim to
know what the extra *is* — MakeMKV reports a duration and nothing else.

Titles of similar length are left as numbered parts, because that is what a
genuinely two-part film looks like, and calling half a film an extra is the
worse of the two mistakes.

### Discs that are not films

Not everything in an optical drive is a film, and until 1.3 everything was
handed to MakeMKV anyway. An audio CD came back as "no titles found" — which is
exactly what an unreachable drive looks like, so the failure sent you off
debugging hardware that was fine.

Every disc is now classified first, from its table of contents and its ISO 9660
root directory. Nothing is mounted and no extra privileges are needed.

| What is in the drive | What happens | Where it lands |
|---|---|---|
| DVD / Blu-ray | MakeMKV → HandBrake, as before | `completed_path`, `plex_path` or `tv_path` |
| Audio CD | cdparanoia → ffmpeg, tagged from MusicBrainz | `music_path` (default `completed_path/Music`) |
| Mixed-mode CD | the audio tracks are ripped, the data track ignored | `music_path` |
| Data disc | byte-for-byte ISO image | `data_disc_path` (default `completed_path/ISO`) |

A disc that cannot be read at all is still treated as video — that is what the
application did before any of this existed, so a pure-UDF Blu-ray with no
ISO 9660 descriptor keeps working exactly as it used to.

**Audio CDs.** cdparanoia does the extraction because CD audio has no error
correction worth the name: a scratch does not produce a read error, it produces
a click, and cdparanoia re-reads and overlaps until the samples agree.
MusicBrainz supplies artist, album, year and track names, looked up by a disc
ID computed from the track layout. Output is `Artist/Album (Year)/01 - Title.flac`,
which is what every music server expects. A CD nobody has ever submitted still
rips — it is filed under its disc ID, which is stable, so the same disc always
lands in the same folder. One unreadable track costs you that track, not the
album.

Set the format (FLAC or MP3), the bitrate and the folder under **Settings →
Audio CDs**, or turn the whole thing off there if you would rather an audio CD
were left alone.

**Data discs.** Only the recorded part of the disc is read: a drive routinely
reports more capacity than the disc holds, and reading past the end produces
I/O errors that look like a failure and are not. The size comes from the ISO
9660 volume descriptor when there is one. A read error is retried before it is
believed, and a copy that fails is deleted rather than left behind — a
half-written ISO that looks complete is worse than no ISO at all.

### Television discs

A box-set disc is not a film with extras: six episodes of similar length, none
of them a "main feature". Left alone, main-feature selection would rip the
longest and silently discard the other five.

So a disc is recognised as television from its **title durations** — three or
more titles of similar length between 15 and 75 minutes — because that is all
that is known before anything is ripped. Grouping is real clustering: every
title in the group must be close to every other, not merely to whichever one
the scan started from, or a 16-minute featurette bridges into four 22-minute
episodes.

Detection only ever **annotates**. The dashboard shows the guess with its
reasoning — including every title length it saw, so a wrong verdict is
correctable rather than baffling — and you confirm the show, season and
starting episode before encoding begins. Calling a film a series renames it
into a season folder, which is much more annoying to undo than the reverse. Any
job can also be marked as a TV disc by hand while it is still ripping.

The show is looked up against TMDb's **TV** catalogue, which is a different
namespace from films: identification has already run a *film* search, and for a
box set that returns a confident-looking film. Picking the show corrects the
title, year and poster, and the file names are previewed against the season's
real episode titles — which is how an off-by-one is caught before forty minutes
of encoding rather than after.

#### Series mode — working through a box set

Marking one disc as television is a small thing. Doing it for six discs of a
season — re-entering the show, the season, and the episode each disc starts at —
is not, and that last number is both easy to get wrong and expensive to get
wrong: a third of the season silently misnumbered, which Plex will happily
display as the wrong episodes.

**Rip a TV series** on the dashboard is a sticky answer to those questions plus
a counter. Set the show and season once, then feed discs:

```
disc 1 → The Wire (2002)/Season 02/The Wire (2002) - S02E01 … E04
disc 2 → …                                                   S02E05 … E08
disc 3 → …                                                   S02E09 … E12
```

The counter advances by however many titles each disc actually produced, so
disc 2 starts at 5 because disc 1 yielded four episodes — not because anyone
said so. If a disc holds a feature-length extra that looked like an episode,
**Fix episode number** in the banner winds it back without re-ripping anything.

While the mode is on it takes over completely: the film identification is
skipped (it is a *film* search, and for a box set it returns a confident-looking
film that would overwrite the show you just named), and main-feature selection
is skipped so every episode is ripped rather than just the longest.

It does not expire. A mode that switched itself off after some interval would
be surprising in the worst way — discs 4–6 of a season quietly numbered from 1
again. It stays until you turn it off, and every page carries a banner saying
so.

#### Thresholds

The thresholds are settings, not constants: anime runs to 24 minutes, a
documentary series to 55, and some box set will sit outside any default.
**Settings → Television** has the shortest and longest episode length, how many
similar titles count as a season, and a switch to turn detection off entirely.

Output follows Plex's TV layout:

```
Show Name (2019)/Season 02/Show Name (2019) - S02E05.mp4
```

Set `tv_path` to your Plex **TV** library. Series never go to `plex_path`:
Plex keeps films and shows in separate libraries with different naming rules,
and a season folder in the movie library is not something it can make sense of.

### Notifications

The pipeline is meant to be unattended — put a disc in, walk away. Without
notifications the only way to learn a rip failed forty minutes ago is to open
the dashboard and look.

| Service | URL to give it |
|---|---|
| **ntfy** | `https://ntfy.sh/your-secret-topic` — pick a topic nobody would guess; anyone who knows it can read your notifications |
| **Gotify** | the server root, e.g. `http://192.168.1.10:8080`, with the application token |
| **Discord** | a channel webhook from *Channel settings → Integrations → Webhooks* |
| **Webhook** | anything that accepts a JSON POST of `{event, title, message}` |

Events are opt-in per type: a disc finished, a rip failed, a disc was inserted.
**Send a test notification** under Settings uses the values on the form rather
than the saved ones, since testing before saving is the only moment a test is
useful.

Delivery is best-effort with a short timeout. A notification service being down
never fails a rip — the film is on disk either way.

### Plex library refresh

Set `plex_url`, `plex_token` and pick a library, and Plex is told to scan the
folder as soon as a film lands there. Without it the film exists on disk and is
invisible in Plex until the next scheduled scan, which reads as the ripper
having failed.

The token: in Plex, open any item → *Get Info* → *View XML*, and copy
`X-Plex-Token` out of the URL. **Fetch libraries** then lists them so you pick
one instead of guessing a section key.

### Hardware encoding

A preset exported from HandBrake on a desktop asks for that desktop's encoder
— Intel Quick Sync, NVENC, VAAPI. Inside an LXC none of them exist unless the
GPU was passed through, and HandBrake fails the same way on every title of
every disc:

```
ERROR: encqsvInit: qsv is not available on the system
Encode failed (error 3).
```

Exit 3 is an initialisation failure, decided before any video is touched,
which is why it is identical every time and why forty minutes of ripping is
wasted before you see it.

**Doctor → Encoding → Test the preset** answers it in seconds with no disc,
and **Doctor → Hardware encoding** says whether a GPU is reachable at all. The
two are separate questions: a preset can want hardware that is present but
unsupported by the build, and a container can have a GPU nobody's preset uses.

`adr-doctor --fix <CTID>` on the Proxmox host adds the passthrough — the DRM
character major and a bind of `/dev/dri` — alongside the optical drive it
already handles. It only offers this when the host actually has a render node,
because binding a device that is not there would be noise.

It also does the half that passthrough alone does not solve. `renderD128` is
`crw-rw---- root:render`: passing the node in makes it visible, opening it
still needs the service user to be in the owning group. In a privileged
container gids map straight through, and the host's `render` gid is almost
never the container's — Proxmox is Debian, the container is Ubuntu, and they
number system groups differently. So the group is matched **by number**, which
is the only thing the kernel checks; advice to `usermod -aG render adr` would
look right and change nothing.

**No access to the host right now?** The encode test offers a second button:
*Encode in software instead*. It lists the software presets HandBrake actually
has, ordered by resemblance to the one configured — someone who chose "Super
HQ 1080p30 Surround (Svenska)" wanted that quality, so the stock "Super HQ
1080p30 Surround" is offered first — then switches and **re-runs the test to
prove it**. If the new preset cannot encode either, the old one is put back,
because leaving a setting in place that has just been shown not to work is
worse than the state you started in.

Software is slower than Quick Sync and that is the whole trade. Nothing about
it needs the Proxmox host.

### Asking someone for help

Every diagnosis of this application used to end the same way: *paste me the
output of `journalctl`*. That needs a shell on the Proxmox host, which the
person looking at the dashboard on their phone does not have — a design
failure, not a support process.

Two things fix it, both in the web UI.

**The Logs page** shows the service's own log. The application keeps its own
rotating copy beside the database, so reading it needs no privileges and no
journald group. Filter by level — asking for WARNING keeps the ERRORs too —
or search for a device path or a job number. Tracebacks stay whole, because a
traceback cut down to its first line is not a traceback.

**Copy diagnostics** puts the whole picture in the clipboard as one block of
plain text: version, every setting, all the self-checks, where storage really
points and whether it is mounted, the drives, the last three failures *with
their tool output*, and the end of the service log.

API keys and tokens are removed before it leaves. The redaction is a whitelist
— a value is shown only if its name is known to be harmless — because the two
mistakes are not symmetric: a missing setting costs a follow-up question, a
leaked token costs you the account.

### Before something fails

The pipeline has always refused to start a rip it knew would fail — a
destination that is missing, read-only, or an unmounted NAS costs forty minutes
and several GB to discover the hard way. But it only ever said so *after* a
disc went in, once per disc. Insert eleven discs and you get eleven identical
red jobs and never a statement of the one thing that is wrong.

The dashboard now runs the same check with no disc in the drive, and says it
once, at the top of the page, before you start: what is broken, and what to do
about it. The advice matches the fault — "not mounted" and "not writable" need
opposite actions, and one piece of generic advice for both helps with neither.

Pressing **Rip** against a broken destination refuses immediately with the same
reason, instead of making another job that fails with it.

It is deliberately the *same* function the pipeline gates on. A warning that
disagreed with the gate would be worse than none: it would either promise a rip
that then fails, or complain about one that would have worked. A test asserts
the two produce identical text.

### When something fails

**Per-job logs.** Every job records what MakeMKV and HandBrake actually said —
the read error on title 3, the complaint about the preset. Open it from the
history page. Previously that lived only in `journalctl`, behind `pct exec`,
mixed in with every other job that week.

**Retry.** A rip is forty minutes and several GB, and most failures happen
*after* the expensive part. Retry looks at what is still on disk and resumes
from the furthest point it can:

| What survived | What retry does |
|---|---|
| The encoded files | Moves them to the destination. No re-encoding. |
| The raw MKVs | Re-encodes them. The disc is not needed. |
| Neither | Says so, instead of pretending. Put the disc back in. |

It tells you which before you commit, and re-checks the destination first —
retrying into the same unmounted share fails identically, and saying so up
front beats a second identical error twenty seconds later.

**A restart mid-job.** Pressing Update while a disc is in is a normal thing to
do, and the job's progress lives in the database while the thread doing the
work does not. Every job that was running is checked at the next start:

| Where it had got to | What happens |
|---|---|
| Mid-rip | Failed, saying so. Nothing survives a killed rip — press Rip to start again. |
| Mid-encode | The encode is queued again by itself. The raw files are intact and the expensive part is done. |
| Waiting to be moved | Failed, pointing at Retry, which re-checks the destination first. |

No notifications are sent for these: restarting is routine, and a burst of
"job failed" messages after every update would train you to ignore them.

**A drive that stops answering.** There is no overall time limit on a rip — a
Blu-ray with many playlists legitimately takes hours, and a slow disc must not
be thrown away. But MakeMKV producing *nothing at all* for thirty minutes is
not slow, it is stuck, and the read would otherwise block for as long as the
service runs, holding the drive with it. The rip is abandoned and the failure
says what to try next: the same drive with a different disc tells a bad disc
from a bad drive.

### Already ripped?

Working through a shelf, the expensive mistake is ripping the same film twice.
Before the rip starts — and *after* identification, when the title is known —
the disc is checked three ways, in descending order of how much each can be
trusted:

| Check | Catches |
|---|---|
| **The library** | `Title (Year)/` already holds a video file at the destination. The only check that is true rather than remembered: it survives a cleared history, a reinstall, and finds films that were in the library before this app existed |
| **TMDb id** | An earlier completed job for the same film — a different pressing, a re-release, a disc with a different label |
| **Disc label** | The fallback for a disc TMDb could not identify |

The label is the weakest signal, so better evidence overrules it: if TMDb
identified both discs and called them different films, a shared label is a
coincidence rather than a duplicate.

A duplicate is always logged, shown in the job's tool output, badged in the
history, and can be sent as a notification. Whether it *stops* the rip is
**Settings → Duplicates → Skip discs already ripped**, off by default —
re-ripping is legitimate when the first attempt came from a scratched disc or a
worse preset, and a false positive that silently cancels a disc is harder to
notice than one that only warns. Turn it on when working through a large shelf;
the disc is then cancelled and ejected before MakeMKV starts, so nothing is
wasted.

Series discs are exempt. Every disc of a box set writes into the same show
folder, which is the normal case rather than a warning; episodes are protected
by their numbering instead.

### MakeMKV key

MakeMKV needs a registration key. Three ways to provide it:

1. **Automatic (default):** the installer fetches the current free beta key from the MakeMKV forum.
2. **Manual:** paste a key in **Settings → MakeMKV key → Refresh / save key**, or set `MAKEMKV_KEY=T-…` at install.
3. **Environment:** set `ADR_MAKEMKV_KEY` for the service (see `/etc/default/adr`).

The beta key rotates roughly monthly — click **Refresh** in Settings if ripping
suddenly fails with a key error.

---

## Plex / NFS integration

### Ripping to a NAS (separate machine on the LAN)

The common homelab setup: the optical drive is in the Proxmox host, the movies
belong on a NAS. Ripping stays **local** (fast scratch on the container disk)
and only the finished MP4s go to the NAS.

```
 Proxmox host — everything happens locally          NAS (192.168.1.10)
 ┌──────────────────────────────────────────┐      ┌──────────────────┐
 │ /dev/sr0 ─rip─► /opt/adr/raw    (MKV)    │      │ /volume1/media   │
 │                     │                    │      │  Title (Year)/   │
 │              transcode                   │      │   …(Year).mp4    │
 │                     ▼                    │      └──────────────────┘
 │            /opt/adr/staging     (MP4)    │              ▲
 └──────────────────────────────────────────┘              │
                       └── one transfer when finished ──────┘
```

Only the finished MP4 crosses the network, and only once. HandBrake writes to
local disk for the whole encode rather than keeping the share busy for 30–40
minutes. This is automatic whenever `completed_path` is a network filesystem;
for local storage the staging step is skipped, since it would just be a
pointless extra copy of several GB.

**The easiest route is the web UI:** open **Storage** in the navigation. It shows
whether your finished files are landing on network storage or quietly on the
container disk, tests that the NAS is reachable, and generates the exact command
to paste into your Proxmox shell — pre-filled with your container ID.

For SMB the page takes a username, and the password is optional:

- **Leave it empty** (recommended) and the generated command starts with a
  `read -rsp` prompt, so the password is typed on the host and never crosses
  the network.
- **Fill it in** for a single copy-paste. It is used to build the command and
  then discarded — never written to the config, database or log — but it does
  travel over plain HTTP to get there, so the page says so.

> The mount itself is deliberately not performed by the web UI. This app runs
> inside the container as an unprivileged user, and the page has no
> authentication; a button that could mount as root on your LAN would be a
> liability, not a feature. So the UI does everything except the privileged
> step.

#### Already added the share under Datacenter → Storage?

Then Proxmox has already mounted it, at `/mnt/pve/<storage-id>`, and there is
nothing to mount. Point the container at it and let Proxmox stay in charge:

```bash
MEDIA_HOST_PATH=/mnt/pve/<storage-id> adr-setup-nas <CTID>
```

This touches neither `/etc/fstab` nor the mount itself. It still does the parts
that matter: it verifies the container's service user can write there, waits for
the mount before guests autostart, bind-mounts it into the container, and turns
on the pre-rip mount check. Run it with no arguments to see which shares the
host currently has mounted.

This is usually the tidier route — the share is managed in one place, survives
reboots, and is visible in the Proxmox UI.

#### Or let the script mount it

Run it directly **on the Proxmox host** — during install or any time afterwards:

```bash
# NFS
NAS_URL=nfs://192.168.1.10/volume1/media adr-setup-nas <CTID>

# SMB / CIFS
NAS_URL=smb://192.168.1.10/media \
NAS_USERNAME=plex NAS_PASSWORD=secret adr-setup-nas <CTID>
```

Or pass `NAS_URL=…` to the installer and it is done as part of the install.

It mounts the share on the host (persisted in `/etc/fstab`), bind-mounts it
into the container at `/mnt/media`, points `completed_path` there, then
**proves the container's `adr` user can actually write there** — and if it
can't, prints the exact NFS export or SMB permission to change.

**Why the mount lives on the host** — one mount serves any number of
containers, the container needs no mount privileges, and it is the documented
Proxmox pattern.

#### Permissions: the one thing that usually bites

The service user is created with a **pinned uid/gid `8420`** precisely so this
is documentable. NFS authorises writes by numeric uid, so the export must
accept 8420:

| NAS | What to set |
|---|---|
| Synology | Shared Folder → Edit → NFS Permissions → *Squash: Map all users to admin*, **or** give uid 8420 write access |
| TrueNAS | Sharing → NFS → Edit → **Mapall User/Group** |
| Linux `/etc/exports` | `/volume1/media <proxmox-ip>(rw,sync,all_squash,anonuid=8420,anongid=8420,no_subtree_check)` then `exportfs -ra` |

SMB is simpler: the share is mounted with `uid=8420,gid=8420`, so you only need
the SMB user to have write access to the share.

#### Two behaviours worth knowing

- **`hard` NFS mounts, on purpose.** With `soft`, a network hiccup aborts the
  in-flight write and HandBrake can produce a silently truncated MP4. `hard`
  blocks until the NAS answers instead of corrupting a multi-GB file.
- **Restart the container after a NAS outage.** A bind-mount captures whatever
  the source resolves to when the container *starts*. If the NAS is remounted
  later, a running container will not see it and keeps writing to the host
  disk. `adr-setup-nas` guards against this three ways: it makes the bare
  mountpoint immutable while unmounted (so a missing NAS becomes a loud error
  instead of a silently filled host disk), it makes Proxmox's guest autostart
  wait for the mount, and it sets `require_completed_mount: true` so a rip
  **refuses to start** unless the share is really mounted — you get an
  immediate, explanatory error instead of discovering it 40 minutes and 8 GB
  later. After any outage:
  ```bash
  pct reboot <CTID>
  ```

#### The pre-rip check

Every rip verifies its destination before MakeMKV is launched: the directory
must exist, be writable by the service user, and have room. With
`require_completed_mount: true` it must also be a genuine mount point. A
configured `plex_path` is checked the same way, since that is where the film is
actually going. A failing check aborts the job immediately and the reason
appears in the job's error in the web UI. Without a NAS the setting stays
`false` and local storage works exactly as before.

#### Local staging

HandBrake writing an encode straight onto a network share means hours of small
random writes over the LAN, and any hiccup lands in the middle of the file.
So it doesn't: encoding always happens in `/opt/adr/staging` on the container's
own disk, and the finished folder is moved to its destination in one sequential
transfer at the end. Raw MKVs never touch the network at all.

**That transfer goes straight to the folder the film will live in.** If the job
is bound for the Plex library, the library *is* the destination —
`completed_path` is not a waypoint on the way there. Writing several GB into a
folder nothing reads, only to move them again, is exactly the network traffic
staging exists to avoid; if the two paths happen to sit on different mounts it
would be a second full copy on top.

So, in order, for a film destined for Plex on a NAS:

```
/dev/sr0  →  /opt/adr/raw       MakeMKV, local
          →  /opt/adr/staging   HandBrake, local
          →  <plex_path>        one sequential transfer — the only network write
```

Nothing reaches the NAS before that last step.

Staging kicks in automatically when the destination is a network filesystem —
staging to and from the same local disk would be a pointless extra copy, so it
is skipped there. Set `stage_locally: false` to turn it off.

If the transfer fails, the encoded files are **left in staging** and the job is
marked failed with the reason. Nothing is deleted before the copy is known to
have arrived.

### Local or already-mounted storage

If the host already has the storage mounted, bind-mount it directly:

```bash
pct set <CTID> -mp0 /tank/media/Movies,mp=/mnt/media
```

…and set `completed_path: /mnt/media` under **Settings**. (Or just set
`MEDIA_HOST_PATH` during install and the installer does both.) Output uses the
Plex layout `Title (Year)/Title (Year).mp4`, so you can point Plex straight at
the host path.

---

## Managing the service

```bash
pct exec <CTID> -- systemctl status adr         # status
pct exec <CTID> -- journalctl -u adr -f         # live logs
pct exec <CTID> -- systemctl restart adr        # restart
bash scripts/uninstall.sh <CTID>                # destroy the whole container (host)
```

### Updating

**From the web UI:** the **Doctor** page checks GitHub for a newer commit and
applies it with one button, streaming the log as it goes. The service restarts
itself at the end and the page reconnects.

How that works is worth a paragraph, because a web page that can install code as
root would be a much bigger thing than a web page that can rip a disc. The app
runs unprivileged with `NoNewPrivileges=yes` and cannot escalate. It *requests*
the update by touching a flag file; `adr-update.path` notices and starts
`adr-update.service`, which runs `update.sh` as root. Repository and branch live
in that unit, never in the HTTP request — so the endpoint cannot be talked into
fetching code from somewhere else. The most an unauthenticated caller on your
LAN can do is ask for the update you already configured.

Running it as a separate unit also matters mechanically: `update.sh` stops and
starts `adr.service`, so an update spawned as a child of the web app would kill
itself halfway through.

**From the host**, unchanged: `update.sh` re-fetches the source, reinstalls
dependencies, reinstalls any unit that changed, and restarts — then waits for
the web UI to answer before reporting success. Your `config/adr.yaml`, database,
and everything in `raw/`, `completed/` and `watch/` are preserved.

```bash
pct exec <CTID> -- /opt/adr/scripts/update.sh
```

The installed commit is recorded in `/opt/adr/.commit` (the install is a working
tree with no `.git`, so nothing else could answer "am I up to date?"). An
install made before 1.0 has no such file; the Doctor page says "cannot tell"
rather than offering a phantom update, and the next update writes it.

For a **private** repo, pass the token through:

```bash
pct exec <CTID> -- env GITHUB_TOKEN=github_pat_xxx /opt/adr/scripts/update.sh
```

`update.sh` runs inside the container, so it cannot refresh the host-side
helpers (`adr-setup-nas` and `adr-doctor`). To update everything, run this on
the Proxmox host:

```bash
pct exec <CTID> -- /opt/adr/scripts/update.sh
for f in setup-nas:adr-setup-nas adr-doctor:adr-doctor; do
  pct pull <CTID> /opt/adr/scripts/${f%%:*}.sh /usr/local/sbin/${f##*:} \
    && chmod +x /usr/local/sbin/${f##*:}
done
adr-doctor --fix <CTID>
```

---

## Verifying the install

```bash
pct status <CTID>                                              # running
pct exec <CTID> -- systemctl is-active adr                     # active
curl -fsS http://<CT-IP>:8080/api/status                       # JSON status
pct exec <CTID> -- /opt/adr/.venv/bin/python -c \
  "from adr.disc import list_optical_drives; print(list_optical_drives())"   # [{'drive': '/dev/sr0', ...}]
pct exec <CTID> -- HandBrakeCLI --preset-list | head           # HandBrake OK
```

Insert a disc and watch `journalctl -u adr -f` log `Disc inserted in /dev/sr0`.

---

## Troubleshooting

### The Doctor page — start here

Open **Doctor** in the web UI. It runs everything the container can check about
itself and says what to do about each failure:

| Check | Catches |
|---|---|
| Optical drives | Passthrough that did not apply, or a cgroup denying the device |
| MakeMKV and HandBrake | A half-finished install where ripping or encoding cannot work |
| HandBrake preset | A preset name missing from the file it is supposed to come from — every encode then fails identically |
| MakeMKV key | The registration key that MakeMKV refuses to open a disc without |
| Destination | The path films actually land in — missing, read-only, unmounted, or full |
| Local scratch space | A container disk too small for a dual-layer DVD plus its encode |
| Job database | An unwritable database, where nothing gets recorded |

A count of failures rides along in the navbar on every page, so a problem finds
you before the failed rip does.

### Testing a drive

The dashboard tells you whether a disc is loaded. **Doctor → Optical drives**
tells you whether the drive *works*, by poking it:

| Probe | Answers |
|---|---|
| Device node | Did the passthrough apply at all? |
| Open device | Does the container's device cgroup allow access? |
| Drive status | What does the drive itself say — empty, tray open, spinning up, disc ready, and what kind of disc? |
| Generic SCSI (SG_IO) | Can MakeMKV talk to it? This is the interface it rips through |
| Read from disc | Do reads work, not just `open()`? Reads the ISO 9660/UDF volume descriptor |

They run in that order and stop at the first failure, because everything after
it is a consequence rather than a separate problem.

The SG_IO probe is the one worth knowing about: a drive can open cleanly, report
a disc, and still refuse SG_IO — at which point the dashboard looks perfectly
healthy and every rip fails. Nothing else in the app exercises that path until
MakeMKV does.

**Test with MakeMKV** goes further and asks MakeMKV to open the disc for real.
It is the only check that exercises the registration key end to end, and the
only one slow enough (up to 90 s) to be worth a separate button.

**Scan for drives** re-reads sysfs and hot-adds anything new, rather than
waiting out the watcher's 30-second cache. If the host has a drive this
container did not get, it says so and points at the host — scanning cannot fix
that, because a device node cannot be added to a running container.

### `adr-doctor` — for what the container cannot see

The device cgroup, the passthrough entries and guest-autostart ordering live in
`/etc/pve/lxc/<CTID>.conf`. The container cannot read or change them — that is
what container isolation is for — so the Doctor page hands over this command
instead of guessing. Run it on the **Proxmox host**:

```bash
adr-doctor <CTID>          # report only, changes nothing
adr-doctor --fix <CTID>    # apply the repairs it found
```

It checks, and with `--fix` repairs:

- the device cgroup rules (`b 11:* rwm` — see below),
- a passthrough entry for every optical drive the host has, plus its `/dev/sg`
  node,
- guest-autostart ordering against the drive's device unit,
- a media share still mounted over `/opt/adr/completed` (the pre-1.0 layout),

then asks the container itself whether it can open each drive, using the same
code the dashboard does.

### The drive worked, then stopped after a reboot

This one has a specific cause worth understanding.

Passthrough entries are written with `optional`, so a device that does not
exist *yet* is skipped — silently. When you install, the container is started
by hand, long after udev has created `/dev/sr0`, and everything works. On the
next host boot, `pve-guests.service` can start the container before udev gets
there. The bind is skipped, and a device node **cannot** be added to a running
container: it stays missing until the container restarts.

`adr-doctor --fix` closes the race by ordering `pve-guests.service` after the
drive's `.device` unit. Until then, `pct reboot <CTID>` gets the drive back.

The dashboard also detects this state directly and says so, rather than showing
an empty drive list. Inside an LXC `/sys` is the *host's* sysfs, so the app can
see that the host has a drive it cannot open — which is exactly the difference
between "no drive" and "passthrough is broken".

### `/dev/sr0` is there but every read fails

`/dev/sr*` are **block** devices with major 11. LXC's default policy denies
everything and then re-allows `b *:* m`, which permits *creating* the node but
not opening it — so the node appears and every `open()` returns `EPERM`. The
config needs an explicit:

```
lxc.cgroup2.devices.allow: b 11:* rwm
```

A `c 11:*` rule does not help; char major 11 is something else entirely.
`adr-doctor --fix` adds the correct rule.

### Everything else

- **No drive detected:** confirm the host sees it (`ls /dev/sr*`) and that the
  passthrough lines exist in `/etc/pve/lxc/<CTID>.conf`. Restart the container
  after attaching a USB drive.
- **MakeMKV “registration” errors:** refresh the beta key (Settings → MakeMKV key).
- **MakeMKV PPA failed to install:** the `heyarje/makemkv-beta` PPA may lag a fresh Ubuntu
  release; install MakeMKV manually, then `systemctl restart adr`.
- **Ripping fails inside an unprivileged container:** optical SG_IO passthrough is
  far simpler in a **privileged** container (`CT_UNPRIVILEGED=0`, the default).
- **Web UI unreachable:** `journalctl -u adr -e` inside the container.

---

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest ruff
pytest -q          # run the test suite
ruff check .       # lint
```

### Project structure

```
adr/        Core package (config, disc, disctype, ripper, encoder, identify, pipeline,
            applog, bundle, preflight, progress, recovery,
            watcher, audiocd, musicbrainz, isobackup, series, seriesmode, naming,
            duplicates, retry, joblog, notify, plex, makemkv_key, storage,
            diagnostics, drivetest, updater)
web/        Flask app (dashboard, history, storage, doctor, settings), templates, static assets
scripts/    install.sh (host), install-container.sh, setup-nas.sh, adr-doctor.sh, update.sh, uninstall.sh
systemd/    adr.service, adr-update.service, adr-update.path
config/     adr.yaml.example
presets/    HandBrake JSON presets
tests/      pytest suite
```

### Tech stack

Python 3.11+ · Flask · SQLAlchemy + SQLite (WAL) · MakeMKV · HandBrakeCLI ·
cdparanoia · ffmpeg · TMDb · MusicBrainz.

---

## License

MIT — see [LICENSE](LICENSE). Built on the excellent
[MakeMKV](https://makemkv.com) and [HandBrake](https://handbrake.fr) projects.
