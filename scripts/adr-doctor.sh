#!/usr/bin/env bash
#
# adr-doctor — check, and optionally repair, an Automatic Disc Ripper container.
# Run this on the PROXMOX HOST.
#
#   adr-doctor <CTID>                report only, change nothing
#   adr-doctor --fix <CTID>          apply the repairs it found
#   adr-doctor --fix --yes <CTID>    ...and restart the container without asking
#
# It exists because the two things that go wrong with this setup are both
# invisible from inside the container:
#
#   1. Optical passthrough that worked right after installing and stopped
#      working after a host reboot. 'lxc.mount.entry ... optional' skips a
#      device that does not exist yet, SILENTLY, and a device node cannot be
#      bind-mounted into a container that is already running. If pve-guests
#      wins the race against udev at boot, the container comes up with no
#      drive and stays that way until it is restarted.
#
#   2. A media share bind-mounted over /opt/adr/completed — the pre-1.0
#      layout. It works, but half the application's own directory is then
#      somebody else's filesystem, which is confusing to reason about and
#      dangerous to run a recursive chown across. 1.0 mounts it at /mnt/media.
#
set -euo pipefail

CT_MEDIA_PATH="${CT_MEDIA_PATH:-/mnt/media}"

FIX=0
ASSUME_YES=0
CTID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fix)  FIX=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 2 ;;
        *)  CTID="$1" ;;
    esac
    shift
done

