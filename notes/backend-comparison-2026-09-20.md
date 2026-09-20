---
status: log
---
# Native BuildKit and Mutagen comparison, 2026-09-20

The POCs support retaining the existing SSH/rsync backend. Native remote BuildKit
can transfer incrementally, but does not provide Pandora's durable request contract.
Mutagen makes steady-state synchronization fast, but requires a Git-aware admission
layer to preserve Pandora's input exclusions. Neither comparison establishes a
sufficient benefit to replace the current backend.

The useful next hypothesis is smaller: generate a verified input manifest without
first copying the entire local repository, transfer only those paths, then verify
an independent remote snapshot before execution. This could use existing rsync.
It is a proposed experiment, not an implemented or validated replacement for the
current capture algorithm.

## Setup and boundaries

The workload was the same Eichler checkout and VM as the earlier compiled-build
and S0-01 trials. Docker builds used two CPUs and 6 GiB without swap. A temporary
registry used one CPU and 256 MiB. The native builder used the existing BuildKit
cache volume, exclusively under Pandora's worker lease. It exposed a Unix socket
through SSH. The registry listened only on the VM's loopback interface. No local
Docker daemon executed builds or tests.

Clients: Docker Buildx v0.35.0-desktop.2 and Mutagen v0.18.1. The builder image was
`moby/buildkit@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8`.
The temporary registry was pinned to
`registry@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373`.

These were backend probes, not coding-agent trials or installed command adapters.
Production command routing remained unchanged. Native measurements stop when the
remote Docker image is available, before the subsequent test and any local artifact
publication. Mutagen measurements include the normal durable worker and verified
journey evidence, but use an experimental preparation script. They do not prove
transparent integration with the agent launcher.

## Native remote BuildKit

The first build transferred approximately 380 MB and took 106.6 seconds inside
Buildx. A second fresh-copy build transferred the context again and took 119.5
seconds despite layer-cache hits. That second transfer overlapped Mutagen's initial
transfer, so its time is not a clean performance comparison.

A stable local staging directory, updated using checksum-based `rsync -rlpc`, kept
unchanged file metadata intact. After priming that context, the build took 2.6
seconds. Snapshot creation plus staging took 9.8 seconds, setup took 1.8 seconds,
and importing the image into remote Docker took 0.2 seconds: 14.4 seconds before
running the image. The current backend previously measured 13.1–15.8 seconds through
verified build evidence retrieval. These boundaries differ, so the result supports
no claim that native transport is faster end to end.

