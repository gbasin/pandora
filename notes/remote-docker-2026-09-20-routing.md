---
status: log
---
# Scoped Docker routing, 2026-09-20

Pandora now routes a bounded Docker build/run grammar through its existing SSH
worker. The target repository needs no tracked configuration. A launcher-selected
external profile declares Dockerfiles, one permitted worktree mount, generated
outputs, and network mode. Unsupported Docker commands receive feedback and never
fall back locally. The exact boundary is in the [Docker pilot README](../experiments/docker/README.md).

## Image and source semantics

A successful build publishes a worktree-private mapping from logical tag to image
ID after stopping its bounded BuildKit worker. Failure preserves the earlier
mapping. Runs resolve the image before queueing, and physical attempt tags remain
retained even after logical removal. The namespace is the canonical local worktree
path hash on the selected worker. Launcher restart does not change it.

Unmounted runs capture no local source and use the built image. Mounted runs get
a separate copy of a fresh frozen snapshot. The copy hides image contents at the
mount destination and is owned by the image's configured user. Named users resolve
through the image passwd/group files. Cleanup returns ownership to the worker so
retention can delete the copy. It never exposes shared source-cache hardlinks to
writes. The existing generated-output publication mechanism returns only declared
outputs after success; failed outputs remain artifacts.

Docker builds use a dedicated, digest-pinned BuildKit daemon with two CPUs and
6 GiB without swap. It stops after each build and retains its native cache volume.
Runs have the same CPU/RAM limit. The common worker lease serializes Docker and
validation workflows. BuildKit provenance records the Dockerfile, resolved base
image, platform, and build graph. This is native BuildKit caching, not a custom
compiler-cache implementation.

## Scripted evidence

The [evidence directory](../experiments/docker/evidence/2026-09-20/) records all
cases. Two disposable Git worktrees used the same `app:test` tag with different
values. Their image IDs and returned values remained separate across new launcher
invocations. The fixture checked that `.dockerignore` excluded a tracked sentinel.

The semantics probe verified:

- Local edits did not change an unmounted image run; a mounted run saw those edits.
- Read-only mounts rejected writes.
- Exit 7 remained exit 7, retained the failed output, and preserved the successful
  local output generation.
- A failed rebuild left the previous tag usable. A successful rebuild changed the
  image, and an identical rebuild reused the same image ID.
- An unsupported detached run returned 64 without submitting a new attempt.
- Removing one worktree's tag left the other worktree intact. An unknown tag
  returned a build instruction and cleared the pre-submission request.

Additional probes verified that an unrelated local external symlink does not
block an unmounted image run, and that a `USER node` image can write through the
supported mounted-source path without changing the configured user.

Lifecycle probes verified explicit run cancellation, cancellation during a
`RUN sleep 60` build step, and same-attempt recovery after killing the local
transport. Killing the build worker with SIGKILL stopped the BuildKit container
through systemd's stop hook. It left the request unresolved rather than inventing
a terminal result. The database-backed Eichler S0-01 regression also passed on the
combined worker path. Thirty-one Python tests passed across command routing,
image mappings, evidence, retention, cleanup, snapshots, and output publication.

## Agent evaluation

One fresh Codex agent and one fresh Claude Opus agent ran concurrently in separate
fixture worktrees. Their briefs specified the desired build/fix/rebuild task and
allowed file, without giving queue or retry instructions. Both completed:

1. Build the seeded image as `app:test`.
2. Run it without a mount and observe exit 1.
3. Change only `value.txt` from `broken-<agent>` to `<agent>`.
4. Rebuild, run successfully, and read local `dist/result.json`.

Each agent submitted exactly two builds and two runs. Their source diffs and JSON
outputs matched the expected agent-specific value. Both used separate ordinary
Docker calls. No human intervention, cancelled wait, infrastructure edit, direct
SSH operation, or local fallback occurred.

Opus first tried `docker context ls`, received the unsupported-command guidance,
and continued. It also tried reading local `dist/result.json` after the failed
run; it was absent as designed. The passing run published it correctly. After
these trials, the client gained an explicit failed-output artifact-directory hint.
The evidence retains the actual earlier transcript. Opus wrapped some commands in
pipelines and explicit exit prints; Pandora did not alter shell pipeline semantics.

A subsequent four-agent pass ran two fresh Codex sessions and two fresh Opus
sessions together. All four completed the same loop, submitting exactly 16 remote
requests in total. Each changed only its own value file, returned its own value,
and finished on a distinct image ID despite using the same logical tag. The longest
recorded queue wait was 10.17 seconds. No agent cancelled, restarted, bypassed, or
requested human intervention. Both Opus sessions tried the unsupported context
read once, then followed the supported build/run path.

Forty resource samples at roughly three-second intervals observed at most one
remote build or run container. The Mac's reported free-memory percentage stayed
between 68 and 68 percent. Sampling can miss subsecond overlap and does not
measure interactive UI latency. The shared lease also enforces serialized execution
in code. The four-agent pass used a small fixture; it does not establish heavy
compiled-build capacity or a twelve-agent operating limit.

## Timing and friction

The first fixture build took 4.86 seconds on the worker and 15.62 seconds through
local evidence retrieval. Warm agent builds took 1.72–1.90 seconds of worker
execution; runs took 0.39–0.44 seconds. Nonqueued scripted calls generally took
12–17 seconds end to end. SSH setup, transfer, polling, and publication dominate
this tiny workload. Queue polling is every ten seconds, which adds noticeable
latency to short jobs. These measurements do not predict compiled application
build times or establish a twelve-agent operating limit.

The initial mounted run failed with EACCES: a root process with dropped Linux
capabilities could not write to the worker-owned snapshot copy. That failure
produced no local output. Matching the private copy's ownership to the image user
fixed it; the complete semantics probe then passed. Its original failure log is
retained alongside the successful evidence.

Switching back from the fixture repository to Eichler caused a 69.1-second source
transfer because the worker has one global latest-source cache pointer. Older
same-repository transfers took roughly 2.4–2.8 seconds. This is filed separately as
[#17](https://github.com/gbasin/pandora/issues/17); no source-cache redesign was
mixed into this change. Image-only requests no longer replace the pointer.

## Boundaries before everyday use

Physical Docker image garbage collection is still manual. Old images stay pinned
so accepted runs cannot lose them; the 10 GiB free-space guard is not a hard disk
quota. BuildKit has cache retention targets. Artifact size limits, general image
reference reclamation, per-repository source reuse, and automatic reconciliation
of missing terminal records need further work.

Build contexts still omit Git-ignored inputs, and local logical tags in Dockerfile
FROM instructions are not translated. Secrets, registry credentials, arbitrary
build flags, detached/interactive containers, forwarded ports, and Compose CLI
routing are unsupported. Declared outputs are exclusively managed generated
directories, not a source merge. The next evaluations should use a representative
compiled application build and broader contention after the storage/cache gaps
are addressed. This pilot does not change required target-repository CI.
