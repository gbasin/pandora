# Session-only validation routing

Launch a trial session with the private executable directory prepended to PATH:

```sh
python3 launch.py --host ubuntu@WORKER_IP --state /tmp/pandora-trial-state -- codex
```

For a scripted check, replace `codex` with `pnpm test:surface borrower-web
smoke.spec.ts`. Run from a bootstrapped trial worktree. A missing dependency
image is prepared automatically on the worker under its exclusive admission
lock and bounded BuildKit resources.

These pnpm command forms route:

- `pnpm test:surface <borrower-web|desk> [file selectors] [--grep PATTERN]`
- `pnpm validate surface <borrower-web|desk> [file selectors] [--grep PATTERN]`
- `pnpm journey <id> [--fault dropped] [--update]`
- `pnpm validate journey <id> [--fault dropped] [--update]`
- The same forms with `pnpm run`.

Other commands delegate to the original pnpm executable. Unsupported surface
flags return a configuration error without running locally. This is the
normal-command treatment; absolute paths, direct package commands, and explicit
PATH overrides can bypass it. It is not OS-level enforcement.

No target-repo files or global shell configuration are modified. The launcher
exports its own session identity, host, state directory, and original executable
paths. The private Codex shim passes explicit shell-environment settings and
retains the existing login-shell policy. Session-specific ZDOTDIR files
load the original startup files and restore the trial prefix afterward; global
dotfiles are unchanged. Put the settings on `codex exec`
when using that subcommand. Harness supervisors may reset inherited PATH, so
launch routing inside the supervisor at its actual Codex entry point.