Fresh mtimes explain the retransfers better than changing builder names. Buildx's
context hint uses the context basename and a persisted client identifier; every
fresh context here had basename `source` and the same client configuration.
BuildKit's default filesystem differ compares metadata. The stable-context result
is consistent with that implementation. [Buildx context identity](https://github.com/docker/buildx/blob/v0.35.0/build/opt.go#L854-L861),
[filesystem differ](https://github.com/tonistiigi/fsutil/blob/586307ad452f/diff_containerd.go#L186-L205).

An additional case changed the HTML marker while preserving its size and original
mtime before staging. Checksum-based staging detected it, BuildKit rebuilt, and the
subsequent image run verified the changed marker. Buildx took 8.7 seconds; preparation
through remote image availability took 21.1 seconds. This tested invalidation rather
than assuming a cache hit was correct.

Image handoff used a loopback registry, followed by remote Docker pull. Import was
0.1–0.2 seconds because the VM already had the base layers. This is not a cold image
handoff benchmark. Exporting directly to the Mac would have added an unnecessary
image round trip. The registry is additional infrastructure for this implementation,
not a claim that every native BuildKit deployment requires one.

In the disconnect probe, a build printed its readiness marker and entered a
30-second sleep. Closing the SSH tunnel caused Buildx to exit 1 with an RPC EOF.
Two seconds later the builder contained only buildkitd; the sleep had disappeared.
A first probe's process-inspection command omitted the PID column required by Docker
and failed after injection; the corrected probe reproduced the result.
Native Solve requests are context-bound and do not establish an application-level
finish-and-recover receipt. [BuildKit client solve](https://github.com/moby/buildkit/blob/v0.26.2/client/solve.go#L239-L305).

A durable supervisor and source provider could restore Pandora's contract, but that
would require an additional design. Running Buildx remotely against an already
transferred immutable snapshot is the existing approach. The POC stable staging
also assumes sequential requests; adoption must retain the per-worktree request
lock before changing its directory.

## Mutagen

Mutagen does not automatically use Git's index and ignore rules. The POC generated
an explicit allowlist from Pandora's manifest: ignore everything, then allow each
selected path and its ancestor directories. It disabled global configuration,
ignored VCS directories, used portable symlinks, and selected one-way replication.
No unrestricted live repository mirror was used.

The safe handoff was: inventory inputs, flush, pause, copy manifest-selected remote
paths into an independent attempt, verify hashes/modes/link targets, re-inventory
local inputs, then resume synchronization and launch the durable worker. No hardlinks
joined the mutable mirror to the execution snapshot. Flush alone does not establish
exact equality or an immutable input. [Mutagen synchronization](https://mutagen.io/documentation/synchronization/),
[ignore behavior](https://mutagen.io/documentation/synchronization/ignores/).

All four S0-01 requests passed with cleanup verified:

| Preparation | Capture | Synchronization | Remote setup and verification | Whole probe |
| --- | ---: | ---: | ---: | ---: |
| Initial mirror | 5.0 s | 133.8 s | 9.9 s | 211.6 s |
| Warm, retained local copy | 5.0 s | 0.3 s | 5.5 s | 63.6 s |
| Warm, retained local copy | 3.2 s | 0.3 s | 5.0 s | 61.6 s |
| Warm, manifest only | 0.6 s | 0.4 s | 4.7 s | 59.3 s |

The initial transfer overlapped a native transfer and the request spent 10.4 seconds
queued. It is not an isolated cold-network benchmark. Later cases ran sequentially.
The manifest-only variant omitted the unused local copy but retained before/after
local inventory and remote snapshot verification. It was one sample, not a latency
percentile. Journey execution itself took about 49–51 seconds.

Two current-backend controls took 65.7 and 61.5 seconds through evidence retrieval,
or 68.9 and 63.2 seconds for the full shell call. Their capture costs varied from
8.1 to 4.8 seconds. Therefore the measurements show a possible modest preparation
benefit, not a reliable 10-second improvement attributable to Mutagen.

Additional fixture probes confirmed:

- A previously frozen copy remained unchanged after live mirror edits.
- An excluded secret marker and excluded nested directory never reached the mirror.
- A new allowed source file did not synchronize until session recreation updated
  the allowlist. Flush alone could not add it.
- Two actual Git worktrees, one nested inside the other, returned distinct values
  and omitted Git metadata and the nested worktree from the parent mirror.
- Killing the fixture session's remote agent made the next flush fail with
  `session is not currently able to synchronize`. A bounded retry recovered in
  2.8 seconds and verified the changed file.

The production adapter would need session ownership per worktree, atomic replacement
of allowlists, bounded reconnect handling, and safe retirement of previous sessions.
The journey POC assumes unchanged membership across its repeated runs; the fixture
separately tested explicit termination and recreation. It does not implement a
concurrent session manager or prove every edit-during-capture ordering. Raw transfer
speed does not remove those responsibilities.

## Proof table

| Claim | Evidence | Status | Remaining consequence |
| --- | --- | --- | --- |
| Native transfer can reuse unchanged context | Stable-context build and metadata differ source | POC-confirmed | Preserve metadata without trusting it for source correctness |
| Native client loss preserves a recoverable job | RPC EOF and stopped build process | Contradicted for direct client topology | Keep or design a durable supervisor |
| Mutagen can replace Git-aware input selection | Static ignores and new-file fixture | Contradicted | Add allowlist/session machinery or sanitized staging |
| Mutagen can feed an isolated service-backed attempt | Four passing S0-01 runs and independent snapshot checks | POC-confirmed | Concurrent adapter lifecycle remains unresolved |
| A paused mirror equals immutable input | Snapshot verification still required by design | Docs-confirmed limitation | Never execute directly in the live mirror |
| Either alternative is a clear end-to-end improvement | Small sequential samples and different boundaries | Unresolved | No production backend switch justified |

## Disposition and cleanup

The recommendation is to keep the current backend and evaluate manifest-directed
rsync as a bounded next experiment. This note does not authorize or implement a
new capture contract. Do not remove existing mutation or artifact checks to improve
latency figures.

The dedicated Mutagen sessions and daemon were terminated. The native builder,
registry container and registry volume, temporary remote mirrors, and local POC
tree were removed. The existing worker, BuildKit caches, and retained attempt
evidence remain. No containers were running at cleanup; the VM had 44 GiB free.
Dated summaries, build logs, and the operator scripts are in
`experiments/backend-comparison/evidence/2026-09-20`.
