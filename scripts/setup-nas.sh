#!/usr/bin/env bash
#
# Attach a NAS share to an Automatic Disc Ripper container.
# Run this on the PROXMOX HOST.
#
#   NAS_URL=nfs://192.168.1.10/volume1/media  ./setup-nas.sh <CTID>
#   NAS_URL=smb://192.168.1.10/media NAS_USERNAME=plex NAS_PASSWORD=secret ./setup-nas.sh <CTID>
#
# The share is mounted on the HOST and bind-mounted into the container as
# /opt/adr/completed, which is the recommended Proxmox pattern: one mount
# serves any number of containers and the container needs no mount privileges.
#
# Ripping stays LOCAL (fast scratch in /opt/adr/raw on the container disk);
# only the finished MP4s are written to the NAS.
#
# Env vars:
#   NAS_URL          nfs://host/export/path  or  smb://host/share[/subdir]   (required)
#   NAS_USERNAME     SMB user     (SMB only)
#   NAS_PASSWORD     SMB password (SMB only)
#   NAS_DOMAIN       SMB domain/workgroup (SMB only, optional)
#   NAS_MOUNTPOINT   host mountpoint          (default /mnt/adr-media)
#   ADR_UID/ADR_GID  uid/gid the container's 'adr' user runs as (default 8420)
#   NAS_EXTRA_OPTS   extra mount options, appended verbatim
#
set -euo pipefail

CTID="${1:-}"
NAS_URL="${NAS_URL:-}"
NAS_USERNAME="${NAS_USERNAME:-}"
NAS_PASSWORD="${NAS_PASSWORD:-}"
NAS_DOMAIN="${NAS_DOMAIN:-}"
NAS_MOUNTPOINT="${NAS_MOUNTPOINT:-/mnt/adr-media}"
ADR_UID="${ADR_UID:-8420}"
ADR_GID="${ADR_GID:-8420}"
NAS_EXTRA_OPTS="${NAS_EXTRA_OPTS:-}"
CREDS_FILE="/root/.adr-nas-credentials"

RD=$'\e[31m'; GN=$'\e[32m'; YW=$'\e[33m'; BL=$'\e[34m'; CL=$'\e[0m'
msg_info()  { echo -e " ${BL}•${CL} $*"; }
msg_ok()    { echo -e " ${GN}✓${CL} $*"; }
msg_warn()  { echo -e " ${YW}!${CL} $*"; }
msg_error() { echo -e " ${RD}✗${CL} $*" >&2; }
die()       { msg_error "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root on the Proxmox host."
[[ -n "$NAS_URL" ]] || die "NAS_URL is required, e.g. NAS_URL=nfs://192.168.1.10/volume1/media $0 <CTID>"
[[ -n "$CTID" ]] || die "Usage: NAS_URL=... $0 <CTID>"
command -v pct >/dev/null 2>&1 || die "'pct' not found — run this on a Proxmox VE node."
pct config "$CTID" >/dev/null 2>&1 || die "Container $CTID does not exist."

# ----------------------------------------------------------------------------- #
# Parse NAS_URL
# ----------------------------------------------------------------------------- #
proto="${NAS_URL%%://*}"
rest="${NAS_URL#*://}"
host="${rest%%/*}"
path="/${rest#*/}"
[[ "$host" != "$rest" ]] || die "NAS_URL must include a share/export path, e.g. nfs://host/volume1/media"

case "$proto" in
    nfs)  FSTYPE=nfs  ; SRC="${host}:${path}" ;;
    smb|cifs) FSTYPE=cifs ; SRC="//${host}${path}" ;;
    *)    die "Unsupported protocol '${proto}' — use nfs:// or smb://" ;;
esac
msg_ok "Parsed: ${proto} host=${host} path=${path}"

# ----------------------------------------------------------------------------- #
# Client packages on the host
# ----------------------------------------------------------------------------- #
if [[ "$FSTYPE" == nfs ]]; then
    command -v mount.nfs >/dev/null 2>&1 || {
        msg_info "Installing nfs-common…"
        apt-get update -qq && apt-get install -y -qq nfs-common >/dev/null
    }
