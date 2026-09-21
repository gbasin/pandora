# Explicit wait recovery, 2026-09-20

The initial twelve-agent trial exposed a Terra agent losing its shell wait handle.
The remote result existed, but a live client retained the worktree lock. Repeated
ordinary commands correctly refused duplicates and did not provide a useful way
to retrieve the result. Gary approved an explicit `pandora wait <attempt-id>` observer.

## Protocol and checks

The original invocation continues to wait normally. A short worktree state lock
protects allocation and publication. A separate per-attempt owner lock records
client ownership. Observers can retrieve and finalize an existing attempt while its
original owner remains alive. Every finalizer rereads the exact active identity.
Evidence downloads use a per-attempt lock and promote the terminal record last.
An observer interrupt detaches; it never cancels remote execution.

The accepted ID prints before capture. An observer waits while the original client
captures inputs and can follow before launch acknowledgement arrives. A completed
same-ID result is checked against current source without republishing. A different
active ID causes exit 75. Dead capture ownership returns actionable original-command
recovery rather than allocating replacement work.

At runtime commit `dd933fa`, 87 routing tests and 234 worker tests passed. Race tests
use real child-process file locks and cover owner-busy recovery, concurrent retrieval,
new allocation while an old owner lives, and late completion/cancellation fences.
The completed-result tests cover changed source, infrastructure acknowledgement,
initial capture, and current Docker-profile artifact limits. Terra reviewed the
integrated paths without finding an additional blocker.

## Live VM observation

From the clean conflict-evaluation integration worktree, a normal `pnpm journey S0-01`
and an independent `pandora wait` observed attempt
`ec82e63ce85f4130ac3798499d9a4814`. The observer entered before submission metadata
existed, waited for capture, streamed remote progress, and returned 0. The original
command also returned 0. A second explicit wait returned the same verified result.
Only one submission existed. Evidence resides under
`~/.local/state/pandora/v01-wait-proof/`, with `proof.json` and separate client logs.

The original client had already exited when the observer returned. This live probe
therefore does not prove stdout backpressure recovery; the owner-alive and late-client
cases are covered by the process-lock tests. The fresh twelve-agent trial evaluates
whether ordinary agents use the printed recovery path without task-specific coaching.
