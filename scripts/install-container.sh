#!/usr/bin/env bash
#
# Automatic Disc Ripper — in-container installer (runs INSIDE the Ubuntu LXC).
#
# Normally invoked by scripts/install.sh on the Proxmox host, but it can also be
# run standalone inside an existing Ubuntu 24.04 container:
#
#   curl -fsSL https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/scripts/install-container.sh | bash
#
# Env vars honoured:
#   ADR_REPO_URL          git URL (default: public repo)
#   ADR_BRANCH            branch  (default: main)
#   GITHUB_TOKEN          token for cloning a private repo (optional)
#   TMDB_API_KEY          written into config/adr.yaml (optional)
#   ADR_MAKEMKV_KEY_MODE  'auto' | 'T-xxxx' | '' (skip)
#   ADR_COMPLETED_PATH    where finished films go (default /opt/adr/completed;
#                         the host installer sets /mnt/media when it mounts one)
#
set -euo pipefail

ADR_REPO_URL="${ADR_REPO_URL:-https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git}"
ADR_BRANCH="${ADR_BRANCH:-main}"
GITHUB_TOKEN="${GITHUB_TOKEN:-}"
TMDB_API_KEY="${TMDB_API_KEY:-}"
ADR_MAKEMKV_KEY_MODE="${ADR_MAKEMKV_KEY_MODE:-auto}"

INSTALL_DIR="/opt/adr"
RUN_USER="adr"

GN=$'\e[32m'; YW=$'\e[33m'; RD=$'\e[31m'; BL=$'\e[34m'; CL=$'\e[0m'
msg_info()  { echo -e " ${BL}•${CL} $*"; }
msg_ok()    { echo -e " ${GN}✓${CL} $*"; }
msg_warn()  { echo -e " ${YW}!${CL} $*"; }
msg_error() { echo -e " ${RD}✗${CL} $*" >&2; }

[[ $EUID -eq 0 ]] || { msg_error "Run as root inside the container."; exit 1; }

export DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------------------- #
# Base packages
# ----------------------------------------------------------------------------- #
msg_info "Updating apt and installing base packages…"
apt-get update -qq
apt-get install -y -qq \
    ca-certificates curl wget gnupg software-properties-common \
    git sudo eject util-linux \
    python3 python3-venv python3-pip >/dev/null

# HandBrakeCLI lives in 'universe'. It is enabled by default in the standard
# Ubuntu images, but enable it explicitly so a minimal template also works.
add-apt-repository -y universe >/dev/null 2>&1 || true
apt-get update -qq
if ! apt-get install -y -qq handbrake-cli >/dev/null 2>&1; then
    msg_error "Could not install handbrake-cli — transcoding will not work."
    msg_error "Check that the 'universe' component is enabled and apt can reach the archive."
    exit 1
fi
msg_ok "Base packages installed (HandBrakeCLI: $(HandBrakeCLI --version 2>&1 | head -1 || echo present))"

# cdparanoia and ffmpeg handle audio CDs: cdparanoia re-reads until the samples
# agree, ffmpeg encodes and tags. Not fatal if they are missing — video discs
# do not need them, and the Doctor page says so rather than the install dying.
msg_info "Installing audio CD tools…"
if apt-get install -y -qq cdparanoia ffmpeg >/dev/null 2>&1; then
    msg_ok "Audio CD tools installed (cdparanoia, ffmpeg)"
else
    msg_warn "Could not install cdparanoia/ffmpeg — audio CDs will not rip."
    msg_warn "    apt-get install -y cdparanoia ffmpeg"
fi

# ----------------------------------------------------------------------------- #
# MakeMKV from the heyarje PPA (no compilation)
#
# https://launchpad.net/~heyarje/+archive/ubuntu/makemkv-beta
# Publishes makemkv-bin / makemkv-oss for noble (24.04) among others.
# ----------------------------------------------------------------------------- #
MAKEMKV_PPA="${MAKEMKV_PPA:-ppa:heyarje/makemkv-beta}"
msg_info "Installing MakeMKV (${MAKEMKV_PPA})…"
MAKEMKV_OK=0
if add-apt-repository -y "$MAKEMKV_PPA" >/dev/null 2>&1; then
    apt-get update -qq
    if apt-get install -y -qq makemkv-bin makemkv-oss >/dev/null 2>&1; then
        MAKEMKV_OK=1
    else
        msg_warn "MakeMKV packages failed to install from ${MAKEMKV_PPA}."
    fi
