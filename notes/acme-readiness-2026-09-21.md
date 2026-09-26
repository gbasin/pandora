---
status: log
---

# Acme readiness triage, 2026-09-21

Question: is Pandora ready for daily Acme agent work, and what stands between
here and there? Pandora was inspected at `370b7d8` (`origin/main`), Acme at
`16c9ead55`. Five read-only investigations covered Pandora's configuration and
coupling, scheduling and sharding, readiness evidence, Acme's agent workflow,
and Acme CI demand. One local measurement of `pnpm check` was taken on the
owner's Mac. Owner decisions from the review session are recorded at the end.

## Verdict

v0.1.1 is implemented, fully on `main`, and backed by real-VM evidence: twelve
concurrent headless agent loops, 24 of 24 receipts verified, no infrastructure
failures, and result fences that never fabricate a pass (75 stale or duplicate,
70 infrastructure). It was validated as a supervised experiment. It is ready for
a supervised pilot on the routed command set. It is not ready as unattended
shared infrastructure, and it is not repo-agnostic.

The sixteen `origin/*` branches not merged into `main` are squash-merge residue
with no content missing from `main`.

## Configuration surface

Worker: Linux x86, Docker with Buildx, systemd, Python 3, rsync. The SSH user
must be named `ubuntu` (`experiments/warm/warm.py:253`) with passwordless sudo.
`~/pandora-warm/worker-config.json` is hand-written, has no defaults, and its
absence silently selects a one-slot legacy mode in which surface sharding fails.
There is no provisioning script. Everything after that file is automatic:
content-addressed helper upload, runtime image, dependency image.

Client: `launch.py --host --state [--suite-shards] [--queue-timeout-seconds]
[--artifact-delivery-limit-bytes] [--docker-profile] -- <agent> [agent flags]`.
Agent flags such as `codex --yolo` pass through unchanged. No `ACME_*`
variable is read. Nothing is installed in the target repository.

## Coupling to Acme

About 60% of `experiments/warm` and most routing plumbing is repo-agnostic:
admission, scheduling, snapshot and transfer, retention, recovery, publication.
Everything that touches a command is Acme-specific:

- Dispatch is literal argv branching in `experiments/routing/commands.py`, with a
  duplicated suite table in `experiments/warm/validation_request.py`.
  `notes/acme-command-catalog-2026-09-21.json` is referenced by no code.
- App names, `apps/<app>/dist`, fixture paths, the journey-id pattern (five
  sites), Postgres credentials, service image digests, and the pnpm and
  Playwright pins are constants.
- `experiments/warm/validation-stack.mjs:48-94` edits Acme's
  `startInstance({...})` call textually and depends on option-key order.
  `validation.mjs:161-184` rewrites turbo filters. `suite.mjs` and `journey.mjs`
  import Acme `.ts` internals by absolute path.

Adding one command touches three to five files. A second repository needs
roughly 2–3k lines of new adapters.

## What Acme agents route and what stays local

Routed: surfaces, journeys including `--update`, and the unfiltered forms of
`test:unit`, `test:tools`, `test`, `validate agent-web`, `test:employee-browser`,
`test:browser-integration`, `test:mockup-browser`, `test:postgres api|scenarios`.

Local by necessity: `dev:stack` and previews, iOS simulator work, the 30-second
per-edit oxc hook, git, `gh`, deploys.

Local and still loading the Mac: `pnpm check`, `test:native-unit`, per-worktree
`pnpm install`, focused tests. Anything invoked through `npx`,
`node tools/validate.mjs`, `turbo`, or an absolute pnpm path bypasses the shim
silently. A routed command run from a subdirectory exits 64 instead of falling
back (`route.py:221-224`).

Pueue is bypassed on the worker. Acme's local machine budget and Pandora's
queue do not know about each other.

Acme's `AGENTS.md` does not mention Pandora.

### `pnpm check`, measured

Local receipts, 139 passed runs: execution p50 40 s, p90 155 s, maximum 377 s;
queue p50 under 1 s. One instrumented run in the main checkout at load average
30–40 on ten cores:

| Step | Wall | CPU | Cores |
|---|---|---|---|
| Six docs checks and design vitest | 4 s | 3 s | ~1 |
| `.github` node tests, 52 tests, concurrency 2 | 16 s | 11 s | 0.7 |
| oxfmt and oxlint | 4 s | 3 s | <1 |
| clock, parity, copy, catalog scripts | 31 s | 10 s | 0.3 |
| turbo typecheck, cache hit | 1 s | 0.5 s | — |
| turbo typecheck, `--force`, `--concurrency=2` | 176 s | 162 s | 0.9, 1.3 GB RSS |

`check` never uses more than about one core. Its tail is a genuine typecheck
cache miss on a starved machine: turbo asked for two cores and received 0.9.
Turbo 2.10.5 already shares one cache across all worktrees (no worktree has its
own `.turbo/cache`), so misses come from changed TypeScript, not cold worktrees.
At the time of measurement the Mac ran 25 agent processes, with `fseventsd`,
Spotlight, `find` and `git` busy across 135–194 worktrees and 4 GB of swap in
use; the 15-minute load average was 67.

