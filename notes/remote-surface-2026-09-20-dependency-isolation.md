# Parallel worktrees with different dependencies

Dated evidence, 2026-09-20 UTC. One fresh Codex session and one fresh Claude Opus
session ran concurrently on the Mac through the unchanged integrated surface
route from Pandora `c2a20b7`. Both required new dependency images. This is a
controlled two-agent experiment, not a twelve-agent capacity result.

## Setup

The evaluator created an integration worktree from Eichler `fbeb008a2` and
committed the trial foundation as `9dd1b392c`. The foundation adds one Playwright
test and changes the source HTML title to Application preview. Each controller
lane has its own frozen pnpm installation and an evaluator-prepared dependency:

| Lane | Root development dependency | HTML marker |
| --- | --- | --- |
| Codex A | is-number 6.0.0 | pandora-codex-a-6.0.0 |
| Opus B | is-number 7.0.0 | pandora-opus-b-7.0.0 |

A per-lane JSON fixture records the expected version and marker. The test reads
the installed package version inside the execution container, checks the marker
in the served HTML, and finally expects the document title Ike. Both local build
directories start with stale HTML and an obsolete file carrying the lane marker.
The package addition also normalized several pnpm lockfile optional flags. These
are evaluator-only changes, with no target-repository PR or merge.

The common brief requires the ordinary command before any source edit, a minimal
title repair, the same command again, and inspection of returned test evidence
and both local HTML outputs. It permits only the title edit and explicitly tells
the agent to preserve the prepared dependencies, fixture, and marker. Neither
agent receives a prewarm instruction or a cache-recovery strategy.

Command: `pnpm test:surface borrower-web pandora-dependency.spec.ts`.

## Results

Both agents completed the fail/edit/pass loop without intervention, cancellation,
resubmission of active work, or local fallback. Each ran validation exactly twice.
The independent verifier checked:

- Different dependency image IDs between lanes, and the correct installed version
  and source marker in each run's test output.
- An initial dependency cache miss and a cache hit after the source-only repair.
- The same dependency image within each lane across both executions.
- Only apps/borrower-web/index.html changed between each pair of source manifests.
- No nested worktrees entered either snapshot. This relies on the existing Git
  exclusions installed by the controller, not generic nested-repository discovery.
- Both returned build directories contain the correct marker and title. The
  obsolete file is absent, and the original stale generation remains recoverable.
- Verified terminal cleanup and no test-container OOM kills.

| Lane / run | Queue | Dependency preparation | Execution | Total before local publication |
| --- | --- | --- | --- | --- |
| Opus initial failure | 0.05 s | 81.17 s | 16.20 s | 115.31 s |
| Codex initial failure | 90.04 s | 79.14 s | 16.47 s | 203.31 s |
| Opus successful rerun | 80.03 s | 0.05 s | 11.30 s | 110.60 s |
| Codex successful rerun | 0.03 s | 0.05 s | 11.16 s | 28.87 s |

Package reuse worked: Opus reused 1,163 packages and downloaded none; Codex reused
1,163 and downloaded only the added version. New images still took about 80
seconds to prepare. Cached packages avoid network downloads, but dependency-image
construction and export remain substantial work. The warm Opus rerun waited
behind the other lane's cold preparation because one lease covers both stages.
This is visible queue contention, not a cache miss or local RAM starvation.

Codex explicitly reported that its first invocation was queued and that it had
read the source but not edited it yet. It waited for the failure before repair.
Opus again used a tail pipeline, so the outer shell status alone was insufficient;
it correctly used the failing test and Pandora terminal output. The underlying
attempt exited 1, and the successful rerun exited 0.

Twenty-four resource samples, roughly ten seconds apart, observed at most one
running experiment container at a time, counting BuildKit and validation. The
Mac's system-wide memory-free percentage stayed at 66–67%. This is a coarse
observation, not an interactive-latency benchmark or a proof that no short peak
occurred between samples. Docker daemon import resources are not included in
container statistics.

## Setup friction

The first agent-fanout init exited 141 before creating a run. Its canonical
repository lookup pipes a large worktree inventory to an awk command that exits
after the first line under pipefail. A retry succeeded. This controller issue was
filed as [Pandora papercut #14](https://github.com/gbasin/pandora/issues/14); the
controller was not modified during this evaluation. It was not an agent trial
failure or a remote validation failure.

## Scope and evidence

The routing code was unchanged. The result supports two concurrent
agent sessions with different dependency versions and sequential remote resource
admission. It does not establish arbitrary install hooks, compiler-cache
invalidation, queue fairness, twelve-agent capacity, or service-backed routing.
The next integration milestone is one service-backed journey with owned service
lifecycle, ordinary command routing, evidence return, and cancellation/recovery.

[Machine-readable evidence](../experiments/dependencies/evidence/2026-09-20-isolation/)
contains attempts, commands, timings, and resource samples. The reusable verifier
is [verify-isolation.py](../experiments/dependencies/verify-isolation.py).
Raw prompts, setup logs, and CLI reports remain under
`~/.local/state/pandora/dependency-trials/`; routed state remains under
`~/.local/state/pandora/dependency-isolation/`. The controller is
`pandora-dependencies-20260920`. Its evaluator worktrees are retained.
