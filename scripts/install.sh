#!/usr/bin/env bash
#
# Automatic Disc Ripper — Proxmox LXC installer (run this on the Proxmox HOST).
#
# One-liner:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/scripts/install.sh)"
#
# What it does:
#   1. Creates a privileged Ubuntu 24.04 LXC (simplest path for optical
#      passthrough; pass CT_UNPRIVILEGED=1 if you have idmap configured).
#   2. Passes the optical drive(s) (/dev/sr*, /dev/sg*) into the container.
#   3. Installs MakeMKV + HandBrakeCLI + the app and enables the systemd service.
#
# Everything is overridable with environment variables — see the defaults below.
# For a fully non-interactive install set ADR_NONINTERACTIVE=1 and any CT_* vars.
#
set -euo pipefail

# ----------------------------------------------------------------------------- #
# Configuration (env-overridable)
# ----------------------------------------------------------------------------- #
ADR_REPO_URL="${ADR_REPO_URL:-https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git}"
ADR_BRANCH="${ADR_BRANCH:-main}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"            # optional — needed only for a private repo

CT_ID="${CT_ID:-}"                          # default: next free VMID
CT_HOSTNAME="${CT_HOSTNAME:-adr}"
CT_CORES="${CT_CORES:-4}"
CT_RAM="${CT_RAM:-2048}"
CT_SWAP="${CT_SWAP:-512}"
CT_DISK="${CT_DISK:-8}"
CT_BRIDGE="${CT_BRIDGE:-vmbr0}"
CT_STORAGE="${CT_STORAGE:-local-lvm}"
CT_TEMPLATE_STORAGE="${CT_TEMPLATE_STORAGE:-local}"
CT_UNPRIVILEGED="${CT_UNPRIVILEGED:-0}"     # 0 = privileged (simplest for optical passthrough)
CT_PASSWORD="${CT_PASSWORD:-}"             # default: random, printed at the end

DISC_DEVICE="${DISC_DEVICE:-}"             # default: first /dev/sr* found
MEDIA_HOST_PATH="${MEDIA_HOST_PATH:-}"     # optional host dir bind-mounted to /opt/adr/completed
TMDB_API_KEY="${TMDB_API_KEY:-}"           # optional
MAKEMKV_KEY="${MAKEMKV_KEY:-auto}"         # auto | T-xxxx | (empty to skip)

ADR_NONINTERACTIVE="${ADR_NONINTERACTIVE:-0}"

# raw.githubusercontent base for this repo/branch, used for the in-container
# fallback fetch and for the error message shown when a private repo has no token.
RAW_BASE="https://raw.githubusercontent.com/$(echo "$ADR_REPO_URL" | sed -E 's#https://github.com/##; s#\.git$##')/${ADR_BRANCH}"

# ----------------------------------------------------------------------------- #
# Pretty output
# ----------------------------------------------------------------------------- #
RD=$'\e[31m'; GN=$'\e[32m'; YW=$'\e[33m'; BL=$'\e[34m'; CL=$'\e[0m'
msg_info()  { echo -e " ${BL}•${CL} $*"; }
msg_ok()    { echo -e " ${GN}✓${CL} $*"; }
msg_warn()  { echo -e " ${YW}!${CL} $*"; }
msg_error() { echo -e " ${RD}✗${CL} $*" >&2; }
die()       { msg_error "$*"; exit 1; }

cleanup_on_fail() {
    local code=$?
    if [[ $code -ne 0 && -n "${_CREATED_CTID:-}" ]]; then
        msg_warn "Install failed. The container $_CREATED_CTID was created but not fully configured."
        msg_warn "Inspect it with: pct config $_CREATED_CTID   /   destroy with: pct destroy $_CREATED_CTID"
    fi
}
trap cleanup_on_fail EXIT

# ----------------------------------------------------------------------------- #
# Pre-flight checks
# ----------------------------------------------------------------------------- #
[[ $EUID -eq 0 ]] || die "Run this as root on the Proxmox host."
command -v pct  >/dev/null 2>&1 || die "'pct' not found — this must run on a Proxmox VE node."
command -v pveam >/dev/null 2>&1 || die "'pveam' not found — this must run on a Proxmox VE node."

