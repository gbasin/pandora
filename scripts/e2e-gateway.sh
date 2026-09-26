#!/bin/sh
# Live-worker proof of the teammate gateway (#156), fully self-provisioning.
# Runs on the e2e runner after the selftest job: the runner's own admin key to
# pandora-ci@localhost installs a teammate key through `worker provision`, the
# probes exercise the forced command over real sshd, and a second provision
# revokes it again. Nothing has to exist on the runner beforehand.
#
# PANDORA_E2E_CONFIG names the e2e client config (same one selftest uses);
# PANDORA_E2E_SSH overrides how the admin side reaches the worker.

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
PANDORA="$ROOT/bin/pandora"
ADMIN_CONFIG=${PANDORA_E2E_CONFIG:-/home/gh-runner/.config-pandora-e2e.toml}
ALIAS=e2e-worker-user
PIN=pandora-ci-user

fail() { echo "::error::e2e-gateway: $*" >&2; exit 1; }

[ -f "$ADMIN_CONFIG" ] || {
    echo "::notice::e2e-gateway: no e2e client config at $ADMIN_CONFIG; skipping"
    exit 0
}

TMP=$(mktemp -d)
SSHC="$HOME/.ssh/config"
SSHC_MARK_BEGIN='# >>> pandora-e2e-gateway >>>'
SSHC_MARK_END='# <<< pandora-e2e-gateway <<<'
CLEANED=

cleanup() {
    [ -n "$CLEANED" ] && return 0
    CLEANED=1
    # Revocation is part of the test, but a failure must never leave a key
    # behind: re-provision the original manifest and lift the ssh alias.
    if [ -f "$TMP/versions.orig.toml" ] && [ -f "$TMP/key" ]; then
        "$PANDORA" --config "$ADMIN_CONFIG" worker provision \
            --versions "$TMP/versions.orig.toml" --no-canary >/dev/null 2>&1 \
            || echo "::warning::e2e-gateway: the revoking provision failed;" \
                    "the teammate key may still be on the worker"
    fi
    if [ -f "$SSHC" ]; then
        awk -v b="$SSHC_MARK_BEGIN" -v e="$SSHC_MARK_END" \
            '$0==b{f=1;next}$0==e{f=0;next}!f' "$SSHC" > "$SSHC.tmp" \
            && cat "$SSHC.tmp" > "$SSHC" && rm -f "$SSHC.tmp" || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT

# The worker's own declaration, as provision last left it. That is the honest
# base: the teammate entry is the only delta the test applies. The root is not
# always ~/pandora (the e2e worker keeps an own root), so it is discovered.
HOST=$(python3 - "$ADMIN_CONFIG" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], 'rb'))['worker']['host'])
PY
)
SSHA="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $HOST"
# shellcheck disable=SC2016   # $f and the glob expand on the remote side
manifest_path=${PANDORA_E2E_MANIFEST:-$($SSHA \
    'for f in ~/*/worker/versions.toml; do [ -f "$f" ] && { echo "$f"; break; }; done')}
[ -n "$manifest_path" ] || fail "no versions.toml under any worker root on $HOST"
WORKER_ROOT=$(dirname "$manifest_path" | xargs dirname)
$SSHA "cat $manifest_path" > "$TMP/versions.orig.toml" \
  || fail "cannot read the worker's manifest over the admin key"

# The engine root the gateway confines to, expanded as the worker sees it.
ER=$(python3 - "$TMP/versions.orig.toml" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], 'rb'))['worker'].get('engine_root',
                                                          '~/pandora-engine'))
