---
status: log
---
# Docker command state trace, 2026-09-20

- t0: worktree A={tag:old}, worktree B={tag:other}. Each tag record is under its
  canonical worktree path hash on the remote host, outside a launcher session.
- t1: A accepts a run. submission={image:old}; the immutable image ID is resolved
  before queue admission. No image deletion occurs when mappings change.
- t2: a build from B finishes. B={tag:new-other}; A's accepted run still uses old.
- t3: A's run executes. A writable mount uses a separate copy of its frozen source,
  never the shared rsync hardlinks. No mount means image contents, regardless of
  later local edits. Output publication follows the declared generated roots.
- t4: A rebuild fails. A={tag:old}; no mapping publication occurs on failure.
- t5: A rebuild succeeds and BuildKit stops. Atomic publication changes A's tag.
  Physical attempt tags preserve old images for accepted runs and recovery.
- t6: transport disappears. The remote worker remains under systemd; retry follows
  the recorded request. Explicit cancellation runs cleanup and preserves the old
  mapping if publication has not occurred. Death after mapping publication but
  before terminal publication remains an operator reconciliation case.

Missing image resolution must fail before remote submission and clear the local
request. Keeping submission metadata for a nonexistent attempt would trap the
agent in recovery; the client removes that metadata on this preflight failure.
Worker death invokes the stop hook. A build's pending intent authorizes stopping
its dedicated builder; a run's intent authorizes removal of its named container.
The shared admission lease and orphan checks prevent a successor using the same
builder during unresolved cleanup. No automatic physical image garbage collection
is implemented in this pilot, so removing a mapping cannot invalidate queued runs.