echo
echo "  Automatic Disc Ripper — Proxmox LXC installer"
echo "  ============================================="
echo

ask() {  # ask <var> <prompt> <default>
    local __var="$1" __prompt="$2" __default="$3" __reply
    if [[ "$ADR_NONINTERACTIVE" == "1" ]]; then
        printf -v "$__var" '%s' "$__default"; return
    fi
    read -rp "  ${__prompt} [${__default}]: " __reply || true
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

# ----------------------------------------------------------------------------- #
# Gather configuration
# ----------------------------------------------------------------------------- #
if [[ -z "$CT_ID" ]]; then
    CT_ID="$(pvesh get /cluster/nextid 2>/dev/null || echo 200)"
fi
ask CT_ID        "Container ID"            "$CT_ID"
ask CT_HOSTNAME  "Hostname"                "$CT_HOSTNAME"
ask CT_CORES     "CPU cores"               "$CT_CORES"
ask CT_RAM       "RAM (MiB)"               "$CT_RAM"
ask CT_DISK      "Disk (GiB)"              "$CT_DISK"
ask CT_STORAGE   "Container storage"       "$CT_STORAGE"
ask CT_BRIDGE    "Network bridge"          "$CT_BRIDGE"
ask CT_UNPRIVILEGED "Unprivileged? (0=no, recommended for optical passthrough)" "$CT_UNPRIVILEGED"

# Detect optical drives on the host
mapfile -t HOST_SR < <(ls -1 /dev/sr[0-9]* 2>/dev/null || true)
if [[ ${#HOST_SR[@]} -eq 0 ]]; then
    msg_warn "No /dev/sr* optical drive found on this host."
    msg_warn "You can attach one later and re-run, or continue (e.g. USB drive plugged in afterwards)."
    DISC_DEVICE="${DISC_DEVICE:-/dev/sr0}"
else
    msg_ok "Optical drive(s) found: ${HOST_SR[*]}"
    DISC_DEVICE="${DISC_DEVICE:-${HOST_SR[0]}}"
fi
ask DISC_DEVICE  "Primary optical device"  "$DISC_DEVICE"

ask TMDB_API_KEY "TMDb API key (optional, blank to skip)" "$TMDB_API_KEY"
ask MAKEMKV_KEY  "MakeMKV key ('auto' fetches free beta key)" "$MAKEMKV_KEY"
ask MEDIA_HOST_PATH "Host dir to bind-mount as /opt/adr/completed (blank=none)" "$MEDIA_HOST_PATH"

if [[ -z "$CT_PASSWORD" ]]; then
    CT_PASSWORD="$(openssl rand -base64 18 2>/dev/null | tr -d '/+=' | cut -c1-20 || echo "adr-$(date +%s)")"
    _PW_GENERATED=1
fi

# ----------------------------------------------------------------------------- #
# Download the Ubuntu 24.04 template if needed
# ----------------------------------------------------------------------------- #
msg_info "Locating Ubuntu 24.04 LXC template…"
pveam update >/dev/null 2>&1 || msg_warn "pveam update failed (continuing with cached list)"
TEMPLATE="$(pveam available --section system 2>/dev/null | awk '/ubuntu-24.04-standard/ {print $2}' | sort -V | tail -1)"
[[ -n "$TEMPLATE" ]] || die "Could not find an ubuntu-24.04-standard template via pveam."

if ! pveam list "$CT_TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    msg_info "Downloading template $TEMPLATE …"
    pveam download "$CT_TEMPLATE_STORAGE" "$TEMPLATE" >/dev/null
fi
msg_ok "Template ready: $TEMPLATE"

# ----------------------------------------------------------------------------- #
# Create the container
# ----------------------------------------------------------------------------- #
msg_info "Creating LXC $CT_ID ($CT_HOSTNAME) …"
pct create "$CT_ID" "${CT_TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CT_CORES" \
    --memory "$CT_RAM" \
    --swap "$CT_SWAP" \
    --rootfs "${CT_STORAGE}:${CT_DISK}" \
    --net0 "name=eth0,bridge=${CT_BRIDGE},ip=dhcp" \
    --features "nesting=1" \
    --unprivileged "$CT_UNPRIVILEGED" \
    --onboot 1 \
    --start 0 \
    --password "$CT_PASSWORD" >/dev/null
_CREATED_CTID="$CT_ID"
msg_ok "Container created"

# ----------------------------------------------------------------------------- #
# Optical-drive passthrough
# ----------------------------------------------------------------------------- #
msg_info "Configuring optical-drive passthrough…"
CONF="/etc/pve/lxc/${CT_ID}.conf"
{
    # /dev/sr* are BLOCK devices with major 11 — the rule must use type 'b',
    # not 'c', or the container is denied access and no disc is ever seen.
    # The 'c 11:*' line is kept as well because some kernels expose the
    # cdrom ioctl interface as a character device of the same major.
    echo "lxc.cgroup2.devices.allow: b 11:* rwm"   # sr*  SCSI CD-ROM  (block major 11)
    echo "lxc.cgroup2.devices.allow: c 11:* rwm"   # sr*  cdrom ioctls (char  major 11)
    echo "lxc.cgroup2.devices.allow: c 21:* rwm"   # sg*  generic SCSI (MakeMKV uses SG_IO)
} >> "$CONF"

# Bind every optical device the host currently has, plus /dev/cdrom and a
# matching /dev/sg node. 'optional' means it won't block start if absent.
declare -A _seen
for dev in "${HOST_SR[@]:-$DISC_DEVICE}" "$DISC_DEVICE"; do
    [[ -n "$dev" ]] || continue
    [[ -n "${_seen[$dev]:-}" ]] && continue
    _seen[$dev]=1
    base="${dev#/dev/}"
    echo "lxc.mount.entry: ${dev} dev/${base} none bind,optional,create=file" >> "$CONF"
done
# MakeMKV issues SG_IO ioctls, so the drive's generic-SCSI node has to come
# along too. Resolve it from sysfs per optical device — NEVER bind every
# /dev/sg* on the host, as those also address the system's SATA/SAS disks and
# handing raw SG_IO on the boot disk to a privileged container is dangerous.
for dev in "${!_seen[@]}"; do
    sg_dir="/sys/block/${dev#/dev/}/device/scsi_generic"
    [[ -d "$sg_dir" ]] || continue
    for sg_node in "$sg_dir"/sg[0-9]*; do
        [[ -e "$sg_node" ]] || continue
        sg="/dev/$(basename "$sg_node")"
        echo "lxc.mount.entry: ${sg} dev/${sg#/dev/} none bind,optional,create=file" >> "$CONF"
    done
done
echo "lxc.mount.entry: /dev/cdrom dev/cdrom none bind,optional,create=file" >> "$CONF"
msg_ok "Passthrough configured ($CONF)"

# Optional media bind-mount for persistence / Plex sharing
if [[ -n "$MEDIA_HOST_PATH" ]]; then
    mkdir -p "$MEDIA_HOST_PATH"
    pct set "$CT_ID" -mp0 "${MEDIA_HOST_PATH},mp=/opt/adr/completed" >/dev/null
    msg_ok "Bind-mounted $MEDIA_HOST_PATH -> /opt/adr/completed"
fi

# ----------------------------------------------------------------------------- #
# Start container and wait for network
# ----------------------------------------------------------------------------- #
msg_info "Starting container…"
pct start "$CT_ID" >/dev/null
for _ in $(seq 1 30); do
    if pct exec "$CT_ID" -- getent hosts archive.ubuntu.com >/dev/null 2>&1; then break; fi
    sleep 1
done
msg_ok "Container is up"

# ----------------------------------------------------------------------------- #
# Transfer the application source into the container
# ----------------------------------------------------------------------------- #
# We fetch the repo on the HOST (so a private repo only needs one token here),
# then push it into the container. The in-container installer then needs no
# GitHub access at all.
msg_info "Fetching application source…"
TMP_SRC="$(mktemp -d)"
CLONE_URL="$ADR_REPO_URL"
if [[ -n "$GITHUB_TOKEN" ]]; then
    CLONE_URL="https://x-access-token:${GITHUB_TOKEN}@${ADR_REPO_URL#https://}"
fi
if git clone --depth 1 --branch "$ADR_BRANCH" "$CLONE_URL" "$TMP_SRC/adr" >/dev/null 2>&1; then
    msg_ok "Cloned $ADR_REPO_URL ($ADR_BRANCH)"
    TARBALL="$TMP_SRC/adr-src.tar.gz"
    tar -C "$TMP_SRC/adr" --exclude='.git' -czf "$TARBALL" .
    pct exec "$CT_ID" -- mkdir -p /opt/adr
    pct push "$CT_ID" "$TARBALL" /tmp/adr-src.tar.gz
    pct exec "$CT_ID" -- tar -xzf /tmp/adr-src.tar.gz -C /opt/adr
    pct exec "$CT_ID" -- rm -f /tmp/adr-src.tar.gz
elif [[ -z "$GITHUB_TOKEN" ]]; then
    rm -rf "$TMP_SRC"
    die "Could not clone $ADR_REPO_URL.
     If the repository is PRIVATE you must supply a token, e.g.:
       export GITHUB_TOKEN=ghp_xxx
       bash -c \"\$(curl -fsSL -H \"Authorization: Bearer \$GITHUB_TOKEN\" $RAW_BASE/scripts/install.sh)\"
     If it is public, check this host's network/DNS access to github.com."
else
    msg_warn "Could not clone $ADR_REPO_URL on the host despite a token being set."
    msg_warn "Falling back to an in-container clone."
fi
rm -rf "$TMP_SRC"

# ----------------------------------------------------------------------------- #
# Run the in-container installer
# ----------------------------------------------------------------------------- #
msg_info "Running in-container installation (this can take a few minutes)…"
pct exec "$CT_ID" -- env \
    ADR_REPO_URL="$ADR_REPO_URL" \
    ADR_BRANCH="$ADR_BRANCH" \
    GITHUB_TOKEN="$GITHUB_TOKEN" \
    RAW_BASE="$RAW_BASE" \
    TMDB_API_KEY="$TMDB_API_KEY" \
    ADR_MAKEMKV_KEY_MODE="$MAKEMKV_KEY" \
    bash -c '
        set -e
        if [[ -f /opt/adr/scripts/install-container.sh ]]; then
            bash /opt/adr/scripts/install-container.sh
        else
            # Source was not pushed from the host. Fetch just the bootstrap
            # script; it clones the rest itself (with the token if private).
            if [[ -n "${GITHUB_TOKEN:-}" ]]; then
                curl -fsSL -H "Authorization: Bearer ${GITHUB_TOKEN}" \
                    "${RAW_BASE}/scripts/install-container.sh" | bash
            else
                curl -fsSL "${RAW_BASE}/scripts/install-container.sh" | bash
            fi
        fi
    '
msg_ok "In-container installation finished"

# ----------------------------------------------------------------------------- #
# Done
# ----------------------------------------------------------------------------- #
# Clear the failure trap first: from here on the install has succeeded, and an
# error while merely looking up the IP must not abort before we have printed the
# generated root password — that is the user's only copy of it.
trap - EXIT
CT_IP="$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)"
echo
msg_ok "Installation complete!"
echo
echo "  ┌────────────────────────────────────────────────────────"
echo "  │  Web UI : http://${CT_IP:-<container-ip>}:8080"
echo "  │  SSH    : ssh root@${CT_IP:-<container-ip>}"
if [[ -n "${_PW_GENERATED:-}" ]]; then
echo "  │  Root pw: ${CT_PASSWORD}   (generated — save it now)"
fi
echo "  │  CTID   : ${CT_ID}"
echo "  └────────────────────────────────────────────────────────"
echo
echo "  Next: open the web UI and (optionally) add your TMDb API key under Settings."
echo
