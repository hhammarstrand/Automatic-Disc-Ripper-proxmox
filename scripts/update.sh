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
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

clone_url="$ADR_REPO_URL"
[[ -n "$GITHUB_TOKEN" ]] && clone_url="https://x-access-token:${GITHUB_TOKEN}@${ADR_REPO_URL#https://}"

if ! git clone --depth 1 --branch "$ADR_BRANCH" "$clone_url" "$TMP/src" >/dev/null 2>&1; then
    msg_error "Could not clone ${ADR_REPO_URL}."
    msg_error "For a private repo re-run with: GITHUB_TOKEN=ghp_xxx $0"
    exit 1
fi
rm -rf "$TMP/src/.git"
msg_ok "Source fetched"

msg_info "Stopping service…"
systemctl stop adr || true

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
if mountpoint -q "$INSTALL_DIR/completed" 2>/dev/null; then
    msg_warn "$INSTALL_DIR/completed is a bind-mount — ownership left to the host."
    msg_warn "1.0 moves that mount to /mnt/media. On the Proxmox host, run:"
    msg_warn "    adr-doctor --fix <CTID>"
else
    chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR/completed" 2>/dev/null || true
fi
msg_ok "Code updated"

msg_info "Updating Python dependencies…"
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
sudo -u "$RUN_USER" "$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
msg_ok "Dependencies updated"

# The unit file may have changed between versions.
if ! cmp -s "$INSTALL_DIR/systemd/adr.service" /etc/systemd/system/adr.service; then
    msg_info "systemd unit changed — reinstalling…"
    install -m 0644 "$INSTALL_DIR/systemd/adr.service" /etc/systemd/system/adr.service
    systemctl daemon-reload
fi

msg_info "Restarting service…"
systemctl start adr

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
msg_info "The host-side helpers are not updated by this script."
msg_info "To refresh them, run this on the Proxmox host:"
echo
echo "    for f in setup-nas:adr-setup-nas adr-doctor:adr-doctor; do"
echo "      pct pull ${CTID_HINT} /opt/adr/scripts/\${f%%:*}.sh /usr/local/sbin/\${f##*:} \\"
echo "        && chmod +x /usr/local/sbin/\${f##*:}"
echo "    done"
echo
msg_info "Then check the container over with:  adr-doctor --fix ${CTID_HINT}"
echo
