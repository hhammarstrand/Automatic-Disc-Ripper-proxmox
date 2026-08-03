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
    msg_error "For a private repo re-run with: GITHUB_TOKEN=ghp_xxx $0"
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
