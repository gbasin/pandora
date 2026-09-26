#!/bin/sh
# Live-worker proof of the teammate gateway (#156), over real sshd against the
# e2e worker pandora-ci@localhost. Self-provisioning: the runner's admin key
# installs bin/gateway and the managed authorized_keys block, the probes
# exercise the forced command, and the block comes off again. Provision's own
# rendering is unit-covered (test_worker.py); this proves what only a live
# box can: sshd's restrict,command= line, the gateway on the real wire, the
# pin in the ledger, and the revocation.
#
# PANDORA_E2E_CONFIG names the e2e client config (same one selftest uses).

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
PANDORA="$ROOT/bin/pandora"
ADMIN_CONFIG=${PANDORA_E2E_CONFIG:-/home/gh-runner/.config-pandora-e2e.toml}
ALIAS=e2e-worker-user
PIN=pandora-ci-user
MARK_BEGIN='# >>> pandora users >>>'
MARK_END='# <<< pandora users <<<'

fail() { echo "::error::e2e-gateway: $*" >&2; exit 1; }

[ -f "$ADMIN_CONFIG" ] || {
    echo "::notice::e2e-gateway: no e2e client config at $ADMIN_CONFIG; skipping"
    exit 0
}

TMP=$(mktemp -d)
SSHC="$HOME/.ssh/config"

cleanup() {
    # The managed block and the ssh alias come off no matter how the run ends.
    # $TMP/key is removed once the inline revoke has run, so a pass leaves this
    # a no-op rather than a second attempt.
    if [ -f "$TMP/key" ]; then
        $SSHA '
            awk "/pandora users >>>/{f=1;next}/pandora users <<</{f=0;next}!f" \
                ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.new \
            && cat ~/.ssh/authorized_keys.new > ~/.ssh/authorized_keys \
            && rm -f ~/.ssh/authorized_keys.new' 2>/dev/null \
            || echo "::warning::e2e-gateway: cleanup could not reach the worker;" \
                    "the managed authorized_keys block may still be on it"
    fi
    if [ -f "$SSHC" ]; then
        awk '/pandora-e2e-gateway >>>/{f=1;next}/pandora-e2e-gateway <<</{f=0;next}!f' \
            "$SSHC" > "$SSHC.tmp" \
            && cat "$SSHC.tmp" > "$SSHC" && rm -f "$SSHC.tmp" || true
    fi
    rm -rf "$TMP"
}
trap cleanup EXIT

HOST=$(python3 - "$ADMIN_CONFIG" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], 'rb'))['worker']['host'])
PY
)
SSHA="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $HOST"

# The roots the gateway confines to: the engine root the client config names,
# and the worker root beside it -- expanded against the worker's own $HOME.
ER=$(python3 - "$ADMIN_CONFIG" <<'PY'
import sys, tomllib
print(tomllib.load(open(sys.argv[1], 'rb'))['worker'].get('engine_root',
                                                          '~/pandora-engine'))
PY
)
HOME_R=$($SSHA 'cd ~ && pwd') || fail "admin ssh to $HOST failed"
case "$ER" in
    /*) ;;                            # already absolute
    \~/*) ER="$HOME_R/${ER#~/}" ;;
    *) ER="$HOME_R/${ER#.}" ;;        # relative: the wire absolutizes it too
esac
WORKER_ROOT="$HOME_R/pandora"

ssh-keygen -q -t ed25519 -N '' -f "$TMP/key" || fail "keygen failed"
PUB=$(cat "$TMP/key.pub")

# --- install the gateway and the teammate key --------------------------------
# Exactly what provision writes: bin/gateway, then one managed
# authorized_keys line rendering restrict,command=... through
# provision.authorized_lines so the live text is the shipped renderer's.
$SSHA "mkdir -p '$WORKER_ROOT/bin' '$HOME_R/.ssh' && chmod 700 '$HOME_R/.ssh'" \
    || fail "cannot write on the worker over the admin key"
$SSHA "cat > '$WORKER_ROOT/bin/gateway' && chmod 755 '$WORKER_ROOT/bin/gateway'" \
    < "$ROOT/pandora/worker/gateway.py" || fail "gateway install failed"
# feeds.allow is the python3 -c allowlist; without it even the bootstrap feed
# that installs a bundle is refused.
(cd "$ROOT" && python3 -c \
 'import sys; from pandora.engine.bundle import feed_manifest; sys.stdout.write(feed_manifest())') \
    | $SSHA "cat > '$ER/feeds.allow'" || fail "feeds.allow install failed"

line=$(cd "$ROOT" && python3 - "$PIN" "$PUB" "$WORKER_ROOT" "$ER" <<'PY'
import sys
from pandora.worker import provision, versions
manifest = versions.normalize({'users': [{'name': sys.argv[1], 'key': sys.argv[2]}]})
print(provision.authorized_lines(manifest, root=sys.argv[3], engine_root=sys.argv[4])[0])
PY
)
{ printf '%s\n%s\n%s\n' "$MARK_BEGIN" "$line" "$MARK_END"; } \
    | $SSHA 'cat >> ~/.ssh/authorized_keys' || fail "authorized_keys write failed"

# The teammate's ssh alias and client config: same worker, other key.
mkdir -p "$HOME/.ssh" && touch "$SSHC"
{
    printf '%s\n' '# >>> pandora-e2e-gateway >>>'
    printf 'Host %s\n  HostName localhost\n  User pandora-ci\n  IdentityFile %s\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' \
        "$ALIAS" "$TMP/key"
    printf '%s\n' '# <<< pandora-e2e-gateway <<<'
} >> "$SSHC"

python3 - "$ADMIN_CONFIG" "$ALIAS" "$TMP" <<'PY'
import sys, tomllib
raw = tomllib.load(open(sys.argv[1], 'rb'))
worker = dict(raw.get('worker') or {})
worker['host'] = sys.argv[2]          # the alias carries user + key
with open(sys.argv[3] + '/config.toml', 'w') as out:
    out.write('[worker]\n')
    for key, value in worker.items():
        out.write('%s = "%s"\n' % (key, value))
    out.write('\n[client]\nstate = "%s"\n' % (sys.argv[3] + '/state'))
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
rm -f "$TMP/key"                        # marks the trap's revoke as done below
$SSHA 'awk "/pandora users >>>/{f=1;next}/pandora users <<</{f=0;next}!f" \
    ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.new \
    && cat ~/.ssh/authorized_keys.new > ~/.ssh/authorized_keys \
    && rm -f ~/.ssh/authorized_keys.new' || fail "revoking the key failed"
out=$($SSHG id 2>&1) && rc=0 || rc=$?
[ "${rc:-0}" -eq 0 ] && fail "the revoked key still authenticates"

echo "e2e-gateway: shells refused, client verbs admitted, runs pinned to $PIN, revoked"
