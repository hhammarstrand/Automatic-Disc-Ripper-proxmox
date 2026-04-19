# Automatic Disc Ripper — Proxmox helper

One-liner install from the Proxmox host shell:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/proxmox/ct/adr.sh)"
```

## What the helper does

1. Creates a **privileged** Debian 12 LXC (default: 2 CPU, 2 GiB RAM, 20 GiB
   disk) on storage `local-lvm`, attached to bridge `vmbr0` via DHCP.
2. Patches `/etc/pve/lxc/<CTID>.conf` so the container can talk to the
   optical drive:
   ```
   lxc.cgroup2.devices.allow: c 11:* rwm   # cdrom major
   lxc.cgroup2.devices.allow: c 21:* rwm   # scsi-generic major (needed by MakeMKV)
   lxc.mount.entry: /dev/sr0 dev/sr0 none bind,optional,create=file 0 0
   ```
3. Installs HandBrakeCLI from apt and builds MakeMKV (oss + bin) from source.
4. Clones this repo to `/opt/adr`, runs `install.sh`, and enables the
   `adr.service` systemd unit.

## Why privileged?

Passing `/dev/sr0` into an unprivileged LXC is possible but needs extra
`idmap` gymnastics: the `disk` / `cdrom` group gid inside the container
must map to the host's. Privileged containers avoid that problem entirely.
If you prefer unprivileged, you'll need to:

1. Leave the container unprivileged.
2. Add `lxc.idmap` entries so the container's `cdrom` group (gid 24 on
   Debian) maps to the host's.
3. Ensure the host's `/dev/sr0` is group-readable by gid matching the
   subuid-shifted value.

This is deliberately not scripted — verify it manually before committing.

## Environment overrides

| Var | Default |
|---|---|
| `CTID` | next available |
| `HOSTNAME` | `adr` |
| `STORAGE` | `local-lvm` |
| `BRIDGE` | `vmbr0` |
| `DISK_GB` | `20` |
| `CORES` | `2` |
| `RAM_MB` | `2048` |
| `OPTICAL_DEV` | `/dev/sr0` |
| `TEMPLATE_STORAGE` | `local` |

Example:

```bash
CTID=250 HOSTNAME=ripper OPTICAL_DEV=/dev/sr1 \
    bash -c "$(wget -qLO - https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/proxmox/ct/adr.sh)"
```

## Multiple optical drives

If you have more than one drive, pass only the first via the helper, then
add the rest by editing `/etc/pve/lxc/<CTID>.conf` and restarting the CT:

```
lxc.mount.entry: /dev/sr1 dev/sr1 none bind,optional,create=file 0 0
```

ADR's disc watcher picks up new devices at runtime.

## After install

- Web UI: `http://<CT IP>:8080`
- Service: `pct exec <CTID> -- systemctl status adr.service`
- Logs:    `pct exec <CTID> -- journalctl -u adr.service -f`
- Config:  `pct exec <CTID> -- nano /opt/adr/config/adr.yaml`
- Rips:    `pct exec <CTID> -- ls /var/lib/adr/completed`

## MakeMKV license

MakeMKV ships as a free beta with a monthly-refreshing key during beta
periods. The build step installs the binary but does **not** install a
key. Register or enter the current beta key from the upstream forum
before the first rip:

```bash
pct exec <CTID> -- makemkvcon reg <KEY>
```
