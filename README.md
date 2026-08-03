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
- **One-command install** on the Proxmox host: creates the container, passes the optical drive through, installs everything, starts the service.
- **No compilation:** MakeMKV from the `heyarje/makemkv-beta` PPA, HandBrakeCLI from Ubuntu universe.
- **Automatic MakeMKV key:** fetches the current free beta key, or accepts your own.
- **Web dashboard** (port 8080) with live progress, job history, a Storage page for NAS setup, settings, and in-browser playback.
- **Multi-drive** support and a **watch folder** for batch encoding of existing video files.
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
| `plex_path` | — | Plex library folder. When set (with `auto_move_to_plex`) films are written **directly** here and never pass through `completed_path` |
| `require_completed_mount` | `false` | Refuse to start a rip unless the destination is a real mount point — set automatically by `adr-setup-nas` |
| `stage_locally` | `true` | Encode to local disk and transfer the finished film in one copy. Only applies when `completed_path` is network storage |
| `staging_path` | `/opt/adr/staging` | Local scratch used while encoding to network storage |

### Where things live

```
/opt/adr/            the application — code, database, venv, scratch space
  ├─ raw/            raw MKVs straight off the disc   (local, deleted after encoding)
  ├─ staging/        HandBrake writes here            (local, always)
  └─ completed/      finished films, when you have no NAS
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

`update.sh` re-fetches the source, reinstalls dependencies, reinstalls the unit
if it changed, and restarts — then waits for the web UI to answer before
reporting success. Your `config/adr.yaml`, database, and everything in
`raw/`, `completed/` and `watch/` are preserved.

```bash
pct exec <CTID> -- /opt/adr/scripts/update.sh
```

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

### `adr-doctor` — start here

Most problems with this setup are invisible from inside the container, because
what is broken is the *host's* view of it. Run this on the **Proxmox host**:

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
adr/        Core package (config, disc, ripper, encoder, identify, pipeline, watcher, makemkv_key)
web/        Flask app (dashboard, history, storage, settings), templates, static assets
scripts/    install.sh (host), install-container.sh, setup-nas.sh, adr-doctor.sh, update.sh, uninstall.sh
systemd/    adr.service unit
config/     adr.yaml.example
presets/    HandBrake JSON presets
tests/      pytest suite
```

### Tech stack

Python 3.11+ · Flask · SQLAlchemy + SQLite (WAL) · MakeMKV · HandBrakeCLI · TMDb.

---

## License

MIT — see [LICENSE](LICENSE). Built on the excellent
[MakeMKV](https://makemkv.com) and [HandBrake](https://handbrake.fr) projects.