else
    command -v mount.cifs >/dev/null 2>&1 || {
        msg_info "Installing cifs-utils…"
        apt-get update -qq && apt-get install -y -qq cifs-utils >/dev/null
    }
    [[ -n "$NAS_USERNAME" ]] || die "NAS_USERNAME is required for smb://"
    umask 077
    { echo "username=${NAS_USERNAME}"
      echo "password=${NAS_PASSWORD}"
      [[ -n "$NAS_DOMAIN" ]] && echo "domain=${NAS_DOMAIN}"
    } > "$CREDS_FILE"
    chmod 600 "$CREDS_FILE"
    msg_ok "SMB credentials written to ${CREDS_FILE} (0600)"
fi

# ----------------------------------------------------------------------------- #
# Mount options
#   _netdev + nofail : never block or fail the host's boot if the NAS is down
#   cifs uid/gid     : present every file as owned by the container's adr user
#   nfs              : the SERVER decides ownership — see the guidance printed below
# ----------------------------------------------------------------------------- #
# 'hard' is deliberate and important: with 'soft', an NFS timeout aborts the
# in-flight write and HandBrake can silently produce a truncated MP4. 'hard'
# blocks until the NAS answers instead of corrupting a multi-GB file.
# '_netdev' is omitted: systemd's fstab generator already treats nfs/cifs as
# network filesystems and orders them after remote-fs.target.
if [[ "$FSTYPE" == nfs ]]; then
    OPTS="nofail,noatime,hard,timeo=150,retrans=3,x-systemd.mount-timeout=30"
else
    OPTS="nofail,noatime,credentials=${CREDS_FILE},uid=${ADR_UID},gid=${ADR_GID},file_mode=0664,dir_mode=0775,x-systemd.mount-timeout=30"
fi
[[ -n "$NAS_EXTRA_OPTS" ]] && OPTS="${OPTS},${NAS_EXTRA_OPTS}"

# ----------------------------------------------------------------------------- #
# Mountpoint, with a guard against writing into it while unmounted
# ----------------------------------------------------------------------------- #
mkdir -p "$NAS_MOUNTPOINT"
# If the NAS is offline the mount fails and the directory stays empty — without
# a guard the container would then quietly fill the HOST disk with rips instead
# of writing to the NAS. Making the bare mountpoint immutable turns that silent
# data-misplacement into a loud permission error. Best-effort: not every
# filesystem supports the immutable attribute.
if ! mountpoint -q "$NAS_MOUNTPOINT"; then
    if chattr +i "$NAS_MOUNTPOINT" 2>/dev/null; then
        msg_ok "Mountpoint guarded (immutable while unmounted)"
    else
        msg_warn "Could not set the immutable guard on ${NAS_MOUNTPOINT} (unsupported filesystem)."
        msg_warn "If the NAS is offline, rips would be written to the host disk instead."
    fi
fi

# ----------------------------------------------------------------------------- #
# Persist in /etc/fstab (idempotent) and mount
# ----------------------------------------------------------------------------- #
FSTAB_LINE="${SRC}  ${NAS_MOUNTPOINT}  ${FSTYPE}  ${OPTS}  0  0"
if grep -qsF " ${NAS_MOUNTPOINT} " /etc/fstab; then
    msg_info "Replacing existing fstab entry for ${NAS_MOUNTPOINT}"
    sed -i "\# ${NAS_MOUNTPOINT} #d" /etc/fstab
fi
cp /etc/fstab "/etc/fstab.adr-backup.$$"
echo "$FSTAB_LINE" >> /etc/fstab
msg_ok "fstab updated (backup: /etc/fstab.adr-backup.$$)"

systemctl daemon-reload 2>/dev/null || true
if mountpoint -q "$NAS_MOUNTPOINT"; then
    umount "$NAS_MOUNTPOINT" 2>/dev/null || true
