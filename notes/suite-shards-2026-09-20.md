---
status: log
---

# Frozen suite plans and isolated shard evidence, 2026-09-20

This change adds a private execution foundation beneath Pandora's launcher.
It does not enable `pnpm journeys` or ask coding agents to dispatch shards.
The implementation reuses Acme's planner and actual plural suite CLI. Parent
invocation recovery, cumulative waiting budgets, fair admission, and catalog
update publication remain separate work.

## State walk

The existing worker owns one immutable attempt and one FIFO lease. It has no
parent invocation or expected shard set. Counting returned reports cannot prove
that a suite finished.

For the new foundation, let `source=H`, `plan=K`, and expected tasks be
`{1:[A],2:[B]}`:

- Plan attempt P returns K, bound to H and exact catalog/replay/shard membership.
- Shard X carries `{plan:K, source:H, shard:1}`. The worker recomputes K from
  frozen source before starting Workers. Its writable source and services are private.
- X returns `{results:[A], unrun:[], exit:0, cleanup:true}`. Aggregating only X
  rejects missing shard 2. Aggregating X twice rejects duplicate shard 1.
- A source edit produces H2. Submission against K fails before remote execution.
  Later shards cannot replan against edited expectations.
- Y carries `{plan:K, source:H, shard:2}`. Losing its client does not replace the
  accepted attempt. Existing systemd ownership and same-attempt retrieval apply.
- If Y returns a failed B, complete aggregation returns failure and retains its
  diagnostic summary. If Y has no valid terminal/evidence, aggregation rejects
  incomplete evidence. Neither path can claim a pass.
- With valid passing X and Y, aggregation checks the exact partition, plan/source
  identity, report checksums, cleanup, and replay identities before returning zero.

There is no fixture publication in this interface. The suite CLI receives no
update flag, and the adapter compares expectation hashes before and after it runs.
Known result files are removed inside each isolated container before execution,
so inherited reports cannot stand in for a failed child process.

## Local evidence

All 97 tests passed: 58 worker/adapter tests, 32 routing tests, and seven generated
output publication tests. New probes cover canonical plan identity, reordered and
missing shards, duplicate journey results, source mismatch, partial infrastructure
failure, shard-local coverage totals, normal versus unsupported request shapes,
actual CLI environment, and stale report removal. Mocked adapter probes are
separate from the VM evidence below.

Review corrected two assumptions before the VM trial: scenario IDs need their
numeric suffix, and coverage totals describe one shard rather than the entire
catalog. Aggregate errors retain unrun IDs and failed-shard details. Informational
uncovered routes remain informational, preserving Acme's test semantics.


## Fail-fast decision

Gary selected stopping new dispatch after the first test failure as the default,
with an optional `--keep-going` flag for collecting further test failures. Running
shards finish within their deadlines. The parent must record deliberately unrun
tasks, rather than infer intentional stopping from absent receipts. This decision
is in the v0.1 contract; the private foundation still requires every requested
shard receipt for aggregation. Its negative trial intentionally executes both
shards to prove that complete failure evidence stays a failure.

The plan digest identifies immutable work, not a coordinated invocation. The
private aggregator accepts independently verified attempts explicitly supplied
by the operator. The future parent must bind its expected shard attempts before
dispatch, retain those identities across recovery, and check them before aggregation.

## VM evidence

Seven sequential attempts ran on the authorized OVH worker against Acme
`8fda4d56c3251bc887526e98f033608a30e2a52b`. The full plan used Pandora
`345a375`; all remaining attempts used `73ee219`.

| Attempt | Total seconds | Execution seconds | Exit |
| --- | ---: | ---: | ---: |
| Full catalog plan | 103.9 | 8.3 | 0 |
| Focused plan | 20.5 | 4.7 | 0 |
| Focused shard 1 | 65.5 | 51.8 | 0 |
| Focused shard 2 | 66.5 | 49.7 | 0 |
| Negative plan | 23.2 | 4.8 | 0 |
| Negative shard 1 | 65.8 | 49.9 | 0 |
| Negative shard 2 | 43.3 | 30.9 | 1 (expected) |

Full planning selected 57 replay IDs from 200 journeys and partitioned them into
shards of 48, 55, 48, and 49 journeys. This was planning evidence, not execution
of the full catalog. The first request spent 79.0 seconds preparing dependencies;
all subsequent requests reused the dependency image.

The focused plan selected S0-01 and S0-02 across two shards. Both passed, including
replay, and aggregation returned zero. A separate snapshot with a deliberately
incorrect S0-02 ledger produced one passing shard and one fixture-drift failure.
Its aggregate returned one and preserved the failure. Missing-shard, duplicate-shard,
and mixed-plan aggregation each returned 75 instead of claiming completion.

All seven terminals verified cleanup. The final worker inspection found no running
Pandora units, containers, or networks. The temporary ledger change was restored
and byte-compared with its backup; the evaluation worktree was clean.

Local receipts, requests, logs, aggregate results, and timings remain under
`~/.local/state/pandora/suite-shards-20260920/`, with `summary.json` as the index.
These sequential backend trials do not establish concurrent suite scheduling,
parent recovery, full-catalog execution, or twelve-agent readiness.
