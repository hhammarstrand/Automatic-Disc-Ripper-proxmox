#!/usr/bin/env bash
#
# Uninstall Automatic Disc Ripper.
#
#  - Inside the container:  removes the service and (optionally) /opt/adr
#       /opt/adr/scripts/uninstall.sh
#  - On the Proxmox host:   pass a CTID to stop & destroy the whole container
#       scripts/uninstall.sh <CTID>
#
set -euo pipefail

confirm() { read -rp "  $1 [y/N]: " r; [[ "${r,,}" == "y" || "${r,,}" == "yes" ]]; }

# ---- Host mode: destroy a container -----------------------------------------
if [[ "${1:-}" =~ ^[0-9]+$ ]] && command -v pct >/dev/null 2>&1; then
    CTID="$1"
    echo "This will STOP and DESTROY Proxmox container $CTID and all its data."
    if confirm "Continue?"; then
        pct stop "$CTID" 2>/dev/null || true
        pct destroy "$CTID"
        echo "✓ Container $CTID destroyed."
    else
        echo "Aborted."
    fi
    exit 0
fi

# ---- Container mode: remove the service -------------------------------------
[[ $EUID -eq 0 ]] || { echo "Run as root inside the container (or pass a CTID on the host)." >&2; exit 1; }

echo "• Stopping and disabling service…"
systemctl disable --now adr 2>/dev/null || true
rm -f /etc/systemd/system/adr.service
systemctl daemon-reload

if confirm "Also delete /opt/adr (config, database, media in completed/)?"; then
    rm -rf /opt/adr
    echo "✓ /opt/adr removed."
else
    echo "• Left /opt/adr in place."
fi
echo "✓ Uninstall complete."
