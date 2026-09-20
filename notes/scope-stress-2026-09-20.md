# Journey updates and shard scheduling: pre-scope stress test

This dated record evaluates proposed extensions. It does not change the v0.1
contract or claim the extensions are implemented. Pandora baseline:
`8acfc91148f7da0911c887ef729ec809ec9bd705`. Eichler planner baseline:
`5fddeb6b081a72af4d690a171c9e00cc007c3c01`.

## Decisions and boundaries

Gary accepted automatic return of tracked journey expectations with cooperative
ownership of those local fixture files while the command runs. This means the
agent or user does not simultaneously edit those same files. It does not change
filesystem permissions or lock out arbitrary editors. Detected conflicts preserve
local edits and return proposed changes separately.

Scheduling should be an operator-configured policy, invisible to coding agents.
Alternating waiting invocations at shard boundaries is a recommendation under
evaluation, not an implemented change to current FIFO. Running shards would not
be interrupted. Server expansion does not remove the need to budget each shard's
supporting services and internal test concurrency.

## Evidence and implications

| Claim | Evidence | Status | Consequence |
| --- | --- | --- | --- |
| Reuse existing journey partitioning | Executed actual Eichler planner against 200 catalog entries, 200 manifest entries and 198 fixture weights. Four shards contained 48, 55, 48 and 49 journeys. All 57 selected replay journeys were assigned, with 15, 12, 18 and 12 per shard. Repeated frozen input produced identical, disjoint, complete membership. | POC-confirmed for planning | Reuse the planner. This did not execute 200 journeys or establish runtime capacity. |
| Return each shard's entire route manifest | Actual `checkRoutes` reads and rewrites shared `write-routes.json`. A synthetic two-shard example overwrote shard A's changed entry with B's unchanged copy. | Contradicted | Extract deltas only for explicitly owned journey IDs, then merge centrally once. Reject overlapping ownership and unexpected edits. |
| Count successful responses to determine completion | Two identical receipts for shard 1 satisfied a naive count of two while shard 2 was missing. | Contradicted | Freeze expected task identities and verify unique receipts, snapshot/plan identity, cleanup and coverage. |
| Compare a file before atomic replacement to protect all writers | A deterministic open-file-descriptor editor wrote the old inode after replacement; its change was absent at the named path. | Contradicted | Use the accepted cooperative fixture-ownership contract. Atomic replacement alone is not conditional publication. |
| Existing stale-source check can remain unchanged | `route.py` checks whole-source digest before delivery. After partial tracked writeback, retry sees Pandora's own changes as stale and closes the active request. | Code-confirmed integration conflict | Recovery must accept exactly the attempt's recorded base or target values on declared output paths, while validating other source normally. |
| A lost worker can safely be reassigned | Hand trace leaves original shard execution unknown when its worker becomes unreachable. Current code has no multi-host fencing. | Unresolved | No transparent replacement of ambiguous running work. Distributed recovery needs explicit design/evidence. |

## Concrete state traces

Shared manifest: `base={A:oldA,B:oldB}`. Shard A returns
`{A:newA,B:oldB}`; shard B returns `{A:oldA,B:newB}`. Whole-file replacement ends
at B's version and loses A. Applying only A's owned key and B's owned key preserves
both updates. A missing download must never be interpreted as a requested deletion.

Publication recovery: the immutable intent is
`base={x:A,y:A}, target={x:B,y:B}`. The process applies x and dies before recording
that step. Retry recognizes `x==target`; `y==base` remains eligible. If y instead
contains C, retain C and report conflict. Never rerun the remote command to recover
local delivery. Multiple files are not one atomic filesystem transaction.

Shard completion: `expected={1/2,2/2}`; receipts `[a:1/2:pass,a:1/2:pass]` imply
`completed={1/2}`, not success. If the second worker is unreachable, its task
remains unknown, not failed-and-safe-to-replace. Once every expected task has
matching terminal and cleanup proof, aggregate evidence and deliver one invocation
result to the agent.

Scheduling illustration, not benchmark: two execution slots start A's first two
10-second shards; A has two further shards. B arrives at second 1 with one
one-second task. Whole-invocation FIFO starts B at second 20. Giving B a turn at
the next completion starts it at second 10 without interrupting any running shard.
This demonstrates a policy tradeoff, not measured production latency.

## External reference points

