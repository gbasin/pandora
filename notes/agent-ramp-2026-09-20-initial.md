---
status: log
---
# Initial two, four, and twelve agent ramp

On 2026-09-20, actual local coding-agent CLIs exercised a two-slot Linux worker.
This trial does **not** establish twelve-agent readiness. Twelve lanes reported
completion, but only eleven had an independently verified repair and passing test.

The dedicated Mac had 16 GiB RAM and ten logical CPUs. The worker reserved
3,500 millicores and 13,000 MiB RAM, with two concurrent requests. Each journey
owned its database, pooler, proxy, and Workers runtime. Surface requests used one
Playwright worker. All lane worktrees had separate frozen dependency installs.

## Tasks and controls

The evaluator committed two isolated regressions at Eichler `8ba797c84`, based on
`53c28ae52`: a missing closing-disclosure milestone card and the borrower-web title
changed from `Ike` to `Application preview`. Four surface cases asserted the title.
The briefs required the ordinary focused test before and after a one-file repair.
They supplied no Pandora recovery strategy. These evaluation changes are not
product changes.

The two-session stage used one Terra Codex CLI and one Claude Opus CLI. The
four-session stage used two of each. The twelve-session stage used six of each,
split evenly between journey and surface tasks. The Claude CLI identified its
model as `claude-opus-5`; Codex used `gpt-5.6-terra` at medium reasoning.

The controller runs were `pandora-v01-two-20260920`,
`pandora-v01-four-20260920`, and `pandora-v01-twelve-20260920`. Prompts, logs, and
worktree identities remain in the local agent-fanout state. Returned manifests,
submissions, and terminals remain under
`~/.local/state/pandora/v01-agent-eval/routing/`.

## Results

Two of two and four of four agents completed the permitted repair and obtained
passing remote evidence. At twelve, all worktrees contained only the permitted
source edit and passed `git diff --check`. Eleven agents obtained the expected
baseline failure followed by a pass against changed source. No cancellation,
interruption, or local heavy-test fallback was found.

The seed snapshot was `91474697ab65541d57c1f61ed47590448f39d0ccb7f6b4714d68b7526bb8cd2d`.
Journey repairs produced `4f33555c985d27e287c8b09f6111f5b34d1b17ed39d17ee48e19e3fe58e03c9a`.
Surface repairs produced `eb67e5e76db1b2e562c9de78989affb27cd8d9d6b9d7f564e7d6baa7d4ddca4a`.

All six Opus lanes had a clean two-attempt trajectory. Several Terra lanes retried
while their own command was active, received exit 75, and inspected Pueue or
Pandora state manually. One Terra journey ran the failing baseline twice. Observed
remote waits included 268.2 seconds for a surface baseline and 142.7 seconds for a
repaired surface. These were remote capacity waits, not local Pueue admission.

The exception was `terra-surface-1`. Its first shell tool call printed `r.output`
and omitted the returned session handle. Its baseline request,
`b5f66c7b0c0c45b3b23e3052dd5146ef`, completed remotely with exit 1 and verified
cleanup. Later calls received “Validation is already active for this worktree.”
The agent made the title repair but never obtained passing validation. The initial
shell command had no completion event and the local request remained active.
Distinct worktree keys ruled out cross-agent identity collisions.

The duplicate guard prevented replacement as intended. Its instruction to wait
on the original tool handle was insufficient after that handle had been lost.
Controller success alone therefore overstated the trial outcome.

## Mac observations

Baseline swap was approximately 3,184 MiB. During the twelve-session stage, sampled
swap rose to approximately 4,659 MiB. Empty-shell median latency was roughly
4 ms before the ramp, reached 11.3 ms in an early twelve-session sample, and
returned to about 4 ms later. A read-only Finder state query took 1,267 ms at four
sessions and 651 ms at twelve. The final sample reported 58% free memory through
`memory_pressure` and a 5.94 ms maximum shell-spawn time.

These are host-level observations, not per-agent attribution or a human typing
and scrolling evaluation. Background machine load existed before the trial.
Samples are retained in `~/.local/state/pandora/v01-agent-eval/mac-*.json` and
`ui-*.json`. The unresolved wait-handle failure prevents a readiness claim even
though these samples show no sustained shell-latency collapse.
