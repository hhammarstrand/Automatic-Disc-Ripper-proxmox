#!/usr/bin/env bash
#
# Update Automatic Disc Ripper in place (run INSIDE the container).
#
#   pct exec <CTID> -- /opt/adr/scripts/update.sh
#
# Re-fetches the application source from GitHub and reinstalls Python
# dependencies. Your configuration, database and media are preserved:
#
#   kept:  config/adr.yaml  adr.db  raw/  completed/  watch/  .MakeMKV/  .venv/
#   replaced: application code (adr/, web/, scripts/, systemd/, presets/, run.py, …)
#
# Env vars:
#   ADR_REPO_URL   git URL   (default: the public repo)
#   ADR_BRANCH     branch    (default: main)
#   GITHUB_TOKEN   token for a private repo (optional)
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Run from a private copy of this file, always.
#
# Bash reads a script incrementally, by byte offset. Further down, this script
# replaces the whole of /opt/adr with a fresh checkout — including this file.
# The moment that copy lands, bash carries on reading at its old offset inside a
# *different* file, and the result is a syntax error on an arbitrary line with
# the service already stopped:
#
#     /opt/adr/scripts/update.sh: line 81: syntax error near unexpected token `('
#
# Re-executing from a copy in /tmp means the bytes bash is reading can never
# change underneath it. This has to happen before anything else.
# --------------------------------------------------------------------------- #
if [[ "${ADR_UPDATE_REEXEC:-}" != "1" ]]; then
    _self_copy="$(mktemp /tmp/adr-update-XXXXXX.sh)"
    cat "$0" > "$_self_copy"
    chmod 0700 "$_self_copy"
    export ADR_UPDATE_REEXEC=1
    export ADR_UPDATE_SELF_COPY="$_self_copy"
    export ADR_UPDATE_ORIGINAL="$0"
    exec bash "$_self_copy" "$@"
fi
SELF_COPY="${ADR_UPDATE_SELF_COPY:-}"
SELF_NAME="${ADR_UPDATE_ORIGINAL:-$0}"

INSTALL_DIR="${INSTALL_DIR:-/opt/adr}"
RUN_USER="${RUN_USER:-adr}"
ADR_REPO_URL="${ADR_REPO_URL:-https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git}"
ADR_BRANCH="${ADR_BRANCH:-main}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"

GN=$'\e[32m'; YW=$'\e[33m'; RD=$'\e[31m'; BL=$'\e[34m'; CL=$'\e[0m'
msg_info()  { echo -e " ${BL}•${CL} $*"; }
msg_ok()    { echo -e " ${GN}✓${CL} $*"; }
msg_warn()  { echo -e " ${YW}!${CL} $*"; }
msg_error() { echo -e " ${RD}✗${CL} $*" >&2; }

[[ $EUID -eq 0 ]] || { msg_error "Run as root inside the container."; exit 1; }
[[ -d "$INSTALL_DIR" ]] || { msg_error "$INSTALL_DIR does not exist — is ADR installed?"; exit 1; }

# User state survives because the update is an overlay copy: the repository
# ships none of config/adr.yaml, adr.db*, raw/, completed/, watch/, .MakeMKV/
# or .venv/, so copying the new tree on top simply never touches them.

msg_info "Fetching latest source from ${ADR_REPO_URL} (${ADR_BRANCH})…"
TMP="$(mktemp -d)"

# The service is stopped partway through this script, and everything between
# then and the restart runs under `set -e`. A failing pip install, a full disk,
# an unwritable file — any of them aborts the script with the application
# STOPPED, which presents as "the web UI is gone" with no clue why.
#
# So the exit trap always tries to bring it back. A failed update that leaves
# the previous version running is a bad afternoon; one that leaves nothing
# running is a broken appliance.
SERVICE_STOPPED=0
cleanup() {
    local status=$?
    rm -rf "$TMP"
    [[ -n "$SELF_COPY" ]] && rm -f "$SELF_COPY"
    if [[ "$SERVICE_STOPPED" -eq 1 && "$status" -ne 0 ]]; then
        echo
        msg_error "The update failed partway through (exit ${status})."
        msg_info "Restarting the service so the previous version keeps running…"
        if systemctl start adr 2>/dev/null; then
            msg_ok "adr is running again. Nothing was lost — retry the update once"
            msg_ok "the cause above is fixed."
        else
            msg_error "Could not restart adr. Look at:  journalctl -u adr -e"
        fi
    fi
    exit "$status"
}
trap cleanup EXIT

clone_url="$ADR_REPO_URL"
[[ -n "$GITHUB_TOKEN" ]] && clone_url="https://x-access-token:${GITHUB_TOKEN}@${ADR_REPO_URL#https://}"