else
    msg_warn "Could not add the MakeMKV PPA ${MAKEMKV_PPA} (network/launchpad issue)."
fi

# Trust the binary on disk, not the exit codes above.
if command -v makemkvcon >/dev/null 2>&1; then
    MAKEMKV_OK=1
    msg_ok "MakeMKV installed: $(makemkvcon --version 2>/dev/null | head -1 || echo present)"
else
    MAKEMKV_OK=0
    msg_warn "makemkvcon is NOT installed — disc ripping will not work."
    msg_warn "The watch folder (HandBrake-only transcoding) still works."
    msg_warn "Install it later inside the container with:"
    msg_warn "    add-apt-repository -y ${MAKEMKV_PPA} && apt-get update && apt-get install -y makemkv-bin makemkv-oss"
fi

# ----------------------------------------------------------------------------- #
# Service user
# ----------------------------------------------------------------------------- #
# The uid/gid is PINNED rather than left to useradd's dynamic allocation.
# In a privileged LXC the container uid is the host uid, and an NFS server
# authorises writes by numeric uid — so a uid that differs per install would
# make "export this share to the ripper" impossible to document. 8420 is
# outside the normal system range and stable across installs.
ADR_UID="${ADR_UID:-8420}"
ADR_GID="${ADR_GID:-8420}"

msg_info "Creating service user '$RUN_USER' (uid/gid ${ADR_UID}:${ADR_GID})…"
if ! getent group "$RUN_USER" >/dev/null 2>&1; then
    groupadd -g "$ADR_GID" "$RUN_USER" 2>/dev/null || groupadd "$RUN_USER"
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    useradd -r -u "$ADR_UID" -g "$RUN_USER" -m -d "$INSTALL_DIR" \
        -s /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
      || useradd -r -g "$RUN_USER" -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$RUN_USER"
fi
usermod -aG cdrom "$RUN_USER" 2>/dev/null || true
usermod -aG disk  "$RUN_USER" 2>/dev/null || true
ADR_UID="$(id -u "$RUN_USER")"; ADR_GID="$(id -g "$RUN_USER")"
msg_ok "Service user ready (uid=${ADR_UID} gid=${ADR_GID})"

# ----------------------------------------------------------------------------- #
# Application source
# ----------------------------------------------------------------------------- #
if [[ -f "$INSTALL_DIR/run.py" ]]; then
    msg_ok "Application source already present in $INSTALL_DIR"
else
    msg_info "Cloning application source…"
    clone_url="$ADR_REPO_URL"
    [[ -n "$GITHUB_TOKEN" ]] && clone_url="https://x-access-token:${GITHUB_TOKEN}@${ADR_REPO_URL#https://}"
    tmp="$(mktemp -d)"
    git clone --depth 1 --branch "$ADR_BRANCH" "$clone_url" "$tmp/src" >/dev/null
    mkdir -p "$INSTALL_DIR"
    cp -a "$tmp/src/." "$INSTALL_DIR/"
    # Record which commit this is BEFORE .git goes away. What lands in
    # $INSTALL_DIR is a working tree, not a checkout, so this file is the only
    # thing that can answer "am I up to date?" later.
    git -C "$tmp/src" rev-parse HEAD > "$INSTALL_DIR/.commit" 2>/dev/null || true
    rm -rf "$tmp" "$INSTALL_DIR/.git"
    msg_ok "Source cloned into $INSTALL_DIR ($(cut -c1-8 "$INSTALL_DIR/.commit" 2>/dev/null || echo unknown))"
fi

mkdir -p "$INSTALL_DIR/raw" "$INSTALL_DIR/completed" "$INSTALL_DIR/watch" "$INSTALL_DIR/config"

# ----------------------------------------------------------------------------- #
# Python virtualenv
# ----------------------------------------------------------------------------- #
msg_info "Creating Python virtualenv and installing requirements…"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
msg_ok "Python dependencies installed"