PY
)
case "$ER" in
    \~/*) ER="$($SSHA 'cd ~ && pwd')/${ER#~/}" ;;
esac

ssh-keygen -q -t ed25519 -N '' -f "$TMP/key" || fail "keygen failed"
{
    cat "$TMP/versions.orig.toml"
    printf '\n[[users]]\nname = "%s"\nrole = "user"\nkey = "%s"\n' \
        "$PIN" "$(cat "$TMP/key.pub")"
} > "$TMP/versions.toml"

# --- provision the teammate in ----------------------------------------------
# This is itself part of the coverage: the manifest renders the managed
# authorized_keys block and installs bin/gateway on the worker.
"$PANDORA" --config "$ADMIN_CONFIG" worker provision \
    --versions "$TMP/versions.toml" --no-canary >/dev/null \
    || fail "provision of the [[users]] entry failed"

# The teammate's ssh alias and client config: same worker, other key.
mkdir -p "$HOME/.ssh" && touch "$SSHC"
{
    printf '%s\n' "$SSHC_MARK_BEGIN"
    printf 'Host %s\n  HostName localhost\n  User pandora-ci\n  IdentityFile %s\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' \
        "$ALIAS" "$TMP/key"
    printf '%s\n' "$SSHC_MARK_END"
} >> "$SSHC"

python3 - "$ADMIN_CONFIG" "$ALIAS" "$TMP" <<'PY'
import sys, tomllib
raw = tomllib.load(open(sys.argv[1], 'rb'))
worker = dict(raw.get('worker') or {})
worker['host'] = sys.argv[2]          # the alias carries user + key
state = sys.argv[3] + '/state'
with open(sys.argv[3] + '/config.toml', 'w') as out:
    out.write('[worker]\n')
    for key, value in worker.items():
        out.write('%s = "%s"\n' % (key, value))
    out.write('\n[client]\nstate = "%s"\n' % state)
PY

SSHG="ssh -o BatchMode=yes -o ConnectTimeout=10 $ALIAS"
USER_CONFIG="$TMP/config.toml"

# --- a gatewayed key admits nothing else ------------------------------------
for probe in \
    "id" \
    "sh -c 'echo hi'" \
    "python3 -c 'print(1)'" \
    "rsync --server -a . /etc/" \
    ; do
    out=$($SSHG "$probe" 2>&1) && rc=0 || rc=$?
    case "$out" in
        *"pandora-gateway: refused as $PIN"*) ;;
        *) fail "probe '$probe' was not refused (exit $rc): $out" ;;
    esac
done

# --- the client's own verbs pass --------------------------------------------
# `worker status` reaches the worker through bundle.ensure and a read-only
# worker.service verb: allowlisted feeds, a module call. Its own verdict may
# be not-ok for unrelated drift; what must not happen is a gateway refusal.
out=$("$PANDORA" --config "$USER_CONFIG" worker --root "$WORKER_ROOT" \
      status 2>&1) && rc=0 || rc=$?
case "$out" in
    *"pandora-gateway: refused"*) fail "worker status was refused: $out" ;;
esac

# --- a full submit, attributed to the pin ------------------------------------
"$PANDORA" --config "$USER_CONFIG" selftest --timeout 1800 >/dev/null \
    || fail "selftest through the gateway failed"

digest=$(cd "$ROOT" && python3 -c \
         'from pandora.engine.bundle import payload; print(payload()[0])')
bundle="$ER/bundles/$digest"
stats=$($SSHG "cd $bundle && PYTHONPATH=$bundle python3 -m pandora.engine.service --root $ER stats") \
    || fail "engine stats through the gateway was refused"
echo "$stats" | grep -q "\"$PIN\"" \
    || fail "the ledger names no run by the pinned client $PIN: $stats"

# --- and the revocation is real ----------------------------------------------
"$PANDORA" --config "$ADMIN_CONFIG" worker provision \
    --versions "$TMP/versions.orig.toml" --no-canary >/dev/null \
    || fail "the revoking provision failed"
rm -f "$TMP/versions.orig.toml"       # so the trap does not re-provision
out=$($SSHG id 2>&1) && rc=0 || rc=$?
[ "$rc" -eq 0 ] && fail "the revoked key still authenticates"

echo "e2e-gateway: provisioned, probed, ran a selftest as $PIN, revoked"
