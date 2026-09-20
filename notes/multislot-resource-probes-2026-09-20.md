---
status: log
---

# Configured worker resource probes, 2026-09-20

The authorized OVH worker ran with two slots, 3500 millicores, 13000 MiB
reserved memory, a 24576 MiB workspace reservation pool, and a 10240 MiB free
space floor. Each journey reserved 1500 millicores and 4864 MiB across its main
container, database, pooler, and proxy. Docker memory and swap ceilings were equal.

Two focused journeys ran concurrently and passed with verified cleanup:
`60029acb42404726ad54828367e74a21` and
`df2271835f4140b8b203c9ac4baa9b02`. Container inspection and the admission
ledger recorded both live attempts. Their execution intervals were 64.35 and
67.94 seconds. A two-shard parent, `111398ddcdd542259a5a9fc0c301fde7`, also
passed with both shard results and a cumulative queue interval of zero seconds.

A five-second execution profile stopped `20fd5bf244b74fc6a08f942974fab6a2`
at 5.0009 seconds. It returned exit 124, a deadline receipt, and verified cleanup.
The operator restored the 1500-second profile after the worker drained.
A separate 128 MiB tmpfs probe crossed a 100 MiB free-space floor and produced
the disk-stop receipt without filling the worker's filesystem.

The reproducible `experiments/scheduler/resource_fault_probe.py` exercised:

- A runtime memory limit: exit 137 and Docker `OOMKilled: true`.
- A BuildKit memory limit: exit 102 and `ResourceExhausted` with an allocation failure.
- A failed rebuild: the prior worktree tag still ran its sentinel successfully.
- A bounded tmpfs exhaustion: exit 1 and a no-space diagnostic.
- Explicit removal of the probe's private image mapping.

Every final probe attempt recorded verified cleanup. Evidence is retained under
`~/.local/state/pandora/resource-faults-20260920-final/result.json` on the Mac.
The first probe stopped because its assertion recognized too few BuildKit OOM
messages. The corrected probe accepted the observed allocation-failure diagnostic
and repeated the complete sequence successfully.

These probes establish resource and failure semantics. They do not establish
capacity for twelve actual coding-agent sessions or full-catalog readiness.
