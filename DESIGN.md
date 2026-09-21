# Pandora: remote validation MVP and evaluation

Design status: SSH pilot implemented and initially evaluated, 2026-09-19.
See [README.md](README.md) for current behavior, evidence, and remaining gaps.
The earlier v2 architecture remains in Git history at `0621e3f`; local POC notes
remain under `tmp/poc/`. Those probes do not establish an integrated system.

## Relationship to v0.1

This document describes the original surface pilot and its evaluation. The
[v0.1 contract](notes/v0.1-contract.md) defines the accepted v0.1 scope.
In that scope, a service-backed workflow is required before daily use, selected
Docker commands are routed, and declared outputs return with conflict checks.
The integrated surface extension now prepares dependencies automatically and
publishes generated build outputs with recoverable directory exchange. The
original pilot description below predates that extension. The S0-01 journey now
shares its snapshot, admission, dependency, and recovery path. A subsequent bounded Docker build/run profile now shares this path, with
worktree-private tags. See the README for current workflow and Docker boundaries.

## Objective and boundaries

Determine whether local coding agents can use remote validation without human
coordination, confusing waits, duplicate execution, or local resource overload.
Agent UX is the primary criterion. Sidecar Mac responsiveness comes before
infrastructure cost, then execution speed. The experiment's infrastructure cap
is US$20, excluding existing model subscriptions.

Agents, editing, and worktrees stay on the dedicated Mac in tmux/agentboard.
Only selected validation executes remotely. Other engineers and ordinary
terminals retain their current behavior. No tracked changes or permanent hooks
are required in Eichler or another target repo. Pandora owns the experiment,
profiles, launcher, telemetry, and evaluation artifacts.

Pandora is not a commitment to a custom execution platform. Use existing tools
first. Do not implement the previous daemon, Docker API proxy, generation
manager, or learned scheduler for this MVP.

## First workload

Use `pnpm test:surface borrower-web`, including supported selectors.
`pnpm validate surface borrower-web` is the equivalent entry point. The current
surface runner builds static fixtures and runs Playwright without Postgres or
Docker services. The subsequent S0-01 profile adds isolated service setup and
teardown without exposing Docker inside the execution container.

Agents perform validation and recovery only. They do not fix seeded product bugs
or take on pending product Issues. The evaluator injects faults exclusively into
trial-owned resources. Use isolated target worktrees and follow their normal
bootstrap requirements. Do not share node_modules between worktrees.

## Session-scoped routing

The trial launcher prepends a private executable directory to PATH and supplies
session/worktree identifiers. A `pnpm` shim routes supported surface invocations
and delegates everything else to the real executable. Preserve argv, cwd,
stdout/stderr, and terminal status. The remote process uses the real executable
and cannot recursively route. Do not modify global shell aliases or user-wide
harness settings. Verify actual PATH inheritance in each harness.

Evaluate three treatments against the same backend and workload:

| Treatment | Behavior | Question |
| --- | --- | --- |
| Normal commands | Route normal entries; observe direct local launches | Are familiar entry points sufficient? |
| Block bypasses | Reject recognized direct heavy launches with an exact supported alternative | Do agents recover or loop around the restriction? |
| Redirect bypasses | Redirect a bounded set of recognized direct invocations | Does automation help without changing meaning? |

Hooks must be scoped to trial sessions. Claude and Codex support must be verified
separately. Unsupported shell compositions, absolute executable paths, and
interactive flags are coverage gaps. Do not claim universal interception or
rewrite arbitrary shell text with broad substitutions. Scripted probes compare
requested and executed commands before any agent trial.

## Source, outputs, and isolation

Submit current tracked files and nonignored untracked files, including dirty
changes. Exclude credentials, local dependencies, and machine caches. Required
ignored fixtures use explicit profile entries. Record a manifest digest of the
submitted bytes and verify it remotely. Later local edits cannot alter accepted
input. Temporary snapshot commits/branches and direct transfer are both allowed.
Temporary commits must not mutate local HEAD, index, or worktree. Experiment refs
must not trigger unrelated workflows or deployments.

