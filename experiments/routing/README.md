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
disables login-shell startup for this session. Put the settings on `codex exec`
when using that subcommand. Harness supervisors may reset inherited PATH, so
launch routing inside the supervisor at its actual Codex entry point.

The [official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
documents shell environment overrides and `allow_login_shell`. Runtime preflight
must still verify executable resolution; inheriting PATH at the outer launcher
was insufficient in this experiment.

## Active request and cancellation

A local file lock and durable descriptor protect one active request per
session/worktree. A repeated invocation returns exit 75 with the existing-request
message. Changed selectors or source do not replace the active request. A normal
terminal result permits a deliberate new run.

An interrupted client leaves the descriptor active until terminal remote state
can be verified. A later invocation reports that state rather than starting
another job. Result recovery after abrupt client loss is not automatic yet;
the current response requires operator review and does not claim a test pass.

Explicit SIGINT/SIGTERM stops the local submission process group, creates a
remote cancellation marker, signals the registered worker, and checks terminal
cleanup. The worker registers before checking that marker, so a late-starting
worker observes cancellation. PID checks include the Linux process start time.
Unknown remote state keeps new requests blocked.

Worker output goes into ordinary files. Separate tail processes provide the live
stream. This prevents a broken SSH output pipe from preventing the worker's
terminal record. Test containers also retain their independent 20-minute deadline.
Cold image builds remain outside the agent trial until their cancellation and
independent deadline are addressed.

## Evaluation boundaries

The current controller supports the initial informed and ordinary-command tests.
Do not treat it as production admission control. The remote lock is not FIFO;
source capture can occur concurrently; retention is manual; and there is no
universal shell interception. Disconnect recovery, startup races, and other
failure paths still require adversarial testing.

The Claude helper runs the user's subscribed `opus` alias with shell/read tools
and a validation-only brief. It retains ordinary account authentication. The
Codex trial uses the existing subscribed CLI and the agent-fanout watchdog.
Neither helper configures a model API key or changes the default model globally.
