---
status: log
---

# Evidence on the background-work idea, 2026-09-20

The [brainstorm](trough-work-ideas-2026-09-20.md) proposed a zero-cost precursor
to its falsifying experiment: mine existing agent transcripts to size the waste,
and study what already exists. Six Claude Opus agents did that in parallel. This
note records what they found and what it changes. The brainstorm note is left as
written.

Sources: two read-only transcript analyses (scripts under
`experiments/transcript-mining/`; parsed data is not committed because it
contains session content) and four web research reports under
`notes/research-2026-09-20/`. The research reports are unreviewed agent output.
They cite a URL per claim and flag what they could not verify; none of those
citations has been independently checked. Numbers quoted from them below carry
that caveat.

## Headline

On Gary's own sessions, the "spend free RAM to save paid tokens" premise mostly
does not hold. The jobs aimed at token waste after a red result (memoization,
hunk-bisect, baseline oracle, flake labelling) address almost nothing measurable.
What survives is different from what the brainstorm ranked first: waiting,
environment failures, and verification aimed at human review.

## Transcript analysis 1: real eichler sessions

244 Claude Code sessions, 2026-07-16 to 2026-09-20: 71,522 assistant turns, 85.0M
output tokens, 23.3B cache-read tokens. 2,445 Bash calls ran a validation
command; 623 were red, in 95 sessions, giving 427 post-red episodes.

Post-red work is between 2.1% of all turns (only episodes that ended with the
same command going green; median 5 turns) and 10.4% (a full 60-turn window after
every red, which includes unrelated work). About 73% of episodes are the agent
finding a bug it really introduced.

| Proposed job | Measured | Verdict |
| --- | --- | --- |
| Memoized identical reruns | 2 identical reruns on unchanged source in 2,445 calls (0.08%) | Not worth building here. Agents narrow a rerun; they do not repeat it. Turbo already caches. |
| Hunk-bisect of the dirty diff | 0 cases in 66 episodes read in full | Not worth building here. tsc, oxlint and vitest name file and line. |
| Baseline oracle | About 4 episodes, about 80 turns (0.1%) | Not supported by this evidence. |
| Same-input rerun for flake labels | About 20 episodes, about 0.5% of turns; a third were assigned flake-hunting | Marginal. Agents already rerun cheaply when they suspect a flake. |
| Environment health check | 8% of reds by direct marker, about 20% by sampled relabel; 0.8–2.2% of turns | The one real category. |
| Push instead of poll | 1,898 turns (2.7%) on `sleep`, `until`, `gh run watch`, `pueue`; 65 more refused by the harness | Real, but almost all of it waits on GitHub Actions and the Codex autoreview, not on local validation. |

Environment failures have two shapes. The cheap one is a fresh worktree with no
`node_modules` (1–4 turns). The expensive ones cost 13–42 turns each: orphaned
`wrangler dev` workers exhausting loopback ports, stale simulator drivers holding
a device, a stale build still being served, another Claude session committing to
the same branch, and the Mac paging so hard that Pueue timed out mid-suite.
Several of these disappear when heavy runs leave the Mac, which supports the v0.1
remote-execution premise rather than the background-work idea.

The generous ceiling for everything a background system could address is about
5.4% of turns and 3.9% of output tokens, and roughly half of that is CI polling.
The tight bound is about 2.9%.

One pattern fit no category: agents chain many commands with `&&`, then spend
turns discovering which segment failed.

Limits. The keyword classifier was wrong 61% of the time; the agent read 66
episodes in full and reweighted, so trust the direction (identical reruns and
bisecting near zero, pre-existing tiny, environment real) more than the point
estimates. The environment estimate rests on 3 positives in an 18-episode sample
(95% interval roughly 4–41%). About 940 runs hid their outcome behind `| tail` or
`| grep`. Turns are not wall-clock: a four-minute Playwright run and a two-second
`tsc` each count once. This is one operator and one disciplined repository, with
explicit "do not assume a flake" norms and static gates (35% of reds) whose
errors explain themselves. A flakier suite or a less disciplined workflow could
look very different.

## Transcript analysis 2: Pandora trial sessions

96 sessions (45 Claude Opus, 51 Codex) across 16 trial groups, 1,598 tool calls,
about 79M tokens; the Claude side was billed $44.32.

Flaky and pre-existing failures: none measured, and none expected. The trials use
evaluator-seeded deterministic faults, so this corpus cannot size either problem.

