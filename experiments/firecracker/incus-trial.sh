#!/usr/bin/env bash
# Incus system containers as the cheaper cousin of a microVM: the run owns a
# private dockerd and localhost just works, without a VM.
#
#   net           create the spike's own bridge and profile
#   golden        build the warm instance (docker + node + pnpm + eichler + deps + images)
#   clone <name>  copy the golden instance, timed
#   run <name>    journey S0-01 inside an instance, timed
#   limits <name> what limits.memory / limits.cpu actually write to the cgroup
#   nuke          remove everything this script created
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
POOL=${POOL:-fcpool}
BRIDGE=${BRIDGE:-incusbrfc}
I="sudo incus"
cd "$SPIKE"

case "${1:-}" in
net)
  $I network show "$BRIDGE" >/dev/null 2>&1 || \
    $I network create "$BRIDGE" ipv4.address=10.140.0.1/24 ipv4.nat=true ipv6.address=none
  $I profile show fc >/dev/null 2>&1 || $I profile create fc
  $I profile device add fc root disk path=/ pool="$POOL" 2>/dev/null || true
  $I profile device add fc eth0 nic network="$BRIDGE" name=eth0 2>/dev/null || true
  # the three keys an unprivileged system container needs to run dockerd
  $I profile set fc security.nesting=true
  $I profile set fc security.syscalls.intercept.mknod=true
  $I profile set fc security.syscalls.intercept.setxattr=true
  $I profile show fc
  ;;

golden)
  name=fc-golden
  t0=$(date +%s.%N)
  $I launch images:ubuntu/26.04 "$name" -p fc
  for _ in $(seq 1 120); do
    $I exec "$name" -- test -e /run/systemd/system && break; sleep 0.5
  done
  t1=$(date +%s.%N)
  echo "launch: $(echo "$t1-$t0"|bc)s"

  $I exec "$name" -- bash -c '
    set -e
    export DEBIAN_FRONTEND=noninteractive
    # the bridge is IPv4-only but dnsmasq hands back AAAA records
    echo "Acquire::ForceIPv4 \"true\";" > /etc/apt/apt.conf.d/99force-ipv4
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
      docker.io docker-compose-v2 docker-buildx ca-certificates curl xz-utils git python3 jq
    curl -4 -fsSL https://nodejs.org/dist/v24.9.0/node-v24.9.0-linux-x64.tar.xz \
      | tar -xJ -C /usr/local --strip-components=1
    npm install -g pnpm@12.3.4 >/dev/null
    node -v; pnpm -v; docker --version'
  t2=$(date +%s.%N)
  echo "toolchain: $(echo "$t2-$t1"|bc)s"

  # eichler source, straight from the host tree, no image build
  tar -C "$SPIKE/eichler" -cf - . | $I exec "$name" -- bash -c 'mkdir -p /work && tar -C /work -xf -'
  t3=$(date +%s.%N)
  echo "source: $(echo "$t3-$t2"|bc)s"

  $I exec "$name" -- bash -c '
    set -e
    docker info | grep -E "Storage Driver|Cgroup Version"
    cd /work && pnpm install --frozen-lockfile 2>&1 | tail -2
    for i in postgres:16 edoburu/pgbouncer:latest ghcr.io/neondatabase/wsproxy:latest; do
      docker pull -q "$i"; done
    docker images --format "{{.Repository}}"'
  t4=$(date +%s.%N)
  echo "deps+images: $(echo "$t4-$t3"|bc)s"
  $I stop "$name"
  $I snapshot create "$name" warm
  t5=$(date +%s.%N)
  echo "stop+snapshot: $(echo "$t5-$t4"|bc)s   total: $(echo "$t5-$t0"|bc)s"
  ;;

clone)
  name=$2
  t0=$(date +%s.%N)
  $I copy fc-golden/warm "$name"
  t1=$(date +%s.%N)
  $I start "$name"
  for _ in $(seq 1 200); do $I exec "$name" -- test -e /run/systemd/system && break; sleep 0.2; done
  t2=$(date +%s.%N)
  printf 'incus-clone %s\tcopy=%.2fs\tstart_to_ready=%.2fs\tdisk=%s\n' "$name" \
    "$(echo "$t1-$t0"|bc)" "$(echo "$t2-$t1"|bc)" \
    "$($I storage volume info "$POOL" container/"$name" 2>/dev/null | awk '/Usage/{print $2$3}')"
  ;;

run)
  name=$2
  t0=$(date +%s.%N)
  $I exec "$name" -- bash -c 'systemctl start docker; for i in $(seq 60); do docker info >/dev/null 2>&1 && break; sleep 0.3; done; echo dockerready'
  t1=$(date +%s.%N)
  set +e
  $I exec "$name" --env JOURNEY_REPLAY=cover -- bash -c \
    'cd /work && node tools/validation/journey-runner.mjs run S0-01' > "logs/incus-$name.log" 2>&1
  rc=$?
  set -e
  t2=$(date +%s.%N)
  peak=$(sudo cat /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.peak 2>/dev/null \
      || sudo cat /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.current 2>/dev/null || echo 0)
  printf 'incus-run %s\trc=%s\tdocker_ready=%.2fs\tjourney=%.2fs\tmem_peak=%sMiB\n' \
    "$name" "$rc" "$(echo "$t1-$t0"|bc)" "$(echo "$t2-$t1"|bc)" "$((peak/1048576))"
  grep -aE "S0-01:|stage-routes" "logs/incus-$name.log" || true
  ;;

limits)
  name=$2
  $I config set "$name" limits.memory=1GiB
  $I config set "$name" limits.memory.enforce=soft
  echo "--- enforce=soft"
  sudo cat /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.max \
           /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.high 2>/dev/null
  $I config set "$name" limits.memory.enforce=hard
  echo "--- enforce=hard"
  sudo cat /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.max \
           /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.high 2>/dev/null
  $I config set "$name" limits.cpu.allowance=50%
  echo "--- cpu.allowance=50%"
  sudo cat /sys/fs/cgroup/incus.slice/incus-"$name".scope/cpu.max \
           /sys/fs/cgroup/incus.slice/incus-"$name".scope/cpu.weight 2>/dev/null
  $I config set "$name" limits.cpu.allowance=100ms/200ms 2>/dev/null || true
  echo "--- OOM containment: allocate 2 GiB against a 1 GiB hard cap"
  $I exec "$name" -- bash -c 'node -e "const a=[];for(;;){a.push(Buffer.alloc(64*1024*1024));}"' 2>&1 | tail -3 || true
  echo "exit=$?"
  sudo cat /sys/fs/cgroup/incus.slice/incus-"$name".scope/memory.events 2>/dev/null
  ;;

nuke)
  for i in $($I list -c n --format csv 2>/dev/null | grep -E '^fc-'); do
    $I delete -f "$i" || true
  done
  $I profile delete fc 2>/dev/null || true
  $I network delete "$BRIDGE" 2>/dev/null || true
  $I storage delete "$POOL" 2>/dev/null || true
  $I list; $I storage list; $I network list
  ;;
esac