RD=$'\e[31m'; GN=$'\e[32m'; YW=$'\e[33m'; BL=$'\e[34m'; CL=$'\e[0m'
msg_info()  { echo -e " ${BL}•${CL} $*"; }
msg_ok()    { echo -e " ${GN}✓${CL} $*"; }
msg_warn()  { echo -e " ${YW}!${CL} $*"; }
msg_error() { echo -e " ${RD}✗${CL} $*" >&2; }
die()       { msg_error "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root on the Proxmox host."
[[ -n "$CTID" ]] || die "Usage: $0 [--fix] [--yes] <CTID>"
command -v pct >/dev/null 2>&1 || die "'pct' not found — run this on a Proxmox VE node."
pct config "$CTID" >/dev/null 2>&1 || die "Container $CTID does not exist."

CONF="/etc/pve/lxc/${CTID}.conf"
[[ -f "$CONF" ]] || die "Missing $CONF."

PROBLEMS=0        # things that are wrong
REPAIRED=0        # things this run changed
NEEDS_RESTART=0   # changes that only take effect at container start

note_problem() { PROBLEMS=$((PROBLEMS + 1)); msg_error "$*"; }
note_fixed()   { REPAIRED=$((REPAIRED + 1)); msg_ok "fixed: $*"; }
would_fix()    { msg_warn "        run with --fix to: $*"; }

echo
echo "  Automatic Disc Ripper — checking container ${CTID}"
echo

# ----------------------------------------------------------------------------- #
# 1. Which optical drives does the HOST have?
# ----------------------------------------------------------------------------- #
HOST_SR=()
for d in /dev/sr[0-9]*; do [[ -b "$d" ]] && HOST_SR+=("$d"); done

if [[ ${#HOST_SR[@]} -eq 0 ]]; then
    note_problem "No optical drive on the Proxmox host (/dev/sr* is empty)."
    msg_warn "        Nothing this script does can conjure one. Check the cable,"
    msg_warn "        the power, and 'lsblk -d -o NAME,TYPE'."
else
    msg_ok "Host optical drive(s): ${HOST_SR[*]}"
fi

# ----------------------------------------------------------------------------- #
# 2. Device cgroup: /dev/sr* is a BLOCK device, major 11
#
# LXC's default policy denies everything and then re-allows 'b *:* m', which
# is mknod only — creating the node is permitted, opening it is not. Without an
# explicit 'b 11:* rwm' the node appears in the container and every open()
# returns EPERM, which reads exactly like a broken drive.
# ----------------------------------------------------------------------------- #
declare -a WANT_CGROUP=(
    "lxc.cgroup2.devices.allow: b 11:* rwm"
    "lxc.cgroup2.devices.allow: c 11:* rwm"
    "lxc.cgroup2.devices.allow: c 21:* rwm"
)
MISSING_CGROUP=()
for rule in "${WANT_CGROUP[@]}"; do
    grep -qxF "$rule" "$CONF" || MISSING_CGROUP+=("$rule")
done

if [[ ${#MISSING_CGROUP[@]} -eq 0 ]]; then
    msg_ok "Device cgroup rules present (block major 11 allowed)"
else
    note_problem "Device cgroup rules missing from ${CONF}:"
    printf '          %s\n' "${MISSING_CGROUP[@]}"
    if [[ "$FIX" -eq 1 ]]; then
        printf '%s\n' "${MISSING_CGROUP[@]}" >> "$CONF"
        note_fixed "added ${#MISSING_CGROUP[@]} cgroup rule(s)"
        NEEDS_RESTART=1
    else
        would_fix "append them"
    fi
fi

# ----------------------------------------------------------------------------- #
# 3. Bind entries for each drive, plus its generic-SCSI node
#
# MakeMKV talks to the drive through SG_IO, so /dev/sgN has to come along. It is
# resolved per drive from sysfs — binding every /dev/sg* would also hand the
# container raw SG_IO on the host's own disks.
# ----------------------------------------------------------------------------- #
MISSING_BINDS=()
want_bind() {
    local dev="$1" line
    line="lxc.mount.entry: ${dev} dev/${dev#/dev/} none bind,optional,create=file"
    grep -qxF "$line" "$CONF" || MISSING_BINDS+=("$line")
}
for dev in "${HOST_SR[@]:-}"; do
    [[ -n "$dev" ]] || continue
    want_bind "$dev"
    sg_dir="/sys/block/${dev#/dev/}/device/scsi_generic"
    [[ -d "$sg_dir" ]] || continue
    for sg_node in "$sg_dir"/sg[0-9]*; do
        [[ -e "$sg_node" ]] && want_bind "/dev/$(basename "$sg_node")"
    done
done

if [[ ${#HOST_SR[@]} -gt 0 ]]; then
    if [[ ${#MISSING_BINDS[@]} -eq 0 ]]; then
        msg_ok "Every host drive (and its /dev/sg node) is passed through"
    else
        note_problem "Passthrough entries missing from ${CONF}:"
        printf '          %s\n' "${MISSING_BINDS[@]}"
        if [[ "$FIX" -eq 1 ]]; then
            printf '%s\n' "${MISSING_BINDS[@]}" >> "$CONF"
            note_fixed "added ${#MISSING_BINDS[@]} passthrough entr(y/ies)"
            NEEDS_RESTART=1
        else
            would_fix "append them"
        fi
    fi
fi

# ----------------------------------------------------------------------------- #
# 3b. The GPU, for hardware encoding
#
# A HandBrake preset exported from a desktop usually asks for that desktop's
# encoder — Quick Sync, NVENC, VAAPI. Inside an LXC none of them exist unless
# the GPU was passed through, and HandBrake fails identically on every title of
# every disc: "encqsvInit: qsv is not available on the system", exit 3, forty
# minutes after the disc went in.
#
# Hardware encoding goes through a DRM render node, character major 226. Only
# offered when the host actually has one: adding a bind for a device that is
# not there would be noise, and 'optional' would hide it anyway.
# ----------------------------------------------------------------------------- #
if [[ -d /dev/dri ]] && compgen -G "/dev/dri/renderD*" >/dev/null; then
    declare -a WANT_GPU=(
        "lxc.cgroup2.devices.allow: c ${DRM_MAJOR:-226}:* rwm"
        "lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir"
    )
    MISSING_GPU=()
    for rule in "${WANT_GPU[@]}"; do
        grep -qxF "$rule" "$CONF" || MISSING_GPU+=("$rule")
    done

    if [[ ${#MISSING_GPU[@]} -eq 0 ]]; then
        msg_ok "GPU is passed through (hardware encoding available)"
    else
        note_problem "The host has a GPU this container cannot use:"
        for node in /dev/dri/renderD*; do
            [[ -e "$node" ]] && printf '          %s\n' "$node"
        done
        msg_warn "        A HandBrake preset that asks for Quick Sync, NVENC or VAAPI"
        msg_warn "        fails on every title until this is passed through."
        if [[ "$FIX" -eq 1 ]]; then
            printf '%s\n' "${MISSING_GPU[@]}" >> "$CONF"
            note_fixed "added GPU passthrough (${#MISSING_GPU[@]} line(s))"
            NEEDS_RESTART=1
        else
            would_fix "append them"
        fi
    fi
else
    msg_ok "No GPU on the host — software encoding is the only option, which is fine"
fi

# ----------------------------------------------------------------------------- #
# 4. Boot ordering — the "worked until I rebooted" bug
# ----------------------------------------------------------------------------- #
DROPIN=/etc/systemd/system/pve-guests.service.d/adr-optical.conf
PRIMARY="${HOST_SR[0]:-}"
if [[ -z "$PRIMARY" ]]; then
    :   # no drive; nothing to order against
elif DEV_UNIT="$(systemd-escape --path --suffix=device "$PRIMARY" 2>/dev/null)" \
        && [[ -n "$DEV_UNIT" ]]; then
    if [[ -f "$DROPIN" ]] && grep -qF "After=${DEV_UNIT}" "$DROPIN"; then
        msg_ok "Guest autostart already waits for ${PRIMARY} (${DEV_UNIT})"
    else
        note_problem "Guest autostart does not wait for ${PRIMARY}."
        msg_warn "        This is why the drive works after installing and is gone"
        msg_warn "        after a reboot: pve-guests can start the container before"
        msg_warn "        udev has created ${PRIMARY}, and 'optional' then skips the"
        msg_warn "        bind without a word."
        if [[ "$FIX" -eq 1 ]]; then
            mkdir -p "$(dirname "$DROPIN")"
            cat > "$DROPIN" <<EOF
[Unit]
# Added by adr-doctor: do not autostart guests before ${PRIMARY} exists, or the
# container silently starts without the drive.
After=${DEV_UNIT}
Wants=${DEV_UNIT}
EOF
            systemctl daemon-reload 2>/dev/null || true
            note_fixed "ordered pve-guests.service after ${DEV_UNIT}"
        else
            would_fix "order pve-guests.service after ${DEV_UNIT}"
        fi
    fi
fi

# ----------------------------------------------------------------------------- #
# 5. Folder layout — a share mounted over the app's own directory
# ----------------------------------------------------------------------------- #
MP_LINE="$(pct config "$CTID" | grep -E '^mp[0-9]+:.*mp=/opt/adr/completed' || true)"
if [[ -n "$MP_LINE" ]]; then
    MP_KEY="${MP_LINE%%:*}"
    MP_SRC="${MP_LINE#*: }"; MP_SRC="${MP_SRC%%,*}"
    note_problem "${MP_KEY} mounts ${MP_SRC} over /opt/adr/completed (pre-1.0 layout)."
    msg_warn "        /opt/adr belongs to the application. 1.0 mounts your library"
    msg_warn "        at ${CT_MEDIA_PATH} instead and points 'Completed MP4 folder' there,"
    msg_warn "        so the app's directory and your films stay separate things."
    if [[ "$FIX" -eq 1 ]]; then
        pct set "$CTID" "-${MP_KEY}" "${MP_SRC},mp=${CT_MEDIA_PATH}" >/dev/null
        note_fixed "${MP_KEY} now mounts ${MP_SRC} at ${CT_MEDIA_PATH}"
        NEEDS_RESTART=1
        # The config rewrite has to happen after the restart, when the mount is
        # actually there — otherwise require_completed_mount refuses every rip.
        MIGRATE_CONFIG=1
    else
        would_fix "move it to ${CT_MEDIA_PATH} and update completed_path"
    fi
else
    msg_ok "No share is mounted over /opt/adr (app directory is the app's own)"
fi

# ----------------------------------------------------------------------------- #
# 6. Restart, if anything we changed only takes effect at container start
# ----------------------------------------------------------------------------- #
RUNNING=0
pct status "$CTID" | grep -q running && RUNNING=1

if [[ "$NEEDS_RESTART" -eq 1 && "$RUNNING" -eq 1 ]]; then
    echo
    DO_RESTART="$ASSUME_YES"
    if [[ "$ASSUME_YES" -eq 0 ]]; then
        read -r -p " Restart container ${CTID} now to apply? [y/N] " ans
        [[ "$ans" =~ ^[Yy] ]] && DO_RESTART=1
    fi
    if [[ "$DO_RESTART" -eq 1 ]]; then
        msg_info "Restarting container ${CTID}…"
        pct reboot "$CTID" >/dev/null 2>&1 || { pct stop "$CTID" >/dev/null; pct start "$CTID" >/dev/null; }
        for _ in $(seq 1 30); do
            pct exec "$CTID" -- true >/dev/null 2>&1 && break
            sleep 1
        done
        NEEDS_RESTART=0
    fi
fi

# Point the app at the relocated library, once the mount is really there.
if [[ "${MIGRATE_CONFIG:-0}" -eq 1 && "$NEEDS_RESTART" -eq 0 ]]; then
    if pct exec "$CTID" -- mountpoint -q "$CT_MEDIA_PATH" 2>/dev/null; then
        # $f/$m are the in-container shell's variables, hence the single quotes;
        # the path arrives as a positional argument.
        # shellcheck disable=SC2016
        if pct exec "$CTID" -- sh -c '
            f=/opt/adr/config/adr.yaml
            m="$1"
            [ -f "$f" ] || exit 1
            if grep -q "^completed_path:" "$f"; then
                sed -i "s|^completed_path:.*|completed_path: $m|" "$f"
            else
                echo "completed_path: $m" >> "$f"
            fi
            chown adr:adr "$f" 2>/dev/null || true
        ' sh "$CT_MEDIA_PATH" 2>/dev/null; then
            pct exec "$CTID" -- systemctl restart adr >/dev/null 2>&1 || true
            note_fixed "completed_path now points at ${CT_MEDIA_PATH}"
        else
            msg_warn "Set 'completed_path: ${CT_MEDIA_PATH}' in /opt/adr/config/adr.yaml by hand."
        fi
    else
        msg_warn "${CT_MEDIA_PATH} is not mounted inside CT ${CTID} yet — check 'pct config ${CTID}'."
    fi
fi

# ----------------------------------------------------------------------------- #
# 7. Ask the container itself whether it can open the drive
#
# This is the check that matters: it uses the same code the dashboard does, so
# a clean bill of health here means a clean dashboard.
# ----------------------------------------------------------------------------- #
echo
if [[ "$RUNNING" -eq 1 && "$NEEDS_RESTART" -eq 0 ]]; then
    msg_info "Asking CT ${CTID} whether it can open the drive…"
    # shellcheck disable=SC2016
    if pct exec "$CTID" -- /opt/adr/.venv/bin/python -c '
import sys
sys.path.insert(0, "/opt/adr")
from adr.disc import diagnose_passthrough
d = diagnose_passthrough()
for drive in d["drives"]:
    state = "ok" if drive["openable"] else ("denied" if drive["node_present"] else "missing")
    print("   %s  %-9s %s" % (drive["device"], state, drive["model"]))
for p in d["problems"]:
    print("   " + p.replace("\n", "\n   "))
sys.exit(0 if d["ok"] else 1)
'; then
        msg_ok "The container can open every optical drive the host has"
    else
        rc=$?
        if [[ $rc -eq 1 ]]; then
            note_problem "The container still cannot use the drive — see above."
        else
            msg_warn "Could not run the in-container check (is ADR installed in ${CTID}?)"
        fi
    fi
elif [[ "$NEEDS_RESTART" -eq 1 ]]; then
    msg_warn "Skipping the in-container check: a restart is still pending."
else
    msg_warn "Container ${CTID} is not running — start it and re-run for a live check."
fi

# ----------------------------------------------------------------------------- #
# Verdict
# ----------------------------------------------------------------------------- #
echo
if [[ "$PROBLEMS" -eq 0 ]]; then
    msg_ok "Nothing wrong found."
elif [[ "$FIX" -eq 1 ]]; then
    msg_ok "${REPAIRED} of ${PROBLEMS} finding(s) repaired."
    [[ "$NEEDS_RESTART" -eq 1 ]] && msg_warn "Restart the container to apply: pct reboot ${CTID}"
else
    msg_warn "${PROBLEMS} finding(s). Re-run with --fix to repair:  $0 --fix ${CTID}"
fi
echo
exit 0
