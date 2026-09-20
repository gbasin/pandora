---
status: log
---

# Background work for agent software factories, 2026-09-20

A brainstorm, not a plan. It is a separate idea from Pandora v0.1 and from the
multi-tenant backend, and it commits to nothing. It started from one observation
in the multi-tenant review: on flat-priced rented hardware, the daily trough is
nearly free compute, and only delay-tolerant work can raise average utilization.
The question here is what that work should be when the customers are teams
running many coding agents. No idea below has been built or measured.

## Framing

Trough compute is nearly free. Tokens, agent wall-time, and human review are not.
The best background jobs are therefore compute-only jobs that save those three:
**spend free RAM to save paid tokens.**

A good trough job is idempotent, runs with egress off, comes in small pieces so a
kill loses little, is elastic so any amount of RAM-time is useful, and produces a
result that is still valuable hours later. Jobs with an agent in the loop also
spend the tenant's token budget, and their model calls must pass the egress-off
default through an allowlisted proxy or the control plane.

## Generators

Every idea here is the same mechanism: a generator with a **trigger**, a **job
template**, a **value function**, and a **delivery sink**. When there is slack,
the scheduler picks the work with the highest expected value per RAM-second.

Value decays at different rates, which maps onto the priority, killable, and
batch tiers. A speculative run is worth a lot for about thirty seconds, until the
agent edits again; then it is cancelled. A bisect is worth a lot while main is
red. Fuzzing has a low, constant value and can run forever.

| Trigger | Generators |
| --- | --- |
| New snapshot seen | speculative validation, hunk-bisect |
| Interactive failure | triage rerun, trace capture |
| Push or PR | full catalog, mutation delta, merge-ahead, evidence pack |
| Main red | auto-bisect |
| Lockfile change | prepare warming, upgrade trial |
| Idle capacity | fuzzing, flake hunting, shard weights, memory-curve learning |
| Cron or tenant-defined | bring-your-own batch |

## Ideas, by what they save

**Stop agents chasing ghosts** (tokens and wall-time after a red result):

- *Baseline oracle.* For any base SHA, a record of which tests already fail or
  flake. The v0.1 hint channel could then say "this failure pre-exists on base".
- *Failure triage.* When an interactive run fails, rerun the failed test on the
  same input and on base in the killable tier, and label the failure `yours`,
  `flaky`, or `pre-existing`.
- *Hunk-bisect.* Find which hunk of the agent's dirty diff broke the test. Delta
  debugging over the snapshot; no tokens.
- *Rich evidence on rerun only.* Keep interactive runs lean; let batch rerun
  failures with Playwright traces and video on.

**Make green faster** (waiting):

- *Memoized results.* Keyed by manifest digest, command, and environment. An
  identical rerun returns instantly; batch precomputes results for branch heads
  and main commits. A remote cache at the level of whole commands, with no
  build-system buy-in.
- *Determinism certification.* Run the same input twice in batch. A command shape
  becomes memoizable only after its outputs match N times.
- *Learned test ordering.* Full-catalog runs teach which files correlate with
  which failures; interactive runs execute the likeliest-to-fail shards first.
- *Automatic follow-ons.* Full catalog after a focused pass; prepare warming for
  lockfile changes on open branches and dependency-bot PRs.
- *Test-impact maps and shard weights.* Both make interactive jobs smaller, which
  frees peak RAM.

**Catch agents colliding** (the merge-time pileup from parallel agents):

- *Cross-agent conflict detection.* Pandora sees every agent's uncommitted
  snapshot, which CI never does. Merge active worktrees pairwise, test, and warn
  before either commits. Cost grows as N², acceptable in the trough.
- *Merge-ahead testing.* Each open branch rebased onto the latest main.
- *Reverse-dependency testing.* When a package changes, run its dependents'
  suites: monorepo consumers, or downstream users for open-source tenants.

**Trust what agents wrote** (human review and post-merge surprises):

- *Mutation testing.* Measures whether agent-written tests assert anything.
- *Seeded-bug canaries.* Inject known mutants into a branch and check whether the
  tenant's tests and review agents catch them. Measures the factory's
  defect-escape rate.
