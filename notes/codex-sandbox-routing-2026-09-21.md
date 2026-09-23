---
status: log
---

# Codex sandbox capabilities and the routing client shape, 2026-09-21

Measured on this Mac, against localhost stand-ins only. No remote host was
contacted. Probe scripts live in `experiments/codex-sandbox/`; raw Codex
transcripts live in `experiments/codex-sandbox/results/`.

## Versions and environment

| Item | Value |
| --- | --- |
| Codex CLI | `codex-cli 0.155.1` (`codex --version`) |
| Codex binary | `/Users/garybasin/.local/bin/codex` |
| Model used for probes | `gpt-6-astra`, `model_reasoning_effort="low"` |
| OS | `Darwin 25.2.0 arm64` |
| Codex shell tool | `/bin/zsh -lc '<command>'` (login shell), recorded in every transcript |
| Local sshd | enabled; `ssh -o BatchMode=yes localhost true` succeeds from an unsandboxed shell |

`codex exec --yolo` and `codex --yolo` are accepted in 0.155.1 as the alias of
`--dangerously-bypass-approvals-and-sandbox`; `--yolo` is not listed in
`--help`. The listed sandbox values are `read-only`, `workspace-write`, and
`danger-full-access`.

The `sandbox_workspace_write` table in this binary has exactly four fields:
`writable_roots`, `network_access`, `exclude_tmpdir_env_var`,
`exclude_slash_tmp`. There is **no localhost-only network option**. Turning on
`network_access` grants full outbound network, which the probe confirms
(DNS plus HTTPS to `example.com` both succeed).

## Question 1: what the recorded Pandora trials actually used

- The agent-fanout Codex lanes run
  `codex exec --json --sandbox workspace-write`, and add
  `-c sandbox_workspace_write.network_access=true` unless `--no-network` is
  passed. Source: `/Users/garybasin/.claude/skills/agent-fanout/scripts/launch-codex-lane`
  lines 201-204 (`codex_args=(exec --json --sandbox workspace-write)`), and the
  `--no-network` help text at line 40.
- When `--pnpm-store` is used the same script also adds `--add-dir "$pnpm_store"`
  plus `-c shell_environment_policy.set.pnpm_config_store_dir=...` and
  `...npm_config_store_dir=...` (lines 288-292).
- Pandora's own Codex wrapper, `experiments/routing/bin/codex`, does not change
  the sandbox mode. It only adds `--add-dir $PANDORA_STATE` and a list of
  `-c shell_environment_policy.set.<VAR>=<json>` overrides for `PANDORA_*`,
  `ZDOTDIR`, and the recorded real executables. Its own comment says it must not
  "replace the user's sandbox configuration or disable it to support routing."
- `experiments/routing/fanout-adapter.py` wires the two together: it writes a
  private `launch-codex-lane` shim that calls `launch.py ... -- <skill>/scripts/launch-codex-lane --codex-bin <routing>/bin/codex`. So the trial lanes ran
  **workspace-write + network_access=true + `--add-dir <state>`**.
- `experiments/routing/README.md` line 167-169 states the same intent: the state
  directory is added with `--add-dir`, "preserving existing writable roots and
  sandbox mode. ... The launcher does not disable the sandbox."
- Routing worked under that configuration. `notes/agent-ramp-2026-09-21-verified.md`
  records twelve concurrent sessions (six Codex Terra, six Claude Opus) running
  `pnpm journey S0-01` and `pnpm test:surface borrower-web chrome.spec.ts`
  through routing, with 24 verified receipts and no local-validation fallbacks.

No note in `notes/` records a Codex run under `read-only`, under
`workspace-write` **without** network, or under `--yolo`. The recorded evidence
covers exactly one sandbox configuration.

Caveat on this machine specifically: `~/.codex/config.toml` sets
`approval_policy = "never"` and `sandbox_mode = "danger-full-access"` globally,
and `[projects."/"] trust_level = "trusted"`. So a bare interactive `codex` or
`codex exec` here is already unsandboxed, which is not the out-of-the-box
default. With `--ignore-user-config`, `codex exec` reports `sandbox: read-only`
(`results/05-default-no-user-config.txt`). That read-only case is the "most
restrictive default" tested below.

