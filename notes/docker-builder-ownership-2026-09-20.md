---
status: log
---

# Docker builder ownership, 2026-09-20

The Docker build workflow now uses the same durable ownership guard as dependency
preparation. It claims the cached builder before any build side effect and holds
the lease through client teardown and verified daemon stop. The cleanup marker is
atomically written with its workflow kind. Recovery handles a claim interrupted
between owner-record creation and marker creation.

Normal cleanup receives the held lease. Worker-death cleanup takes the guard's
lock and checks the durable owner. Neither path stops another owner's daemon.
Unowned legacy activity or unknown Docker state stays blocked for reconciliation.
A new worktree image mapping is published only after build success and verified
cleanup. Failed builds and uncertain cleanup preserve the previous mapping.

The existing builder name, cache volume, logical image tags, and agent commands
are unchanged. Production admission remains single-slot.

## Evidence

All 131 worker tests passed, including six Docker ownership integration cases:
successful publication, failed-build mapping preservation, cleanup failure after
a successful build, client teardown ordering on timeout, interrupted marker
publication, and live-owner exclusion followed by dead-owner recovery. The shared
guard tests cover delayed old markers and the owner-removal interruption window.
Terra reviewed the integration and found no blocking issues.

A real VM probe held the worker's exclusive capacity lock and ran five separate
workflow invocations against a private worktree key:

- Built an image containing a known file, then ran that image in a separate call.
- Failed a rebuild deliberately and verified the previous mapping stayed intact.
- Killed the owning Python process during a build's `RUN` command. The actual
  systemd cleanup entrypoint stopped the owned daemon and produced a verified
  admission-cleanup receipt without fabricating a test terminal.
- Invoked delayed cleanup from the original build while the later build was
  running and verified the current owner remained active.
- Rebuilt the original input and verified cached steps and the same cache volume.

The interrupted attempt was `539e51440b014e57a847d6dd986c53d7`.
The unrelated bounded sentinel survived every phase. The probe removed its image
tags, logical mapping, and sentinel. No containers or builder ownership records
remained active afterward. Logs and receipts are retained under
`~/.local/state/pandora/docker-builder-ownership-20260920/docker-first/`.

This proves the Docker workflow ownership lifecycle, not parallel journey
throughput. Ownership-aware worker checks, builder-aware admission, disk accounting,
and concurrent parent dispatch remain necessary before enabling overlap.
