#!/usr/bin/env bash
# Proxmox LXC installer for Automatic Disc Ripper.
#
# Run from the Proxmox host shell:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/proxmox/ct/adr.sh)"
#
# Creates a privileged Debian 12 LXC, passes through /dev/sr0, installs
# HandBrakeCLI and MakeMKV, drops in the ADR app, and enables the
# systemd service.
#
# Environment overrides (all optional):
#   CTID=...           ID for the new container (default: pvesh-suggested)
#   HOSTNAME=adr       hostname of the new container
#   STORAGE=local-lvm  root-fs storage pool
#   BRIDGE=vmbr0       network bridge
#   DISK_GB=20         root disk size
#   CORES=2
#   RAM_MB=2048
#   OPTICAL_DEV=/dev/sr0

set -euo pipefail

msg() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run this script as root on the Proxmox host."
command -v pveversion >/dev/null || die "This doesn't look like a Proxmox host (no pveversion)."

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
HOSTNAME="${HOSTNAME:-adr}"
STORAGE="${STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
DISK_GB="${DISK_GB:-20}"
CORES="${CORES:-2}"
RAM_MB="${RAM_MB:-2048}"
OPTICAL_DEV="${OPTICAL_DEV:-/dev/sr0}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"

if [[ ! -e "$OPTICAL_DEV" ]]; then
    die "Optical device $OPTICAL_DEV not found on host. Set OPTICAL_DEV=... to override."
fi

msg "Refreshing LXC templates"
pveam update >/dev/null

TEMPLATE=$(pveam available -section system | awk '/debian-12-standard/ {print $2}' | tail -n1)
[[ -n "$TEMPLATE" ]] || die "No debian-12-standard template available."

if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
    msg "Downloading template $TEMPLATE"
    pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

msg "Creating LXC $CTID ($HOSTNAME)"
ROOT_PASSWORD=$(openssl rand -base64 16)
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$HOSTNAME" \
    --cores "$CORES" \
    --memory "$RAM_MB" \
    --swap 512 \
    --rootfs "${STORAGE}:${DISK_GB}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp,firewall=0" \
    --features nesting=1 \
    --unprivileged 0 \
    --onboot 1 \
    --password "$ROOT_PASSWORD" \
    --ostype debian

CONF="/etc/pve/lxc/${CTID}.conf"
msg "Patching $CONF for optical-drive passthrough"

# cdrom major 11, scsi-generic major 21 (needed for MakeMKV to issue
# SCSI commands to the drive).
cat >>"$CONF" <<EOF
# --- added by adr.sh ---
lxc.cgroup2.devices.allow: c 11:* rwm
lxc.cgroup2.devices.allow: c 21:* rwm
lxc.mount.entry: ${OPTICAL_DEV} dev/${OPTICAL_DEV#/dev/} none bind,optional,create=file 0 0
EOF

msg "Starting container"
pct start "$CTID"

# pct exec may race with cloud-init on first boot; retry briefly.
for _ in 1 2 3 4 5; do
    if pct exec "$CTID" -- true 2>/dev/null; then break; fi
    sleep 2
done

msg "Running in-container installer"
INSTALL_URL="https://raw.githubusercontent.com/hhammarstrand/Automatic-Disc-Ripper-proxmox/main/proxmox/install/adr-install.sh"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates"
pct exec "$CTID" -- bash -c "curl -fsSL '$INSTALL_URL' | bash"

IP=$(pct exec "$CTID" -- hostname -I | awk '{print $1}')

cat <<EOF

-------------------------------------------------------------
  Automatic Disc Ripper LXC is ready.

  CTID:      $CTID
  Hostname:  $HOSTNAME
  Root pw:   $ROOT_PASSWORD
  Web UI:    http://${IP}:8080
  Optical:   $OPTICAL_DEV (passed from host)

  Check status:  pct exec $CTID -- systemctl status adr.service
  Follow logs:   pct exec $CTID -- journalctl -u adr.service -f
-------------------------------------------------------------
EOF
