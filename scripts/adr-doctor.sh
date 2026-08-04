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

# The version of the application this copy of the script was taken from.
#
# adr-doctor lives on the Proxmox host, but it is *copied* out of the
# container at install time — so updating the app does not update this. A
# stale copy silently skips whatever checks were added since, and then reports
# "nothing wrong found", which is worse than failing: it is a clean bill of
# health from a script that never looked. Compared against the container's own
# version below.
ADR_DOCTOR_VERSION="1.15.1"

CT_MEDIA_PATH="${CT_MEDIA_PATH:-/mnt/media}"
# The user the service runs as inside the container.
RUN_USER="${RUN_USER:-adr}"

# Kept so a refreshed copy can be re-executed with exactly what was asked for.
ORIGINAL_ARGS=("$@")

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
echo "  Automatic Disc Ripper — checking container ${CTID}  (doctor ${ADR_DOCTOR_VERSION})"
echo

# ----------------------------------------------------------------------------- #
# 0. Is this copy of the script current?
#
# It is copied out of the container at install time and never updated by the
# in-container updater, which cannot write to the host. An old copy quietly
# skips every check added since it was taken and then prints "nothing wrong
# found" — a clean bill of health from a script that never looked for the
# problem. Better to say so before running anything.
# ----------------------------------------------------------------------------- #
# A copy that has just re-executed itself puts itself in place, from /tmp
# where nothing is rewriting it, and then gets on with the job.
if [[ "${ADR_DOCTOR_REFRESHED:-}" == "1" ]]; then
    if [[ "$0" == /tmp/adr-doctor-* ]]; then
        if install -m 0755 "$0" /usr/local/sbin/adr-doctor 2>/dev/null; then
            msg_ok "Updated /usr/local/sbin/adr-doctor to ${ADR_DOCTOR_VERSION}"
        else
            msg_warn "Running ${ADR_DOCTOR_VERSION}, but could not replace"
            msg_warn "        /usr/local/sbin/adr-doctor — it will still be old next time."
        fi
        rm -f "$0"
    fi
    CT_VERSION=""      # already reconciled; do not ask again
else
CT_VERSION="$(pct exec "$CTID" -- /opt/adr/.venv/bin/python -c \
    'import sys; sys.path.insert(0, "/opt/adr"); import adr; print(adr.__version__)' \
    2>/dev/null | tr -d "[:space:]")" || CT_VERSION=""
fi

if [[ -n "$CT_VERSION" && "$CT_VERSION" != "$ADR_DOCTOR_VERSION" ]]; then
    msg_warn "This adr-doctor is ${ADR_DOCTOR_VERSION}; container ${CTID} runs ${CT_VERSION}."
    msg_warn "        Checks added after ${ADR_DOCTOR_VERSION} will be skipped, and a clean"
    msg_warn "        result below would not mean much. Refresh it first:"
    echo
    echo "    pct pull ${CTID} /opt/adr/scripts/adr-doctor.sh /usr/local/sbin/adr-doctor && chmod +x /usr/local/sbin/adr-doctor"
    echo
    refresh="n"
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        refresh="y"
    else
        read -r -p "  Refresh this copy from the container and re-run? [Y/n] " reply
        [[ "$reply" =~ ^[Nn]$ ]] || refresh="y"
    fi

    if [[ "$refresh" == "y" ]]; then
        # Re-exec from a copy in /tmp rather than overwriting this file and
        # carrying on. Bash reads a script incrementally by byte offset; a
        # script that replaces itself mid-run continues reading at its old
        # offset inside different bytes, which is a syntax error on an
        # arbitrary line. The new copy installs itself, below.
        _new="$(mktemp /tmp/adr-doctor-XXXXXX.sh)"
        if pct pull "$CTID" /opt/adr/scripts/adr-doctor.sh "$_new" >/dev/null 2>&1 \
                && [[ -s "$_new" ]]; then
            chmod 0755 "$_new"
            msg_ok "Fetched ${CT_VERSION} from the container; re-running."
            echo
            export ADR_DOCTOR_REFRESHED=1
            exec bash "$_new" "${ORIGINAL_ARGS[@]}"
        fi
        rm -f "$_new"
        msg_warn "Could not fetch a newer copy. Carrying on with ${ADR_DOCTOR_VERSION}."
        echo
    fi
