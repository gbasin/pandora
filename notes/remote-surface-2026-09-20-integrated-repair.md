# Integrated surface repair and output return

Dated evidence, 2026-09-20 UTC. This extends the real SSH surface route, rather
than the small output fixture. No changes were merged into Eichler. All seeded
source changes belong to evaluator-owned worktrees.

## What changed

The normal routed command prepares missing dependencies automatically. A
persistent Docker-container BuildKit builder has two CPUs, 6 GiB RAM without
swap, a pnpm package cache, and a 15-minute preparation deadline. It stops after
preparation, including cancellation. Tests keep their existing separate bounded
container and worker lease. The recipe includes the pinned runtime Dockerfile,
workspace package manifests, lockfile, pnpm settings, and patches. Each test
container still gets its own writable installation. Vite builds run each time.

A successful command returns both `apps/borrower-web/dist` and
`apps/borrower-web/e2e/dist`. It replaces each ignored generated directory with a
verified generation and retains the previous directory. Tracked output files and
symlink destinations are refused. No source files are written back. The request
stays active until local publication completes. Retrying interrupted delivery
reuses verified downloaded evidence without another execution or SSH download.
The local evidence directory now uses the same attempt ID as the remote run.

Source consistency is checked on initial completion as well as recovery. A pass
for older source returns 75 if local input changed. It cannot be reported as a
pass for the newer source. Output publication assumes exclusive ownership of
these generated directories, and state/worktree placement on one filesystem
that supports atomic directory exchange.

## Inputs and trial setup

- Pandora execution foundation: `ba188b5`; explicit request-lock cleanup and its
  recovery regression test landed in `d05a673` during the first pair. That change
  closes a file descriptor on function return; it does not change the command,
  source, or publication protocol. The remaining pairs used `d05a673`.
- Eichler base: `fbeb008a2`, current origin/main when the evaluation began.
- Evaluator foundation: `c90d2cf3d`. It changes the HTML document title from Ike
  to Application preview and adds one Playwright assertion expecting Ike.
- Each lane starts from that foundation, has its own frozen pnpm installation,
  and has deliberately stale dist and e2e/dist directories with an obsolete file.
- Six fresh sessions: Codex default CLI model and Claude's subscribed `opus`
  alias, reported by the CLI as `claude-opus-5`. No model API key was configured.
- The agent-fanout controller supervises every agent. At most two coding agents
  run at once. The remote worker admits one preparation or validation at a time.
- Codex keeps its workspace sandbox. The trial grants write access to the
  dedicated Pandora state directory and shared pnpm store explicitly. Its private
  node_modules remain in the lane. No global shell configuration changes.

The common brief requires the first normal command before editing, then a local
repair, the same command again, and inspection of test evidence plus both local
builds. The only editable source is apps/borrower-web/index.html. Tests, tooling,
dependencies, commits, and new worktrees are outside the agent's scope. The brief
explains that the shell routes validation remotely, but gives no delivery-recovery
strategy or workaround. The task is intentionally narrow; this is not a measure
of general debugging ability.

Command: `pnpm test:surface borrower-web pandora-iteration.spec.ts`.

## Agent results

All six sessions reproduced the expected failure, changed only the title,
ran the same validation once more, and inspected correct local generated HTML.
Each used exactly two remote executions. No human coaching, cancellation,
cleanup workaround, or local validation fallback occurred. Independent review
checked source diffs, snapshot identities, terminal cleanup, JUnit, published
HTML, obsolete-file removal, and the retained original output generations.

| Session | Failed execution | Successful execution | Command totals, fail / pass |
| --- | --- | --- | --- |
| Codex 1 | 15.18 s | 10.98 s | 53.13 / 28.13 s |
| Codex 2 | 15.57 s | 11.02 s | 42.09 / 37.98 s |
| Codex 3 | 15.22 s | 11.09 s | 31.38 / 29.63 s |
| Opus 1 | 15.56 s | 10.83 s | 82.80 / 26.75 s |
| Opus 2 | 15.37 s | 10.97 s | 32.15 / 31.39 s |
| Opus 3 | 15.12 s | 10.85 s | 72.49 / 37.47 s |

Totals include source capture, transfer, queue, execution, and evidence retrieval;
they are measured by warm.py before local publication and later retention
acknowledgement. Queue time varies because the baseline and fault probes also
use the same worker. All twelve agent executions reused the dependency image.
Warm dependency lookup was about 0.05 seconds. This is a small build, not a
long-compilation benchmark.

Opus used shell pipelines ending in tail. Their shell status masks pnpm failure
without pipefail, as already observed in the earlier pilot. All three correctly
read the actual failing test and terminal result. Pandora does not change shell
pipeline semantics. Codex enumerated the evidence directory, which also contains
the captured source, producing an unnecessarily large listing. After these
samples, the transport was changed to print direct test-report and diagnostic
paths. That wording change received scripted validation, not another six-agent
sample.

## Scripted checks and retained failures

