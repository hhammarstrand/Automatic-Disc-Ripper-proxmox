# Changelog

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
