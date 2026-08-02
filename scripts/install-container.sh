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

# ----------------------------------------------------------------------------- #
# MakeMKV from the Heyarne PPA (no compilation)
# ----------------------------------------------------------------------------- #
msg_info "Installing MakeMKV (Heyarne PPA)…"
if add-apt-repository -y ppa:heyarne/makemkv >/dev/null 2>&1; then
    apt-get update -qq
    if apt-get install -y -qq makemkv-bin makemkv-oss >/dev/null 2>&1; then
        msg_ok "MakeMKV installed: $(makemkvcon --version 2>/dev/null | head -1 || echo present)"
    else
        msg_warn "MakeMKV packages failed to install from the PPA."
        msg_warn "Ripping will not work until 'makemkvcon' is available — see README troubleshooting."
    fi
else
    msg_warn "Could not add the MakeMKV PPA (network/launchpad issue)."
    msg_warn "Install MakeMKV manually later; HandBrake-only encoding still works."
fi

# ----------------------------------------------------------------------------- #
# Service user
# ----------------------------------------------------------------------------- #
msg_info "Creating service user '$RUN_USER'…"
if ! id "$RUN_USER" >/dev/null 2>&1; then
    useradd -r -m -d "$INSTALL_DIR" -s /usr/sbin/nologin "$RUN_USER"
fi
usermod -aG cdrom "$RUN_USER" 2>/dev/null || true
usermod -aG disk  "$RUN_USER" 2>/dev/null || true
msg_ok "Service user ready"

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
    rm -rf "$tmp" "$INSTALL_DIR/.git"
    msg_ok "Source cloned into $INSTALL_DIR"
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

# Ownership before we run anything as the service user
# NOTE: 'completed' may be a bind-mount of a host media library (MEDIA_HOST_PATH
# / mp0). A recursive chown would walk into it and rewrite ownership of every
# file in the user's Plex library, so it is excluded deliberately.
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
        if sudo -u "$RUN_USER" HOME="$INSTALL_DIR" \
            "$INSTALL_DIR/.venv/bin/python" -m adr.makemkv_key --ensure >/dev/null 2>&1; then
            msg_ok "MakeMKV beta key fetched and stored"
        else
            msg_warn "Could not auto-fetch the MakeMKV beta key."
            msg_warn "Add one later via the web UI → Settings → 'Refresh MakeMKV key'."
        fi
        ;;
    *)
        if sudo -u "$RUN_USER" HOME="$INSTALL_DIR" ADR_MAKEMKV_KEY="$ADR_MAKEMKV_KEY_MODE" \
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
install -m 0644 "$INSTALL_DIR/systemd/adr.service" /etc/systemd/system/adr.service
systemctl daemon-reload
systemctl enable --now adr.service >/dev/null 2>&1 || systemctl enable --now adr.service
msg_ok "Service enabled"

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
