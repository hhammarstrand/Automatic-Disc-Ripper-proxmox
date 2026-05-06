# Automatic Disc Ripper for Proxmox

**Automatic Disc Ripper for Proxmox** — headless DVD/Blu-ray ripping and
transcoding, designed to run inside a Proxmox LXC container with an optical
drive passed through from the host.

Insert a disc. ADR rips it with MakeMKV, ejects the tray so the next disc can
go in, and transcodes to MP4 with HandBrake in the background. Everything is
monitored from a web UI on port 8080.

This is a Linux port of
[hhammarstrand/Automatic-Disc-Ripper-for-Windows](https://github.com/hhammarstrand/Automatic-Disc-Ripper-for-Windows).
The web UI, TMDb metadata lookups, job database, HandBrake preset discovery,
and watch-folder feature are unchanged. The Windows-specific WMI and pywin32
bits have been replaced with Linux equivalents (`/dev/sr*`, the
`CDROM_DRIVE_STATUS` / `CDROMEJECT` ioctls, `blkid` for volume labels).

> **Project status — beta.** The port compiles and the unit tests pass, but
> this fork has not yet been exercised end-to-end against real Proxmox
> hardware with an optical drive. Issues and PRs welcome.

> **Credits.** Design borrows from
> [Automatic Ripping Machine](https://github.com/automatic-ripping-machine/automatic-ripping-machine):
> the cdrom-group + service-user pattern, the decoupled rip/encode queue,
> the optional udev rule in `packaging/99-adr.rules`, and the general
> "insert disc → MP4 on Plex" flow are all prior art from ARM.
>
> ARM is the mature project in this space — if you need audio CDs
> (abcde), data-disc ISO backups, multi-episode TV support, or a Docker
> deployment, use ARM instead. This fork is a lighter-weight option for
> users who already run Proxmox and want a small single-purpose LXC.

---

## Quick install on Proxmox (recommended)

From the **Proxmox host shell**, run:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/proxmox/ct/adr.sh)"
```

The helper script creates a Debian 12 LXC container, installs HandBrakeCLI,
MakeMKV, and the app, sets up a systemd service, and patches the CT's config
to pass through `/dev/sr0`. See [`proxmox/README.md`](proxmox/README.md) for
details.

---

## Manual install (any Linux host)

If you already have a Linux box or LXC and just want to run the app:

### 1. Install MakeMKV and HandBrakeCLI

| Tool | How |
|---|---|
| **HandBrakeCLI** | `apt install handbrake-cli` on Debian 12 |
| **MakeMKV** | No Debian package. Build from source: see `proxmox/install/makemkv.sh` or the [MakeMKV forum guide](https://forum.makemkv.com/forum/viewtopic.php?f=3&t=224). The `makemkvcon` binary must land in `/usr/bin/` (or point `makemkv_path` in the config at it). |

Also install helpers used for disc detection:

```bash
apt install -y eject util-linux python3-venv
```

### 2. Install ADR

```bash
git clone https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git /opt/adr
cd /opt/adr
sudo ./install.sh
```

`install.sh` creates a venv, installs Python dependencies, seeds
`config/adr.yaml` from the example, creates `/var/lib/adr/{raw,completed}`,
and installs + enables a systemd unit that runs the app as the `adr` user.

The app listens on `http://0.0.0.0:8080` by default. Visit it from any
browser on the LAN.

To run without systemd (e.g. for development), pass `--no-service`:

```bash
./install.sh --no-service
.venv/bin/python run.py
```

---

## How disc detection works on Linux

ADR polls each `/dev/sr*` device every 3 s using the `CDROM_DRIVE_STATUS`
ioctl. When a drive reports `CDS_DISC_OK`, a job is queued. The volume label
is read via `blkid`. Ejection is done with the `CDROMEJECT` ioctl, falling
back to the `eject(1)` command.

Polling is simple and requires no root. Event-driven detection via udev is
not used by the app itself; the 3-second latency is negligible next to a
20–40 minute rip. An **optional** udev rule
(`packaging/99-adr.rules`, modeled on ARM's rules) is shipped for
observability — install it if you want disc-insert events logged to syslog
for troubleshooting.

### Running as a non-root user

The `adr` user needs to be in the `cdrom` group so it can open `/dev/sr*`.
`install.sh` adds the user to that group automatically. Ejection requires
write access to the device, which the `cdrom` group grants on Debian.

---

## Configuration

All settings live in `config/adr.yaml`. Defaults:

| Key | Default | Notes |
|---|---|---|
| `makemkv_path` | `/usr/bin/makemkvcon` | |
| `handbrake_path` | `/usr/bin/HandBrakeCLI` | |
| `raw_path` | `/var/lib/adr/raw` | MKV staging |
| `completed_path` | `/var/lib/adr/completed` | final MP4s |
| `drives` | `auto` | or list: `["/dev/sr0", "/dev/sr1"]` |
| `disabled_drives` | `[]` | list of `/dev/sr*` paths to ignore |
| `no_eject_drives` | `[]` | list of `/dev/sr*` paths where auto-eject is off |
| `drive_labels` | `{}` | friendly names, e.g. `/dev/sr0: "LG External"` |
| `web_host` | `0.0.0.0` | |
| `web_port` | `8080` | |
| `tmdb_api_key` | `''` | or set `ADR_TMDB_API_KEY` env var |

The settings page in the web UI edits the same file.

---

## Logs and troubleshooting

If running under systemd:

```bash
journalctl -u adr.service -f
```

Common issues:

- **No drives detected** — confirm `/dev/sr0` exists inside the container and
  the service user is in the `cdrom` group (`id adr`). On Proxmox, the host
  must pass the device into the LXC via a `lxc.mount.entry` (see
  `proxmox/README.md`).
- **MakeMKV "no disc" on a valid disc** — usually a libdvdcss / region /
  scanner quirk. Test `makemkvcon -r info dev:/dev/sr0` manually.
- **Eject fails** — another process may hold the device open. Check with
  `lsof /dev/sr0`.

---

## License

MIT, same as upstream.