The [official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
documents shell environment overrides and `allow_login_shell`. Runtime preflight
must still verify executable resolution; inheriting PATH at the outer launcher
was insufficient in this experiment.

## Active request and cancellation

A local file lock and durable descriptor protect one active request per
worktree. A repeated invocation returns exit 75 with the existing-request
message. Changed selectors or source do not replace the active request. A normal
terminal result permits a deliberate new run.

An interrupted client leaves its descriptor active. The recovery behavior,
independent worker lifetime, and verified artifact retrieval are described below.
Cold dependency builds run automatically under the worker resource lease.

## Evaluation boundaries

The current controller supports the initial informed and ordinary-command tests.
FIFO admission serializes remote execution. Local source capture can occur
concurrently; unresolved and legacy data need operator cleanup; and there is no
universal shell interception. Recovery and startup-race guarantees are limited to the recorded fault tests.
Unknown terminal state still requires operator recovery.

The Claude helper runs the user's subscribed `opus` alias with shell/read tools
and a validation-only brief. It retains ordinary account authentication. The
Codex trial uses the existing subscribed CLI and the agent-fanout watchdog.
Neither helper configures a model API key or changes the default model globally.

## Supervised Codex trial

The installed agent-fanout controller does not forward a Codex executable
override through its `start` command. Create a private adapter directory with
`fanout-adapter.py`, then use its printed `agent-fanout` path for Codex starts.
It selects the bundled native watchdog through a session launcher and passes
its supported `--codex-bin` option explicitly. The original skill scripts remain
unchanged. Status, logs, collection, and cancellation still use agent-fanout.

The adapter relies on the controller resolving its launch helpers relative to
its invocation directory. Pin or recheck this behavior if the skill changes.

## Recovery pilot update

The recovery pilot gives each remote worker a systemd service independent of its
SSH client. Queue admission defaults to 15 minutes, test containers expire after
20 minutes, and the whole worker receives the accepted queue limit plus 25 minutes.
Explicit cancellation still
signals the owned attempt and verifies cleanup.

The active-request guard now belongs to the canonical worktree within the chosen
local state directory. A new launcher session can recover the same request. Keep
the same `--state` directory. Changing the state directory, moving the worktree,
or using another client machine does not discover the earlier request.

Retrying the identical command reconnects to the recorded attempt and retrieves
checksummed artifacts into a staging directory. Only complete verified evidence
is promoted locally. A changed command or host does not replace an active request.
If local source changed, recovery returns 75 and identifies the result as applying
to the earlier snapshot. A subsequent deliberate invocation validates new source.
Recovery is bounded and returns an unresolved status if the connection cannot be
restored. A missing worker terminal record remains an operator recovery case.

The session option `--treatment normal|block|redirect` evaluates one recognized
bypass: `pnpm --filter @eichler/borrower-web test:e2e [selectors]`, optionally with
`--workers=1` and `--reporter=line,junit`. Normal passes it through, block returns
the supported command, and redirect submits the equivalent surface profile.
Arbitrary absolute paths and shell constructions remain outside this coverage.

## Environment compatibility

The launcher preserves inherited environment variables and prepends its private
PATH directory. It records the initial pnpm and Codex executables. Ordinary pnpm
commands delegate to that recorded executable, including after a startup file
would have selected another version. This can conflict with version-manager
expectations and must not be described as completely transparent.

Session-only zsh files source the original startup files and then restore the
wrapper prefix. They do not edit those original files. Codex receives explicit
Pandora and ZDOTDIR environment overrides, without setting PATH through Codex
configuration. That explicit PATH setting discarded a tested login-profile PATH
addition; removing it preserved both the addition and routing. The earlier pilot forced non-login
shells, which could omit `.zprofile` or `.zlogin` setup. The recovery revision
removes that override and retains the existing login-shell policy. Other Codex
settings are retained. Explicit conflicting CLI config
or a shell command that changes PATH can still bypass routing. Claude inherits
the environment; its shell behavior must be tested separately.

The real-shell test confirms that original startup files execute, global files
remain unchanged, ordinary pnpm uses the original executable, and a manual PATH
prepend inside a running shell can select a different pnpm. This is opt-in command
routing, not a tamper-resistant policy boundary.

## Integrated surface outputs

Cold dependencies are now prepared automatically by the bounded BuildKit worker.
A successful command verifies returned artifacts and publishes the two declared
the selected app’s build directories at their normal paths. Previous directories are
retained under `<state>/<worktree-key>/<attempt>/publication/<output-index>/generation`.
Only ignored directories with no tracked files qualify. Symlink output paths are
rejected. No source files are replaced.

The request remains active through local delivery. If publication fails, correct
the reported local problem and retry the identical command. A downloaded terminal
result is reverified and delivered locally without another SSH execution. Do not
remove the active record or its publication receipts to resolve a transient error.
Source changes detected after execution produce exit 75, even on the first
invocation. The previous result remains evidence for the submitted snapshot.

The state directory must be writable by the agent's shell and on the same
filesystem as the worktree. The session-only Codex wrapper adds this state directory with `--add-dir`,
preserving existing writable roots and sandbox mode. The evaluated supervisor
separately grants pnpm-store access. The launcher does not disable the sandbox.

## Service-backed journey

Run `pnpm journey <id>` or `pnpm validate journey <id>` from the repository root.
The same forms with `pnpm run` work. Append `--fault dropped` for replay and
`--update` for expectation return, in that order when combined. The selected ID
must exist in the frozen catalog. Other flags and the plural
`journeys` command stop with feedback. Direct package commands can still bypass
this opt-in wrapper.

The shared worker lease covers dependency preparation, service startup, the
journey, and cleanup. Each invocation gets a fresh private database, pooler, proxy,
and network. No local services start. The runner uses the repository's external
stack mode and the same journey and route checks, with generated principal
printing disabled. It does not invoke the local Docker Compose entry point.

The command prints phase progress and a local `results/journey.json` path. Returned
reports are checksummed. A successful journey does not publish borrower-web build
outputs. Fix source locally, then invoke the same ordinary command again.

Explicit cancellation removes owned services before clearing the request. Lost
transport keeps the remote execution alive; retry follows the same attempt.
Systemd removes owned resources after worker death, but a missing terminal remains
unresolved. Do not delete an active record to work around that condition. Host
reboot, unavailable Docker, and generalized operator reconciliation are not yet
covered by an automatic recovery service.

## Docker profile

Add `--docker-profile /absolute/path/profile.json` to the session launcher to
route the bounded Docker grammar. The profile remains outside the target repo.
The private `docker` executable never delegates unsupported commands locally,
including Docker read commands and sessions without a selected profile. A run
without a mount uses its pinned image and captures no local source. See
[the Docker pilot](../docker/README.md) for supported flags, source ownership,
image retention, outputs, and the evaluated repair loop.

## Admission and deadlines

Requests receive a durable FIFO ticket after remote registration and input
verification. Upload start time does not determine ticket order. One worker lease
covers preparation, execution, collection, and cleanup. Waiting commands print
their ticket, requests ahead, elapsed wait, and queue limit. They start no local
validation and do not replace existing work.

Set the session default with `--queue-timeout-seconds 900` on the launcher.
A Docker profile may override it with `"queue_timeout_seconds": 900`; this override
applies only to Docker commands. Both accept integer seconds from 1 through 86400.
The default is 900 seconds. The accepted attempt records its effective limit.
Retry retains that limit even if the session configuration changes. The worker
supervisor allows the queue limit plus 25 minutes; the result follower allows a
further 45 seconds for shutdown and retrieval.

A queued cancellation or expired wait starts no validation. A dead waiting
process can leave the queue. A dead executing process blocks successors until
cleanup is positively verified. Cleanup proof does not manufacture a terminal
result: that attempt remains unresolved for operator reconciliation. Corrupt
queue state stops admission instead of bypassing the queue. Dependency or surface
worker death can still need operator cleanup.

Drain older clients and workers before deploying FIFO admission. Older worker
code uses only the resource lock and cannot honor the new ticket ordering.

## Focused journey expectation updates

`pnpm journey <id> --update` and `pnpm validate journey <id> --update` return
the selected ledger and its route-manifest update automatically to the local worktree.
The corresponding `pnpm run` forms work. Catalog updates
remain implementation targets.

Wait for the command before editing those expectation files. Review the resulting
`git diff`, then run ordinary validation without `--update`. Pandora preserves
unrelated route entries. It rejects symlink paths and currently requires regular
0644 expectation files. Keep the state directory on the worktree's filesystem.

If local delivery is interrupted, retry the identical command. Pandora resumes
its recorded publication without running the journey again. A detected conflict
leaves local contents intact and prints paths to proposed files and conflict
evidence. Before retrying automatic return, restore a conflicted destination to its captured
version under `<attempt>/source/`, preserving your edit separately. After an
interrupted publication, an already-returned target must remain at that target.
Do not delete the active record or its publication intent. Changes to other source make the result stale and
retain the proposals without publishing them.

Per-file backups and intent live under the attempt's `publication` directory.
Failed remote updates retain available proposals as diagnostics and do not publish
them. This is declared expectation return under cooperative ownership; it does
not synchronize arbitrary source files or promise one atomic multi-file update.
