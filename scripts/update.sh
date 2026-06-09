#!/usr/bin/env bash
#
# Update Automatic Disc Ripper in place (run INSIDE the container).
#
#   pct exec <CTID> -- /opt/adr/scripts/update.sh
#
set -euo pipefail

INSTALL_DIR="/opt/adr"
RUN_USER="adr"

[[ $EUID -eq 0 ]] || { echo "Run as root inside the container." >&2; exit 1; }

echo "• Stopping service…"
systemctl stop adr

if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "• Pulling latest code…"
    sudo -u "$RUN_USER" git -C "$INSTALL_DIR" pull --ff-only
else
    echo "! $INSTALL_DIR is not a git checkout — skipping code pull."
    echo "  (Installed via host push. Re-run the host installer to update code.)"
fi

echo "• Updating Python dependencies…"
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "• Restarting service…"
systemctl start adr
echo "✓ Update complete."
