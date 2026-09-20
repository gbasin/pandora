---
status: log
---

# Focused journey and browser-surface coverage, 2026-09-20

This trial expands the SSH pilot from S0-01 and borrower-web to focused catalog
journeys, dropped-response replay, expectation updates, and both browser surfaces
with file and `--grep` selectors. Agents still invoke the normal pnpm commands.
The launcher remains opt-in and can be bypassed by direct executable paths.

## Implementation and review

The router preserves selectors as an argv array through JSON submission. The
worker checks the requested surface app. Returned success evidence must match
the submitted workflow, journey ID and modes, or surface app before publication.
Both app build directories return to their normal local paths after success.

Review found two cases that the original narrow adapter did not cover:

- SX-20 and SX-21 have no API ledger. Updates now return only their route
  manifest. A report cannot omit a ledger that existed in captured input.
- The underlying CLI accepts `--update` and `--fault dropped` in either order.
  Pandora accepts both, and rejects unsupported options with the supported form.

The final local validation covers 82 tests: 43 worker tests, 32 routing tests,
and seven generated-output publication tests. The behavioral adapter test uses
mock imports; it is separate from the real VM results below. Review used Terra
agents for the adapters, integration tests, read-only review, and VM execution.

## Real VM evidence

The trial used the existing 4-CPU, 16-GiB Linux worker and Eichler snapshot base
`fbeb008a283221bfedccc8fd47a6f6337d1ddf4d`. The evaluation worktree already had
an S0-01 expectation edit; that edit was preserved.

All eight commands exited zero with verified terminal cleanup. The final VM
check found no running Pandora units, containers, or networks. Every invocation
hit the dependency-image cache. Times include source capture, transfer, execution,
and evidence retrieval; these short focused tests had no material queue wait.

| Routed command after `pnpm` | Total seconds | Execution seconds | Attempt |
| --- | ---: | ---: | --- |
| `journey S0-02` | 65.7 | 51.6 | `eb25309507bc4e5294a7022e4d49f365` |
| `journey S0-02 --fault dropped` | 46.4 | 32.7 | `24fb9da936054454a39a8a56ed39a62b` |
| `test:surface desk closing.spec.ts --grep separate package evidence` | 30.4 | 18.2 | `c974f9485e134f05b9156065864e1ada` |
| `test:surface borrower-web origination.spec.ts --grep removal request preserves applicants` | 31.5 | 17.3 | `d8cd791a0cae4a14837b6cbee0c1d624` |
| `journey SX-20 --update` | 32.0 | 11.1 | `9b211f8fd1c34f37af6578101d5d3e97` |
| `journey SX-20` | 23.9 | 10.4 | `85680adf7b8e4ae1af27fdb6b3bfebd6` |
| `journey S0-02 --update` | 64.3 | 52.3 | `3259047608804c3392e443b47becf9cb` |
| `journey S0-02` | 77.6 | 50.9 | `5011c5a8cfb64e54a67cda4ba44cc625` |

The browser selectors each ran exactly two tests. Both app build roots returned
with matching file hashes. SX-20 update returned only `write-routes.json`, and
S0-02 update returned its own ledger plus that manifest. All 199 unrelated route
entries were unchanged for each update. Ordinary validation passed afterward.

The first two cases used worker code `f8a5bd6`; the remaining cases used
`6cea86a`. The final flag-order and error-message adjustment in `ecd092a` has
local behavioral coverage. It does not change the tested worker invocations.

Local logs, receipts, output checksums, source snapshots, and assertions are
retained under `~/.local/state/pandora/workflow-coverage-20260920/`. The worktree
retains its preexisting S0-01 edit and the trial-generated S0-02 expectation edit.

## Scope limits

These are sequential scripted validation runs, not coding-agent repair sessions
or concurrency evidence. They do not establish full catalog coverage, shard
execution, multi-server scheduling, or twelve-agent readiness. Failed runs keep
diagnostics; arbitrary source writeback remains outside the contract.

The user also selected one cumulative queue-wait budget per sharded invocation.
This is recorded in the v0.1 contract. The current worker still schedules whole
invocations in FIFO order; shard scheduling remains future implementation.
