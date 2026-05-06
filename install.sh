#!/usr/bin/env bash
# Automatic Disc Ripper for Proxmox — local install script.
#
# Sets up a venv, installs Python dependencies, seeds a config file,
# creates the state directories, and (optionally) installs a systemd
# service that runs the app as the `adr` user.
#
# Usage:
#   sudo ./install.sh               # full install with systemd service
#   ./install.sh --no-service       # venv + deps only, skip systemd
#
# Requires: python3, python3-venv. MakeMKV and HandBrakeCLI must be
# installed separately (see README).

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "$0")" && pwd)}"
STATE_DIR="${STATE_DIR:-/var/lib/adr}"
SERVICE_USER="${SERVICE_USER:-adr}"
INSTALL_SERVICE=1

for arg in "$@"; do
    case "$arg" in
        --no-service) INSTALL_SERVICE=0 ;;
        -h|--help)
            sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

echo ">> Installing in $INSTALL_DIR"

if ! command -v python3 >/dev/null; then
    echo "python3 is required" >&2; exit 1
fi

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    echo ">> Creating virtualenv"
    python3 -m venv "$INSTALL_DIR/.venv"
fi

echo ">> Installing Python dependencies"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

if [[ ! -f "$INSTALL_DIR/config/adr.yaml" ]]; then
    echo ">> Seeding config/adr.yaml from example"
    cp "$INSTALL_DIR/config/adr.yaml.example" "$INSTALL_DIR/config/adr.yaml"
fi

echo ">> Creating state directories under $STATE_DIR"
mkdir -p "$STATE_DIR/raw" "$STATE_DIR/completed"

if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "systemd install requires root; re-run with sudo or pass --no-service" >&2
        exit 1
    fi
    if ! command -v systemctl >/dev/null; then
        echo "systemctl not found; re-run with --no-service to skip the unit" >&2
        exit 1
    fi
    if ! getent group cdrom >/dev/null; then
        echo "WARNING: 'cdrom' group does not exist; the service user will not be able to open /dev/sr*" >&2
    fi

    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        echo ">> Creating system user $SERVICE_USER"
        useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    fi
    # cdrom group lets the service open /dev/sr*
    usermod -a -G cdrom "$SERVICE_USER" || true

    chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR" "$INSTALL_DIR/config"

    echo ">> Installing systemd unit"
    install -m 644 "$INSTALL_DIR/packaging/adr.service" /etc/systemd/system/adr.service
    sed -i \
        -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
        -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
        /etc/systemd/system/adr.service

    systemctl daemon-reload
    systemctl enable --now adr.service

    echo ">> Service status:"
    systemctl --no-pager --lines=5 status adr.service || true
fi

cat <<EOF

Installation complete.

Config:   $INSTALL_DIR/config/adr.yaml
Raw:      $STATE_DIR/raw
Completed: $STATE_DIR/completed
Web UI:   http://$(hostname -I | awk '{print $1}'):8080
EOF