fi

msg_info "Mounting ${SRC} → ${NAS_MOUNTPOINT}…"
if ! mount "$NAS_MOUNTPOINT"; then
    msg_error "Mount failed. The fstab entry was kept so you can retry with: mount ${NAS_MOUNTPOINT}"
    if [[ "$FSTYPE" == nfs ]]; then
        msg_error "Check the export is visible:  showmount -e ${host}"
    else
        msg_error "Check credentials and share name:  smbclient -L //${host} -U ${NAS_USERNAME}"
    fi
    exit 1
fi
mountpoint -q "$NAS_MOUNTPOINT" || die "mount reported success but ${NAS_MOUNTPOINT} is not a mount point."
msg_ok "Mounted: $(findmnt -n -o SOURCE,FSTYPE,SIZE "$NAS_MOUNTPOINT" 2>/dev/null || echo "$SRC")"

# ----------------------------------------------------------------------------- #
# Prove the container's user can actually write there
# ----------------------------------------------------------------------------- #
msg_info "Verifying uid ${ADR_UID} can write to the share…"
PROBE="${NAS_MOUNTPOINT}/.adr-write-test.$$"
if setpriv --reuid "$ADR_UID" --regid "$ADR_GID" --clear-groups \
        touch "$PROBE" 2>/dev/null; then
    rm -f "$PROBE"
    msg_ok "uid ${ADR_UID} can write to the NAS"
    WRITE_OK=1
else
    WRITE_OK=0
    msg_error "uid ${ADR_UID} CANNOT write to ${NAS_MOUNTPOINT} — ripping will fail at the final step."
    echo
    if [[ "$FSTYPE" == nfs ]]; then
        cat <<EOF
   NFS authorises writes by NUMERIC uid, so the share must accept uid ${ADR_UID}.
   Do ONE of these on the NAS:

     a) Give uid ${ADR_UID} write access to the exported directory:
          chown -R ${ADR_UID}:${ADR_GID} <exported path>

     b) Or squash all clients to a user that owns the directory
        (Linux /etc/exports syntax):
          ${path}  <proxmox-ip>(rw,sync,all_squash,anonuid=${ADR_UID},anongid=${ADR_GID},no_subtree_check)
        then:  exportfs -ra

     Synology: Control Panel → Shared Folder → Edit → NFS Permissions →
       Squash: "Map all users to admin", or set the folder's owner to uid ${ADR_UID}.
     TrueNAS: Sharing → NFS → Edit → Mapall User/Group.
EOF
    else
        cat <<EOF
   The share is mounted with uid=${ADR_UID}, so this is a SERVER-side permission
   problem: the SMB user '${NAS_USERNAME}' lacks write access to ${path}.
   Grant that user read/write on the share in the NAS admin UI and re-run.
EOF
    fi
    echo
fi

# ----------------------------------------------------------------------------- #
# Make Proxmox start guests only after the share is mounted.
#
# This matters more than it looks. A bind-mount captures whatever the source
# resolves to at CT-start time: if the container starts before the NAS is
# mounted it binds the bare (empty, local) directory, and mounting the NAS on
# the host afterwards does NOT become visible inside the running container.
# It would keep writing rips to the host disk until restarted.
# ----------------------------------------------------------------------------- #
DROPIN_DIR=/etc/systemd/system/pve-guests.service.d
mkdir -p "$DROPIN_DIR"
printf '[Unit]\n# Added by adr-setup-nas: do not autostart guests before the NAS is mounted.\nRequiresMountsFor=%s\n' \
    "$NAS_MOUNTPOINT" > "${DROPIN_DIR}/adr-nas.conf"
systemctl daemon-reload 2>/dev/null || true
msg_ok "Guest autostart now waits for ${NAS_MOUNTPOINT}"

