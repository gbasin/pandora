#!/bin/bash
# Capability probe for a Codex shell tool under different sandbox modes.
# Prints one RESULT line per check. Never prints secret contents.
# Paths are literal so the script needs no environment of its own.

WT=/Users/you/Code/pandora-wt/codex-sandbox-probe
PROBE=$WT/experiments/codex-sandbox
ADD=/Users/you/.local/state/pandora-probe/adddir
NOADD=/Users/you/.local/state/pandora-probe/notadded
STATE=/Users/you/.local/state/pandora-probe
TCP_PORT=18711

# BASH_ENV/zsh startup files in this environment run fnm, which fails noisily
# under a read-only sandbox and would otherwise mask every real error.
unset BASH_ENV

say() { printf 'RESULT %-22s %s\n' "$1" "$2"; }

check() { # name, command...
  local name=$1; shift
  local out rc
  out=$("$@" 2>&1); rc=$?
  out=$(printf '%s' "$out" | grep -v 'fnm_multishells\|fnm env\|Maybe there are some' | tr '\n' '|' | tail -c 200)
  if [ $rc -eq 0 ]; then say "$name" "PASS rc=0 $out"; else say "$name" "FAIL rc=$rc $out"; fi
}

# A clean non-login, non-rc bash so startup files cannot change the exit code.
sh_c() { env -u BASH_ENV bash --noprofile --norc -c "$1"; }

py_tcp() {
  python3 - "$1" "$2" <<'EOF'
import socket, sys
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4)
s.sendall(b"ping\n")
print(s.recv(64).decode().strip())
EOF
}

py_uds_connect() {
  python3 - "$1" <<'EOF'
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(4)
s.connect(sys.argv[1])
s.sendall(b"ping\n")
print(s.recv(64).decode().strip())
EOF
}

py_uds_bind() {
  python3 - "$1" <<'EOF'
import os, socket, sys
p = sys.argv[1]
try:
    os.unlink(p)
except FileNotFoundError:
    pass
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(p)
s.listen(1)
print("bound", p)
s.close()
os.unlink(p)
EOF
}

py_dns() {
  python3 -c 'import socket;print(socket.getaddrinfo("example.com",443)[0][4][0])'
}

py_https() {
  python3 -c 'import urllib.request;print(urllib.request.urlopen("https://example.com/",timeout=8).status)'
}

echo "===PANDORA_PROBE_BEGIN==="
say PROBE_VERSION "4"
say UNAME "$(uname -srm)"
say WHOAMI "$(id -un)"
say CWD "$PWD"
say TMPDIR_VALUE "${TMPDIR:-unset}"

# --- network ------------------------------------------------------------
check NET_TCP_LOCALHOST py_tcp 127.0.0.1 "$TCP_PORT"
check NET_DNS_EXAMPLE py_dns
check NET_HTTPS_EXAMPLE py_https

# --- ssh ----------------------------------------------------------------
check SSH_CONFIG_PARSE sh_c "ssh -G localhost | head -1"
check SSH_LOCALHOST_EXEC ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no localhost 'echo SSH_EXEC_OK'
# ssh appends ~18 random characters to ControlPath, and macOS caps a unix
# socket path at 104 bytes, so the worktree ControlPath must stay short.
check SSH_CM_WORKTREE sh_c "ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o ControlMaster=auto -o ControlPersist=5 -o ControlPath=$WT/.cm-probe localhost 'echo CM_OK'; ls -l $WT/.cm-probe >/dev/null"
check SSH_CM_ADDDIR sh_c "ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o ControlMaster=auto -o ControlPersist=5 -o ControlPath=$ADD/cm-add.sock localhost 'echo CM_OK'; ls -l $ADD/cm-add.sock >/dev/null"
check SSH_CM_TMPDIR sh_c "ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o ControlMaster=auto -o ControlPersist=5 -o ControlPath=\${TMPDIR:-/tmp}/cm-tmp.sock localhost 'echo CM_OK'; ls -l \${TMPDIR:-/tmp}/cm-tmp.sock >/dev/null"

# --- unix socket bind (ControlPath creation stand-in) --------------------
check BIND_WORKTREE py_uds_bind "$PROBE/run/bind-wt.sock"
check BIND_ADDDIR py_uds_bind "$ADD/bind-add.sock"
check BIND_NOTADDED py_uds_bind "$NOADD/bind-noadd.sock"
check BIND_DOT_SSH py_uds_bind "$HOME/.ssh/pandora-probe-bind.sock"
check BIND_TMPDIR py_uds_bind "${TMPDIR:-/tmp}/pandora-probe-bind.sock"
check BIND_SLASH_TMP py_uds_bind "/tmp/pandora-probe-bind.sock"

