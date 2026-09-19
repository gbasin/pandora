# Docker build/run and output-promotion probes

Dated evidence, 2026-09-19. These probes inform the proposed v0.1 contract in
[PR #8](https://github.com/gbasin/pandora/pull/8). They do not implement Docker
routing or establish readiness for agent use.

## Environment and method

Gary authorized use of the existing OVH trial VM in this conversation. The worker
was idle, with about 14 GiB available RAM and 80 GiB free disk. Docker Engine was
29.1.3. Buildx was absent; the evaluator installed Ubuntu's docker-buildx package
0.30.1-0ubuntu1. No new cloud resources were provisioned.

The [probe script](../experiments/probes/buildkit_primitives.py) created a dedicated
BuildKit builder with one CPU, 1 GiB RAM, and no extra swap allowance. Docker
inspect verified those settings. Runtime containers had 128 MiB RAM, half a CPU,
64 PIDs, and networking disabled. The script used a digest-pinned Node 24 image.
Builder bootstrap took 2.670 seconds and was separate from the cold-build timing.

The fixture copies JavaScript and syntax-checks it. A synthetic dependency file
invalidates a step that writes a cache-mount marker. This is not a pnpm install,
compiler benchmark, or representative application workload. Each invocation uses
an independent Docker CLI process, but no coding agent or Pandora wrapper.

## Observations

| Operation | Seconds | Result |
| --- | ---: | --- |
| Cold fixture build | 4.821 | Cache mount initially empty; image runs with worktree-a content |
| Identical rebuild | 0.750 | All fixture steps report CACHED |
| Source edit, second physical tag | 0.866 | Earlier steps cached; new image runs with worktree-b content |
| Synthetic dependency change | 0.896 | Invalidated step executes and reports PROBE_STORE_WARM |
| Invalid source rebuild | 0.218 | Build fails; prior tag still runs with worktree-b content |
| Mount worktree over /app | 0.173 | Run fails because the mount hides /app/dist/result.js |

Both physical image tags returned their own contents after separate builds.
This supports using namespaced physical tags behind logical worktree tags. It
does not test a mapping implementation, persistence across launcher restart,
concurrent tag publication, or queued-run digest pinning.

The bind-mount result matters for profiles: a mounted source directory hides
image files at that location, including installed dependencies or build outputs.
Do not promise that mounting a fresh source snapshot preserves the image's
workspace. Profiles must place dependencies elsewhere or explicitly prepare the
mounted workspace, while preserving the meaning of the supported Docker command.

An earlier local probe successfully checked distinct tag contents but failed an
assertion that identical builds must have identical image IDs. The local probe
did not preserve enough build metadata to explain that difference. The remote
probe used explicit provenance settings and checked cache reports and runtime
contents. No claim about the local failure's cause follows.

## Output promotion counterexample

The [local race probe](../experiments/probes/output_promotion_race.py) deterministically
orders three operations: verify unchanged destination, make a local edit, then
replace the destination with downloaded output. The local edit is lost.

This disproves a simple compare-then-replace protocol. It does not expose a
deployed workspace-writeback bug: the pilot returns artifacts into its own state
directory and does not implement general output return into a worktree.

An advisory lock alone cannot prevent arbitrary editors from writing. Before
enabling automatic workspace output promotion, prove preservation and recovery
under concurrent edits, renames, and open file handles. Immutable run artifacts
remain the recovery source. The promised conflict behavior is still unresolved
at the implementation level; this evidence does not relax it.

## Evidence and cleanup

[Raw evidence](../experiments/probes/evidence/2026-09-19-buildkit/report.json)
contains step timings. Adjacent stdout/stderr files record cache decisions,
runtime contents, failures, and builder limits. steps.json includes cleanup.
output-race.json records the deterministic local counterexample.

The probe removed its execution containers, tagged fixture images, and dedicated
builder. Buildx remains installed. Downloaded base/builder images and evidence
remain on the VM. Existing Pandora trial images and evidence were preserved.
The VM remains provisioned under the existing trial arrangement.

These results support a BuildKit-based implementation and explicit mount rules.
They do not establish real dependency-install savings, long-build scalability,
resource behavior under twelve agents, or safe output promotion. The next
technical proof should address output preservation before integrating writeback.