# ----------------------------------------------------------------------------- #
# Bind-mount into the container
# ----------------------------------------------------------------------------- #
msg_info "Attaching ${NAS_MOUNTPOINT} to container ${CTID} as /opt/adr/completed…"
if pct config "$CTID" | grep -q "^mp0:"; then
    msg_warn "Container ${CTID} already has mp0 — replacing it:"
    pct config "$CTID" | grep '^mp0:' | sed 's/^/      /'
fi
pct set "$CTID" -mp0 "${NAS_MOUNTPOINT},mp=/opt/adr/completed" >/dev/null
msg_ok "Bind-mount configured"

WAS_RUNNING=0
if pct status "$CTID" | grep -q running; then
    WAS_RUNNING=1
    msg_info "Restarting container ${CTID} to apply the mount…"
    pct reboot "$CTID" >/dev/null 2>&1 || { pct stop "$CTID" >/dev/null; pct start "$CTID" >/dev/null; }
    for _ in $(seq 1 30); do
        pct exec "$CTID" -- true >/dev/null 2>&1 && break
        sleep 1
    done
fi

# ----------------------------------------------------------------------------- #
# Confirm from inside the container
# ----------------------------------------------------------------------------- #
if [[ "$WAS_RUNNING" -eq 1 ]]; then
    msg_info "Checking the mount from inside the container…"
    if pct exec "$CTID" -- mountpoint -q /opt/adr/completed 2>/dev/null; then
        msg_ok "/opt/adr/completed is the NAS share inside CT ${CTID}"
        # From now on a rip refuses to start unless the share is really
        # mounted, instead of quietly filling the container disk.
        # shellcheck disable=SC2016
        # Single quotes are intended: $f is a variable of the shell running
        # INSIDE the container, not of this host script.
        if pct exec "$CTID" -- sh -c '
            f=/opt/adr/config/adr.yaml
            [ -f "$f" ] || exit 1
            if grep -q "^require_completed_mount:" "$f"; then
                sed -i "s/^require_completed_mount:.*/require_completed_mount: true/" "$f"
            else
                echo "require_completed_mount: true" >> "$f"
            fi
            chown adr:adr "$f" 2>/dev/null || true
        ' 2>/dev/null; then
            pct exec "$CTID" -- systemctl restart adr >/dev/null 2>&1 || true
            msg_ok "Rips will now refuse to start if the NAS is not mounted"
        else
            msg_warn "Could not enable the mount check — set 'require_completed_mount: true'"
            msg_warn "in /opt/adr/config/adr.yaml to guard against an unmounted NAS."
        fi
        if pct exec "$CTID" -- sudo -u adr test -w /opt/adr/completed 2>/dev/null; then
            msg_ok "The 'adr' service user can write to it"
        else
            msg_warn "The 'adr' user cannot write to it yet — see the NFS/SMB guidance above."
        fi
    else
        msg_warn "/opt/adr/completed is not a mount point inside the container."
        msg_warn "Check:  pct config ${CTID} | grep mp0"
    fi
fi

echo
echo "  ┌────────────────────────────────────────────────────────"
echo "  │  NAS      : ${SRC}"
echo "  │  Host mount: ${NAS_MOUNTPOINT}"
echo "  │  In CT     : /opt/adr/completed   (finished MP4s)"
echo "  │  Scratch   : /opt/adr/raw         (stays local — fast)"
echo "  │  Writable  : $([[ "${WRITE_OK}" -eq 1 ]] && echo yes || echo 'NO — fix permissions above')"
echo "  └────────────────────────────────────────────────────────"
echo
echo "  In the web UI under Settings, 'Completed MP4 folder' should stay"
echo "  /opt/adr/completed — it now points at the NAS. Leave 'Plex movie"
echo "  library path' empty: output is already Plex-shaped as"
echo "  'Title (Year)/Title (Year).mp4'."
echo
echo "  ${YW}If the NAS is ever offline and remounted, restart the container:${CL}"
echo "      pct reboot ${CTID}"
echo "  A bind-mount is captured at container start, so a share mounted"
echo "  afterwards stays invisible inside a running container."
echo
