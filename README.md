# Pandora

Pandora runs selected heavy validation on a Linux worker while coding agents,
source edits, and Git worktrees stay on the local Mac. Agents use their normal
commands and existing CLI subscriptions. Installation does not change the target
repository or other engineers' shells.

The v0.1 implementation supports Eichler browser surfaces, service-backed journeys,
sharded full suites, expectation updates, and a bounded Docker build/run profile.
Final twelve-agent evaluation is in progress. [Issue #27](https://github.com/gbasin/pandora/issues/27)
tracks readiness against the [v0.1 contract](notes/v0.1-contract.md).

## Start a session

The client needs Python 3.10+, Git, rsync, SSH, pnpm, and the selected agent CLI.
Use a bootstrapped target worktree with its own dependencies. The worker needs
Linux x86, Docker with Buildx, systemd, Python 3, rsync, and SSH access. The current
profiles run trusted repository code under one trusted SSH user.

For a new worker, adapt [worker-config.example.json](experiments/warm/worker-config.example.json)
to its hardware and install it at `~/pandora-warm/worker-config.json` before the first
invocation. The example matches the evaluated two-slot worker. For an existing
worker, drain requests and use the operator migration procedure below. Do not
overwrite a live configuration.

From the target worktree:

```sh
python3 /path/to/pandora/experiments/routing/launch.py \
  --host ubuntu@WORKER_IP \
  --state "$HOME/.local/state/pandora/default" \
  -- codex
```

Use `-- claude` for Claude Code. For supervised Codex lanes, use the
[agent-fanout adapter](experiments/routing/README.md#supervised-codex-trial).
The recorded CLI evaluations are headless. Agentboard integration and interactive
preview forwarding have not been evaluated.

Inside the session, `command -v pnpm` must point to
`experiments/routing/bin/pnpm`. The launcher gives its children a private PATH and
shell configuration. Other pnpm commands delegate to the executable selected at
launch. Absolute paths, explicit PATH changes, and direct package commands can
bypass routing. This is command wrapping, not OS resource enforcement.

## Commands agents use

Run supported commands from the worktree root:

```sh
pnpm test:surface <borrower-web|desk> [file selectors] [--grep PATTERN] [--keep-going]
pnpm validate surface <borrower-web|desk> [file selectors] [--grep PATTERN] [--keep-going]
pnpm journey <id> [--fault dropped] [--update]
pnpm validate journey <id> [--fault dropped] [--update]
pnpm journeys [--update] [--keep-going]
pnpm validate journeys [--update] [--keep-going]
```

The same forms with `pnpm run` work. Unsupported options stop with feedback.
Fast standalone checks, unit tests, and builds remain local. A build required by
a remote test runs with that test. Target-repository CI stays unchanged.

The original command waits and streams progress. A second validation in the same
worktree reports the existing request and returns 75. It never replaces active
work. If the agent loses its shell wait handle, feedback prints the exact command:

```sh
pandora wait <attempt-id>
```

This follows and finishes the existing request, even while the original client
lives. Ctrl-C detaches this observer without cancelling remote work. An explicit
interrupt of the original validation command requests cancellation and verifies
cleanup. After client or SSH loss, retry recovers the accepted attempt. Keep the
same worktree path and launcher `--state` directory.

A completed result is not proof for later edits. Recovery checks the current
source and returns 75 for stale input. A newer request cannot be overwritten by
an older client. [Wait recovery evidence](notes/explicit-wait-2026-09-20.md) covers
these identity and publication fences.

## Source, results, and caches

Every invocation freezes dirty tracked files and nonignored untracked files.
Credentials, dependencies, caches, and registered nested worktrees are excluded.
These exclusions are not a secret scanner. A verified manifest binds remote
execution and returned evidence to the submitted bytes. Later edits do not change
accepted input. No continuous sync service runs.

The worker reuses source transfers and dependency images. Installation inputs key
the image, and a missing image builds automatically through bounded BuildKit with
a persistent pnpm package cache. Each run gets fresh writable containers and
private services. Databases, service state, and arbitrary compiler caches do not
persist between runs. Warm images reduce setup cost but do not skip source checks
or the workflow's build.

Logs and reports return under the printed absolute evidence path:

```text
<state>/<worktree-key>/<attempt-id>/
  submission.json       accepted command, source, configuration
  terminal.json         verified exit and cleanup evidence
  results/              reports, diagnostics, declared outputs
  publication/          delivery receipts and prior output generations
```

Surface suites build production and fixture assets once, then run native Playwright
shards against that build. Shard screenshots return with the compiled outputs.

Successful surface validation replaces `apps/<app>/dist` and
`apps/<app>/e2e/dist`. These are exclusively owned generated directories. Each
replacement is atomic; the two directories are not one transaction. Old generations
remain in the attempt. Failed-run outputs remain as artifacts. Interrupted delivery
resumes the same result without another execution.

Journey `--update` returns declared ledger fixtures and relevant entries in
`packages/scenarios/fixtures/write-routes.json`. During the command, do not edit
those declared files locally. Other source remains editable but can make a result
stale. Review the returned `git diff`, then validate without `--update`.

Conflicting local fixture edits remain intact. Pandora prints proposed-file paths
and `pandora resolve-expectations <id> --keep-local`. Manually merge the declared
files before running that command. It accepts local contents without validating
them; ordinary validation is still required. Full-catalog publication requires
all expected shards to succeed. Partial failed suites do not update fixtures.

Artifact delivery defaults to 2 GiB per invocation. Set
`--artifact-delivery-limit-bytes` to change it. Refusal preserves the remote result.
Raising the limit retrieves the same result without rerunning tests. A selected
Docker profile can override the session limit for Docker commands.

## Scheduling and operating limits

v0.1 schedules on one server. An operator-owned worker configuration limits CPU,
RAM, disk reservations, simultaneous execution, and per-invocation parallelism.
Reservations include dependency preparation and supporting services through verified
cleanup. A slot count never overrides the resource budget.

Fair turns between waiting invocations are the default configured policy. Strict
FIFO is available. Running shards are not interrupted to make room. Full suites
stop dispatching after their first test failure; `--keep-going` collects further
failures. Already-running shards drain. Infrastructure failure or deadline expiry
stops dispatch regardless of this flag.

`--suite-shards` partitions both browser and journey suites. It defaults to four and accepts 1–32. Shard count controls partitioning,
not simultaneous resource availability. `--queue-timeout-seconds` defaults to 900
and accepts 1–86400. A suite consumes one cumulative waiting budget only when work
is waiting and none of its shards is admitted. Execution has a separate deadline.
Accepted limits survive reconnection.

The evaluated worker has four CPUs and about 16 GiB RAM, with two admitted slots,
a 3.5 CPU / 13,000 MiB reservation ceiling, and a 1,500-second execution deadline.
Twelve local sessions therefore queue behind two heavy executions. More sessions
do not imply twelve simultaneous test containers. Increasing concurrency requires
more worker resources and a drained configuration change.

Use the [scheduler documentation](experiments/scheduler/README.md) and
[operator recovery procedure](experiments/warm/notes/operator-recovery.md) for
configuration changes. Never delete the ledger or an active request to unblock work.
A dead worker without terminal evidence remains unresolved after cleanup until an
operator explicitly acknowledges the loss. The client returns infrastructure failure
70, never a fabricated test result. The next deliberate invocation can then start.

Retention keeps ten prior completed local attempts and ten released remote attempts,
plus protected current and unresolved work. Three recent dependency images are kept,
with referenced images pinned. These are retention targets, not disk quotas.
Unresolved and older experimental data can consume additional disk. Low worker disk
space blocks new preparation. Provisioning and VM deletion remain manual.

## Optional Docker profile

Pass `--docker-profile /absolute/path/profile.json` to enable:

```sh
docker build [-f RELATIVE_DOCKERFILE] -t TAG .
docker run --rm [-v "$PWD:CONTAINER_PATH[:ro]"] TAG [COMMAND [ARG...]]
docker image rm TAG
```

The external profile declares allowed inputs, mounts, outputs, limits, and deadlines.
Tags are worktree-private and survive launcher restarts. Failed rebuilds retain the
previous image. Image-only runs use the built image; mounted runs freeze current
local input. Unsupported Docker commands stop without local fallback, including
sessions without a Docker profile. See the [exact Docker contract](experiments/docker/README.md).

## Verification and boundaries

The evidence includes full 200-journey validation, an eight-shard full update and
ordinary validation of all returned expectations, real Codex/Opus repair and
conflict-resolution loops, generated-output recovery, Docker image isolation,
client/SSH loss, worker loss, OOM, deadlines, disk exhaustion, and artifact-limit
recovery. Key records:

- [Full catalog return and validation](notes/catalog-return-validation-2026-09-20.md)
- [Configured overlap and resource fault probes](notes/multislot-resource-probes-2026-09-20.md)
- [Real-agent expectation conflicts](notes/expectation-conflicts-2026-09-20-agents.md)
- [Worker-loss recovery](notes/operator-recovery-2026-09-20-journey.md)
- [Docker coding loops](notes/remote-docker-2026-09-20-routing.md)
- [Dependency isolation](notes/remote-surface-2026-09-20-dependency-isolation.md)
- [Compiled build invalidation](notes/compiled-build-2026-09-20.md)
- [Initial twelve-agent trial and its failed readiness gate](notes/agent-ramp-2026-09-20-initial.md)

These controlled trials are not long-term reliability measurements. Multi-server
placement, arbitrary Docker/Compose commands, detached services, interactive previews,
native macOS/iOS builds, continuous source sync, universal interception, and a CI
control plane are outside v0.1. The [contract](notes/v0.1-contract.md) records the
accepted scope. [DESIGN.md](DESIGN.md) preserves the earlier pilot rationale.
