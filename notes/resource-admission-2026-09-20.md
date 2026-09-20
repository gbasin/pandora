---
status: log
---

# Resource admission foundation, 2026-09-20

Gary selected invocation-level waiting time for parallel suites: charge time only
when unfinished queued work has no running shard. Do not add parallel shards'
waiting times together. The accepted queue budget survives dispatch and client
reconnects.

The new admission core is isolated from normal command routing. Production still
uses one FIFO worker slot. This separation allows resource and failure semantics
to be checked before any worker can overlap shared preparation or image cleanup.

## State walk

Let worker capacity be `CPU=1000m, RAM=256MiB, slots=2`. Suite A has three tasks,
each requesting `500m,128MiB`, and an invocation cap of two.

- At t=0, `A={waiting:[A1,A2,A3], running:[], waited:0}`.
- At t=3, A1 is admitted. `capacity={used:500m,128MiB,1}`, `A.waited=3`.
- A2 is admitted. `capacity={used:1000m,256MiB,2}`. A3 cannot fit. Its waiting
  does not increment A's clock while A1 or A2 holds an admitted reservation.
- Focused invocation B queues B1. B has no running task, so B's own clock runs.
- A1 completes and publishes verified cleanup. Its owner settles the reservation.
  B gets the next fair turn, ahead of A3. No running task was interrupted.
- If both A tasks finish with A3 still waiting, A's clock resumes from three
  seconds. Re-registering A cannot replace that accumulated time or its budget.
- If an admitted owner dies while its container survives, its running reservation
  stays charged. Admission stops even if arithmetic suggests spare capacity.
- After explicit container removal and a matching cleanup receipt, that reservation
  can be released. No test terminal is fabricated for the dead owner.

The admission database serializes the decision and resource reservation. Caller
side effects begin only after commit. Process locks identify liveness; a missing
lock is not cleanup evidence. Clock state is tied to one worker boot. Unknown
configuration, clock, or database state stops new admission.

New invocations enter near the current service turn instead of always receiving
turn zero. Tests cover a new arrival not jumping an older waiting invocation after a
focused request gets its turn. The policy refuses small-request backfill
past a selected request that cannot fit, trading utilization for starvation
avoidance. Strict FIFO remains a configuration option.

## Audit findings before production overlap

The current worker treats any journey container or shared builder as an orphan,
which would reject another valid live execution. Dependency and Docker builds
use fixed shared BuildKit daemons; their cleanup can stop the daemon. Dependency
image retention also relies on the global worker lock. These operations need
explicit ownership and image-use protection before admission can be widened.

The audit also found that suite child timers targeted systemd units that do not
exist: the remote parent starts ordinary child processes. That bug is handled in
a separate bounded deadline fix. Out-of-order receipt aggregation and fail-fast
with multiple active children remain parent-dispatcher integration work.

## Evidence

Nineteen focused tests passed: nine durable-admission tests, seven policy tests,
and three probe failure-path tests. The complete worker test suite passed all 98 tests.
They cover CPU/RAM/slot bounds, per-invocation caps, fair turns and strict FIFO,
queue-clock pause/resume and recovery, fail-fast with live siblings, dead-owner
barriers, duplicate identities, legacy lock exclusion, and malformed clocks,
configuration or database state.

A real Docker probe ran on the authorized VM using code `ef29f4c`. Two containers
with enforced 0.5 CPU and 128 MiB limits overlapped. A later focused request was
admitted before the waiting third suite task. A deliberately killed worker left
its container alive and blocked the next request. Removing that container and
publishing cleanup released the blocker without creating a test terminal.
A second independent probe using `dd5cd13` repeated these checks and directly
verified two simultaneously live containers. All probe containers were removed
afterward. A subsequent review fixed the probe cleanup path for failed container
creation: verified absence records failure, while unknown cleanup retains the
admission barrier. Three tests cover those paths.

The local evidence is under
`~/.local/state/pandora/resource-admission-20260920/first-result.json` and
`second-result.json` in the same directory. This probe
uses a separate ledger and small Node processes. It does not establish concurrent
journey performance, production integration, or twelve-agent readiness.