- *Fuzzing and property tests.* Fully elastic; the corpus is the checkpoint.
- *Review evidence pack.* Per PR: full catalog, mutation delta, coverage delta,
  visual diffs.
- *Matrix runs and executable docs.* Node versions, browsers, dependency ranges;
  every code block in the docs.

**Pandora testing itself on real workloads:**

- *Shadow replay.* Re-run real tenant jobs in batch on a candidate backend (VM
  executor, network volume, lazy object-store hydration) and compare outcomes
  and timings. This is how the deferred storage measurement and a Docker-to-VM
  migration could use real workloads at no extra cost.
- *Memory-curve learning.* Run unknown command shapes once in batch so they can
  be packed when submitted interactively.
- *Continuous fault matrix.* The v0.1 recovery and cleanup probes, indefinitely.
- *Reproducibility drift.* Rebuild prepare images from scratch nightly to catch
  non-reproducible installs, yanked packages, and base-image CVEs.
- *Sticky-volume upkeep.* Verify, compact, and collect.

**Trough-only offering: quiet hosts.** Benchmarks must never run packed beside
other jobs. The trough is the only time a whole host can be leased exclusively
and cheaply, so benchmark-grade quiet runs are something the trough can sell and
the peak cannot. This needs an `exclusive` placement mode granted only in slack.

**Agent-in-the-loop** (spends tokens): dependency-upgrade attempts that open a PR
only when green; best-of-N candidate patches validated in parallel; test
generation accepted only if mutation score improves; bug-report reproduction;
codemod trials; overnight refactors gated by batch verification; an arena that
replays the tenant's past tasks across models and prompts to tune the factory.

## Dependency order

Gary wants all four bundles. The ideas depend on each other, so the order mostly
follows from that:

0. Shared base: a result store keyed by input digest, command, and environment;
   generators triggered from submitted snapshots; pressure evidence on results.
1. Memoized results and same-input reruns. Cheapest; yields the `flaky` label.
2. Baseline oracle: the full catalog on every main commit. Triage's
   `pre-existing` label, shard weights, fail-first ordering, determinism
   certification, and auto-bisect all read this data.
3. Automatic follow-ons: full catalog after a focused pass, merge-ahead, prepare
   warming.
4. Diff-level work: hunk-bisect, pairwise conflicts, reverse dependencies.
5. Elastic fillers: mutation, canaries, fuzzing, evidence packs.

## Trigger ladder

How far Pandora looks ahead of an explicit command is open; the rungs ship in
order and do not change the generator model.

1. **Submitted snapshots only.** Reuses the v0.1 capture path and consent story.
   Misses edits made since the last command.
2. **Commit and push triggers.** A webhook or git hook; no file watching.
3. **Resident watcher, opt-in.** Answers before the agent asks, but brings back
   continuous-sync complexity, Mac load (the pilot's top priority), and the
   live-source admission race found in the manifest-transfer experiment. Build it
   only if measured data shows most interactive runs miss the memo cache by one
   edit. Batch can measure that miss rate.

## Risks

- **Pressure-induced false results.** Batch jobs run throttled and swapped, so
  timing-sensitive tests fail for that reason alone. Flake hunting in batch can
  manufacture flakes. Every result needs PSI and throttle evidence attached;
  whether that is sufficient is open.
- **Nobody is waiting.** An unsolicited result is worthless unless it lands where
  an agent or orchestrator reads it, without becoming noise.
- **Demand exceeds the trough.** Twelve agents make 66 worktree pairs; the
  catalog is 200 journeys per commit; mutation testing is unbounded. On a shared
  fleet one tenant's fuzzers could starve another's triage.
- **Consent.** Speculative and cross-agent work runs tenant code nobody asked to
  run and reads dirty worktrees.

## Open

- Where unsolicited results are delivered: the hint channel on the next
  interactive result, a findings queue an orchestrator pulls, PR checks, or a
  routed mix.
- Rationing across tenants and generators. One candidate: a fair share of slack
  per tenant, ranked inside that share by decaying value per RAM-second, with
  unused share flowing to others. Not decided.
- How far speculation goes, and consent for it.
- Whose tokens pay for agent-in-the-loop generators.
- Whether this is a Pandora feature, a product on top of it, or something else.
