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

The worker reuses source files and a dependency image keyed by installation inputs.
A cache miss automatically prepares the image remotely with a bounded BuildKit
builder and a persistent pnpm package cache. Each run gets
its own writable container, capped at two CPUs and 6 GiB RAM, with one Playwright
worker and a 20-minute container deadline. One heavy run executes at a time.
Extra requests wait and report worker occupancy. Admission is not FIFO.

The original shell invocation stays open until completion. Logs and known test
artifacts return locally. A repeated request from the same worktree and state directory
reports the active request and returns 75, even if source or selectors changed.
It does not replace or submit another run. Unknown cleanup state blocks a new
request in that worktree. Retrying after client loss recovers the existing attempt
and verifies its artifacts. A changed local source produces a stale-result notice
and exit 75, rather than a pass for newer edits.

Successful validation publishes `apps/borrower-web/dist` and
`apps/borrower-web/e2e/dist` at their normal local paths. Each directory is
replaced atomically, with its previous generation retained in the attempt's
`publication/` directory. These are exclusively managed generated outputs, not
source writeback. The two replacements are individually atomic, not one
transaction. A failed publication keeps the completed attempt active; retrying
the same command finishes delivery without running tests again. The local state
and worktree must be on the same filesystem supporting directory exchange.

## Current evidence

The [integrated repair trial](notes/remote-surface-2026-09-20-integrated-repair.md)
records three successful diagnose/edit/rerun samples each for Codex and Opus,
automatic dependency preparation, returned build directories, and delivery
recovery without duplicate execution.

The [recovery and contention trial](notes/remote-surface-2026-09-19-recovery-and-contention.md)
records the earlier recovery results. The [initial agent trial](notes/remote-surface-2026-09-19-agent-trials.md)
preserves earlier setup failures and outcomes.

- Abrupt client death, interrupted artifact retrieval, and a 55-second SSH outage
  recovered the original attempt. Changed source was not reported as newly tested.
- A container OOM returned 137 with verified cleanup.
- Two Codex and two Claude Opus agents each reported 46 passed, including distinct
  source sentinels. Queue waits ranged from zero to 290 seconds.
- A Codex agent waited 700 seconds with live output hidden by `tail`, then reported
  the passing result without replacing or cancelling its run.
- For one direct command, normal wrapping allowed local execution, blocking made
  Codex stop, and an exact redirect completed remotely.

These are small controlled scenarios, not reliability estimates. Workload samples
and prompts are documented in the notes. No twelve-agent or interactive Mac
responsiveness claim follows from them.

The launcher still changes executable precedence. It preserves the existing
Codex login-shell policy and avoids a forced Codex PATH setting, which discarded
login-profile additions in a compatibility probe. Manual PATH overrides and
absolute executable paths can bypass routing. Ordinary pnpm delegation uses the
executable selected at launcher start. See the [environment details](experiments/routing/README.md#environment-compatibility).

The [output UX trial](notes/remote-output-2026-09-20-agent-ux.md) separately tests
returned build artifacts and generated source with Codex and Opus. Its fixture
is evaluator-only and does not extend the production routing commands. The
[generation-publication follow-up](notes/remote-output-2026-09-20-generation-publication.md)
tests automatic replacement of existing generated outputs with three fresh
samples per agent and delivery-recovery probes.

## Try the pilot

Use a disposable Linux x86 worker with Docker, systemd, Python 3, rsync, SSH, and
about 16 GiB RAM. The client needs Python 3.10+, rsync, SSH, Git, pnpm, and the
selected agent CLI. This runs trusted repository code under one trusted SSH user.

1. Install Docker with Buildx, systemd, Python 3, and rsync on the worker.
2. Prepare a bootstrapped trial worktree with its own dependencies.
3. Launch a session from the target worktree using an absolute path to Pandora:

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
Codex supervisor integration, and cancellation behavior. Cold dependency builds
run automatically, with a 15-minute preparation deadline, two CPUs, and 6 GiB
RAM without swap. The dependency recipe assumes Eichler's installation inputs.

Keep local state and returned evidence until unresolved requests are reconciled.
New integrated runs retain ten prior completed local attempts and ten released
remote attempts, plus protected current/latest and unresolved work. Old output
generations live as long as their attempt. Three recent dependency images are
kept, with images referenced by retained containers pinned. Earlier experiment
data is excluded from these sweeps. These are retention targets, not hard disk
quotas; low worker disk space blocks new preparation. Cloud provisioning and
deletion are manual. Deleting the VM, rather than only stopping
workloads, is necessary to stop its instance billing.

## Next evaluation

The next milestone is a controlled full coding-loop evaluation before daily use.
The [v0.1 contract and evaluation matrix](notes/v0.1-contract.md) records the
agreed scope and proposed command semantics. It requires local edit/test/fix
iteration, one service-backed workflow, specific Docker build/run patterns,
worktree-scoped image tags, and conflict-checked return of declared outputs.
The surface pilot now covers the edit/test/fix loop and publishes two generated
build directories. Service-backed routing and Docker command routing remain
unimplemented; their component probes do not establish the integrated behavior.

Evaluate source consistency, cache invalidation, output recovery, and parallel
worktree isolation before increasing concurrency. Include Codex and Claude Opus.
Warm-image reuse alone does not establish compiled-build performance. The matrix
requires cold builds, identical reruns, source edits, and dependency changes.

The current [pilot design](DESIGN.md) documents the narrower implemented trial.
The dated notes preserve what happened in each experiment; later merges do not
rewrite their historical claims.
