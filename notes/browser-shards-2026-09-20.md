---
status: log
---

# Browser-surface sharding evidence

Gary required internal browser-surface sharding for v0.1. The SSH worker at
`40.160.93.34` ran native Playwright shards under its existing two-slot resource
configuration. The ordinary surface command stayed unchanged.

One admitted planner built production and fixture assets once, then froze the
selected native test IDs, their shard partition, and compiled-file hashes.
Each shard received a separate container with the same captured source and
verified compiled files. No shard changed the captured source directory.

The initial probe, `186ce291832344ae9463fcace2ccf887`, exposed an output mistake:
successful browser tests add screenshots beneath `e2e/dist`. Comparing the
entire post-test tree to the compiled tree incorrectly rejected those additions.
The adapter now checks that compiled files remain unchanged and collects new
files separately. Local publication assembles the planner build and shard
additions without downloading duplicate compiled trees. Different bytes for the
same returned path are rejected.

## Verified focused runs

| App or case | Attempt | Native tests | Shards with reports | Exit |
| --- | --- | ---: | ---: | ---: |
| Borrower, including two empty shards | `099f6174a78d42698e336a70159f2ca0` | 4 | 6/6 | 0 |
| Desk, both viewport projects | `2d9ad59f32724976a8492672dc5aba4a` | 6 | 3/3 | 0 |
| Default fail-fast | `c46c8cf87a334b15a5cca682b0c27323` | 4 selected | 2/4 | 1 |
| Explicit keep-going | `6dee0795405148a4adf5066ef730bb7f` | 4 | 4/4 | 1 |

All four had verified cleanup. The default failure explicitly left shards three
and four unrun. Empty shards executed native Playwright with
`--pass-with-no-tests`; they were not fabricated passing receipts.

The borrower command automatically returned eight screenshot files plus both
compiled output directories. Desk returned fourteen generated files; the
orchestrator exercised the same publication helper after direct warm-run
retrieval. Every returned generated file and every compiled file matched its
verified artifact hash. Prior borrower output generations remained available.

The source for the passing runs was
`eb67e5e76db1b2e562c9de78989affb27cd8d9d6b9d7f564e7d6baa7d4ddca4a`.
The failing runs captured the deliberately broken title at source
`91474697ab65541d57c1f61ed47590448f39d0ccb7f6b4714d68b7526bb8cd2d`.

Invocation queue time was 0.4304 seconds for borrower, 6.6604 seconds for Desk,
2.8619 seconds for fail-fast, and 20.5058 seconds for keep-going. These are
invocation wall-clock waiting measurements, not sums of overlapping shard waits.

An earlier six-shard attempt, `7f0b9835b68f49e18ad1ad724663c002`, stopped with
infrastructure exit 75 when Docker inventory failed before an empty shard
started. Its completed browser tests passed. The original error omitted Docker's
stderr. Diagnostics now retain it. A separate bounded probe performed 24
create/remove cycles while reading Docker inventory ten times without reproducing
the error. This does not establish a cause or a fix for that intermittent error.

The shared dispatcher also passed the existing service-backed journey regression:
`ab89f228157340fb8fd79bcb9689c082`, two selected journeys over two shards, including
required dropped-response replay and verified cleanup.

Raw evidence and independent hash checks are under
`~/.local/state/pandora/surface-shards-proof/`; `verified-surface-results.json`
records the focused-run audit. This event does not establish twelve-agent readiness.

## Full suites and failure recovery

Full Desk validation passed 298 native tests across four shards in attempt
`a60b34c7c4644ce0ad28cdc0371bb2e7`. Full borrower validation passed 433 native
tests across four shards in attempt `7d6394db874f4989b69c0e4476d9f23e`.
Both returned verified terminal success and cleanup. They shared the same worker
while separate fault probes queued behind them.

A two-test fixture deliberately wrote different bytes to the same generated path
from different shards. Both native tests passed, but attempt
`5ab12ade16a34a238e92b0df9828383f` correctly returned infrastructure exit 75 and
`results/surface-output-error.json`. It retained both artifacts and completed the
invocation without publishing contradictory files or requiring an impossible
same-result retry.

A separate fixture paused inside native tests. Cancelling parent
`4f0991355bda4d94a50b87fb36c9f3d2` while a shard was running returned exit 130,
verified cleanup, and an authenticated `results/surface-cancelled.json` receipt.
The borrower suite continued and passed. The cancellation receipt does not claim
that unfinished tests passed.

An independent read-only audit reran the current evidence validator over all four
terminal results. Logical output assembly verified 197 Desk files (25,021,206
bytes) and 332 borrower files (25,196,365 bytes), with no conflicting duplicate
paths. Client elapsed times were 651.84 seconds for Desk and 1,078.01 seconds for
borrower, including 56.55 and 189.32 seconds of invocation queue time respectively.
`independent-surface-receipt-audit.json` retains the detailed checks. These timings
include deliberate competing probes and are not an uncontended performance claim.
