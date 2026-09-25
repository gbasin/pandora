#!/bin/sh
# Live-worker proof of the teammate gateway (#156): SSH_ORIGINAL_COMMAND ->
# bin/gateway -> the engine, over real sshd, against the e2e worker
# pandora-ci@localhost. Runs on the self-hosted e2e runner, after the selftest
# job has proven the admin path.
#
# The runner needs these files once, provisioned by hand like the e2e worker
# itself (see "Sharing a worker" in docs/worker.md):
#
#   ~/.ssh/pandora-e2e-user          a teammate keypair (ssh-keygen -t ed25519)
#   ~/.ssh/pandora-e2e-user.pub      declared in the e2e worker's versions.toml:
#                                    [[users]] name = "pandora-ci-user",
#                                    role = "user", key = "<this file>" -- then
#                                    `pandora worker provision` on that host
#   ~/.ssh/config                    an alias:
#                                    Host e2e-worker-user
#                                      HostName localhost
#                                      User pandora-ci
#                                      IdentityFile ~/.ssh/pandora-e2e-user
#                                      IdentitiesOnly yes
#   ~/.config-pandora-e2e-user.toml  the e2e client config with
#                                    [worker] host = "pandora-ci@e2e-worker-user"
#
# ENGINE_ROOT names the e2e worker's engine root as the worker sees it
# (default /home/pandora-ci/pandora-engine). Until the files exist this script
# reports a skipped gate and exits 0: green before the runner is ready, a real
# gate after.

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
PANDORA="$ROOT/bin/pandora"
ALIAS=e2e-worker-user
PIN=pandora-ci-user
KEY=${PANDORA_E2E_USER_KEY:-$HOME/.ssh/pandora-e2e-user}
CONFIG=${PANDORA_E2E_USER_CONFIG:-$HOME/.config-pandora-e2e-user.toml}
ENGINE_ROOT=${PANDORA_E2E_ENGINE_ROOT:-/home/pandora-ci/pandora-engine}

SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $ALIAS"

if [ ! -f "$KEY" ] || [ ! -f "$CONFIG" ]; then
  echo "::notice::e2e-gateway: no teammate key or config on this runner; the" \
       "live gateway gate is skipped until provisioned (see script header)"
  exit 0
fi

fail() { echo "::error::e2e-gateway: $*" >&2; exit 1; }

# --- a gatewayed key admits nothing else ------------------------------------
# Each must fail, and each refusal must name the pinned client.
for probe in \
    "id" \
    "sh -c 'echo hi'" \
    "python3 -c 'print(1)'" \
    "rsync --server -a . /etc/" \
    ; do
  out=$($SSH "$probe" 2>&1) || rc=$?
  rc=${rc:-0}
  case "$out" in
    *"pandora-gateway: refused as $PIN"*) ;;
    *) fail "probe '$probe' was not refused (exit $rc): $out" ;;
  esac
done

# --- the client's own verbs pass --------------------------------------------
# `worker status` goes through bundle.ensure and a read-only worker.service
# verb: an allowlisted python3 -c, a confined rsync when the bundle moves, and
# a module call -- the whole admitted surface except a run.
$PANDORA --config "$CONFIG" worker status >/dev/null \
  || fail "worker status through the gateway was refused"

# --- a full submit, attributed to the pin ------------------------------------
# selftest brings its own scratch daemon and claims client e2e-<host>; the
# ledger must record the pin instead.
$PANDORA --config "$CONFIG" selftest --timeout 1800 \
  || fail "selftest through the gateway failed"

digest=$(cd "$ROOT" && python3 -c \
         'from pandora.engine.bundle import payload; print(payload()[0])')
bundle="$ENGINE_ROOT/bundles/$digest"
stats=$($SSH "cd $bundle && PYTHONPATH=$bundle python3 -m pandora.engine.service --root $ENGINE_ROOT stats") \
  || fail "engine stats through the gateway was refused"
echo "$stats" | grep -q "\"$PIN\"" \
  || fail "the ledger names no run by the pinned client $PIN: $stats"

echo "e2e-gateway: shells refused, client verbs admitted, runs pinned to $PIN"