## Question 2: measured capability matrix

Exact commands:

```sh
# stand-in servers, started OUTSIDE the sandbox
python3 experiments/codex-sandbox/servers.py \
  <worktree>/experiments/codex-sandbox/run/srv-wt.sock \
  ~/.local/state/pandora-probe/adddir/srv-add.sock \
  /tmp/pandora-probe-srv.sock \
  ~/.local/state/pandora-probe/srv-state.sock
# TCP listener: 127.0.0.1:18711

# one run per mode (experiments/codex-sandbox/run-probe.sh)
codex exec -C <worktree> --skip-git-repo-check -c 'notify=[]' \
  -c 'model_reasoning_effort="low"' <MODE FLAGS> \
  "Run exactly this command once ... bash <worktree>/experiments/codex-sandbox/probe.sh"
```

Mode flags:

| Column | Flags |
| --- | --- |
| base | no Codex at all: `bash probe.sh` in a normal shell |
| RO | `-s read-only` (= `codex exec` default with no user config) |
| WW | `-s workspace-write` |
| WW+net | `-s workspace-write -c sandbox_workspace_write.network_access=true --add-dir ~/.local/state/pandora-probe/adddir` (the agent-fanout shape) |
| DFA | `-s danger-full-access` (equivalent to `--yolo`) |

The launching environment for every Codex run prepended
`~/.local/state/pandora-probe/shimbin` to `PATH` and exported
`PANDORA_PROBE_VAR`, `PANDORA_STATE`, `PANDORA_PROBE_TOKEN`,
`PANDORA_PROBE_SECRET`. No `shell_environment_policy` override was passed.

| Check | base | RO | WW | WW+net | DFA |
| --- | --- | --- | --- | --- | --- |
| TCP to localhost listener outside sandbox | PASS | FAIL | FAIL | PASS | PASS |
| DNS resolve example.com | PASS | FAIL | FAIL | PASS | PASS |
| HTTPS GET example.com | PASS | FAIL | FAIL | PASS | PASS |
| `ssh -G localhost` (config parse, no connect) | PASS | PASS | PASS | PASS | PASS |
| `ssh -o BatchMode=yes localhost true` | PASS | FAIL | FAIL | PASS | PASS |
| ssh ControlMaster socket in worktree | PASS | FAIL | FAIL | PASS | PASS |
| ssh ControlMaster socket in `--add-dir` dir | PASS | FAIL | FAIL | PASS | PASS |
| ssh ControlMaster socket in `$TMPDIR` | PASS | FAIL | FAIL | PASS | PASS |
| bind unix socket in worktree | PASS | FAIL | FAIL | PASS | PASS |
| bind unix socket in `--add-dir` dir | PASS | FAIL | FAIL | PASS | PASS |
| bind unix socket in non-added dir | PASS | FAIL | FAIL | FAIL | PASS |
| bind unix socket in `~/.ssh` | PASS | FAIL | FAIL | FAIL | PASS |
| bind unix socket in `$TMPDIR` | PASS | FAIL | FAIL | PASS | PASS |
| bind unix socket in `/tmp` | PASS | FAIL | FAIL | PASS | PASS |
| connect to outside-sandbox UDS in worktree | PASS | FAIL | FAIL | PASS | PASS |
| connect to outside-sandbox UDS in `--add-dir` dir | PASS | FAIL | FAIL | PASS | PASS |
| connect to outside-sandbox UDS in `/tmp` | PASS | FAIL | FAIL | PASS | PASS |
| connect to outside-sandbox UDS in `~/.local/state/...` (NOT added) | PASS | FAIL | FAIL | PASS | PASS |
| write file in worktree | PASS | FAIL | PASS | PASS | PASS |
| write file in `--add-dir` dir | PASS | FAIL | FAIL | PASS | PASS |
| write file in non-added dir | PASS | FAIL | FAIL | FAIL | PASS |
| write file in `/tmp` | PASS | FAIL | PASS | PASS | PASS |
| write file in `$TMPDIR` | PASS | FAIL | PASS | PASS | PASS |
| write file in `$HOME` | PASS | FAIL | FAIL | FAIL | PASS |
| hardlink worktree -> `--add-dir` dir | PASS | FAIL | FAIL | PASS | PASS |
| rename worktree -> `--add-dir` dir | PASS | FAIL | FAIL | PASS | PASS |
| rename `--add-dir` dir -> worktree | PASS | FAIL | FAIL | PASS | PASS |
| `test -r ~/.ssh/config` | PASS | PASS | PASS | PASS | PASS |
| `test -r ~/.ssh/id_ed25519` | PASS | PASS | PASS | PASS | PASS |
| `test -r ~/.ssh/known_hosts` | PASS | PASS | PASS | PASS | PASS |
| custom env var `PANDORA_PROBE_VAR` present | yes | yes | yes | yes | yes |
| env var named `*_TOKEN` present | yes | yes | yes | yes | yes |
| env var named `*_SECRET` present | yes | yes | yes | yes | yes |
| shim dir present in `PATH` | yes | yes | yes | yes | yes |
| shim dir **first** in `PATH` | no | no | no | no | no |
| shim binary resolves via `command -v` | PASS | PASS | PASS | PASS | PASS |
| `git rev-parse` in worktree | PASS | PASS | PASS | PASS | PASS |
| `rsync` on PATH | PASS | PASS | PASS | PASS | PASS |

