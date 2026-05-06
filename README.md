# Automatic Disc Ripper for Proxmox

**Headless DVD/Blu-ray ripping for a Proxmox LXC.** Insert a disc, ADR
rips it with MakeMKV, ejects the tray so you can swap to the next one,
and transcodes to MP4 with HandBrake in the background. Everything is
visible in a web dashboard on port 8080.

This is a Linux port of
[hhammarstrand/Automatic-Disc-Ripper-for-Windows](https://github.com/hhammarstrand/Automatic-Disc-Ripper-for-Windows).
The web UI, TMDb metadata lookups, job database, HandBrake preset
auto-discovery, and watch-folder feature are unchanged. The Windows
WMI / pywin32 layer is replaced with `/dev/sr*` enumeration plus the
kernel's `CDROM_DRIVE_STATUS` and `CDROMEJECT` ioctls (with `eject(1)`
as a fallback). Volume labels come from `blkid`.

> **Project status — beta.** Compiles, unit tests pass, but not yet
> exercised end-to-end against real Proxmox hardware with an optical
> drive. Expect rough edges. Issues and PRs welcome.

> **Credits.** Design borrows heavily from
> [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine):
> the cdrom-group + service-user pattern, the decoupled rip/encode queue,
> the optional udev rule in `packaging/99-adr.rules`, and the general
> "insert disc → MP4 on Plex" flow. ARM is the mature project in this
> space — if you need audio CDs (abcde), data-disc ISO backups,
> multi-episode TV, or a Docker deployment, **use ARM instead**. This
> fork is the lighter-weight option for users who already run Proxmox
> and want a small single-purpose LXC.

---

## Quick install on Proxmox

From the **Proxmox host shell**, as root:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/proxmox/ct/adr.sh)"
```

The helper:

1. Creates a privileged Debian 12 LXC (default: 2 CPU / 2 GiB RAM /
   20 GiB disk on `local-lvm`, DHCP on `vmbr0`).
2. Patches `/etc/pve/lxc/<CTID>.conf` so the container can talk to
   `/dev/sr0` (cgroup allows for cdrom/SCSI majors + a bind-mount).
3. Inside the CT, installs HandBrakeCLI from apt and builds MakeMKV
   (oss + bin) from source — the precompiled `makemkvcon` only;
   no Qt, no GUI.
4. Clones this repo to `/opt/adr`, runs `install.sh`, enables the
   systemd unit.

All overrides (`CTID`, `HOSTNAME`, `STORAGE`, `BRIDGE`, `DISK_GB`,
`CORES`, `RAM_MB`, `OPTICAL_DEV`, `TEMPLATE_STORAGE`) are documented
in [`proxmox/README.md`](proxmox/README.md).

---

## Manual install (any Linux host or LXC)

If you already have a Linux box and just want to run the app:

### 1. System packages

```bash
sudo apt install -y \
    handbrake-cli eject util-linux \
    python3 python3-venv \
    build-essential pkg-config curl \
    libc6-dev libssl-dev libexpat1-dev \
    zlib1g-dev libbz2-dev liblzma-dev
```

### 2. MakeMKV from source

MakeMKV is not in Debian repos (licensing). Build the headless CLI:

```bash
MAKEMKV_VERSION=1.17.9
cd /tmp
curl -fsSLO "https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz"
curl -fsSLO "https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz"
tar xzf makemkv-oss-${MAKEMKV_VERSION}.tar.gz && tar xzf makemkv-bin-${MAKEMKV_VERSION}.tar.gz
cd makemkv-oss-${MAKEMKV_VERSION}
./configure --prefix=/usr --disable-gui && make -j"$(nproc)" && sudo make install
cd ../makemkv-bin-${MAKEMKV_VERSION}
mkdir -p tmp && echo accepted >tmp/eula_accepted
make PREFIX=/usr -j"$(nproc)" && sudo make PREFIX=/usr install
```

The same logic lives in [`proxmox/install/adr-install.sh`](proxmox/install/adr-install.sh)
(see the `build_makemkv` function) and is automatically invoked by the
Proxmox helper.

### 3. ADR

```bash
sudo git clone https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git /opt/adr
cd /opt/adr
sudo ./install.sh                 # full install with systemd service
# or:
./install.sh --no-service         # venv + deps only, run manually
```

`install.sh` creates a venv at `/opt/adr/.venv`, installs deps from
`requirements.txt`, seeds `config/adr.yaml` from the example, creates
`/var/lib/adr/{raw,completed}`, creates the `adr` system user (added
to the `cdrom` group), installs `/etc/systemd/system/adr.service`,
and enables it.

Web UI: `http://<host-ip>:8080`.

For development: `./install.sh --no-service && .venv/bin/python run.py`.

---

## How disc detection works

ADR polls each `/dev/sr*` device every 3 s with the `CDROM_DRIVE_STATUS`
ioctl. When a drive reports `CDS_DISC_OK`, a job is queued. The volume
label is read with `blkid`. Ejection uses the `CDROMEJECT` ioctl, with
`eject(1)` as a fallback.

In `auto` mode the device list is re-globbed every 10 s, so hot-plugged
USB DVD drives appear in the dashboard within ~10 s of insertion.