# ----------------------------------------------------------------------------- #
# Config
# ----------------------------------------------------------------------------- #
msg_info "Seeding configuration…"
if [[ ! -f "$INSTALL_DIR/config/adr.yaml" ]]; then
    cp "$INSTALL_DIR/config/adr.yaml.example" "$INSTALL_DIR/config/adr.yaml"
fi
if [[ -n "$TMDB_API_KEY" ]]; then
    # Escape characters that are special in a sed replacement.
    esc_key="$(printf '%s' "$TMDB_API_KEY" | sed -e 's/[\/&|]/\\&/g')"
    sed -i "s|^tmdb_api_key:.*|tmdb_api_key: '${esc_key}'|" "$INSTALL_DIR/config/adr.yaml"
    msg_ok "TMDb API key written to config"
fi

# Where finished films land. Empty means "keep them on the container's own disk"
# ($INSTALL_DIR/completed); the host installer sets this to /mnt/media when it
# bind-mounts a library, so the app writes to the mount instead of to a
# directory of its own that happens to be shadowed by one.
if [[ -n "${ADR_COMPLETED_PATH:-}" ]]; then
    esc_dest="$(printf '%s' "$ADR_COMPLETED_PATH" | sed -e 's/[\/&|]/\\&/g')"
    if grep -q "^completed_path:" "$INSTALL_DIR/config/adr.yaml"; then
        sed -i "s|^completed_path:.*|completed_path: ${esc_dest}|" "$INSTALL_DIR/config/adr.yaml"
    else
        echo "completed_path: ${ADR_COMPLETED_PATH}" >> "$INSTALL_DIR/config/adr.yaml"
    fi
    msg_ok "Finished films will be written to ${ADR_COMPLETED_PATH}"
    mkdir -p "$ADR_COMPLETED_PATH" 2>/dev/null || true
    # Only take ownership of a directory we own. A bind-mounted library belongs
    # to the host, and chowning it would rewrite every file the user already has.
    if mountpoint -q "$ADR_COMPLETED_PATH" 2>/dev/null; then
        msg_info "${ADR_COMPLETED_PATH} is a mount — its ownership stays with the host."
    else
        chown "$RUN_USER:$RUN_USER" "$ADR_COMPLETED_PATH" 2>/dev/null || true
    fi
fi

# Ownership before we run anything as the service user.
# NOTE: 'completed' may be a bind-mount of a host media library on installs
# made before 1.0 moved that mount to /mnt/media. A recursive chown would walk
# into it and rewrite ownership of every file in the user's library, so it is
# excluded deliberately — the app only ever needs to write new files there.
find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name completed \
    -exec chown -R "$RUN_USER:$RUN_USER" {} +
chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

if mountpoint -q "$INSTALL_DIR/completed" 2>/dev/null; then
    msg_warn "$INSTALL_DIR/completed is a bind-mount — its ownership is left to the host."
    msg_warn "Make sure uid $(id -u "$RUN_USER") can write there, e.g. on the Proxmox host:"
    msg_warn "    chown -R $(id -u "$RUN_USER"):$(id -g "$RUN_USER") <your media path>"
else
    chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR/completed"
fi