The cold baseline built its dependency image in 114.38 seconds, including browser
runtime preparation and image export. Frozen pnpm installation itself took about
11 seconds. The real smoke suite passed all 49 tests in a 99.26-second execution.
The evaluator then changed the local foundation while that baseline was in flight;
the initial invocation correctly returned 75 for changed source and did not
publish that older result. It is not counted as a successful current-source run.

An actual delivery refusal made the local generated-output parent unwritable.
Remote validation completed successfully; the launcher returned 75 and kept the
same attempt active. Restoring permissions and repeating the same command
published the output without another execution. Recovery took 0.77 seconds before
retention integration and 1.62 seconds with the final retention acknowledgement.
The process-exit-before/after-exchange tests and a partial-two-root publication
test also passed. The route regression test forbids spawning any subprocess on
completed-run recovery, apart from mocked Git inspection and retention control.

An earlier fault setup replaced dist with an external symlink before capture.
Git no longer ignored that entry as a directory; snapshot capture rejected it
before submission. The evaluator restored the original directory and changed the
probe to a permission failure. This rejected capture was not an agent failure and
started no remote test. Its misleading generic recovery message was corrected to
say capture failed before submission.

A changed package manifest forced automatic dependency preparation. The evaluator
sent SIGTERM while BuildKit was exporting layers. The route returned 130 with
verified cleanup; Docker inspection confirmed the BuildKit container stopped.
Subsequent queued agent validations ran normally. This proves that observed
cancellation point, not every daemon import phase or abrupt VM-loss case.

Retention checks used disposable remote directories, two tiny containers, and
five temporary image tags. Collection preserved a live container despite an
inconsistent terminal receipt, removed an old stopped container and its released
attempt, kept ten recent released attempts, and kept three recent managed image
tags. Unit checks protect unresolved, legacy, and explicitly pinned attempts.

After retention and direct artifact-path reporting were integrated, a final
current-source smoke run passed all 49 tests again and published both outputs.
All 23 local Python tests passed. The agent-run cgroup evidence reports no OOM
kills.

A seventh fresh session tested the final Codex wrapper without the trial's
manual writable-roots override. The wrapper adds only Pandora's state directory
with `--add-dir`, alongside existing sandbox access. That session completed the
same fail/edit/pass loop, returned both builds, and changed only the intended
source. It used the printed JUnit path instead of enumerating the entire source
snapshot. This is one follow-up observation, not a new three-sample comparison.

## State trace

Before publication: local request = active(A); remote = terminal(A, 0, cleanup);
local evidence = verified(A); outputs = old0, old1; receipts = none.

After exchanging output 0, before its receipt is updated: request = active(A);
output0 inode = incoming0; stage0 inode = previous0; receipt0 = prepared;
output1 = old1. If the client exits here, no successful command was reported.

Retry with unchanged source: request = active(A); terminal and downloaded hashes
are verified locally; no new worker is launched. Publisher 0 recognizes incoming0
at the destination and does not exchange again. Publisher 1 completes its own
exchange. Both receipts = published; request = terminal(A); retention release is
best effort; the command returns 0.

Retry after a source change: the result is labeled stale and the command returns
75. No new publication occurs on that invocation. An earlier partial publication
may remain, so the feedback tells the caller to inspect retained outputs. A new
invocation captures and validates the new source. There is no promise of one
atomic transaction spanning both directories.

## Retention and limits

New integrated runs keep ten prior completed attempts per local worktree and ten
released attempts remotely, plus protected current/latest and unresolved work.
Three recent managed dependency image tags are retained; referenced images remain
pinned. Old output generations expire with their attempt. Earlier experiment data
is deliberately excluded. Lost retention acknowledgement preserves remote data.

BuildKit cache collection targets 12 GB used and 10 GB free. Worker preparation
refuses to start below 10 GiB free. These policies are not hard disk quotas and do
not cap active/unresolved data, snapshots being uploaded, Docker daemon import
memory, or a workload's writable layer. The system still needs operational disk
limits and operator recovery for unknown terminal state before unattended use.
Docker's [container driver](https://docs.docker.com/build/builders/drivers/docker-container/)
and [BuildKit configuration](https://docs.docker.com/build/buildkit/toml-configuration/)
document the builder resource and cache settings used here.

Service-backed routing, selected Docker CLI routing, parallel worktrees with
changed dependency versions, and long-build caches remain separate milestones.
The existing service and dependency component probes are not substitutes for
those integrated trials. No twelve-agent capacity or Mac responsiveness claim
follows from six small repair samples.

## Evidence

Machine-readable results are in
[the integrated evidence directory](../experiments/routing/evidence/2026-09-20-integrated/).
Raw CLI reports, prompts, setup logs, and fault logs are retained under
`~/.local/state/pandora/integrated-trials/`. The controller is
`pandora-integrated-20260920`; trial worktrees and source changes are retained.
The raw routed state and snapshots are under
`~/.local/state/pandora/integrated-surface/`.
