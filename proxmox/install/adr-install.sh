#!/usr/bin/env bash
# In-container installer for Automatic Disc Ripper.
# Runs inside a fresh Debian 12 LXC and sets up everything needed to
# rip discs: system packages, MakeMKV (built from source), the ADR
# app itself, and a systemd service.

set -euo pipefail

msg() { printf '\n\033[1;36m>> %s\033[0m\n' "$*"; }

REPO_URL="${REPO_URL:-https://github.com/hhammarstrand/Automatic-Disc-Ripper-proxmox.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/adr}"
MAKEMKV_VERSION="${MAKEMKV_VERSION:-1.17.9}"

msg "Installing system dependencies"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ca-certificates curl git \
    python3 python3-venv python3-pip \
    handbrake-cli eject util-linux udev \
    build-essential pkg-config \
    libc6-dev libssl-dev libexpat1-dev \
    zlib1g-dev libbz2-dev liblzma-dev \
    qtbase5-dev libqt5svg5-dev

msg "Building MakeMKV ${MAKEMKV_VERSION} from source"
build_makemkv() {
    local tmp
    tmp=$(mktemp -d)
    cd "$tmp"
    curl -fsSLO "https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz"
    curl -fsSLO "https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz"
    tar xzf "makemkv-oss-${MAKEMKV_VERSION}.tar.gz"
    tar xzf "makemkv-bin-${MAKEMKV_VERSION}.tar.gz"
    (cd "makemkv-oss-${MAKEMKV_VERSION}" && ./configure --prefix=/usr >/dev/null && make -j"$(nproc)" && make install)
    # The -bin package requires accepting the EULA. The env var below
    # simulates the "accept" prompt used by upstream's Makefile.
    mkdir -p "makemkv-bin-${MAKEMKV_VERSION}/tmp"
    echo "accepted" > "makemkv-bin-${MAKEMKV_VERSION}/tmp/eula_accepted"
    (cd "makemkv-bin-${MAKEMKV_VERSION}" && make PREFIX=/usr -j"$(nproc)" && make PREFIX=/usr install)
    cd / && rm -rf "$tmp"
}
if command -v makemkvcon >/dev/null; then
    msg "makemkvcon already installed — skipping build"
else
    build_makemkv
fi

msg "Fetching ADR from $REPO_URL ($REPO_BRANCH)"
if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --depth=1 origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" checkout "$REPO_BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"
else
    git clone --depth=1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

msg "Running install.sh"
chmod +x "$INSTALL_DIR/install.sh"
"$INSTALL_DIR/install.sh"

msg "Install complete. Service status:"
systemctl --no-pager --lines=10 status adr.service || true
