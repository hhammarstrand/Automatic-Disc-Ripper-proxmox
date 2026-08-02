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

# Re-assert ownership on everything except the preserved media dirs, which may
# be a bind-mount owned by the host.
chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR" 2>/dev/null || true
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