[Buildkite concurrency groups](https://buildkite.com/docs/pipelines/configure/workflows/controlling-concurrency)
and [job priorities](https://buildkite.com/docs/pipelines/configure/workflows/job-priority)
separate concurrency and priority controls.
[Kueue fair sharing](https://kueue.sigs.k8s.io/docs/concepts/fair_sharing/)
uses resource-sharing policy across tenants.
[Slurm scheduling](https://slurm.schedmd.com/sched_config.html) distinguishes
strict priority and backfill scheduling. These support making policy configurable;
they do not establish that Pandora needs their full machinery.

Python documents [atomic replacement](https://docs.python.org/3.13/library/os.html#os.replace),
not an atomic compare-content-and-replace operation. The local race POC supplies
the direct evidence for the narrower guarantee above.

## Journey-specific constraints from source review

Each shard must receive the same frozen catalog, route manifest and fixture weights.
The replay cover is computed globally before partitioning. The clean pass and its
selected dropped-response replay remain together. Replanning later shards against
already updated expectations would change the work assignment.

Shard-local `.journeys/results.json`, `errors.json` and `coverage.json` have fixed
names. Isolate shard workspaces and namespace returned evidence. Do not execute
multiple shard processes against one mutable checkout. Each isolated update may
produce proposals, but a single authority must merge them; no concurrent writer
may mutate a shared route manifest.

The direct runner's CI guard differs from the validation wrapper: the wrapper
rejects all `--update` under CI, while direct fixture/manifest code treats missing
and existing expectations differently. An explicit Pandora update mode must define
this environment behavior rather than accidentally relying on `CI=true`.

Coverage JSON is informational in the current journey runner. Its presence alone
is not a gate. Preserve the runner's status meanings and record planned/completed
replay identities; any stronger coverage acceptance rule requires its own explicit
decision rather than silently changing Eichler test semantics.

## Proposed implementation order, not ratified scope

First implement focused journey update return with a declared output allowlist,
immutable publication intent, source-aware retry and conflict artifacts. Prove the
ordinary agent loop and injected delivery interruption before enabling catalog
updates. A failed update run can retain proposed files for diagnosis without
silently publishing a partial catalog.

Then represent a suite as one invocation plus a frozen task plan. Reuse existing
partitioning, isolate tasks, aggregate exact expected membership, and merge update
proposals centrally. Prove this on the current worker with bounded task execution
before claiming multi-server recovery or throughput. Scheduling policy, resource
budgets and per-invocation concurrency belong in operator configuration; agents
should keep issuing ordinary validation commands.

Unresolved acceptance work includes a real multi-shard run and aggregate failure
injection, remote worker-loss reconciliation, full-catalog update failure policy,
resource/retention limits, and twelve actual coding-agent sessions. Neither a
planner POC nor a successful focused update closes those gates.

## Local publication POCs

The synthetic cooperative publication POC autoapplied an unchanged target and
preserved a detected local edit with a separate unified patch. An intentionally
naive multi-file writer demonstrated a mixed generation after interruption; its
blind retry was negative evidence, not the recommended design.

A separate journal POC stored immutable base/target values for declared paths,
including an explicit missing-file sentinel. All assertions passed:

- A conflict discovered during preflight wrote no files and created no journal.
- An injected interruption after writing x but before its receipt left the journal
  applying. Retry recognized x as already at its expected target.
- A subsequent edit to y survived retry. The result identified y as a conflict and
  did not perform pending additions or deletions after the conflict.
- New-file and deletion operations used missing/base/target checks.
- An unrelated file survived; the publisher never scanned a directory to infer
  which undeclared files should be deleted.

This is a local synthetic proof of the recovery logic under cooperative ownership.
It is not an integrated Pandora publisher, multi-file atomicity, or protection
against arbitrary concurrent writers. Root reran the journal assertions after
reviewing the POC and identifying the existing stale-source integration conflict.

## Real remote update POC

An isolated experiment adapter invoked the actual Eichler CLI for
`S0-01 --update`, then invoked the same CLI without `--update` against the generated
expectations. Both returned exit zero. The source fixture was based on
`fbeb008a283221bfedccc8fd47a6f6337d1ddf4d`; the local fixture worktree was not edited.
The remote attempt was `4834c74166064b9891f7e7d50de1ad8f`.

To exercise generation, only the disposable container copy had expectations
removed. Update mode ran with CI unset; subsequent validation ran with CI enabled.
The adapter suppressed raw CLI stdout/stderr because the single-journey CLI prints
principal handoff data. Production needs deliberate diagnostics handling, not
silent logs. Returned evidence included the before/after expectation files.

The verified terminal reported exit zero and cleanup true. Execution took
90.1 seconds for update plus validation; dependencies were a cache hit. This is
one focused update experiment, not a full-catalog or sharded update benchmark.
Returned files were retained as experiment artifacts, not automatically published
into the actual local source tree. The separate synthetic publisher POC supplies
only the local delivery evidence. End-to-end integrated automatic writeback remains
unproven.

The copied remote harness came from the retained source snapshot under attempt
`8773956111d6446f99de8ed8415c1187`, with only the POC journey adapter modified.
It predates the latest FIFO implementation; this was not another FIFO test.
Read-only postflight found no matching owned containers or networks, and the
service-cleanup receipt was verified with no errors.

The forced-absence setup removed the entire container-local route manifest, not
merely S0-01's key. Consequently, the returned manifest shrank from 129,427 bytes
to 1,646 bytes and contains the focused journey only. Publishing that file wholesale
would erase the other journeys' entries. This is additional evidence for declared
key ownership and central merging, not proof of a safe production manifest update.
The ledger stayed 189,610 bytes but changed per-run generated data. Ordinary
validation accepted it; review suitability of a production expectation diff remains
separate.

Sanitized output identities:

| File | Before SHA-256 | After SHA-256 |
| --- | --- | --- |
| S0-01.ledger.jsonl | `4c0f6a445cb26ff749bedee9ab6724e0e14a6ad74cce5757739e193bdf8be426` | `daba9931db9faccd809a2e8a8e9081d3cb436c6b5d48f82dd035b43b0c5c2f6b` |
| write-routes.json | `b2a5401d812a7476c10707c9ef6ce3e94983b91ffa761d1d916fd1e81753fb4a` | `b95135aec2a8b4b09d2c4a211c50aa93477b343fc34b7e3fc82756308dbacb3e` |

Ephemeral POC code and fixture artifacts were removed after this sanitized record
was written. No target-repository source or current Pandora behavior was changed.
