#!/bin/bash
# Run probe.sh inside a Codex shell tool under one sandbox mode.
# Usage: run-probe.sh <label> [extra codex args...]
set -u
WT=/Users/you/Code/pandora-wt/codex-sandbox-probe
PROBE=$WT/experiments/codex-sandbox
SHIM=/Users/you/.local/state/pandora-probe/shimbin
label=$1; shift

PROMPT="Run exactly this command once, with no edits and no retries, then print its complete stdout verbatim in your final message:

bash $PROBE/probe.sh

Do not summarise, do not fix any failing line, do not run anything else. Failures are the expected data."

export PATH="$SHIM:$PATH"
export PANDORA_PROBE_VAR=probe-var-ok
export PANDORA_STATE=/Users/you/.local/state/pandora-probe
export PANDORA_PROBE_TOKEN=probe-token-value
export PANDORA_PROBE_SECRET=probe-secret-value

set -x
codex exec -C "$WT" --skip-git-repo-check -c 'notify=[]' \
  -c 'model_reasoning_effort="low"' "$@" "$PROMPT" \
  > "$PROBE/results/$label.txt" 2>&1 < /dev/null
rc=$?
set +x
echo "exit=$rc log=$PROBE/results/$label.txt"