Record command/selectors, source digest, runtime image, limits, and attempt ID.
A temporary SHA alone is not proof of the actual transferred files. Each job
gets its own writable workspace, processes, and network environment. Preserve
safe dependency/incremental caches, not arbitrary background services or test
state. Use a fresh resource-bounded container for execution on the shared VM.

Return command stdout/stderr without silent rewriting. Report queue and
infrastructure status separately from test failure. Collect bounded complete
logs, structured Playwright results, traces, and screenshots. Download the
surface profile's known artifacts automatically to a session/run-specific local
location, and identify it in the result.

General source write-back is deferred: this entry point rejects snapshot-update
flags. Report unsupported mutation commands rather than executing a different
check. Future write-back must compare against submitted bytes, preserve newer
local edits, and finish before command completion. Mutagen remains a candidate,
not an MVP dependency or a substitute for source-consistency checks.

Warm iteration and independent clean final validation are the intended broader
workflow. Existing GitHub PR checks stay in place and cannot be satisfied by
experimental results. No merge/deployment behavior changes.

## Queue, cancellation, and debugging

One Linux VM has fixed execution slots. Start with one; admit a second only after
measuring a run's memory and establishing a host reserve. Apply CPU/RAM ceilings.
Excess requests queue. Never automatically burst to paid capacity or run locally
because the remote is busy or unavailable.

Normal invocation blocks with concise accepted/queued/running/terminal feedback.
The agent may wait; it need not invent other work. Quiet stdout is not failure.
An equivalent active request from the same worktree and local state directory reconnects or
reports the existing job. A changed request while one is queued reports that
fact; it does not submit or replace automatically. A deliberate rerun after a
terminal result creates a new attempt. Preserve identity through ambiguous
network acknowledgements. This is duplicate prevention, not result caching.

Explicit cancellation stops the owned workload and verifies cleanup. A tool
yield with a continuing handle is not cancellation. Abrupt client loss is a
separate case. The selected pilot policy lets the existing attempt finish within
its deadline and recovers the same attempt on retry. Explicit cancellation still
stops the owned workload. The worker lifetime is independent of SSH.
Never infer remote termination from CLI death, or start another execution while
the previous one is unresolved.

Retain failed workspace files temporarily, release compute, and reacquire a slot
for diagnostics against a copy of retained state. Live processes need not survive.
If the backend cannot offer this cheaply, record that gap before expanding scope.

Proposed recovery boundary, not yet ratified: agents can diagnose and restart
services in their own isolated environment. They cannot restart the shared host
or Docker daemon, clear shared state, or kill other jobs. Diagnostic setup fixes
must enter reproducible repository setup before clean final validation.

## Execution backend assessment

| Path | Existing capabilities | Integration gap |
| --- | --- | --- |
| Crabbox with owned SSH worker | Dirty sync, execution, logs, explicit artifacts | Static SSH is direct-only; shared-host queue/isolation and client-loss behavior |
| GitHub Actions with owned runner | Familiar control plane, queue, run IDs, artifacts | Dirty-source submission, local wrapper, disposable bounded execution |
| Buildkite Preflight with owned agent | Dirty snapshot commits, submission, agent-facing watcher | Experimental CLI, new account/setup, isolation and local result handling |

The selected first path is the small SSH harness, as requested for the initial
agent trial. It provides frozen-source submission, warm dependencies, a one-slot
worker lock, bounded containers, streamed logs, and artifact return. The launcher
routes selected normal commands without changing the target repository. This
choice evaluates routing, waiting, and cancellation; it does not evaluate a CI
control plane.

GitHub Actions with owned runners, Crabbox, and Buildkite remain alternatives.
Reconsider them if measured reliability or maintenance gaps justify the change.
Do not implement another backend before establishing the current pilot's failure
behavior. Initial evidence and untested cases are linked from the README.

## Evaluation protocol

Pin target revision, toolchain, harness versions, prompts, and command selectors.
Record cold/warm cache state. Establish a passing ordinary baseline and a remote
scripted smoke test before agent fanout. Do not load-test the shared Mac as a
baseline. Use synthetic fixture data only.

Scripted cases precede live agents:

- Passing run and a deterministic evaluator-controlled test failure.
- More submissions than slots, including a deliberately slow execution.
- Repeated request and changed source while queued.
- Explicit cancellation while queued and while running.
- Temporary network loss and abrupt CLI death, separately.
- Bounded OOM and worker interruption within trial-owned isolation.
- Interrupted artifact download and diagnostic access to a failed workspace.
- Distinct per-worktree sentinels proving no source/result cross-contamination.