Polling needs no root. Event-driven detection via udev is **not**
required — the 3-second latency is negligible next to a 20–40 minute
rip. An optional udev rule
([`packaging/99-adr.rules`](packaging/99-adr.rules), modeled on ARM's)
ships for observability: install it if you want disc-insert events
in syslog for troubleshooting.

```bash
sudo install -m 644 packaging/99-adr.rules /etc/udev/rules.d/99-adr.rules
sudo udevadm control --reload-rules
```

The service user must be in the `cdrom` group to open `/dev/sr*`.
`install.sh` adds the `adr` user automatically.

---

## Configuration

All settings live in `config/adr.yaml`. The web UI's Settings page
edits the same file. Defaults:

| Key | Default | Notes |
|---|---|---|
| `makemkv_path` | `/usr/bin/makemkvcon` | |
| `handbrake_path` | `/usr/bin/HandBrakeCLI` | |
| `raw_path` | `/var/lib/adr/raw` | MKV staging (auto-cleaned after encode) |
| `completed_path` | `/var/lib/adr/completed` | final MP4s |
| `drives` | `auto` | or list: `["/dev/sr0", "/dev/sr1"]` |
| `disabled_drives` | `[]` | `/dev/sr*` paths to ignore at runtime |
| `no_eject_drives` | `[]` | `/dev/sr*` paths where auto-eject is off |
| `drive_labels` | `{}` | friendly names, e.g. `/dev/sr0: "LG External"` |
| `min_title_length` | `120` | seconds; titles shorter than this are skipped |
| `main_feature_only` | `true` | rip only the longest title (skip menus, trailers) |
| `handbrake_preset` | `Fast 1080p30` | any built-in or custom preset |
| `handbrake_preset_file` | `''` | path to a JSON preset (auto-discovered from `presets/`) |
| `max_encode_jobs` | `1` | parallel HandBrake workers |
| `web_host` / `web_port` | `0.0.0.0` / `8080` | |
| `tmdb_api_key` | `''` | or set `ADR_TMDB_API_KEY` env var |
| `plex_path` | `''` | Plex movie library; empty = disabled |
| `auto_move_to_plex` | `true` | move matched movies to Plex after encode |
| `watch_path` | `''` | folder to scan for video files; empty = disabled |
| `watch_output_path` | `''` | output folder for watch-folder jobs (defaults to `completed_path`) |
| `watch_interval` | `5` | seconds between watch-folder scans |
| `log_level` | `INFO` | one of `DEBUG`, `INFO`, `WARNING`, `ERROR` |

---

## Repository layout

```
adr/                  # Core Python package
  config.py             Loads/saves config/adr.yaml
  disc.py               /dev/sr* enumeration, ioctl-based eject
  encoder.py            HandBrakeCLI wrapper
  identify.py           TMDb lookup + similarity scoring
  models.py             SQLAlchemy job/track schema (SQLite + WAL)
  pipeline.py           Per-drive rip → eject → encode orchestration
  ripper.py             MakeMKV robot-mode wrapper
  utils.py              Helpers (filename sanitiser, parse_disc_label, …)
  watcher.py            Watch-folder scanner

web/                  # Flask UI + JSON API on port 8080
  app.py                Routes (/api/jobs, /api/drives, /api/tmdb, …)
  templates/            Bootstrap 5 dark theme
  static/

config/               adr.yaml.example with Linux defaults
presets/              Drop HandBrake JSON presets here
tests/                pytest unit tests (no disc/network needed)
packaging/            adr.service systemd unit + 99-adr.rules udev rule
proxmox/              ct/adr.sh + install/adr-install.sh + README

install.sh            Local installer (venv, systemd, user, perms)
run.py                Entry point — starts pipeline and Flask
```

---

## Logs and troubleshooting

Tail the service log:

```bash
journalctl -u adr.service -f
```

Common issues:

- **No drives detected** — confirm `/dev/sr0` exists *inside the
  container* (`ls -l /dev/sr*`), and the service user is in the
  `cdrom` group (`id adr`). On Proxmox, the host must pass the device
  via a `lxc.mount.entry` plus a `lxc.cgroup2.devices.allow` line —
  see [`proxmox/README.md`](proxmox/README.md).
- **MakeMKV "no disc" on a valid disc** — almost always libdvdcss /
  region / scanner quirks. Test manually:
  `makemkvcon -r info dev:/dev/sr0`.
- **Eject fails** — another process holds the device open. Check with
  `lsof /dev/sr0`. The watch-folder scanner does not touch `/dev/sr*`,
  but a previous `makemkvcon` may still be running.
- **MakeMKV beta key expired** — MakeMKV ships as a free beta with a
  monthly-refreshing key. After the key expires `makemkvcon` will
  refuse to rip. Grab the current key from the
  [MakeMKV forum](https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053)
  and run `makemkvcon reg <KEY>` (or paste it via the GUI on a
  desktop machine and copy `~/.MakeMKV/settings.conf` into the
  container).

---

## Development

```bash
git clone https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git
cd Automatic-Disc-Ripper-proxmox
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

The unit tests don't need a real disc or network — they cover MakeMKV
robot-mode parsers, TMDb scoring, filename sanitisation, and disc-label
parsing.

---

## License

MIT, same as upstream.
