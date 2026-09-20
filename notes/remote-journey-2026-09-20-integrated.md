---
status: log
---
# Integrated service-backed journey, 2026-09-20

This trial added `pnpm journey S0-01` and `pnpm validate journey S0-01` to the
existing SSH route. Agents stayed on the Mac. Pandora captured their current
worktree, reused the dependency image, queued the request, and returned a local
journey report. No tracked Eichler configuration changed.

## Execution boundary

The source was Eichler `fbeb008a283221bfedccc8fd47a6f6337d1ddf4d`. A trial-only
foundation, `242d1a468`, added an unconditional throw in S0-01. Each agent received
its own nested worktree and frozen dependency installation. Neither dependency
inputs nor package versions changed during repair.

The same worker lease covers dependency preparation, private service startup,
the journey and cleanup. The runner has two CPUs and 6 GiB, while Postgres,
PgBouncer and the WebSocket proxy each have half a CPU and respectively 768,
256 and 128 MiB. These are aggregate per-container limits, not a host-wide cgroup.
The service images are digest-pinned. They share the runner's network namespace
on a private bridge; there are no published host ports or Docker socket mounts.
Each attempt gets a fresh database. pnpm packages and dependency images stay warm.

The runner calls Eichler's external stack mode, loads S0-01, runs its normal
journey and route checks, and stops the stack. Principal printing is disabled.
Worker startup output is suppressed because it contains generated credentials;
Pandora prints phase progress instead. Journey failure details and a checksummed
JSON report return locally. Arbitrary journey flags and the full suite are not
implemented by this profile.

## Fault probes

The [lifecycle evidence](../experiments/services/evidence/2026-09-20-integrated/lifecycle.json)
records the injected cases. Each checks both container and network inventories.

- Explicit cancellation returned 130 with verified cleanup.
- Killing only the local transport returned 137. Repeating the same ordinary
  command recovered the same attempt and returned its successful result.
- Killing the remote worker with SIGKILL invoked systemd's cleanup hook and
  removed its resources. No terminal result was fabricated. That worktree's
  request remains unresolved and requires operator reconciliation.

The independent service deadline requests worker stop after 20 minutes; the
worker also has a 40-minute overall deadline. This trial exercised worker stop
and SIGKILL cleanup, not a 20-minute wall-clock expiration or host reboot. Unknown
Docker state leaves cleanup pending. The explicit state trace is in
[journey-lifecycle-2026-09-20.md](journey-lifecycle-2026-09-20.md).

An initial baseline and repaired rerun passed. The injected throw returned exit 1
and `clean: Pandora seeded journey fault`. Successful worker execution took
49–52 seconds, including service startup and teardown. The injected early failure
took about 10 seconds. All used the existing dependency image, with approximately
0.05 seconds of image lookup and no dependency rebuild.

## Friction encountered

The first lifecycle submission hit an existing retention defect before starting
validation: earlier Docker artifact copies were root-owned, so the worker could
not delete their contents. Both workflow paths now give returned results to the
worker user without following symlinks. Verified earlier trial results received
the same ownership repair. The failed submission's log is retained in the evidence
folder. An already partially deleted old attempt remains outside automatic
retention because its metadata was removed before the failure.

The agent-fanout controller's previously filed startup SIGPIPE defect
([Pandora #14](https://github.com/gbasin/pandora/issues/14)) recurred on three init
attempts. No run was created by those attempts. A subsequent init succeeded;
the controller itself was not changed. One Opus dispatch initially omitted the
required brief option and was rejected before starting an agent; the corrected
dispatch succeeded.

## Agent evaluation

One fresh Codex sample and one fresh Claude Opus sample completed the same repair
under the agent-fanout controller, concurrently with each other and an evaluator's
borrower-web smoke run. Both ran the normal journey command before editing, read
the failure JSON, removed only the unconditional throw, reran, and read the
successful JSON. Their resulting journey source matched the original file
byte-for-byte. Each submitted exactly two remote executions. No human coaching,
cancellation, restart, direct SSH/Docker operation, or local validation fallback
occurred during either agent run.

| Agent run | Queue seconds | Execution seconds | Result |
| --- | ---: | ---: | --- |
| Opus, seeded fault | 0.05 | 10.59 | Expected failure |
| Codex, seeded fault | 0.05 | 10.75 | Expected failure |
| Opus, repaired | 70.05 | 50.36 | Pass |
| Codex, repaired | 140.35 | 50.45 | Pass |

Every run reused the same dependency image. Codex read source while its initial
validation was pending but did not edit until it received the failure. It
explicitly reported waiting through its existing shell handle for the queued
rerun. Opus piped output through `tail`, which masks the pipeline's shell status
and delays visible queue feedback, but it read the report and correctly identified
both results. No global shell semantics were changed to compensate.

Both agents reported a harmless duplicate deadline-timer stop warning. The
redundant stop was removed after their trials, followed by a final real journey
check. The agent evidence reflects the warning-bearing revision, not a fabricated
warning-free transcript.

The mixed-workflow surface regression passed all 49 smoke tests and published
both declared build directories. Twenty-seven Python tests passed across routing,
transport, cleanup, snapshots, retention, and output publication. Evidence is in
[the integrated evidence directory](../experiments/services/evidence/2026-09-20-integrated/).
Raw controller reports and dirty evaluator worktrees remain local for inspection.

## Remaining limits

This is one service-backed workflow on one trusted worker. It does not establish
arbitrary Compose compatibility, direct Docker command routing, twelve-agent
capacity, host-reboot recovery, or automatic reconciliation of missing terminal
records. Service runtime state is disposable; only dependency caches persist.
Queued jobs are not FIFO. The tests explicitly instructed agents to wait and to
use the normal validation command, so they do not prove unprompted discovery.
