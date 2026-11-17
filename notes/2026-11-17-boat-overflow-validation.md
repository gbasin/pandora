# Boat overflow validation

Initial validation passed on disposable eight-CPU, 16 GiB Boat VMs. Automatic
overflow remains disabled. Experimental adapters live in
`pandora.engine.overflow_worker` and the separate Eichler Compute worktree's
`eichler_compute.pandora_lease`. See `docs/boat-overflow.md` for their contract
and remaining production integration.

The final Pandora candidate passed 1,964 tests through the existing worker and
the same 1,964 tests inside nested Incus on a fresh Boat host, with two skips in
each run. All 23 worker canary checks and real signed-result verification passed.
CI smoke tasks passed before and after Pandora on that host. These were not
GitHub-dispatched production CI or representative Eichler workload benchmarks.
The Compute suite passed 161 tests plus nine subtests with its verified pinned
dstack dependency. Both worktrees passed lint.

A forced kill removed all 29 recorded task/guest processes and produced no
result. Empty cgroups remained, so failed cleanup must quarantine and retire
the VM. The working bootstrap preserves the task PID namespace but places
Incus work in sibling VM cgroups outside Docker task accounting. This requires
the exact exclusive host shape, enforced Incus ceilings, and host headroom.

The initial eligibility ceiling is 12,288 MiB. Larger ceilings remain on OVH
regardless of learned reservation. CPU topology beyond the tested non-SMT
eight-core shape needs validation. No pool size change was made. A short CI
history snapshot was insufficient for representative combined-demand replay.

Validation exposed and fixed the run-index directory-mtime shortcut and a quota
probe that discarded dd's diagnostic. Deterministic regressions reproduced both
failures on the original code.

Three disposable VMs were removed; test fleets were absent or terminated and a
complete Boat inventory found no owned test VMs. The installed checkouts and
production fleet were unchanged. Public proof and selected validation scripts
are saved locally at
`/Users/gary/Code/validation-artifacts/pandora-overflow-20261117/report.md`.
