---
status: log
---

# Twelve-agent verification, 2026-09-21 UTC

Twelve concurrent local coding-agent sessions completed unassisted failure,
repair, and passing-validation loops against Pandora runtime `e779a84` (merged
in PR #61). Six used Codex Terra and six used Claude Opus. Six exercised the
service-backed S0-01 journey and six exercised borrower browser validation with
four native Playwright shards. The commands were unchanged:

```sh
pnpm journey S0-01
pnpm test:surface borrower-web chrome.spec.ts
```

The agents received only their assigned worktree, allowed source file, command,
and requirement to wait for a completed post-repair result. No agent received a
continuation prompt, recovery recipe, or operator intervention during this trial.
The headless Claude launcher disabled background tasks and Monitor, as documented
in the separate headless CLI evidence. Interactive Claude settings stayed unchanged.

## Verified repair loops

The root auditor authenticated every terminal result with the current evidence
validator, recomputed the current source digest in each worktree, and checked
returned surface output hashes. A separate Terra audit reviewed agent actions,
reports, and diffs. Controller success alone did not count.

| Session | Baseline failure | Repaired pass |
| --- | --- | --- |
| opus-journey-1 | `ab625a92df9e4b9cba591f5032e3c9eb` | `d7f92c27a693490a8037c17584a13429` |
| opus-journey-2 | `284e867b0a0e4413ae5a01442c0a7ad8` | `1737b62933784a73910850def9754457` |
| opus-journey-3 | `a8dee180dc884bfc8bc96c28598c8775` | `4577c88599df4bde8090108c6c39096b` |
| opus-surface-1 | `2473e9d681494a258a76f849b91d1ce5` | `5fbf907a5e2047f6a5963ccc302aed39` |
| opus-surface-2 | `8bbcf4ae014e45eea18de6e7633082cd` | `22a33e28898749a6aa84e973f04e0c04` |
| opus-surface-3 | `6f43ed8e02e240e18abcc1f479d60254` | `b3c93a16ac9e45e2ab6daa54d8c8b197` |
| terra-journey-1 | `aaa7a240ea974d7eaa55ca246676f453` | `d94539e0b2cc4037895738a4d4c5f5cb` |
| terra-journey-2 | `9f7122d64d57495ca542a3ed08411dc3` | `5b5daae96b4b4aea8525b9c69362e07a` |
| terra-journey-3 | `cb0d4e9fc6814df89c07b849ec73b4b8` | `6f98c5ef3bba46ccb3a4b750c4196ba0` |
| terra-surface-1 | `c7332a5fe4f34241bbfe0993f0eac42a` | `19cc6002d9614d98935eb957355c6701` |
| terra-surface-2 | `bef19ebbd32c4584a46b88cd4db270b2` | `6dd91c5d25fd41ca868a6e529e46109f` |
| terra-surface-3 | `4a332f7e43c34ff4a797979fe12bb174` | `d5626b0bac4f47d59e3d0dcd026c7dc3` |

All 24 task receipts verified: twelve intended test failures and twelve passes,
with verified cleanup. There were no infrastructure failures, duplicate attempts,
local-validation fallbacks, hidden test changes, or wrong-source passes.
Each journey agent changed only `apps/api/src/closing/projections.ts`; each surface
agent changed only `apps/borrower-web/index.html`. All six surface repairs returned
15 declared output files each, with no hash mismatch. Worktrees remain dirty and
preserved as evidence. All twelve controller lanes finished successfully, all
local active records became terminal, and the final Docker inventory was empty.

## Capacity and timing

The worker had four CPUs and about 16 GiB RAM. Its configured resource ceiling was
3.5 CPUs and 13,000 MiB RAM, with two admitted executions and at most two concurrent
shards per invocation. A live ledger sample showed two running and nine waiting
requests. Twelve agent sessions did not become twelve simultaneous test containers.

Maximum invocation queue time was 489.28 seconds, below the default
900-second budget. Snapshot capture had a 7.92-second median and
17.51-second maximum. Source transfer had a 6.40-second median and
10.41-second maximum. These include concurrent Mac and worker load.

The dedicated Mac had 16 GiB RAM. Its memory-pressure samples moved from normal
before launch to warning during startup and part of the run, then returned to
normal before completion. Empty-shell median latency stayed between 3.65 and
4.74 ms; the largest individual sample was 19.45 ms. Swap used rose from about
4,067 MiB to a peak sampled 4,508 MiB and finished near 4,500 MiB. Swap occupancy
alone does not measure ongoing swapping. Read-only Finder state calls completed
in 3.10 seconds during the trial and 2.29 seconds at the end. These include
Computer Use overhead. The samples are responsiveness proxies, not a human
interactive-use report or a long-term capacity guarantee.

Six unrelated orphaned shell busy loops had been removed before the baseline.
Earlier trial Mac samples are confounded by those loops; they are not used for
this result.

## Setup deviation

The preparatory lane-install commands mistakenly ran in the canonical Eichler
checkout instead of the twelve assigned worktrees. Only the integration worktree
was correctly bootstrapped beforehand. Agents found their own dependencies absent
and ran frozen installs in their assigned worktrees before validation. These
installs took about a minute each. Early CPU and memory samples therefore include
concurrent local dependency installation; this was not a prewarmed-session trial.
The launcher working directories and validation source identities were correct.
Future preparation must run `pnpm -C <assigned-worktree> install --frozen-lockfile`
and verify that worktree's installed tools before dispatch.

## Interpretation and retained evidence

This trial satisfies the agreed twelve-session automated repair-loop gate on the
evaluated two-slot worker. It follows earlier two- and four-agent trials and the
full journey/browser suite and fault-probe evidence. It does not establish
long-term unattended reliability, arbitrary-workload capacity, Agentboard UI
integration, or interactive preview forwarding. Memory warning during startup
remains an operating observation on this 16 GiB Mac.

An intermittent Docker inventory error occurred in earlier trials. It did not
recur here. Improved stderr retention supports diagnosis if it returns; no cause
or permanent fix has been established.

Raw evidence is retained at `~/.local/state/pandora/v01-surface-ramp/`, including
briefs, controller start/end records, `root-receipt-audit.json`,
`agent-action-audit.json`, `timing-audit.json`, host samples, the setup deviation,
and all 24 authenticated attempts. The controller run is
`pandora-v01-surface-ramp-20260921`. Runtime and input revision records are stored
alongside it. After all lanes completed, controller cleanup stopped the run supervisor and
marked the run cleaned. It preserved every worktree and evidence file.
