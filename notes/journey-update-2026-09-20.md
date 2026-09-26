# Focused journey updates with automatic tracked-file return

Evidence recorded 2026-09-20. This is the first integrated slice of the expanded
v0.1 scope, not support for the full catalog or arbitrary source synchronization.

`pnpm journey S0-01 --update` now uses the same frozen snapshot, bounded remote
services and recovery path as ordinary validation. The remote runner enables
update mode explicitly and returns the ledger plus route manifest as checksummed
proposals. The local adapter requires every non-S0-01 route entry to match the
captured manifest before it can publish either file.

The publisher preflights all declared destinations and records immutable
base/target intent before writing. It recognizes an already-applied target on
retry, preserves detected conflicts, and points to proposed files and retained
conflict evidence. Non-output source is checked separately; Pandora's own partial
writes no longer trigger a false stale-source result. Ordinary `git diff` exposes
returned changes. Cooperative ownership of those fixture files is required;
arbitrary editor races and multi-file atomicity are not promised.

Before VM evaluation, 72 local tests passed: 29 routing/publication, 36 worker and
seven generated-output tests. A route-level fault test used the real publisher,
interrupted after its first write but before receipt, then retried with subprocess
creation forbidden. Retry completed publication without another remote run. Other
tests cover unchanged outputs, additions/deletions, preflight and partial conflicts,
unsafe paths, unrelated manifest entries, and update propagation to the JS runner.

The dedicated Acme worktree was created from origin/main at
`fbeb008a283221bfedccc8fd47a6f6337d1ddf4d` and bootstrapped independently. Only its
S0-01 ledger and S0-01 route entry were removed to seed the update test; the other
199 route entries remained. No existing developer worktree was altered.


## Integrated VM result

Both runs used implementation `3e930da79a6d00ca441db39e1e15cc0413a94984`.
The update invocation `ab83600b9c9142549045ddd1d722cf32` passed and published both
expectations locally. The route manifest returned to its exact original contents;
all 199 unrelated entries retained their hashes. Only the declared S0-01 ledger
remained in the fixture worktree's Git diff.

The ordinary invocation `f2cd704138a2495b983b325d8069b8fa` then passed with
`update:false`, no proposals, and unchanged expectation hashes. Both runs verified
terminal and service cleanup; final postflight found no running Pandora units,
containers or networks.

| Invocation | Snapshot | Transfer | Queue | Execution | Request total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Update, ticket 14 | 4.9 s | 2.2 s | 0.007 s | 49.1 s | 62.6 s |
| Validate, ticket 15 | 5.0 s | 2.2 s | 0.008 s | 49.2 s | 62.6 s |

Both dependency preparations were warm. Request total comes from worker transport
metadata and excludes final local publication/release overhead. These are single
observations, not latency percentiles. Logs and assertions remain under
`~/.local/state/pandora/integrated-update-20260920`.

A subsequent local-only correction, `973d2d8`, moved temporary replacement files
out of the checkout into the attempt's publication directory on the same filesystem.
Its abrupt child-exit test proves an interrupted pre-replace write leaves no new
source file and retry succeeds. All 30 routing tests passed after this change;
the unchanged worker and generated-output suites retain their 36 and seven passes,
for 73 tests total. The remote runner was unchanged, so the VM runs were not repeated.

## Remaining limits

This implements focused S0-01 update only. It does not implement catalog updates,
other journey IDs, sharded execution, multi-server scheduling or the twelve-agent
readiness trial. Conflicts preserve data and retain the active result; accepting a
manually merged destination as final needs a future explicit resolution operation.
The present recovery path restores the captured base before automatic retry, or
retains exact already-published targets after interrupted delivery. There is no
claim that arbitrary simultaneous editors are fenced out.
