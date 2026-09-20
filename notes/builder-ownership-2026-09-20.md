---
status: log
---

# Dependency builder ownership, 2026-09-20

Dependency preparation now acquires an exclusive process lock and records its
attempt identity before it can create or use the persistent BuildKit daemon.
The lock spans preparation, client teardown, daemon stop, and verification that
no builder container is running. The builder name and cache volume are unchanged.

A dead owner leaves its durable record. Another attempt cannot steal ownership.
Cleanup takes the same lock without waiting and checks the recorded identity.
A delayed cleanup never stops a successor's daemon. Unknown Docker state retains
the ownership and pending-cleanup barriers.

After verified stop, cleanup removes the owner record first and the attempt's
pending marker last. A failure between these operations leaves a recoverable
marker. Recovery can clear that marker only while holding the builder lock,
with no other owner, after verifying that the builder is stopped. The marker
therefore remains an admission barrier until recovery is safe.

Recovery is wired into the systemd cleanup entrypoint and the suite parent's dead
child cleanup. It can issue a resource-cleanup receipt without fabricating a test
terminal. A missing test result remains unresolved.

## Evidence

All 125 worker tests passed, including nine ownership tests. Coverage includes
live-owner exclusion, dead-owner recovery, stale cleanup against a successor,
unknown Docker state, corrupt ownership, unowned active builders, and injected
failure between owner removal and pending-marker removal. Terra review found
the removal-order gap; the revised implementation and regression passed review.

Three real VM probes used the existing dependency builder while holding the
worker's exclusive capacity lock. Each performed a small build, killed the Python
owner during a second build's `RUN` command, recovered the builder, and rebuilt
the original input. Each verified that delayed cleanup left the current owner
running, the same cache volume survived, the final build reported cached steps,
and an unrelated bounded sentinel container survived. The first probe preceded
the removal-order correction; the second and third used the corrected guard.

The third probe invoked the actual systemd cleanup entrypoint after owner death.
It verified an admission-cleanup receipt, absence of the pending marker, and no
test terminal. Its interrupted attempt was `58e94dcc78224ad28583d956396a701f`.
The probes removed their image tags and sentinel containers. The builder stopped
and had no remaining ownership record. Logs and receipts are retained under
`~/.local/state/pandora/builder-ownership-20260920/`.

## Boundary

Normal commands remain single-slot. This guard protects the dependency builder.
The Docker workflow's builder still needs ownership integration. The guard
refuses a busy builder; it does not add a second hidden queue. Parallel admission
must account for builder serialization, CPU/RAM, and disk before enabling overlap.
These probes do not establish parallel journey throughput or twelve-agent readiness.
