# Pandora

Pandora is an opt-in experiment for running heavy validation remotely while coding
agents, edits, and worktrees stay on a local Mac. The current SSH pilot runs one
Eichler borrower-web surface workflow on a dedicated Linux VM. It is not ready
for unattended daily use or twelve-agent concurrency.

## How it works

Start an agent through the session launcher. The launcher sets a private PATH
and shell configuration for that process and its children. A `pnpm` wrapper
recognizes supported validation commands and submits them over SSH. The agent
itself still runs locally with its existing CLI authentication.

```text
Local agent → normal pnpm command → frozen worktree snapshot → SSH worker
                                                            ↓
Local exit status + logs + artifacts ← bounded test container ← one slot
```

This is executable wrapping, not interception of all shell commands or an OS
resource policy. Other pnpm commands use the original executable. Absolute paths,
direct package commands, and explicit environment overrides can bypass routing.
No tracked target-repo files or global shell settings change.

Supported commands, from the target repository root:

```sh
pnpm test:surface borrower-web [file selectors]
pnpm validate surface borrower-web [file selectors]
```

The same forms with `pnpm run` work. Surface flags are rejected. There is no
automatic local fallback, source write-back, Mutagen session, or CI dispatch.
Required target-repo CI remains unchanged.

Each invocation captures dirty tracked files and nonignored untracked files,
with explicit exclusions for credentials and local dependencies. These exclusions
are not a general secret scanner. The client checks for changes during capture,
then uploads a frozen snapshot and verifies its manifest remotely. Later edits
do not change submitted input.

The worker reuses source files and a prepared dependency image. Each run gets
its own writable container, capped at two CPUs and 6 GiB RAM, with one Playwright
worker and a 20-minute container deadline. One heavy run executes at a time.
Extra requests wait and report worker occupancy. Admission is not FIFO.

The original shell invocation stays open until completion. Logs and known test
artifacts return locally. A repeated request from the same session/worktree
reports the active request and returns 75, even if source or selectors changed.
It does not replace or submit another run. Unknown cleanup state blocks a new
request in that session/worktree.

## Current evidence

The [September 19 agent trial](notes/remote-surface-2026-09-19-agent-trials.md)
records outcomes, failed setup attempts, and limitations. The
[machine-readable evidence](experiments/routing/evidence/2026-09-19.json)
contains timings and run identities.

- Claude Opus completed the normal command with ordinary instructions: 45 passed.
- Codex waited approximately 100 seconds for the slot, then reported 45 passed.
- Opus correctly reported an evaluator-controlled test failure and exit 1.
- Scripted probes verified duplicate rejection and explicit queued/running cancellation.
- Warm successful commands took approximately 115 seconds without queueing,
  including approximately 85 seconds of tests.

These are a few scenarios, not a reliability estimate. Three Codex routing
preflights failed before launcher fixes. The successful queued run did not need
human intervention, but initial integration did. Mac responsiveness under twelve
agents has not been established.

## Try the pilot

Use a disposable Linux x86 worker with Docker, systemd, Python 3, rsync, SSH, and
about 16 GiB RAM. The client needs Python 3.9+, rsync, SSH, Git, pnpm, and the
selected agent CLI. This runs trusted repository code under one trusted SSH user.

1. Build the pinned image using the [surface setup](experiments/surface/README.md).
2. Prepare a bootstrapped trial worktree with its own dependencies.
3. Run the [warm harness](experiments/warm/README.md) once to prepare the matching dependency image.
4. Launch a session from the target worktree using an absolute path to Pandora:

```sh
python3 /path/to/pandora/experiments/routing/launch.py \
  --host ubuntu@WORKER_IP \
  --state "$HOME/.local/state/pandora/pilot" \
  -- codex
```

Use `-- claude` for an interactive Claude session. The recorded Claude evaluation
used the headless helper; interactive Claude and Agentboard integration are not
yet validated. Check `command -v pnpm` inside the agent's shell before validation.
It must resolve to Pandora's `experiments/routing/bin/pnpm`.

See the [routing README](experiments/routing/README.md) for shell configuration,
Codex supervisor integration, and cancellation behavior. Agent routing refuses a
cold dependency build. When installation inputs change, an operator must prepare
the new image. The dependency recipe currently assumes Eichler's install inputs.

Keep local state and returned evidence until unresolved requests are reconciled.
Failed stopped containers and snapshots require manual retention cleanup. Cloud
provisioning and deletion are manual. Deleting the VM, rather than only stopping
workloads, is necessary to stop its instance billing.

## Next evaluation

The next bounded change should make interrupted execution recoverable, then
measure agent behavior under more contention:

1. Inject abrupt CLI death, SSH loss, OOM, and interrupted artifact download.
   Verify truthful status, bounded remote execution, and no duplicate submissions.
2. Add a recovery path that retrieves the original result and artifacts without
   requiring the agent to diagnose infrastructure. Resolve the client-loss policy
   from these tests: a bounded reconnect window or cancellation, not indefinite work.
3. Run four agents against one slot. Include waits beyond harness watchdog limits,
   piped output, repeated requests, and per-worktree source/result sentinels.
4. Compare normal command wrapping with blocking or redirecting recognized bypasses.
   Measure intervention, incorrect retries, local heavy execution, and correct outcomes.
5. Add deadline/cancellation coverage for cold image builds, bounded retention,
   repeatable worker setup, and verified Agentboard integration before daily use.

Advance toward twelve agents only after these checks pass. Generalize to journeys
or another repository afterward. There is no commitment yet to Pueue, a CI control
plane, multi-host scheduling, learned admission, or a general sandbox platform.

[DESIGN.md](DESIGN.md) describes the evaluation design and deferred choices.
The dated notes preserve what happened in each experiment; later merges do not
rewrite their historical claims.
