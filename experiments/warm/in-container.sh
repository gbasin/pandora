#!/bin/bash
set -uo pipefail
mkdir -p /workspace/results
cd /workspace/source
printf 'readiness\n' > /workspace/results/phase
node tools/check-worktree-deps.mjs
status=$?
if [ "$status" -eq 0 ]; then
  printf 'testing\n' > /workspace/results/phase
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