Waiting dominates, and the harness decides what form it takes. 42.7% of tool
calls were waiting or polling (34.2% without one 244-call outlier). In the median
Codex session 53% of tool calls are empty polls. Claude instead blocks in one
foreground call: 89 timed validation calls, median 69 s, maximum 403 s, 144
minutes blocked in total. One Codex session resubmitted a byte-identical command
four more times, each returning exit 75, and inspected lock files with `od` in
between.

After a red result the median is 2.5 turns to the next edit. About two thirds of
those turns are legitimate diagnosis; about a quarter are waiting or confusion
about infrastructure feedback (exit 75, unsupported Docker commands, one exit-70
worker crash, and one agent polling eichler's local Pueue queue for a remote run).

31 of 96 sessions independently rediscovered the same seeding commit with
`git log`. "Whose change broke this" is identical for every agent on a base
commit, which makes it cheap to precompute. In one session the agent correctly
ignored a pre-existing warning only because a baseline run existed. Both are
anecdotes for the baseline oracle, not measurements.

Representativeness is weak: one-line fixes with pre-diagnosed briefs shrink the
legitimate-work denominator, and polling concentrates in the deliberately
saturated four- and twelve-agent lanes.

## What already exists

**Commodity; do not build.**

- Remote validation of uncommitted agent work. CircleCI ships it on every plan
  (Chunk Sidecars), Buildkite Preflight does a temp-branch variant and frames log
  caching as token savings, and crabbox (MIT) is "warm a box, sync the diff, run
  the suite".
- Fast runners at about $0.004 per 2-vCPU-minute; colocated NVMe caches;
  copy-on-write sticky disks promoted on exit 0 (Blacksmith and Namespace built
  this independently, which matches the multi-tenant note's sticky-path design).
- Predictive test selection (eight or more vendors; Gradle Develocity keys on
  input fingerprints, not commits, so it already covers uncommitted work).
- Merge-ahead testing (six vendors, GitHub's native queue, Zuul).
- Plain flaky-versus-broken classification (free in CircleCI, Playwright, Bazel).
- Memoization where a hermetic build graph exists (Nx `affected`, Bazel action
  cache). Pandora adds value only without one, which is where content hashing is
  hardest.
- An MCP server (seven test-intelligence vendors ship one) and agent sandboxes
  (fifteen or more vendors).

**Close neighbours worth reading.**

- *Clash* (MIT, Rust): cross-worktree conflict detection on uncommitted changes
  via `git merge-tree`, delivered through a Claude Code hook. Textual only; it
  cannot say "merges clean but breaks the tests".
- *greentree* (August 2026, tiny): memoizes test commands keyed on
  `git write-tree`, a command hash and an environment fingerprint, aimed at
  agents. Nearly Pandora's manifest-digest key.
- *Nx Cloud*: input-hash memoization, hash-derived flake detection, its own
  compute, an MCP server, and "Self-Healing CI", where an agent proposes a fix,
  verifies it by rerunning the task, and applies it. The closest shipping thing
  to the brainstorm's end state; limited to Nx monorepos and triggered from CI.
- *Morph Infinibranch*: forks a VM with memory state in under 250 ms. A possible
  backend or a competitor.
- *BuildBuddy*: snapshots whole Firecracker VMs including a warm build server,
  chunked into a content-addressed store with `userfaultfd` lazy paging; median
  CI about 30 s. *Namespace* routes the job to the node that already holds the
  cache. Both bear on the cache-tier decision the multi-tenant note deferred.

**Unclaimed across the surveys.**

- Selling idle capacity as generated verification work; speculating before being
  asked.
- Triage whose "pre-existing" arm actually runs the base commit. The flaky-test
  market mostly analyses results customers already produced.
- Flake-aware auto-bisect when main goes red. Google reports that about 40% of
  red test ranges have no culprit, and that flake-aware bisection raised accuracy
  from 0.80 to 0.97.
- Hunk-level bisect of an uncommitted diff (a 1999 technique with no maintained
  tool), and determinism certification for tests rather than build artifacts.
- Behavioural cross-agent conflict tests.
- A hosted mutation-testing compute service. None exists.

Unclaimed is not the same as wanted. The eichler analysis found no demand for
hunk-bisect or the baseline oracle in one disciplined repository.

## Verification aimed at human review

The token analyses cannot test this bundle, and it has the strongest external
evidence.

- Diff-scoped mutation testing surfaced as a review comment is the one technique
  with industrial deployment, published developer acceptance, and evidence that
  the signal maps to real bugs. Google runs it for 24,000+ developers, including
  TypeScript, and reports mutants coupling to 70% of high-priority bugs.
- Agent-written tests are measurably weak. Meta measured coverage-optimised LLM
  tests at a 2.4% mutant kill rate, rising to 15% with mutation guidance, and 49%
  of the valuable tests added no line coverage. Tests generated after faulty LLM
  code detected 14% of faults against 25% for independently generated tests. So
  "passes and raises coverage" does not measure fault detection.
- Faros reports, across 10,000 developers, +98% merged PRs and +91% review time.
- Design constraints. Free compute buys breadth across many changes, not depth on
  one target. Noise control is the real cost: Google needed hundreds of
  suppression rules to move mutant usefulness from 15% to 89%, and holds
  analyzers to under 10% "not useful". Preempt generation, never verification:
  ClusterFuzz fuzzes on preemptible workers and bisects on stable ones.
- Ranked by value for agent-written code, fit for preemptible compute, and low
  noise: (1) mutate the agent's own diff and check that the agent's own tests
  catch it; (2) diff-scoped mutation testing generally; (3) flake detection by
  mass repetition, the cheapest credible first step.
- Not worth building for a TypeScript, Playwright and Postgres monorepo:
  coverage-guided JavaScript fuzzing, SQL fuzzers, visual or replay regression,
  and home-built deterministic simulation. Do not claim a defect-escape rate:
  detectors trained on synthetic bugs collapse on real ones.

## Economics

- Blacksmith, on owned bare metal, published margin against paid utilization:
  about 35% gross margin at 10% utilization, 70% at 20%, 85% and up at 35%. The
  business tolerates low utilization. Raising utilization, which motivated the
  brainstorm, is therefore a weaker reason to build this than it first seemed.
- The research agent read that curve as "background work costs about 50 margin
  points". That inference is wrong for flat-priced hardware: free background work
  changes neither cost nor revenue. It costs margin only if the capacity could
  otherwise be sold, or if capacity scales with load.
- "Free RAM" exists only on flat-priced metal. Per-second sandboxes (Runloop,
  Cloudflare, Depot, Vercel) bill nothing while idle, so on elastic compute every
  speculative run is bought. The idea's economics depend entirely on the
  bare-metal substrate choice.
- Earthly failed twice with public post-mortems: buyers treat CI as a commodity,
  bottom-up adoption did not convert to revenue, and its free tool cannibalised
  the paid one. The mature end of this market prices per seat, not per minute.
- Managed spot tiers for CI do not exist; WarpBuild withdrew its in June 2026.

## Delivery

The brainstorm left delivery open. `additionalContext` on a `PostToolUse` hook
works the same way in Claude Code and Codex CLI, and Clash already delivers
conflict warnings that way. A label can ride the agent's own tool result with no
new surface.

One trajectory study (not verified) reports validation at 16.6% and setup at
10.0% of agent rounds on SWE-bench Verified, with cache reads dominating cost, so
pasted test output is paid for again on every later turn. That argues for short
labelled results over raw logs.

## What this changes

Dropped or demoted from the brainstorm, on this evidence:

- Predictive test selection, learned ordering, and merge-ahead: commodity.
- Memoized results: commodity with a build graph, and near-zero demand in eichler.
- Hunk-bisect and the baseline oracle: unclaimed, but no measured demand. Keep as
  ideas; do not build first.
- The "cheapest falsifying experiment" in the brainstorm (memo cache plus baseline
  oracle, measured in tokens per task) would most likely show nothing on eichler.
  It should be replaced.

Surviving, in order of evidence:

1. **Waiting.** The largest measured cost in the Pandora trials, as polling turns
   for Codex and blocked wall-clock for Claude. This is v0.1 work (admission,
   slot hold, feedback that stops resubmission), not background work. Speculation
   helps only if it is cheap, which depends on flat-priced capacity.
2. **Environment health.** The best-evidenced token saver: report per-worktree
   dependency freshness, orphaned processes, port holders, stale builds, and
   "another session owns this branch". Much of it is local, not remote, and part
   of it vanishes once heavy runs leave the Mac.
3. **Trust in agent-written tests.** Mutating the agent's own diff against the
   agent's own tests. Strong external evidence, no existing hosted service,
   naturally elastic and killable, and aimed at review time rather than tokens.
   Untested on eichler.
4. **Compute-spending triage and flake-aware auto-bisect.** Unclaimed and suited
   to Pandora's position, but demand is unmeasured outside one disciplined repo.

A better falsifying experiment for the third item: run diff-scoped mutation on a
sample of merged agent-written eichler PRs, by hand, with no infrastructure, and
count how many surviving mutants Gary would have wanted flagged in review. If
few, this bundle fails too.

Open, unchanged: rationing, consent for speculation, whose tokens pay for
agent-in-the-loop jobs, and whether any repository other than eichler looks
different. That last question matters most. Every demand number here comes from
one operator.