# ----------------------------------------------------------------------------- #
# MakeMKV registration key
# ----------------------------------------------------------------------------- #
msg_info "Configuring MakeMKV registration key (mode: ${ADR_MAKEMKV_KEY_MODE:-auto})…"
install -d -o "$RUN_USER" -g "$RUN_USER" -m 0700 "$INSTALL_DIR/.MakeMKV"
case "${ADR_MAKEMKV_KEY_MODE}" in
    ""|none|skip)
        msg_warn "Skipping MakeMKV key — set one later in the web UI (Settings)."
        ;;
    auto|AUTO)
        # PYTHONPATH is required: 'python -m adr.makemkv_key' only resolves the
        # 'adr' package if the install dir is importable, and this script does
        # not run from there.
        if sudo -u "$RUN_USER" HOME="$INSTALL_DIR" PYTHONPATH="$INSTALL_DIR" \
            "$INSTALL_DIR/.venv/bin/python" -m adr.makemkv_key --ensure >/dev/null 2>&1; then
            msg_ok "MakeMKV beta key fetched and stored"
        else
            msg_warn "Could not auto-fetch the MakeMKV beta key."
            msg_warn "Add one later via the web UI → Settings → 'Refresh MakeMKV key'."
        fi
        ;;
    *)
        if sudo -u "$RUN_USER" HOME="$INSTALL_DIR" PYTHONPATH="$INSTALL_DIR" \
            ADR_MAKEMKV_KEY="$ADR_MAKEMKV_KEY_MODE" \
            "$INSTALL_DIR/.venv/bin/python" -m adr.makemkv_key --ensure --key "$ADR_MAKEMKV_KEY_MODE" >/dev/null 2>&1; then
            msg_ok "MakeMKV key stored"
        else
            msg_warn "Supplied MakeMKV key was rejected (malformed?). Set one in the web UI later."
        fi
        ;;
esac

# ----------------------------------------------------------------------------- #
# systemd service
# ----------------------------------------------------------------------------- #
msg_info "Installing and starting systemd service…"
# The container has no way to discover its own Proxmox CTID, so record it here.
# The Storage page uses it to generate a ready-to-run adr-setup-nas command.
if [[ -n "${ADR_CTID:-}" ]]; then
    touch /etc/default/adr && chmod 0644 /etc/default/adr
    sed -i '/^ADR_CTID=/d' /etc/default/adr
    echo "ADR_CTID=${ADR_CTID}" >> /etc/default/adr
fi
install -m 0644 "$INSTALL_DIR/systemd/adr.service" /etc/systemd/system/adr.service

# In-app updates. The web UI runs unprivileged and cannot escalate; it touches a
# flag file and this path unit starts the (root) update service. That keeps
# "can request an update" and "can run code as root" as separate capabilities.
install -m 0644 "$INSTALL_DIR/systemd/adr-update.service" /etc/systemd/system/adr-update.service
install -m 0644 "$INSTALL_DIR/systemd/adr-update.path" /etc/systemd/system/adr-update.path

systemctl daemon-reload
systemctl enable --now adr.service >/dev/null 2>&1 || systemctl enable --now adr.service
if systemctl enable --now adr-update.path >/dev/null 2>&1; then
    msg_ok "Service enabled (in-app updates available)"
else
    msg_warn "Service enabled, but adr-update.path could not be started —"
    msg_warn "updates will need to be run from the host with update.sh."
fi

# ----------------------------------------------------------------------------- #
# Health check
# ----------------------------------------------------------------------------- #
msg_info "Waiting for the web UI to respond…"
ok=0
for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null http://127.0.0.1:8080/api/status 2>/dev/null; then ok=1; break; fi
    sleep 1
done
if [[ $ok -eq 1 ]]; then
    msg_ok "Web UI is up at http://$(hostname -I | awk '{print $1}'):8080"
else
    msg_warn "Web UI did not respond yet. Check: journalctl -u adr -e"
fi

# ----------------------------------------------------------------------------- #
# Component summary — say plainly what will and will not work
# ----------------------------------------------------------------------------- #
echo
echo "  Component status"
echo "  ────────────────"
if [[ "$MAKEMKV_OK" -eq 1 ]]; then
    echo -e "   ${GN}✓${CL} MakeMKV      $(command -v makemkvcon)   — disc ripping enabled"
else
    echo -e "   ${RD}✗${CL} MakeMKV      missing               — disc ripping DISABLED"
fi
if command -v HandBrakeCLI >/dev/null 2>&1; then
    echo -e "   ${GN}✓${CL} HandBrakeCLI $(command -v HandBrakeCLI) — transcoding enabled"
else
    echo -e "   ${RD}✗${CL} HandBrakeCLI missing               — transcoding DISABLED"
fi
if [[ -s "$INSTALL_DIR/.MakeMKV/settings.conf" ]]; then
    echo -e "   ${GN}✓${CL} MakeMKV key  stored"
else
    echo -e "   ${YW}!${CL} MakeMKV key  not set             — add it in the web UI under Settings"
fi
echo
