#!/bin/bash
set -uo pipefail

app="${PANDORA_SURFACE_APP:-borrower-web}"
case "$app" in
  borrower-web)
    package='@eichler/borrower-web'
    app_root='apps/borrower-web'
    ;;
  desk)
    package='@eichler/desk'
    app_root='apps/desk'
    ;;
  *)
    printf 'Unsupported PANDORA_SURFACE_APP: %s\n' "$app" >&2
    exit 64
    ;;
esac

workspace_root="${PANDORA_WORKSPACE_ROOT:-/workspace}"
results_root="$workspace_root/results"
cgroup_root="${PANDORA_CGROUP_ROOT:-/sys/fs/cgroup}"
node_bin="${PANDORA_NODE_BIN:-node}"
pnpm_bin="${PANDORA_PNPM_BIN:-pnpm}"

mkdir -p "$results_root"
printf '{"app":"%s"}\n' "$app" > "$results_root/surface.json"
cd "$workspace_root/source"
printf 'readiness\n' > "$results_root/phase"
"$node_bin" tools/check-worktree-deps.mjs
status=$?
if [ "$status" -eq 0 ]; then
  printf 'testing\n' > "$results_root/phase"
  PLAYWRIGHT_JUNIT_OUTPUT_FILE="$results_root/junit.xml" \
    "$pnpm_bin" --filter "$package" test:e2e "$@" --workers=1 --reporter=line,junit \
      --output="$results_root/playwright"
  status=$?
fi
printf '%s\n' "$status" > "$results_root/exit-code"
cat "$cgroup_root/memory.peak" > "$results_root/memory-peak-bytes"
cat "$cgroup_root/memory.events" > "$results_root/memory-events"
cat "$cgroup_root/cpu.stat" > "$results_root/cpu-stat"
if [ -d "$app_root/test-results" ] && [ ! -d "$results_root/playwright" ]; then
  cp -R "$app_root/test-results" "$results_root/playwright"
fi
if [ "$status" -eq 0 ]; then
  for output in "$app_root/dist" "$app_root/e2e/dist"; do
    mkdir -p "$results_root/outputs/$(dirname "$output")"
    cp -R "$output" "$results_root/outputs/$output" || status=70
  done
  printf '%s\n' "$status" > "$results_root/exit-code"
fi
printf 'finished\n' > "$results_root/phase"
exit "$status"
