#!/bin/bash
# Prepares the host for one-job e2e VMs, as root, from a Pandora checkout:
#
#   sudo deploy/e2e-runner/setup-host.sh /opt/pandora-e2e/src
#
# Registers the bridge with the host firewall when it is installed, then creates
# the Incus project, a btrfs storage pool of its own (so each VM is a
# copy-on-write clone of the image), the bridge and its egress ACL, and the VM
# profile, and installs the pool daemon and its unit. Starts nothing and holds no
# credential. Safe to run again. E2E_EXTRA_EXCLUDE adds IPv4 addresses the
# VMs must not reach beyond the host's own interface addresses, for example
# OVH failover IPs routed to this server.
set -euo pipefail

SRC=${1:?usage: setup-host.sh <pandora checkout>}
HERE=$SRC/deploy/e2e-runner
PROJECT=pandora-e2e
PROFILE=pandora-e2e
BRIDGE=pandorae2ebr0
SUBNET=${E2E_SUBNET:-10.142.0.1/24}
ACL=pandora-e2e-vm
POOL=pandora-e2e
POOL_SIZE=${E2E_POOL_SIZE:-120GiB}
FIREWALL=/etc/eichler-host-firewall
VM_CPU=${E2E_VM_CPU:-4}
VM_MEMORY=${E2E_VM_MEMORY:-8GiB}
VM_DISK=${E2E_VM_DISK:-40GiB}

[ "$(id -u)" -eq 0 ] || { echo 'setup-host.sh: run as root' >&2; exit 64; }

if ! incus project show "$PROJECT" >/dev/null 2>&1; then
  incus project create "$PROJECT" -c features.images=true -c features.profiles=true \
    -c features.storage.volumes=true -c features.networks=false
fi

# The host firewall drops guest traffic to the host except DHCP and DNS on
# bridges in its guest_bridges set. Register the bridge before it exists.
if [ -d "$FIREWALL" ]; then
  install -d -m 0755 "$FIREWALL/local.d"
  printf 'add element inet eichler_host guest_bridges { "%s" }\n' "$BRIDGE" \
    > "$FIREWALL/local.d/bridge-$BRIDGE.nft"
  systemctl reload eichler-host-firewall
fi

if ! incus storage show "$POOL" >/dev/null 2>&1; then
  incus storage create "$POOL" btrfs size="$POOL_SIZE"
fi

if ! incus network show "$BRIDGE" >/dev/null 2>&1; then
  incus network create "$BRIDGE" ipv4.address="$SUBNET" ipv4.nat=true ipv6.address=none
fi
incus network acl show "$ACL" >/dev/null 2>&1 || incus network acl create "$ACL"
mapfile -t OWN < <(ip -4 -o addr show scope global | awk '{split($4, a, "/"); print a[1]}')
EXCLUDE=()
for address in "${OWN[@]}" ${E2E_EXTRA_EXCLUDE:-}; do EXCLUDE+=(--exclude "$address"); done
python3 -I "$HERE/e2e_pool.py" acl "${EXCLUDE[@]}" | incus network acl edit "$ACL"
incus network set "$BRIDGE" security.acls="$ACL" \
  security.acls.default.egress.action=reject security.acls.default.ingress.action=reject

incus --project "$PROJECT" profile show "$PROFILE" >/dev/null 2>&1 \
  || incus --project "$PROJECT" profile create "$PROFILE"
incus --project "$PROJECT" profile edit "$PROFILE" <<EOF
description: Pandora e2e one-job VM
config:
  limits.cpu: "$VM_CPU"
  limits.memory: $VM_MEMORY
  boot.autostart: "false"
devices:
  root:
    type: disk
    path: /
    pool: $POOL
    size: $VM_DISK
  eth0:
    type: nic
    name: eth0
    network: $BRIDGE
    security.port_isolation: "true"
EOF

install -d -m 0700 /opt/pandora-e2e /etc/pandora-e2e
install -m 0500 "$HERE/e2e_pool.py" /opt/pandora-e2e/e2e_pool.py
install -m 0644 "$HERE/pandora-e2e-pool.service" /etc/systemd/system/pandora-e2e-pool.service
[ -f /etc/pandora-e2e/pool.toml ] || install -m 0600 "$HERE/pool.example.toml" /etc/pandora-e2e/pool.toml
systemctl daemon-reload
echo "setup-host.sh: ready. The pool is installed but not enabled."
