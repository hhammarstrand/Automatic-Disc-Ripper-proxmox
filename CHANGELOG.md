# Changelog

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

### 1.1.1 — the gaps 1.1 left

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
