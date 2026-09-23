#!/usr/bin/env bash
# Cheaper cousin: Incus system containers. Same property Firecracker buys —
# the run owns a private dockerd, localhost just works — without a VM.
# Its own bridge and its own btrfs pool on a loop file; the host dockerd is
# untouched (btrfs is needed because the host root is ext4: no reflink, and
# `incus copy` of a golden instance wants snapshots).
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
POOL=${POOL:-fcpool}
BRIDGE=${BRIDGE:-incusbrfc}
POOL_FILE=$SPIKE/incus-pool.img
POOL_SIZE=${POOL_SIZE:-14G}

case "${1:-install}" in
install)
  sudo apt-get install -y incus btrfs-progs >/dev/null
  sudo adduser "$USER" incus-admin >/dev/null 2>&1 || true
  incus --version
  ;;
init)
  # btrfs pool on a loop file inside our own directory
  [ -f "$POOL_FILE" ] || truncate -s "$POOL_SIZE" "$POOL_FILE"
  sudo incus admin init --minimal >/dev/null 2>&1 || true
  # Incus refuses a plain file on ext4; give it a loop block device instead.
  lo=$(losetup -j "$POOL_FILE" | cut -d: -f1)
  [ -n "$lo" ] || lo=$(sudo losetup --find --show "$POOL_FILE")
  echo "pool loop: $lo"
  sudo incus storage show "$POOL" >/dev/null 2>&1 || \
    sudo incus storage create "$POOL" btrfs source="$lo"
  sudo incus network show "$BRIDGE" >/dev/null 2>&1 || \
    sudo incus network create "$BRIDGE" ipv4.address=10.140.0.1/24 ipv4.nat=true ipv6.address=none
  sudo incus profile show fc >/dev/null 2>&1 || sudo incus profile create fc
  sudo incus profile device add fc root disk path=/ pool="$POOL" 2>/dev/null || true
  sudo incus profile device add fc eth0 nic network="$BRIDGE" name=eth0 2>/dev/null || true
  sudo incus profile set fc security.nesting=true
  sudo incus profile set fc security.syscalls.intercept.mknod=true
  sudo incus profile set fc security.syscalls.intercept.setxattr=true
  sudo incus storage list; sudo incus network list
  ;;
*) echo "usage: $0 install|init" >&2; exit 2 ;;
esac
