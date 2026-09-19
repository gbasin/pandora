# Session-only validation routing

Launch a trial session with the private executable directory prepended to PATH:

```sh
python3 launch.py --host ubuntu@WORKER_IP --state /tmp/pandora-trial-state -- codex
```

For a scripted check, replace `codex` with `pnpm test:surface borrower-web
smoke.spec.ts`. Run from a bootstrapped trial worktree. The matching dependency
image must already exist on the worker. This agent trial refuses a cold image
build instead of letting agents modify the shared environment.

Only these command forms route:

- `pnpm test:surface borrower-web [file selectors]`
- `pnpm validate surface borrower-web [file selectors]`
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
Cold dependency builds remain excluded from agent routing.

## Evaluation boundaries

The current controller supports the initial informed and ordinary-command tests.
Do not treat it as production admission control. The remote lock is not FIFO;
source capture can occur concurrently; retention is manual; and there is no
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
SSH client. Queue admission expires after 15 minutes, test containers after
20 minutes, and the whole worker after 40 minutes. Explicit cancellation still
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
