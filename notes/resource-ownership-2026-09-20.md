---
status: log
---

# Resource ownership checks, 2026-09-20

The worker now inventories managed containers, builders, and journey networks
before starting work. A live resource must match its exact reserved name, ownership
labels, submitted workflow, cleanup intent where required, and held attempt lock.
Its owner must also belong to the caller's explicit set of admitted attempts.
Builder daemons additionally require matching durable ownership and a held builder
lock. A live process alone does not establish admission.

Production supplies only the current exclusively admitted attempt. The guard
therefore does not enable overlap. Concurrent admission must supply its own
running-reservation snapshot when it is integrated. The guard changes no capacity,
writes no cleanup receipts, and stops or removes nothing.

Unknown, dead, mislabeled, or unadmitted owners block execution. One fresh inventory
handles a peer completing and removing resources between enumeration and ownership
validation. Persisting ambiguity stops execution for operator reconciliation.

New surface containers have explicit attempt/workflow labels. Existing exited
surface diagnostics with the exact reserved name and legacy warm-surface label
remain allowed, preserving the old retention behavior. This exception is limited
to stopped diagnostics; it is not evidence of test completion, authorization to
remove a container, or release of an unresolved admission reservation.

## Evidence

All 143 worker tests passed on the final repeat run, including twelve new ownership
cases. An earlier run exposed the pre-existing non-atomic cleanup-receipt write in
a multiprocessing test fixture, filed as [#42](https://github.com/gbasin/pandora/issues/42).
Production already publishes that receipt atomically. No admission implementation
or fixture was changed in this work.

An isolated VM probe used the experimental resource scheduler to admit two actual
bounded containers, each with 0.5 CPU and 128 MiB limits and its own network. The
guard accepted both admitted live owners, rejected a live owner omitted from the
admission set, and rejected a killed owner while its container remained running.
The guard did not remove that container. Explicit cleanup and a matching cleanup
receipt restored acceptance of the surviving owner without inventing a terminal
for the killed owner. Probe containers and networks were removed afterward.

The normal integrated S0-01 journey `be0f0d51df0448c7a64507d06a56e602` passed with
exit 0 and verified cleanup through the updated worker bundle. No active containers
or journey networks remained. Local evidence is retained under
`~/.local/state/pandora/resource-ownership-20260920/`.

Terra reviewed the ownership/liveness distinction and the stopped-diagnostic
exception. Review also identified an existing retention gap: stopped-container
removal checks exact name but not labels. Ownership verification for destructive
retention remains separate work before production overlap. These probes do not
establish parallel journey throughput or twelve-agent readiness.
