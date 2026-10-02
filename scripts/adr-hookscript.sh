#!/usr/bin/env bash
#
# adr-hookscript — Proxmox hookscript for an Automatic Disc Ripper container.
# Runs on the PROXMOX HOST; Proxmox calls it at every phase of the container's
# life as  adr-hookscript.sh <CTID> <phase>.  Only pre-start does anything.
# Installed by 'adr-doctor --fix'.
#
# A bind-mount is captured when the container starts. If the media share is not
# mounted on the host at that moment, the container binds the bare directory
# underneath it instead, and keeps that until it is restarted — mounting the
# share afterwards does not reach a running container. It happens after a power
# cut: the host boots faster than the NAS, the fstab entry is 'nofail' and gives
# up after 30 seconds, and guests start anyway. The ripper then refuses every
# disc, for as long as nobody restarts it.
#
# So before the container starts, every bind-mount source that is meant to be a
# mount — an fstab entry, or a storage under /mnt/pve — is mounted, retrying for
# a while, and the start is refused if it never appears. A container that did
# not start is noticed. One that started on the wrong directory is not.
#
set -u

VMID="${1:-}"
PHASE="${2:-}"
[[ "$PHASE" == pre-start ]] || exit 0

WAIT_SECONDS="${ADR_SHARE_WAIT:-300}"
CONF="${ADR_LXC_CONF_DIR:-/etc/pve/lxc}/${VMID}.conf"
FSTAB="${ADR_FSTAB:-/etc/fstab}"
STORAGE_CFG="${ADR_STORAGE_CFG:-/etc/pve/storage.cfg}"

say() { echo "adr-hookscript: $*" >&2; }

# Whether Proxmox storage <id> is something mounted, from storage.cfg.
storage_is_mounted() {
    awk -v id="$1" '
        /^[a-z]+:[[:space:]]/ { type = $1; sub(/:$/, "", type); name = $2; next }
        name == id && $1 == "is_mountpoint" && $2 != "no" && $2 != "0" { mp = 1 }
        name == id { seen = 1; t = type }
        END {
            if (!seen && t == "") { exit 1 }
            if (t ~ /^(nfs|cifs|cephfs|glusterfs)$/ || mp) { exit 0 }
            exit 1
        }' "$STORAGE_CFG" 2>/dev/null
}

# What has to be mounted for *src* to be the real thing, as kind:storage:target.
# Prints nothing for a path that is simply meant to be a directory on the host.
expected_mount() {
    local src="$1" best="" target
    if [[ "$src" == /mnt/pve/* ]]; then
        local id="${src#/mnt/pve/}"
        id="${id%%/*}"
        # Only a storage that is mounted is waited for: a network one, or a
        # directory storage marked is_mountpoint. A plain directory storage
        # under /mnt/pve never becomes a mount point, and waiting for it held
        # the container for five minutes and then refused it, every boot.
        if storage_is_mounted "$id"; then
            echo "pve:${id}:/mnt/pve/${id}"
        fi
        return
    fi
    # The longest fstab target that is src or a parent of it. "/" covers every
    # path and says nothing, so it never counts.
    while read -r _ target _; do
        [[ "$target" == /?* ]] || continue
        target="${target%/}"
        if [[ "$src" == "$target" || "$src" == "$target"/* ]] \
                && (( ${#target} > ${#best} )); then
            best="$target"
        fi
    done < <(grep -vE '^[[:space:]]*(#|$)' "$FSTAB" 2>/dev/null)
    [[ -n "$best" ]] && echo "fstab::${best}"
}

[[ -r "$CONF" ]] || exit 0

pending=()
while IFS= read -r line; do
    [[ "$line" == \[* ]] && break          # snapshots follow; they are not this start
    [[ "$line" =~ ^mp[0-9]+:[[:space:]]*([^,]+) ]] || continue
    src="${BASH_REMATCH[1]}"
    [[ "$src" == /* ]] || continue         # a volume on a storage, not a bind-mount
    m="$(expected_mount "$src")"
    [[ -n "$m" ]] && pending+=("$m")
done < "$CONF"

deadline=$((SECONDS + WAIT_SECONDS))
for m in "${pending[@]}"; do
    kind="${m%%:*}"
    rest="${m#*:}"
    storage="${rest%%:*}"
    target="${rest#*:}"
    until mountpoint -q "$target"; do
        if [[ "$kind" == pve ]]; then
            # Activating a storage is what mounts it.
            pvesm status --storage "$storage" >/dev/null 2>&1 || true
        else
            mount "$target" >/dev/null 2>&1 || true
        fi
        mountpoint -q "$target" && break
        if (( SECONDS >= deadline )); then
            say "${target} is still not mounted after ${WAIT_SECONDS}s, so CT ${VMID} is not started:"
            say "it would bind the empty directory underneath and refuse every disc."
            say "Mount it (mount ${target}), then: pct start ${VMID}"
            exit 1
        fi
        say "waiting for ${target} to be mounted…"
        sleep 10
    done
done
exit 0