Failure messages are uniform: `PermissionError: [Errno 1] Operation not
permitted` for sockets, `Operation not permitted` from the shell for denied
writes, and `ssh: connect to host localhost port 22: Operation not permitted`.
All three denials come from the macOS Seatbelt profile, not from an approval
prompt (approval policy was `never` in every run).

### Facts that follow from the matrix

1. **The Seatbelt network deny covers AF_UNIX, not just AF_INET.** Under
   `read-only` and under `workspace-write` without network, connecting to a unix
   socket fails even when the socket sits in `/tmp`, and binding one fails even
   inside the writable worktree. `network_access=true` is what unblocks unix
   sockets, in both directions.
2. **Connecting to a unix socket does not need write access to its directory.**
   Under WW+net the probe connected to a server socket in
   `~/.local/state/pandora-probe/`, which was not a writable root, while
   *creating* a socket in a non-added directory failed. Binding is a filesystem
   write; connecting is a network operation.
3. **`network_access=true` is all-or-nothing.** There is no config key for
   "localhost only". Enabling it for a routing client also opens arbitrary
   outbound internet from the agent's shell.
4. **`--add-dir` is required for the state directory.** Under WW+net, writes to
   the added directory succeeded and writes to a sibling non-added directory
   failed, and hardlink and rename between the worktree and the added directory
   both worked (same filesystem, both writable roots).
5. **Codex 0.155.1 does not strip custom environment variables by default.**
   With no `shell_environment_policy` override at all, `PANDORA_PROBE_VAR`,
   `PANDORA_PROBE_TOKEN`, and `PANDORA_PROBE_SECRET` all reached the shell tool
   with their values intact. The default `inherit` is effectively `all` here.
   The explicit `-c shell_environment_policy.set.*` lines in
   `experiments/routing/bin/codex` are belt-and-braces for this version, not a
   requirement; they remain correct insurance against a user who sets
   `inherit = "core"`.
6. **`PATH` is re-derived by a login shell, so a global shim dir is not first.**
   Codex runs `/bin/zsh -lc`, and the login files put
   `/Users/garybasin/.local/bin`, then Homebrew, ahead of whatever PATH Codex
   inherited. The probe's shim directory survived into the tool and resolved by
   name, but only because nothing earlier in `PATH` shadows it. On this machine
   `which -a pnpm` in a login shell returns `/opt/homebrew/bin/pnpm` first, so a
   global shim directory appended by the launcher's environment would lose to
   Homebrew. A global PATH shim must be installed in a directory the login files
   themselves put first (here `~/.local/bin`), or exported from the rc files.