if ! git clone --depth 1 --branch "$ADR_BRANCH" "$clone_url" "$TMP/src" >/dev/null 2>&1; then
    msg_error "Could not clone ${ADR_REPO_URL}."
    msg_error "For a private repo re-run with: GITHUB_TOKEN=ghp_xxx $SELF_NAME"
    exit 1
fi
# Record the commit before .git goes away — it is the only thing that can
# answer "am I up to date?" from a working tree with no checkout.
NEW_COMMIT="$(git -C "$TMP/src" rev-parse HEAD 2>/dev/null || true)"
rm -rf "$TMP/src/.git"
msg_ok "Source fetched (${NEW_COMMIT:0:8})"

OLD_COMMIT="$(cat "$INSTALL_DIR/.commit" 2>/dev/null || true)"
if [[ -n "$NEW_COMMIT" && "$NEW_COMMIT" == "$OLD_COMMIT" ]]; then
    msg_ok "Already at the latest commit — reinstalling anyway to be sure."
fi

# --------------------------------------------------------------------------- #
# Never stop the service on top of a running rip.
#
# 'systemctl stop adr' kills the whole control group, and MakeMKV is in it.
# MakeMKV writes each title as it goes, so the rip dies with 'exited with code
# -15' and leaves MKVs in raw/ that look perfectly ordinary in a directory
# listing and are truncated in the middle of a frame. What that costs is an
# hour of ripping, silently, and it is easy to do: updating in the middle of a
# film is exactly when someone is sitting there waiting for it.
# --------------------------------------------------------------------------- #
if [[ "${ADR_UPDATE_FORCE:-}" != "1" ]]; then
    # Ask the application, not an HTTP endpoint that never carried the answer.
    #
    # This used to grep /api/status for a "status" field. That endpoint returns
    # drives, queue size, worker count and watch-folder state — and has never
    # contained a job status at all, so the pattern could not match and the
    # guard never once fired. Every update since it was written was free to
    # stop the service on top of a running rip, which is exactly what it exists
    # to prevent and exactly what kept happening.
    #
    # updater._job_in_progress reads the database directly and is the same
    # check the Update button uses to withhold itself. One question, one
    # answer, no serialisation format in between.
    busy="$("$INSTALL_DIR/.venv/bin/python" -c '
import sys
sys.path.insert(0, "'"$INSTALL_DIR"'")
from adr.updater import _job_in_progress
sys.stdout.write(_job_in_progress())
' 2>/dev/null || echo "__check_failed__")"

    if [[ "$busy" == "__check_failed__" ]]; then
        # A broken venv is itself a reason to update, so this does not block.
        # It is said out loud because the alternative — a guard that fails
        # open in silence — is the bug being fixed here.
        msg_warn "Could not ask the application whether a job is running."
        msg_warn "Continuing; if a rip is in progress it will be interrupted."
        busy=""
    fi

    if [[ -n "$busy" ]]; then
        msg_error "A job is in progress (${busy}) — not updating."
        msg_warn "Stopping the service now would kill it. MakeMKV writes titles as"
        msg_warn "it goes, so the rip would die part-way and leave files that look"
        msg_warn "fine and are truncated; you would lose the hour and have to re-rip."
        echo
        msg_info "Wait for it to finish, then run this again. To update anyway:"
        echo
        echo "    ADR_UPDATE_FORCE=1 $SELF_NAME"
        echo
        exit 1
    fi
fi

msg_info "Stopping service…"
systemctl stop adr || true
SERVICE_STOPPED=1

msg_info "Replacing application code (preserving config, database and media)…"
# Copy the new tree over the old one. Because everything in PRESERVE lives in
# paths the repo does not ship, a plain overlay copy leaves them untouched.
cp -a "$TMP/src/." "$INSTALL_DIR/"

# Re-assert ownership — but never recurse into 'completed'. On installs made
# before 1.0 that directory is a bind-mount of the user's media library, and a
# recursive chown would rewrite the ownership of every film they own.
find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name completed \
    -exec chown -R "$RUN_USER:$RUN_USER" {} + 2>/dev/null || true
chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true

