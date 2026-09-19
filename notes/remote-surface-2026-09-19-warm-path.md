---
status: log
---

# Remote surface experiment: warm source and dependencies, 2026-09-19

The warm-path experiment passed two 45-test borrower-web smoke runs. It reuses
remote source files and installed dependencies without sharing a writable test
workspace. This addresses the cold-transfer problem recorded in the
[first baseline](remote-surface-2026-09-19-baseline.md).

## Mechanism and rationale

`experiments/warm/` captures tracked and nonignored untracked source into a
private local snapshot, including dirty edits and tracked deletions. It leaves
Git HEAD and the index unchanged. Known credential filenames and dependencies
are excluded. A manifest identifies the submitted bytes; the worker verifies it.
The capture checks for concurrent edits but is not an atomic filesystem snapshot.

Rsync reuses files from an earlier remote snapshot. Tests receive a copy inside
their own container and cannot mutate the remote source cache. Dependencies live
in an image keyed by installation inputs and the base image ID. A fresh writable
container overlay gives each run its own node_modules while reusing installed
package bytes. The image retains browser binaries too. Fixture builds still run.

A worker-side file lock limits preparation and execution to one run at a time.
Waiters report occupancy every ten seconds. This guard demonstrates bounded
admission but is not a FIFO queue or durable job control plane.

This is still an SSH experiment. It has not deployed the provisional GitHub
Actions option from the design PR or established the eventual backend choice.
Normal pnpm command routing, duplicate prevention, and agent trials remain outside
this measured step. Cancellation and client-loss behavior remain unverified.

## Setup correction

The first dependency-image build failed because its source directory was owned
by root while installation ran as node. The build now creates and assigns the
workspace to node before installation. The stopped failed intermediate container
was removed. The corrected image is
`sha256:f1e5818057c025ccb99e298a651139df78bf0336260a933fa8ffa8b563753cc2`.

## Measured runs

Both successful runs used Eichler revision `b18725e9c` in a dedicated trial
worktree. That is newer than the initial baseline, so these are not a controlled
performance comparison against its test duration. Both use two CPUs, a 6 GiB
memory limit, and one Playwright worker.

| Measurement | Build dependency image | Reuse dependency image |
| --- | ---: | ---: |
| Attempt | `79fd6262352443f7a1fb0b664a066558` | `b3adcc9229b242f4af194e6532885bfa` |
| Local capture | 3.33 s | 4.90 s |
| Source rsync | 2.88 s | 2.81 s |
| Worker queue | <0.01 s | 70.00 s |
| Dependency preparation | 83.44 s | 0.09 s |
| Container setup, tests, collection, cleanup | 93.39 s | 92.60 s |
| Playwright test time | 85.36 s | 85.10 s |
| Total invocation | 195.25 s | 185.26 s |
| Result | 45 passed | 45 passed |

The second run deliberately overlapped the first to exercise waiting. Its total
includes 70 seconds queued. Subtracting that queue gives approximately 115 seconds
of other work; this is not a separately measured no-queue invocation.

On the first capture after creating the trial worktree, local snapshotting took
22.58 seconds. Subsequent captures took 3–5 seconds. Filesystem cache warmth is a
plausible explanation, not an isolated measurement of the cause.

For the second successful run, only a 16-byte sentinel file changed. Rsync
reported one file transferred, 16 bytes of file content, 418,114 bytes sent, and
84 bytes received. The approximately 418 KB file-list overhead still exists;
"only changed files" does not mean only 16 bytes crossed the network. This is a
substantial reduction from the original 291 MiB compressed archive upload.

## Isolation checks

While the second run was queued, the evaluator changed the local sentinel from
`submitted-value` to `later-local-edit`. The accepted remote source retained
`submitted-value`. Evidence is in the second run's `frozen-input-proof.json`.
The sentinel was evaluator-owned and did not modify product behavior.

A separate bounded, network-disabled container wrote a marker into its installed
node_modules. A fresh container from the same cached image did not contain that
marker. This checks that dependency writes do not contaminate the image used by
the next run. It does not establish hostile multi-tenant security.

Two local snapshot tests also cover dirty and untracked source, tracked deletion,
ignored and known secret-file exclusion, unchanged Git index, later local edits,
tamper detection, and rejection of external symlinks.

## Evidence and remaining limits

Local evidence is under `/tmp/pandora-warm-01/` (failed image preparation),
`/tmp/pandora-warm-02/`, and `/tmp/pandora-warm-03/`. The source snapshots and
successful-run metrics remain on the disposable VM under `~/pandora-warm/runs/`.
Successful test containers were removed. Cloud deletion remains Gary's agreed
manual responsibility for this trial.

This establishes warm source transfer, installed-dependency reuse, frozen queued
input, and serialized remote execution for this workload. It does not yet
establish transparent agent UX, duplicate-request behavior, robust cancellation,
or recovery after CLI/network loss. The implementation is specific to Eichler's
install inputs; arbitrary source-dependent installation hooks require an expanded
cache key/context. Disk retention is manual and the image-build phase lacks an
independent deadline. These limitations must be addressed or bounded before
unattended agent evaluation.