7. **`~/.ssh` is readable in every mode, including `read-only`.** Key material
   and `ssh_config` stay readable; only the network call is blocked. (The probe
   used `test -r` and never printed contents.)
8. **A ControlPath in the worktree is length-constrained.** ssh appends about 18
   random characters and macOS caps a unix socket path at 104 bytes; the first
   probe version failed with `unix_listener: path ... too long` for
   `<worktree>/experiments/codex-sandbox/run/cm-wt.sock` even outside the
   sandbox. That is a path-length fact, not a sandbox fact, but it constrains
   where a ControlMaster socket can live.

## Question 4: what this means for designs A and B

### Design A - in-process shim doing ssh/rsync from inside the sandbox

- Works under `workspace-write` **only with** `network_access=true` **and**
  `--add-dir <state>`. That is exactly the recorded agent-fanout configuration,
  and the twelve-agent trial is evidence it holds up.
- Does **not** work under `workspace-write` without network: ssh cannot connect.
- Does **not** work under `read-only`: no network at all, and no snapshot writes.
- Minimum user configuration for A, per launch:
  `--sandbox workspace-write`, `-c sandbox_workspace_write.network_access=true`,
  `--add-dir <state dir>`. As persistent config that is
  `sandbox_mode = "workspace-write"`,
  `sandbox_workspace_write.network_access = true`, plus
  `sandbox_workspace_write.writable_roots = ["<state dir>"]`.
- Cost of A: the user must grant the agent full outbound network, because Codex
  has no localhost-only setting. That is a strictly larger grant than routing
  needs. It also means every agent session holds the SSH key material path and
  opens its own connection.
- A cannot be made to work under the default (`read-only`) sandbox by any
  configuration short of changing the sandbox mode.

### Design B - local daemon outside the sandbox, thin client inside

- **B does not work under all modes.** Under `read-only` and under
  `workspace-write` without network, the in-sandbox client cannot connect to a
  unix socket or to a localhost TCP port. Both fail with
  `PermissionError: [Errno 1] Operation not permitted`. The Seatbelt network
  deny is what blocks it, and no socket location avoids it — worktree, added
  directory, `/tmp`, and `~/.local/state` all fail identically.
- **B works under `workspace-write` + `network_access=true`, and it needs less
  than A there.** The client only needs to connect to one unix socket; it does
  not need `--add-dir` for the state directory, because the daemon owns that
  directory and connecting to a socket inside it does not require write access.
  That removes the whole "state dir must be a writable root and on the same
  filesystem as the worktree" constraint from the agent's side.
- So the honest statement is: B reduces the required grant from
  "network + writable state root" to "network", and moves the snapshot, ssh,
  rsync, and polling out of the sandbox. It does **not** make routing work
  without loosening the sandbox at all.

B's costs:

- **Daemon lifecycle.** Something must start it and keep it alive: launchd agent,
  or lazy autostart from the shim — but a lazy autostart cannot work, because
  the shim runs inside the sandbox and a process it spawns inherits the sandbox.
  The daemon must be started from outside, which means launchd or the session
  launcher. That is a new failure mode the current design does not have.
- **Authenticating local callers.** Any process on the machine that can reach the
  socket can submit work. Filesystem permissions on the socket directory are the
  practical control (mode 0700 under `~/.local/state`), plus a per-session token
  that the launcher injects as an env var. On a single-user Mac this is modest,
  but it is new surface.
- **Streaming and exit codes.** The current shim writes to its own stdout and
  exits with the command's status. B must frame stdout/stderr and a final exit
  code over the socket, and keep the heartbeat cadence the agents rely on.