# The two directories systemd runs as root must not be writable by the service
# user.
#
# adr-update.service has ExecStart=/opt/adr/scripts/update.sh and runs as uid 0.
# The chown above recurses into scripts/, so without this the unprivileged web
# UI — unauthenticated by design, on the LAN — could overwrite that file and
# then ask for an update, and systemd would execute the new bytes as root.
# ProtectSystem=full does not cover /opt, so nothing else stops it. That is the
# whole privilege separation this application claims to have.
chown -R root:root "$INSTALL_DIR/scripts" "$INSTALL_DIR/systemd" 2>/dev/null || true
chmod 0755 "$INSTALL_DIR/scripts" 2>/dev/null || true
chmod 0755 "$INSTALL_DIR"/scripts/*.sh 2>/dev/null || true
chmod 0644 "$INSTALL_DIR"/systemd/* 2>/dev/null || true
if mountpoint -q "$INSTALL_DIR/completed" 2>/dev/null; then
    msg_warn "$INSTALL_DIR/completed is a bind-mount — ownership left to the host."
    msg_warn "1.0 moves that mount to /mnt/media. On the Proxmox host, run:"
    msg_warn "    adr-doctor --fix <CTID>"
else
    chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR/completed" 2>/dev/null || true
fi
msg_ok "Code updated"

msg_info "Updating Python dependencies…"
# --quiet hides the reason when this fails, and this is the step most likely to
# fail: no network, a proxy, a half-built venv. Capture it and show it.
if ! pip_log="$(sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" \
        install --upgrade pip wheel 2>&1)"; then
    msg_error "Could not update pip/wheel:"
    echo "$pip_log" | tail -15
    exit 1
fi
if ! pip_log="$(sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" \
        install -r "$INSTALL_DIR/requirements.txt" 2>&1)"; then
    msg_error "Could not install requirements:"
    echo "$pip_log" | tail -15
    exit 1
fi
msg_ok "Dependencies updated"

# System tools a newer version needs that an older install never had. Audio CD
# ripping arrived after the first release, so an existing container has neither
# tool. Missing them is not fatal — video discs are unaffected and the Doctor
# page names the command — so this never aborts the update.
missing_tools=()
for tool in cdparanoia ffmpeg; do
    command -v "$tool" >/dev/null 2>&1 || missing_tools+=("$tool")
done
if [[ ${#missing_tools[@]} -gt 0 ]]; then
    msg_info "Installing new system tools: ${missing_tools[*]}…"
    if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing_tools[@]}" >/dev/null 2>&1; then
        msg_ok "Installed: ${missing_tools[*]}"
    else
        msg_warn "Could not install ${missing_tools[*]} — audio CDs will not rip."
        msg_warn "    apt-get install -y ${missing_tools[*]}"
    fi
fi

# The GPU's userspace half, for a container installed before it was installed.
#
# This runs as root and an update is the only moment the application can reach
# apt at all — the service itself is unprivileged with NoNewPrivileges, which
# is deliberate. Without this the fix for "HandBrake cannot see the GPU" is a
# command on the Proxmox host, and someone updating from their phone has no
# host. The update button is the whole point.
#
# Only when a render node is actually present, and only when the runtime is
# actually missing: an install that already works must not run apt on every
# update, and a container with no GPU has no use for Intel's media stack.
if compgen -G "/dev/dri/renderD*" >/dev/null 2>&1; then
    gpu_vendor=""
    for node in /dev/dri/renderD*; do
        [[ -e "$node" ]] || continue
        gpu_vendor="$(cat "/sys/class/drm/$(basename "$node")/device/vendor" 2>/dev/null || true)"
        break
    done
    # The VA-API driver is a choice between alternatives, not a list.
    #
    # intel-media-va-driver-non-free and intel-media-va-driver Conflict with
    # and Replace one another, and installing the second removes the first.
    # Listing both and installing whatever dpkg reported missing did exactly
    # that: the non-free driver was already there, the free one counted as
    # absent, apt swapped them, and a routine update took away HEVC encode,
    # MPEG-2, VP8 and Quick Sync — leaving HandBrake with no hardware encoder
    # on a machine where it had just started working.
    #
    # So the driver is a group, satisfied by *any* member, tried best first.
    # i965 is last and only reached if neither iHD build exists: it is for
    # pre-Broadwell hardware, and installing it beside iHD gives libva two
    # drivers to choose between for the same chip.
    #
    # The runtimes below are a genuine list — libmfx1 (Gen 9 to Gen 11) and
    # libmfxgen1 (Alder Lake and later) cover different silicon, do not
    # conflict, and which one a processor needs is not something anyone
    # should have to look up.
    case "$gpu_vendor" in
        0x8086) gpu_driver_choices=(intel-media-va-driver-non-free
                                    intel-media-va-driver i965-va-driver)
                gpu_packages=(libmfx1 libmfxgen1 libvpl2 vainfo) ;;
        0x1002) gpu_driver_choices=(mesa-va-drivers)
                gpu_packages=(vainfo) ;;
        *)      gpu_driver_choices=()
                gpu_packages=() ;;
    esac

    _adr_installed() {
        dpkg-query -W -f='${Status}' "$1" 2>/dev/null \
            | grep -q "^install ok installed$"
    }

    gpu_installed=()

    # The driver group: nothing to do if any member is already present.
    gpu_have_driver=0
    for pkg in "${gpu_driver_choices[@]}"; do
        if _adr_installed "$pkg"; then
            gpu_have_driver=1
            break
        fi
    done
    if [[ ${#gpu_driver_choices[@]} -gt 0 && "$gpu_have_driver" -eq 0 ]]; then
        # The index is often months stale in a container that has only ever
        # been updated through this script, and apt-get install then fails on
        # every name with 404. Once, and only when there is something to fetch.
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
        for pkg in "${gpu_driver_choices[@]}"; do
            if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" \
                    >/dev/null 2>&1; then
                gpu_installed+=("$pkg")
                break
            fi
        done
    fi

    # The rest, package by package, and not gated on gpu.runtime_state().
    # Gating on that was a bug of the same family: it answers "is a Quick Sync
    # runtime installed", true the moment either one is, so a container with
    # libmfxgen1 on a processor needing libmfx1 reported the stack fine and
    # skipped the install that would have fixed it.
    for pkg in "${gpu_packages[@]}"; do
        _adr_installed "$pkg" && continue
        # One at a time and best-effort: the names differ across releases and
        # some live in components a container may not have enabled. One
        # unavailable name must not take the rest with it.
        if DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$pkg" \
                >/dev/null 2>&1; then
            gpu_installed+=("$pkg")
        fi
    done

    if [[ ${#gpu_installed[@]} -gt 0 ]]; then
        msg_ok "GPU media stack installed: ${gpu_installed[*]}"
        msg_info "  Settings → Encoding → Test encoder proves it by encoding two seconds."
    elif [[ "$gpu_have_driver" -eq 0 && ${#gpu_driver_choices[@]} -gt 0 ]]; then
        # Silence here read as "nothing needed doing", which is the opposite of
        # what it meant: the GPU is present and none of its packages installed.
        msg_warn "No GPU media package could be installed — encoding stays on the CPU."
        msg_warn "    apt-get update && apt-get install -y ${gpu_driver_choices[0]}"
    fi
fi

# Unit files may have changed between versions — and adr-update.* may not exist
# at all on an install made before in-app updates.
units_changed=0
for unit in adr.service adr-update.service adr-update.path; do
    src="$INSTALL_DIR/systemd/$unit"
    [[ -f "$src" ]] || continue
    if ! cmp -s "$src" "/etc/systemd/system/$unit"; then
        install -m 0644 "$src" "/etc/systemd/system/$unit"
        msg_info "Installed systemd unit: $unit"
        units_changed=1
    fi
done
if [[ "$units_changed" -eq 1 ]]; then
    systemctl daemon-reload
    # Enabling is idempotent, and this is what turns on updating from the web UI
    # for an install that predates it.
    systemctl enable --now adr-update.path >/dev/null 2>&1 \
        || msg_warn "Could not enable adr-update.path — updates stay host-side."
fi

if [[ -n "$NEW_COMMIT" ]]; then
    echo "$NEW_COMMIT" > "$INSTALL_DIR/.commit"
    chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR/.commit" 2>/dev/null || true
fi

msg_info "Restarting service…"
systemctl start adr
SERVICE_STOPPED=0

# Confirm it actually came back up rather than claiming success blindly.
ok=0
for _ in $(seq 1 20); do
    if curl -fsS -o /dev/null http://127.0.0.1:8080/api/status 2>/dev/null; then ok=1; break; fi
    sleep 1
done
if [[ $ok -eq 1 ]]; then
    msg_ok "Update complete — web UI is responding."
else
    msg_warn "Service restarted but the web UI did not respond within 20s."
    msg_warn "Check: journalctl -u adr -e"
    exit 1
fi

# adr-setup-nas and adr-doctor are copies of scripts/ living on the Proxmox
# HOST, because they use pct. This script runs inside the container and cannot
# reach them, so say so rather than leaving stale copies behind.
CTID_HINT="<CTID>"
if [[ -r /etc/default/adr ]]; then
    # shellcheck disable=SC1091
    . /etc/default/adr 2>/dev/null || true
fi
[[ -n "${ADR_CTID:-}" ]] && CTID_HINT="$ADR_CTID"

echo
msg_warn "The host-side helpers were NOT updated — this script runs inside the"
msg_warn "container and cannot write to the host. adr-doctor is a copy, and an old"
msg_warn "copy skips whatever checks are new and then says 'nothing wrong found'."
echo
msg_info "Run these two on the Proxmox host, in this order:"
echo
echo "    pct pull ${CTID_HINT} /opt/adr/scripts/adr-doctor.sh /usr/local/sbin/adr-doctor && chmod +x /usr/local/sbin/adr-doctor"
echo "    pct pull ${CTID_HINT} /opt/adr/scripts/setup-nas.sh /usr/local/sbin/adr-setup-nas && chmod +x /usr/local/sbin/adr-setup-nas"
echo
msg_info "Then:  adr-doctor --fix ${CTID_HINT}"
echo
