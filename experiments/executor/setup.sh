#!/usr/bin/env bash
# Host preparation for the Incus executor driver. Everything this creates is
# named after Pandora and lives in its own project, so `teardown` is exact.
#
#   install   incus + btrfs-progs from the distro archive
#   init      btrfs pool on a loop file, a dedicated bridge, project `pandora`
#   show      what exists now
#   teardown  remove the project, the bridge and the pool (not the package)
set -euo pipefail
ROOT=${ROOT:-$HOME/incus-exec}
POOL=${POOL:-pandorapool}
BRIDGE=${BRIDGE:-pandorabr0}
PROJECT=${PROJECT:-pandora}
PROFILE=${PROFILE:-runner}
POOL_FILE=$ROOT/pool.img
POOL_SIZE=${POOL_SIZE:-18G}
SUBNET=${SUBNET:-10.141.0.1/24}
I="sudo incus"

case "${1:-show}" in
install)
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y incus btrfs-progs
  incus --version
  ;;

init)
  mkdir -p "$ROOT/logs"
  # Incus needs a database before anything else; --minimal makes no pool we use.
  $I admin init --minimal >/dev/null 2>&1 || true

  # btrfs on a loop file: the host root is ext4, so there is no reflink, and
  # `incus copy` of a golden instance wants a snapshotting backend.
  [ -f "$POOL_FILE" ] || truncate -s "$POOL_SIZE" "$POOL_FILE"
  lo=$(losetup -j "$POOL_FILE" | cut -d: -f1 | head -1)
  [ -n "$lo" ] || lo=$(sudo losetup --find --show "$POOL_FILE")
  echo "pool loop: $lo"
  $I storage show "$POOL" >/dev/null 2>&1 || $I storage create "$POOL" btrfs source="$lo"

  # One bridge of our own. The managed dnsmasq answers AAAA on an IPv4-only
  # bridge, so instances force IPv4 for apt/curl (see golden build).
  $I network show "$BRIDGE" >/dev/null 2>&1 || \
    $I network create "$BRIDGE" ipv4.address="$SUBNET" ipv4.nat=true ipv6.address=none
  # The host FORWARD policy is DROP with only Docker's jumps installed.
  sudo iptables -C FORWARD -i "$BRIDGE" -j ACCEPT 2>/dev/null || \
    sudo iptables -I FORWARD -i "$BRIDGE" -j ACCEPT
  sudo iptables -C FORWARD -o "$BRIDGE" -j ACCEPT 2>/dev/null || \
    sudo iptables -I FORWARD -o "$BRIDGE" -j ACCEPT

  $I project show "$PROJECT" >/dev/null 2>&1 || \
    $I project create "$PROJECT" -c features.images=true -c features.profiles=true \
       -c features.storage.volumes=true -c features.networks=false

  P="$I --project $PROJECT"
  $P profile show "$PROFILE" >/dev/null 2>&1 || $P profile create "$PROFILE"
  $P profile device add "$PROFILE" root disk path=/ pool="$POOL" 2>/dev/null || true
  $P profile device add "$PROFILE" eth0 nic network="$BRIDGE" name=eth0 2>/dev/null || true
  # The three keys an unprivileged system container needs to run dockerd,
  # plus the idmap isolation the design asks for.
  $P profile set "$PROFILE" security.nesting=true
  $P profile set "$PROFILE" security.syscalls.intercept.mknod=true
  $P profile set "$PROFILE" security.syscalls.intercept.setxattr=true
  $P profile set "$PROFILE" security.idmap.isolated=true
  $P profile show "$PROFILE"
  ;;

show)
  $I storage list; $I network list; $I project list
  $I --project "$PROJECT" list || true
  ;;

teardown)
  P="$I --project $PROJECT"
  for i in $($P list -c n --format csv 2>/dev/null); do $P delete -f "$i" || true; done
  $P profile delete "$PROFILE" 2>/dev/null || true
  $I project delete "$PROJECT" 2>/dev/null || true
  $I network delete "$BRIDGE" 2>/dev/null || true
  $I storage delete "$POOL" 2>/dev/null || true
  lo=$(losetup -j "$POOL_FILE" | cut -d: -f1 | head -1)
  [ -z "$lo" ] || sudo losetup -d "$lo"
  echo "--- after teardown"
  $I storage list; $I network list; $I project list
  ;;

*) echo "usage: $0 install|init|show|teardown" >&2; exit 2 ;;
esac