fi

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

    # ------------------------------------------------------------------- #
    # The half that passthrough alone does not solve.
    #
    # /dev/dri/renderD128 is crw-rw---- root:render. Passing the node in makes
    # it visible; opening it still needs the service user to be in the owning
    # group. In a privileged container gids map straight through, and the
    # host's 'render' gid is almost never the container's — Proxmox is Debian,
    # the container is Ubuntu, and they number their system groups
    # differently. So the group is matched by *number*, not by name, which is
    # the only thing the kernel actually checks.
    # ------------------------------------------------------------------- #
    if [[ "$FIX" -eq 1 ]] && pct status "$CTID" 2>/dev/null | grep -q running; then
        for node in /dev/dri/renderD128 /dev/dri/card0; do
            [[ -e "$node" ]] || continue
            node_gid="$(stat -c %g "$node" 2>/dev/null)" || continue
            [[ -n "$node_gid" ]] || continue
            # shellcheck disable=SC2016  # $1/$2 belong to the inner shell
            if pct exec "$CTID" -- sh -c '
                set -e
                gid="$1"; user="$2"
                group="$(getent group "$gid" | cut -d: -f1)"
                if [ -z "$group" ]; then
                    group="adr-dri-$gid"
                    groupadd -g "$gid" "$group" >/dev/null 2>&1 || true
                    group="$(getent group "$gid" | cut -d: -f1)"
                fi
                [ -n "$group" ] || exit 1
                if id -nG "$user" | tr " " "\n" | grep -qx "$group"; then exit 3; fi
                usermod -aG "$group" "$user"
            ' _ "$node_gid" "$RUN_USER" >/dev/null 2>&1; then
                note_fixed "added ${RUN_USER} to the group owning ${node} (gid ${node_gid})"
                NEEDS_RESTART=1
            else
                rc=$?   # the condition's status: 3 means "already a member"
                if [[ $rc -eq 3 ]]; then
                    msg_ok "${RUN_USER} can already use ${node} (gid ${node_gid})"
                else
                    msg_warn "Could not give ${RUN_USER} access to ${node} (gid ${node_gid})."
                    msg_warn "        Hardware encoding will fail with 'permission denied'."
                fi
            fi
        done
    fi

    # ------------------------------------------------------------------- #
    # The other half that passthrough alone does not solve: the driver.
    #
    # This is the failure that looks fixed. The node is passed through, the
    # group is right, every check above is green — and HandBrake still says
    # "encqsvInit: qsv is not available on the system", because Quick Sync
    # does not talk to the kernel directly. It goes through a VA-API driver
    # and a Media SDK / oneVPL runtime, and a minimal container image ships
    # neither. Nothing inside the container can install them: the service
    # runs unprivileged, and apt needs root.
    #
    # Which packages depends on whose GPU it is, so the PCI vendor decides.
    # Installing Intel's media stack on an AMD box would be pure noise.
    # ------------------------------------------------------------------- #
    # Asked twice — before and after the install — so it lives in one place.
    #
    # The container answers with the application's own check rather than a
    # shell probe of its own. A second, looser implementation here is exactly
    # how this went wrong once already: the script accepted any VA driver at
    # all, reported the stack installed, and the web UI then told the user the
    # encoder was missing from their HandBrake build. One answer, one place.
    CT_RUNTIME_DETAIL=""
    ct_has_va_driver() {
        local out
        out="$(pct exec "$CTID" -- /opt/adr/.venv/bin/python -c '
import sys
sys.path.insert(0, "/opt/adr")
from adr import gpu
state = gpu.runtime_state()
print("OK" if state["ok"] else "MISSING")
print(state["detail"])
' 2>/dev/null)" || out=""

        if [[ -z "$out" ]]; then
            # An older container has no runtime_state(). Fall back to the
            # crude probe rather than refusing to answer — and say so, so a
            # pass here is not mistaken for the real check.
            CT_RUNTIME_DETAIL="(checked without the container's own test — it is too old to ask)"
            # shellcheck disable=SC2016  # $d belongs to the container's shell
            pct exec "$CTID" -- sh -c '
                for d in /usr/lib/x86_64-linux-gnu/dri /usr/lib/dri /usr/lib64/dri; do
                    ls "$d"/*_drv_video.so >/dev/null 2>&1 && exit 0
                done
                exit 1
            ' >/dev/null 2>&1
            return
        fi

        CT_RUNTIME_DETAIL="$(printf '%s\n' "$out" | tail -n +2)"
        [[ "$(printf '%s\n' "$out" | head -1)" == "OK" ]]
    }

    if pct status "$CTID" 2>/dev/null | grep -q running; then
        if ct_has_va_driver; then
            msg_ok "The GPU driver stack is installed in the container"
            [[ -n "$CT_RUNTIME_DETAIL" ]] && msg_info "  ${CT_RUNTIME_DETAIL}"
        else
            # 0x8086 Intel, 0x1002 AMD. Read from the first render node,
            # which is the one HandBrake would use.
            gpu_vendor=""
            for node in /dev/dri/renderD*; do
                [[ -e "$node" ]] || continue
                gpu_vendor="$(cat "/sys/class/drm/$(basename "$node")/device/vendor" 2>/dev/null || true)"
                break
            done

            # Runtimes first, because they are what is usually missing: a
            # container often already has libvpl (the dispatcher, pulled in as
            # a dependency of something else) and nothing for it to dispatch
            # to. libmfxgen1 is the oneVPL runtime for Gen11 and later,
            # libmfx1 the older Media SDK — which one applies depends on the
            # chip, so both are attempted and either is enough.
            case "$gpu_vendor" in
                0x8086) VA_PACKAGES="libmfxgen1 libmfx1 intel-media-va-driver-non-free intel-media-va-driver i965-va-driver libvpl2 vainfo" ;;
                0x1002) VA_PACKAGES="mesa-va-drivers vainfo" ;;
                *)      VA_PACKAGES="" ;;
            esac

            if [[ -z "$VA_PACKAGES" ]]; then
                msg_warn "A GPU is passed through, but the driver stack it needs is not"
                msg_warn "        installed and its vendor (${gpu_vendor:-unknown}) is not one this"
                msg_warn "        script knows how to install for. Hardware presets will fail;"
                msg_warn "        a software preset (x264/x265) works regardless."
                [[ -n "$CT_RUNTIME_DETAIL" ]] && msg_warn "        ${CT_RUNTIME_DETAIL}"
            else
                note_problem "The GPU is passed through but the container cannot use it:"
                if [[ -n "$CT_RUNTIME_DETAIL" ]]; then
                    msg_warn "        ${CT_RUNTIME_DETAIL}"
                else
                    msg_warn "        the driver stack it needs is not installed, so HandBrake"
                    msg_warn "        reports the hardware as unavailable even though the render"
                    msg_warn "        node is there."
                fi
                if [[ "$FIX" -eq 1 ]]; then
                    msg_info "Installing the GPU driver stack in CT ${CTID} — this downloads a few MB…"
                    # Each package separately and best-effort on purpose. The
                    # names differ across Debian and Ubuntu releases and some
                    # live in non-free, which not every container has enabled.
                    # One unavailable name must not take the others with it,
                    # and any single VA driver is enough to succeed.
                    # shellcheck disable=SC2016  # $1 belongs to the inner shell
                    pct exec "$CTID" -- sh -c '
                        export DEBIAN_FRONTEND=noninteractive
                        apt-get update -qq >/dev/null 2>&1 || true
                        for pkg in $1; do
                            apt-get install -y -qq "$pkg" >/dev/null 2>&1 \
                                && echo "  installed: $pkg"
                        done
                    ' _ "$VA_PACKAGES" || true

                    if ct_has_va_driver; then
                        note_fixed "installed the GPU driver stack in the container"
                    else
                        msg_error "        The driver stack is still incomplete after installing."
                        [[ -n "$CT_RUNTIME_DETAIL" ]] && msg_error "        ${CT_RUNTIME_DETAIL}"
                        msg_error "        On Debian the best Intel driver lives in non-free —"
                        msg_error "        enable it in the container's /etc/apt/sources.list, or"
                        msg_error "        use a software preset."
                    fi
                else
                    would_fix "install ${VA_PACKAGES%% *} and friends in the container"
                fi
            fi
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
