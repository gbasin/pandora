---
status: log
---

# Remote suite parent, 2026-09-20

This slice connects normal `pnpm journeys [--keep-going]` commands to one remotely
owned suite invocation. The coding agent remains local. No target repository
files or global shell settings change.

## State walk

The previous implementation had independent plan and shard attempts. A local
loop around them would stop dispatching when its client died. A content digest
could not prove which executions belonged to the same invocation.

The new flow uses these explicit states:

- Before submission: `local={active:P, source:H}` and `remote={}`. The existing
  worktree request lock prevents a second local invocation from replacing P.
- After acceptance: `remote={parent:P, source:H, children:[A,B,C]}`. The parent
  writes all child identities before staging A. Its systemd unit owns child
  processes. The SSH connection does not own them.
- Planning: `parent={completed:[], wait:0}`, `A={task:plan, source:H}`. A acquires
  the shared FIFO lease. P holds no worker lease. A returns frozen plan K.
- After planning: `parent={completed:[A], plan:K, wait:3}`. If the configured queue
  budget is 10 seconds, B receives 7 seconds, not a fresh 10. B reads the same
  immutable source H, with private writable containers and services.
- Client loss: `local={disconnected}`, `parent={running:B}`. Remote execution
  continues. Reconnecting follows P. It does not freeze current source or submit
  A or B again. A changed launcher shard count cannot alter accepted work.
- Default failure: B returns a verified test failure. `parent={completed:[A,B],
  stop:test-failure, unrun:[C]}`. C keeps its reserved identity but is never staged.
  The summary names its unrun journeys. Missing evidence is not converted into a
  synthetic skipped-shard receipt.
- Keep-going failure: B returns a test failure. `parent={completed:[A,B], wait:5}`.
  C receives the remaining 5 seconds. Its result joins B's failure, so the final
  invocation still exits one. Infrastructure failures stop dispatch in either mode.
- Worker loss: a staged B has no terminal receipt. Resource cleanup can release
  its FIFO barrier after proving no resources remain. `parent={unresolved:B}`
  still cannot claim complete evidence. No replacement is started.

The first dispatcher is sequential. Returning the lease after each child lets
already queued focused work get a turn. Configurable parallel dispatch and fair
turns across concurrent suites remain separate work. One cumulative queue budget
covers planning and shards; preparation, execution, and evidence collection share
a 25-minute allowance outside queue time. Existing child deadlines also apply.

## Review findings

Review caught a streaming race: a parent writing a child's log could race the
child's artifact checksum. Child processes now write their own log files; the
parent only reads them for progress output.

Failed planning now returns a separate verified failure receipt because no valid
plan exists yet. A successful parent requires the exact reserved child receipts,
matching source and plan, complete membership or an explicit stopping reason,
and verified cleanup. Parent queue accounting is checked against child receipts.

The cleanup path distinguishes an unstaged reservation from a staged child with
missing terminal evidence. Both can have no running resources, but only the first
is known not to have executed. The second keeps the parent unresolved.

## Limits

The parent survives client loss. A crashed remote parent is not restarted.
Explicit cancellation remains distinct from losing the client process. Neither
client recovery nor the dispatcher replaces ambiguous remote work.

Normal routing currently accepts the full catalog with an optional `--keep-going`.
Legacy environment overrides stop with feedback instead of silently changing the
plan. Private operator requests can select exact IDs for bounded evaluations.
Catalog `--update`, configurable concurrent dispatch, and twelve-agent readiness
are not established by this change.

## Verification

All 126 local tests passed across worker, routing, and output-return modules.
This includes cumulative waiting across child attempts, strict child identity
binding, missing receipt rejection, failed planning, cleanup with a held child
lock, default fail-fast, keep-going, and a deadline during final collection.
Suite-specific routing tests verify that recovery preserves the original request
when the launcher shard count changes. Every unsupported suite environment
override is rejected before state creation.

Five sequential parent invocations ran on the authorized OVH worker against
Eichler `8fda4d56c3251bc887526e98f033608a30e2a52b`:

| Probe | Exit | Observed result |
| --- | ---: | --- |
| Client killed, then reconnected | 0 | Same parent and child IDs completed two passing shards |
| Default fail-fast | 1 | First shard failed; second was never staged and its journey was named as unrun |
| Keep-going | 1 | Both shards ran and both fixture failures were retained |
| Worker occupied after planning | 75 | Remaining cumulative queue allowance expired; neither shard executed |
| Unknown selected journey | 1 | Verified planning failure returned; no shards were dispatched |

The success run selected S0-01 and S0-02 and included both dropped-response
replays. The local Python client was killed while the first shard ran. The remote
parent completed that shard and dispatched the second before recovery finished.
The successful request used code `c0cc053`; later receipt hardening revalidated
its complete returned evidence.

The failure probes temporarily replaced both selected ledgers with invalid
fixtures. Fail-fast took 51.1 seconds end to end and keep-going took 81.7 seconds.
Both used backend code `ece554e`. The originals were backed up, restored, and
byte-compared; the evaluation worktree was clean afterward.

The contention probe used a three-second cumulative queue allowance. A bounded
probe held the worker slot for six seconds immediately after planning. The first
shard exhausted the remaining allowance without starting tests. The parent
reported 3.013 seconds of measured waiting, including polling overshoot, and
explicitly listed both unrun journeys. The probe released its lock afterward.

All five parent terminals verified cleanup. The final worker check found no
running Pandora units, containers, or networks. The final validator accepted
all retained evidence after the review fixes. Local receipts and probe scripts
remain in `~/.local/state/pandora/suite-parent-20260920/`, indexed by `summary.json`.

These are bounded two-journey backend trials plus routing tests, not a full
200-journey execution or a coding-agent concurrency evaluation. No claim of
twelve-agent readiness follows from them.