- **Cancellation.** Today Ctrl-C in the agent's shell kills the shim and the
  existing `pandora wait` observer semantics apply. With B, the client dying must
  be distinguishable from an explicit cancel; the daemon needs an explicit
  cancel message and a disconnect policy (detach and keep running, matching
  today's observer behaviour, is the closer match).
- **State ownership.** The active-request lock, the snapshot, and the publication
  receipts move into the daemon. That is mostly a benefit, but it changes the
  recovery story: the runbook currently assumes the state directory is reachable
  from the agent's shell.

B's side benefits:

- One SSH ControlMaster for the whole machine instead of one per agent session,
  which also sidesteps the 104-byte ControlPath limit entirely.
- Shim startup cost drops to a socket connect; no per-invocation ssh handshake.
- Single place for host, state directory, and treatment configuration, so the
  `-c shell_environment_policy.set.*` list shrinks to one socket path (or a
  fixed default path needing no env at all).
- The daemon can implement a fallback-to-local policy and the queue admission
  view in one place.
- It fits a global PATH shim better: a global `pnpm` shim needs no session
  launcher, no `PANDORA_*` environment, and no `--add-dir`; it needs only a
  well-known socket path. The remaining requirement is the PATH-ordering fact
  from finding 6 — the shim directory must be one the login files put first.
- SSH private keys stop being read by the sandboxed process.

## Question 5: recommendation

Adopt design B, but do not claim it removes the sandbox requirement. State the
requirement plainly instead: **routing needs `network_access = true`**, in both
designs, because Codex's Seatbelt profile blocks unix sockets under the same
switch as TCP. Under the out-of-the-box `read-only` default, no client design
works.

B is still the better shape, for three measured reasons: it drops the
`--add-dir`/writable-root/same-filesystem requirement, it drops the per-session
launcher and environment injection (which is what a global PATH shim needs), and
it keeps SSH keys and the ControlMaster outside the agent's sandbox.

### Minimal protocol sketch for B

Transport: `SOCK_STREAM` unix socket at `$PANDORA_SOCK`, default
`~/.local/state/pandora/<profile>/client.sock`, directory mode `0700`. The
daemon binds it; the shim only connects, so the path needs no writable root.

Framing: newline-delimited JSON, one object per line, in both directions.

Client to daemon, first line only:

```json
{"v":1,"op":"run","token":"<session token or null>","cwd":"<abs worktree path>",
 "argv":["pnpm","test:surface","borrower-web","chrome.spec.ts"],
 "env":{"PANDORA_TREATMENT":"normal"},"tty":false}
```

Daemon to client, streamed:

```json
{"t":"accepted","attempt":"<attempt-id>","queue_position":2}
{"t":"log","stream":"stdout","data":"..."}
{"t":"heartbeat","elapsed_s":30,"state":"running"}
{"t":"exit","code":0,"attempt":"<attempt-id>"}
```

Client to daemon, any time after the first line:

```json
{"t":"cancel","attempt":"<attempt-id>"}
{"t":"detach"}
```

Semantics:

- The daemon resolves the worktree, takes the active-request lock, snapshots,
  and runs the remote attempt. The client never touches the state directory.
- A client disconnect without `cancel` means detach: the attempt keeps running
  and is re-attachable with `{"op":"attach","attempt":"..."}`. This preserves
  today's `pandora wait` behaviour.
- `cancel` is explicit and idempotent.
- Exit code 75 and the existing feedback strings pass through unchanged as the
  `exit` code plus a final `log` frame, so agent-facing behaviour does not
  change.
- Auth: the socket's directory mode is the primary control; `token` is compared
  against a per-launch value when the launcher sets one, and is ignored when the
  daemon is configured single-user. A missing or wrong token gets
  `{"t":"exit","code":77,"error":"unauthorized"}`.
- `op:"ping"` returns daemon version, configured host, and whether the SSH
  ControlMaster is up, so the shim can produce a useful error instead of a
  socket timeout when the daemon is down.
- When the socket is absent or refuses, the shim prints one actionable line
  ("pandora daemon not running: run `pandora daemon start`") and falls back to
  the recorded real `pnpm` only if the invoked command is not a routed form.

## What was not tested

- No remote worker was contacted. All network probes used a localhost listener,
  localhost sshd, and `example.com` for DNS/HTTPS.
- Claude Code's own environment and PATH survival were out of scope.
- No daemon was implemented; B's protocol above is a sketch, not a measurement.
- Linux Landlock behaviour was not tested; every result here is macOS Seatbelt.
