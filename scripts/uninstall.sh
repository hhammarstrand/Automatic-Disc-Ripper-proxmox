#!/usr/bin/env bash
#
# Uninstall Automatic Disc Ripper.
#
#  - On the Proxmox host:   scripts/uninstall.sh <CTID>
#       stops and destroys the container, and offers to take away the things
#       the installer left on the host itself: the two helper commands, the
#       guest-startup drop-in, the fstab entry and the NAS password file.
#
#  - Inside the container:  /opt/adr/scripts/uninstall.sh
#       removes the services and, if you say so, /opt/adr.
#
# Installing touches more than one machine, so uninstalling has to as well.
# Destroying the container alone leaves adr-doctor on the host, an fstab line
# mounting a share for something that no longer exists, and — the one that
# matters — a file holding the NAS password in /root.
#
set -euo pipefail

GN=$'\e[32m'; YW=$'\e[33m'; RD=$'\e[31m'; BL=$'\e[34m'; CL=$'\e[0m'
msg_info()  { echo -e " ${BL}•${CL} $*"; }
msg_ok()    { echo -e " ${GN}✓${CL} $*"; }
msg_warn()  { echo -e " ${YW}!${CL} $*"; }
msg_error() { echo -e " ${RD}✗${CL} $*" >&2; }

confirm() { read -rp "  $1 [y/N]: " r; [[ "${r,,}" == "y" || "${r,,}" == "yes" ]]; }

CREDS_FILE="/root/.adr-nas-credentials"
DROPIN="/etc/systemd/system/pve-guests.service.d/adr-optical.conf"

# ---- Host mode: destroy a container, then clean up after the installer ------
if [[ "${1:-}" =~ ^[0-9]+$ ]] && command -v pct >/dev/null 2>&1; then
    CTID="$1"
    echo
    msg_warn "This will STOP and DESTROY Proxmox container ${CTID} and everything on its disk."
    msg_warn "Media on a NAS or a bind-mounted host directory is NOT on that disk and stays."
    echo
    if ! confirm "Destroy container ${CTID}?"; then
        echo "Aborted. Nothing was changed."
        exit 0
    fi

    pct stop "$CTID" 2>/dev/null || true
    pct destroy "$CTID"
    msg_ok "Container ${CTID} destroyed."

    # Everything below is on the host and outlives the container. Each is
    # offered separately: someone running two containers wants the shared
    # helpers to stay, and the fstab line may be mounting a share they use
    # for something else entirely.
    echo
    msg_info "The installer also put these on this host:"

    for helper in /usr/local/sbin/adr-doctor /usr/local/sbin/adr-setup-nas; do
        [[ -e "$helper" ]] || continue
        if confirm "Remove ${helper}?"; then
            rm -f "$helper"
            msg_ok "Removed ${helper}"
        else
            msg_info "Left ${helper} in place."
        fi
    done

    if [[ -f "$DROPIN" ]]; then
        if confirm "Remove the guest-startup ordering drop-in (${DROPIN})?"; then
            rm -f "$DROPIN"
            rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
            systemctl daemon-reload 2>/dev/null || true
            msg_ok "Removed ${DROPIN}"
        else
            msg_info "Left ${DROPIN} in place."
        fi
    fi

    if grep -q "Automatic Disc Ripper" /etc/fstab 2>/dev/null; then
        echo
        msg_info "This line is in /etc/fstab:"
        grep -n -A1 "Automatic Disc Ripper" /etc/fstab | sed 's/^/      /'
        if confirm "Remove it (the share is unmounted, nothing on the NAS is touched)?"; then
            # A dated copy first. Editing fstab wrongly is the kind of mistake
            # that shows up at the next boot, by which time the original is the
            # only thing that would have helped.
            cp /etc/fstab "/etc/fstab.adr-uninstall.$(date +%Y%m%d%H%M%S)"
            target="$(awk '/Automatic Disc Ripper/{getline; print $2}' /etc/fstab | head -1)"
            if [[ -n "$target" ]]; then
                umount "$target" 2>/dev/null || true
            fi
            sed -i '/Automatic Disc Ripper/{N;d;}' /etc/fstab
            systemctl daemon-reload 2>/dev/null || true
            msg_ok "Removed the fstab entry (a backup of the old file is beside it)."
        else
            msg_info "Left /etc/fstab alone."
        fi
    fi

    # Last, and asked in its own words: this one is a password sitting in a
    # file for an application that no longer exists.
    if [[ -f "$CREDS_FILE" ]]; then
        echo
        msg_warn "${CREDS_FILE} holds your NAS username and password."
        if confirm "Delete it?"; then
            rm -f "$CREDS_FILE"
            msg_ok "Deleted ${CREDS_FILE}"
        else
            msg_warn "Left ${CREDS_FILE} in place — it still holds the password."
        fi
    fi

    echo
    msg_ok "Done."
    exit 0
fi

# ---- Container mode: remove the services ------------------------------------
[[ $EUID -eq 0 ]] || {
    msg_error "Run as root inside the container, or pass a CTID on the host."
    exit 1
}

msg_info "Stopping and disabling services…"
# adr-update.path and its service arrive with in-app updating and are missed by
# any uninstall that only knows about adr.service.
for unit in adr.service adr-update.path adr-update.service; do
    systemctl disable --now "$unit" 2>/dev/null || true
    rm -f "/etc/systemd/system/${unit}"
done
systemctl daemon-reload
msg_ok "Services removed."

if [[ -d /opt/adr ]]; then
    # The hazard this check exists for: on installs made before 1.0,
    # /opt/adr/completed is a bind-mount of the user's media library. A
    # recursive delete would go straight through it and take the films with
    # it — an uninstall that destroys the thing it was ripping.
    mounted=()
    while IFS= read -r point; do mounted+=("$point"); done < <(
        findmnt -rn -o TARGET 2>/dev/null | grep '^/opt/adr/' || true)

    if [[ ${#mounted[@]} -gt 0 ]]; then
        echo
        msg_warn "Something is mounted inside /opt/adr:"
        printf '      %s\n' "${mounted[@]}"
        msg_warn "Deleting the directory would delete through the mount. Unmount"
        msg_warn "it first if you want /opt/adr gone:"
        printf '        umount %s\n' "${mounted[@]}"
        msg_info "Leaving /opt/adr in place."
    elif confirm "Also delete /opt/adr (config, database, and anything in completed/)?"; then
        rm -rf /opt/adr
        msg_ok "/opt/adr removed."
    else
        msg_info "Left /opt/adr in place."
    fi
fi

echo
msg_ok "Uninstall complete."