# --- unix socket connect to servers outside the sandbox ------------------
check UDS_CONNECT_WORKTREE py_uds_connect "$PROBE/run/srv-wt.sock"
check UDS_CONNECT_ADDDIR py_uds_connect "$ADD/srv-add.sock"
check UDS_CONNECT_SLASH_TMP py_uds_connect "/tmp/pandora-probe-srv.sock"
check UDS_CONNECT_STATE py_uds_connect "$STATE/srv-state.sock"

# --- filesystem ---------------------------------------------------------
check FS_WRITE_WORKTREE sh_c "echo x > $PROBE/run/w-wt.txt"
check FS_WRITE_ADDDIR sh_c "echo x > $ADD/w-add.txt"
check FS_WRITE_NOTADDED sh_c "echo x > $NOADD/w-noadd.txt"
check FS_WRITE_SLASH_TMP sh_c "echo x > /tmp/pandora-probe-w.txt"
check FS_WRITE_TMPDIR sh_c "echo x > \${TMPDIR:-/tmp}/pandora-probe-w.txt"
check FS_WRITE_HOME sh_c "echo x > \$HOME/pandora-probe-w.txt && rm -f \$HOME/pandora-probe-w.txt"
check FS_HARDLINK_WT_TO_ADD sh_c "rm -f $ADD/link-from-wt.txt; echo y > $PROBE/run/src.txt && ln $PROBE/run/src.txt $ADD/link-from-wt.txt"
check FS_RENAME_WT_TO_ADD sh_c "rm -f $ADD/moved.txt; echo y > $PROBE/run/mv-src.txt && mv $PROBE/run/mv-src.txt $ADD/moved.txt"
check FS_RENAME_ADD_TO_WT sh_c "rm -f $PROBE/run/moved-back.txt; echo y > $ADD/mv2-src.txt && mv $ADD/mv2-src.txt $PROBE/run/moved-back.txt"

# --- readability of ssh material (never printed) ------------------------
check READ_SSH_CONFIG test -r "$HOME/.ssh/config"
check READ_SSH_KEY test -r "$HOME/.ssh/id_ed25519"
check READ_KNOWN_HOSTS test -r "$HOME/.ssh/known_hosts"

# --- environment / PATH -------------------------------------------------
say PATH_FIRST3 "$(printf '%s' "$PATH" | tr ':' '\n' | head -3 | tr '\n' ' ')"
case "$PATH" in
  /Users/you/.local/state/pandora-probe/shimbin:*) say PATH_SHIM_FIRST "PASS shim dir is first" ;;
  */pandora-probe/shimbin*) say PATH_SHIM_FIRST "PARTIAL shim present but not first" ;;
  *) say PATH_SHIM_FIRST "FAIL shim dir absent" ;;
esac
check PATH_SHIM_RESOLVES sh_c "command -v pandoraprobe-marker && pandoraprobe-marker"
say ENV_PANDORA_PROBE_VAR "${PANDORA_PROBE_VAR:-<stripped>}"
say ENV_PANDORA_STATE "${PANDORA_STATE:-<stripped>}"
say ENV_PANDORA_PROBE_TOKEN "${PANDORA_PROBE_TOKEN:+<present>}${PANDORA_PROBE_TOKEN:-<stripped>}"
say ENV_PANDORA_PROBE_SECRET "${PANDORA_PROBE_SECRET:+<present>}${PANDORA_PROBE_SECRET:-<stripped>}"
say ENV_HOME "${HOME:-<stripped>}"
say ENV_USER "${USER:-<stripped>}"
say ENV_LANG "${LANG:-<stripped>}"
say ENV_COUNT "$(env | wc -l | tr -d ' ')"
say ENV_NAMES "$(env | cut -d= -f1 | sort | tr '\n' ' ' | cut -c1-400)"

# --- tooling ------------------------------------------------------------
check TOOL_GIT sh_c "git -C $WT rev-parse --abbrev-ref HEAD"
check TOOL_RSYNC sh_c "command -v rsync"
echo "===PANDORA_PROBE_END==="