Routing `check` would not make it faster (a slot is one throttled CPU plus
13–22 s overhead). It would remove about 160 CPU-seconds per cold run from the
Mac, on the order of one CPU-hour per day. This is one sample under load; repeat
it on a quiet machine before relying on the step ratios.

## Sharding and scheduling

Surfaces use Playwright `--shard=i/n` after a build-once planner; journeys use
Acme's duration-weighted `shardJourneys`. Each shard is a separately admitted
attempt. Aggregation requires every shard report, exact membership and verified
cleanup; conflicting shard outputs return 75. Shard concurrency is bounded by
`max_parallel` (2), not by `--suite-shards`. There is no multi-host placement.

Reservations are static per role: 1000m/4096 MiB for the main container, plus
db, pool and proxy (1500m/4864 MiB total) for service-backed work. A cold
dependency build reserves 2000m/6144 MiB and a worker-wide exclusive token. The
policy is fair-turn or FIFO with no backfill; a dead running owner blocks all
admission until an operator acknowledges it. Reconfiguration is a drained
change through `operator_recovery.py migrate`.

Extra cores cannot be used inside a run: Playwright `--workers=1`
(`surface-runner.mjs:28`), `JOURNEY_CONCURRENCY=1` (`suite.mjs:198`),
`workers = min(2, cpu_millis // 1000)` (`validation.py:29`). Measured runs sat at
about 0.9 CPU with 60–85% of CFS periods throttled. The worker is slower than
the M1 Pro on most suites; `test:unit` and `agent-web` are close (`pnpm test` 674 s versus 2.9 min). A
larger worker buys less queueing, not faster runs, until those caps are lifted.

Warm per-invocation overhead is 13–22 s. Source capture occasionally stalls
61–109 s for unknown reasons.

## Sizing

Slots ≈ min(⌊(cores − 1) / 1.5⌋, ⌊(RAM_MiB − 2048) / 4864⌋).

| Worker | Slots | Disk | Status |
|---|---|---|---|
| 4c / 16 GiB | 2 | 96 GB | measured; 12 agents, max queue 489 s |
| 8c / 32 GiB | 4 | ≥100 GB | extrapolated |
| 16c / 64 GiB | ~10 | ≥250 GB | extrapolated |
| 32c / 128 GiB | ~20 | ≥250 GB | extrapolated |

Acme CI for comparison: a full merge-queue pass is about 100 vCPU-minutes;
peak observed demand was 14 concurrent runs and 33 runs per hour, very bursty
(3.6 days of retained history). Nothing above two slots has been run. Untested
at scale: SQLite ledger polled at 1 Hz by every waiter, head-of-line blocking,
an unexplained intermittent Docker inventory failure, BuildKit concurrency,
rsync fan-in, retention targets that are not quotas, and
`browser-integration` peaking at 3.4 GiB of a 4 GiB cap.

Recommendation: 16c / 64 GiB x86 with 250 GB or more NVMe, starting at
`max_running` 4–6 and ramping with ledger and dockerd observation.

## Blockers before daily use

1. Attachment. The launcher wraps a session only at start. Interactive Claude
   Code and Agentboard were never evaluated. `agent-fanout init` exits 141 on
   Acme (#14, #51).
2. Operations. One hand-provisioned VM, no provisioning script, no monitoring,
   manual recovery after worker death, retention without quotas.
3. Two slots.
4. Silent local execution of everything unrouted; no signal about residual Mac
   load.
5. Textual coupling to Acme internals.
6. Unresolved defects: intermittent Jest timeout (Acme #1318); 61–109 s
   capture stalls.

## Multi-user

For mutually trusted engineers this is days of work: make the `ubuntu` user
configurable, add a client or user identity to the worktree key (currently a
hash of the path only, `route.py:245`), per-user fair share, provisioning,
per-user keys, and automatic reconciliation of dead owners. Isolation between
users is the large version: every run is root-equivalent through `sudo docker`.

## Owner decisions, 2026-09-21

- Goal: offload first. Latency is not the pilot's objective.
- `pnpm check`: not routed until measured properly; the measurement above shows
  it is a victim of Mac saturation rather than a cause.
- Entry points: all of interactive Claude Code, agent-fanout Codex lanes,
  Agentboard and headless CLIs must route, including launches with agent flags.
- Pandora must become repo-agnostic. Scope a rewrite of the adapter layer around
  a repo-owned configuration and prove its ergonomics with a POC
  (`poc/repo-config`).
- Worker unreachable or queue timeout: fall back to running the command
  locally. This reverses the current fail-closed exit 70 for that case; it must
  not apply to lost or ambiguous results of accepted work.
- Codex must route without `--yolo`. Evaluate the in-sandbox shim against a
  client daemon outside the sandbox (`experiment/codex-sandbox`).
- Users: the owner plus one to a few trusted engineers.
- Worker upgrade: deferred until this work completes.
