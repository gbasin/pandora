#!/usr/bin/env bash
# Prove what a run owns and that killing it leaves nothing.
#   inventory            list every host object this spike created
#   reap <n>             SIGKILL vm n, then remove its tap, NAT rules, dm device, loops
#   reap-all             every vm dir under run/
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
cd "$SPIKE"

inventory() {
  echo "== firecracker processes"; pgrep -a firecracker || echo "(none)"
  echo "== fctap devices";        ip -br link show type tun 2>/dev/null | grep '^fctap' || echo "(none)"
  echo "== iptables nat";         sudo iptables -t nat -S POSTROUTING | grep '172\.30\.' || echo "(none)"
  echo "== iptables filter";      sudo iptables -S FORWARD | grep fctap || echo "(none)"
  echo "== device-mapper";        sudo dmsetup ls | grep '^fcrun' || echo "(none)"
  echo "== loop devices";         losetup -a | grep -E 'spike-fc|fcrun' || echo "(none)"
  echo "== mounts";               mount | grep -E 'spike-fc|fcrun' || echo "(none)"
  echo "== cow files";            ls -la /tmp/fcrun*.cow 2>/dev/null || echo "(none)"
}

reap() {
  n=$1
  if [ -f "run/vm$n/pid" ]; then
    pid=$(cat "run/vm$n/pid")
    kill -9 "$pid" 2>/dev/null && echo "SIGKILL $pid" || echo "pid $pid already gone"
  fi
  sleep 0.5
  bash scripts/dmsnap.sh down "fcrun$n" || true
  python3 scripts/fcvm.py net down "$n" || true
  echo "reaped vm$n"
}

case "${1:-inventory}" in
  inventory) inventory ;;
  reap) reap "$2" ;;
  reap-all)
    for d in run/vm*; do reap "${d#run/vm}"; done
    inventory ;;
esac