Then run informed agents followed by fresh agents with ordinary validation
instructions and no backend tutorial. Remote/queue status remains truthful in
both stages. Compare all three routing treatments with matched prompts and
reset fault conditions; rotate order to reduce learning and cache confounders.

Start with two local agents, then four. Escalate toward twelve only after local
headroom, subscription capacity, budget, and earlier results permit it. Keep the
remote slot count small to force queueing. Observe direct local bypasses only
within a conservative trial window; stop owned trial processes if they threaten
host headroom and record the intervention as a failure. Never kill unrelated
work. Evaluate subscribed Claude and Codex CLIs separately before aggregating.

## Measures and decision rule

Persist timestamped acceptance, queue, execution, cancellation, cleanup, and
result events linked to session, worktree, request, source, and run IDs. Observe
owned child processes after the waiting CLI exits. Record host memory pressure
and job usage over time; elapsed duration is not CPU time, summed RSS is not
physical memory, and overlapping durations are not host busy time.

Hard failures are wrong-source results, cross-job interference, false success,
lost local changes, unbounded duplicate execution, and cleanup reported complete
without verification. Any hard failure blocks adoption.

Compare human interventions per task, incorrect recovery actions, local heavy
bypasses, retries, cancellation loops, model/tool calls spent waiting, and correct
completion. Also measure queue/sync/setup/run/return timings, Mac responsiveness,
remote memory peaks, cold/warm behavior, cost, and maintenance burden. Do not
hide correctness failures inside a weighted score.

Choose the simplest treatment with correct outcomes and autonomous recovery in
the tested cases. Report sample counts and limitations; a small pilot cannot
establish a production failure rate. Preserve failures as evidence, not just
successful demonstrations.

## Cost and teardown

Use hourly Hetzner or OVHcloud Linux x86 capacity, targeting about 16 GB RAM and
at least four vCPUs subject to the actual account/region quote. Prefer an existing
account. The hard infrastructure cap is US$20, including ancillary charges.

OVHcloud's worldwide page on 2026-09-19 lists b3-16 (16 GB, four vCores, 100 GB
NVMe) at US$0.1208/hour excluding VAT. Twelve hours is US$1.4496 for that listed
instance, not an all-in quote. Verify region, tax, networking, storage, and account
pricing before provisioning. Re-quote after its announced October 1 changes.
Hetzner's page did not expose a reliable numeric quote in this assessment.

Record the accepted rate, creation timestamp, exact provider IDs, and an initial
12-hour deletion deadline. Set a stop threshold below $20 with allowance for
cleanup and extras. Use an independent cleanup timer/control path, not a test
agent's survival. Shutdown is not deletion and may keep billing. Verify removal
of owned instances, volumes, snapshots, and billable IPs; retain cleanup evidence.
Extend the window only within the remaining cap. Never delete unrelated resources.

## Delivery and deferred work

Deliver a Pandora-owned profile, session launcher, pinned worker setup, and
scripted smoke/fault harness before agent evaluation. Produce an evidence report
and recommend adoption, refinement, or abandonment. The first worker and agent evaluation are complete; the full fault matrix and
broader adoption evaluation remain incomplete. See the README for the boundary.

Deferred: learned admission, automatic command classification, universal shell
rewriting, continuous sync, general source write-back, microVMs, multi-host
scheduling, native iOS, arbitrary journey stacks, and replacement of PR checks.

## References

- [Crabbox static SSH](https://crabbox.sh/providers/ssh.html)
- [Crabbox sync](https://crabbox.sh/features/sync.html)
- [Crabbox attach](https://crabbox.sh/commands/attach.html)
- [GitHub dispatch](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
- [GitHub runner lifecycle](https://docs.github.com/en/actions/reference/runners/self-hosted-runners)
- [Buildkite Preflight](https://buildkite.com/docs/platform/cli/preflight)
- [OVHcloud prices](https://www.ovhcloud.com/en/public-cloud/prices/)
- [Hetzner billing](https://www.hetzner.com/cloud/cost-optimized/)
