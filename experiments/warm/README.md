# Warm surface experiment

This extends the clean-commit baseline with dirty-source capture, incremental
transfer, and reusable installed dependencies. It is the scripted layer used by the
[session router](../routing/README.md). See the root README for agent trial results.

## Run

Install Python 3, rsync, Docker with Buildx, and systemd on the disposable worker.
The runtime recipe in `../surface/Dockerfile` is built automatically. The current worker
preset assumes the SSH user is `ubuntu`, with UID 1000 and passwordless sudo. Use Python 3.10 or
newer, rsync, and SSH on the Mac.

```sh
python3 -B warm.py \
  --host ubuntu@WORKER_IP \
  --repo /path/to/trial-worktree \
  --output /tmp/pandora-warm-01 \
  smoke.spec.ts
```

Choose a new output directory for each attempt. File selectors are supported.
Interactive and mutation flags are not supported. No credentials or local
node_modules are sent. Known secret filenames are excluded and listed in
`submission.json`; this is not a general secret-content scanner.

## Source and dependency behavior

The client inventories tracked and nonignored untracked files using Git. It
copies their current contents, omits tracked deletions, and hashes the copied
bytes. It checks source again before accepting the snapshot. An observed edit
during capture rejects the submission; it does not silently retry against newer
source. The capture is an optimistic consistency check, not a filesystem-wide
transaction. External symlinks and submodules are rejected.

Rsync compares the private frozen copy against the last remote snapshot. It
transfers changed files and reuses unchanged files from that snapshot. Every
attempt gets a separate source directory. The worker verifies the full manifest
before execution. Test containers never mount or modify these source snapshots.
The worker's single SSH user is trusted to preserve them.

A dependency image is keyed by the pinned runtime recipe, build-recipe version, workspace
package manifests, lockfile, pnpm settings, and patches. The first matching run
installs frozen dependencies. Later runs reuse the built image. Each container
has its own writable overlay, including node_modules, so test writes cannot alter
the cached image or another container. Source is copied into the container at the
same absolute path used during installation. Installation inputs already in the
keyed image retain their timestamps; copying identical patch bytes with a newer
timestamp caused pnpm to reject the reused installation in a later trial.

The profile currently assumes Eichler's installation inputs. Arbitrary install
hooks that read other source files need an expanded dependency key/context or a
cold install. Do not treat this as a universal monorepo dependency cache.

The fixture builds still run on every validation. Incremental build-cache reuse
has not been added. Browser binaries come from the base image and must match the
target lockfile's Playwright version.

## Admission and evidence

A worker-side file lock admits one dependency preparation or test at a time.
Waiters print that the worker is occupied. This is a capacity guard, not a FIFO
queue, durable job service, or duplicate-request policy. Multiple submissions
can still capture and transfer source concurrently.

Test containers use the baseline's limits: two CPUs, 6 GiB RAM without swap,
512 processes, and one Playwright worker. The dependency image builder has a
two-CPU and 6 GiB limit. The container has a 20-minute worker deadline. The image
build has a 15-minute client deadline. The dedicated BuildKit container stops
after preparation, including on cancellation. Its cache volume persists.
BuildKit garbage collection targets at most 12 GB of cache and 10 GB free disk;
these targets are not hard disk quotas. Integrated runs retain ten released attempts, with the latest snapshot protected.
Three recent dependency image tags are retained; referenced images stay pinned.
Legacy and unresolved data is never automatically deleted. A worker with less
than 10 GiB free refuses preparation and execution.

Local evidence includes the manifest and source identity, transfer statistics,
stage timings, cache-hit status, raw stdout/stderr, JUnit, available Playwright
artifacts, cgroup metrics, and terminal Docker state. Successful containers are
removed after collection. Failed containers are stopped and retained for diagnosis until their
acknowledged attempt ages out of retention. This is file retention, not live process retention.

Explicit cancellation was tested through the session router. Abrupt client loss,
interrupted transfers, disk quotas, retention sweeps, and dependency-cache
corruption still need fault tests before broader use. A
failed preparation can leave partial evidence. Do not interpret a missing test
result as a pass. This harness does not manage billable cloud resources.

Successful runs export both selected-app build directories under
`results/outputs/`. The session router verifies and publishes them locally.
Direct `warm.py` calls only retrieve evidence; they do not publish into a worktree.
The dependency builder is `pandora-surface-deps-v3`. An unexpectedly running
builder without the worker lock blocks subsequent validation until an operator
reconciles it. The worker never starts tests on top of unresolved preparation.
