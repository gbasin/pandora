#!/bin/bash
set -uo pipefail
mkdir -p /workspace/source /workspace/results
cd /workspace/source
sha256sum /input/source.tar.gz > /workspace/results/source.sha256
if ! tar -xf /input/source.tar.gz; then exit 70; fi
printf 'installing\n' > /workspace/results/phase
pnpm install --frozen-lockfile
status=$?
if [ "$status" -eq 0 ]; then
  printf 'testing\n' > /workspace/results/phase
  # Same fixture-build and test entry point used by the surface CI lane.
  # Explicitly serial until the measured memory peak justifies more capacity.
  PLAYWRIGHT_JUNIT_OUTPUT_FILE=/workspace/results/junit.xml \
    pnpm --filter @eichler/borrower-web test:e2e "$@" --workers=1 --reporter=line,junit
  status=$?
fi
printf '%s\n' "$status" > /workspace/results/exit-code
cat /sys/fs/cgroup/memory.peak > /workspace/results/memory-peak-bytes
cat /sys/fs/cgroup/memory.events > /workspace/results/memory-events
cat /sys/fs/cgroup/cpu.stat > /workspace/results/cpu-stat
if [ -d apps/borrower-web/test-results ]; then
  cp -R apps/borrower-web/test-results /workspace/results/playwright
fi
printf 'finished\n' > /workspace/results/phase
exit "$status"
