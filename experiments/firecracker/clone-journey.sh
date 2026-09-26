#!/usr/bin/env bash
# Run journey S0-01 in N restored clones at the same time and report each one's
# wall time, exit code and the host RSS of its firecracker process.
# Each clone already has dockerd up and acme's compose stack running on the
# SAME fixed ports (127.0.0.1:5432 / :5433) inside its own VM.
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
cd "$SPIKE"
CLONES=${*:-2 3}

for d in $CLONES; do
  # a restored clone wakes with the snapshot's clock; step it before anything
  # that checks token expiry runs
  bash scripts/snapclone.sh ssh "$d" "date -s '$(date -u '+%Y-%m-%d %H:%M:%S')' >/dev/null" || true
done

for d in $CLONES; do
  (
    t0=$(date +%s.%N)
    bash scripts/snapclone.sh ssh "$d" \
      "cd /work && export JOURNEY_REPLAY=cover && node tools/validation/journey-runner.mjs run S0-01" \
      > "logs/clone$d-s001.log" 2>&1
    rc=$?
    t1=$(date +%s.%N)
    printf 'clone%s\trc=%s\twall=%.1fs\trss=%sMiB\n' "$d" "$rc" "$(echo "$t1-$t0"|bc)" \
      "$(( $(awk '/VmRSS/{print $2}' "/proc/$(cat run/vm$d/pid)/status" 2>/dev/null || echo 0) / 1024 ))"
  ) &
done
wait
