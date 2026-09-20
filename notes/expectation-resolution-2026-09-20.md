---
status: log
---

# Explicit expectation resolution, 2026-09-20

Conflicting journey expectation publication now retains verified base/target intent
before returning actionable feedback. The private launcher exposes
`pandora resolve-expectations ATTEMPT --keep-local` from the worktree root.
The command holds the same worktree request lock and requires matching active,
submitted, and verified successful terminal identities.

Resolution writes no source files. It records the current contents of every declared
expectation path, then closes the request. These local contents are not remotely
validated. The next ordinary validation tests the current worktree.

The durable receipt is the acceptance boundary. If the client dies after writing
the receipt or after marking intent resolved, retry resumes completion using that
same receipt. Later local edits remain untouched and do not replace the recorded
contents. A missing or inconsistent receipt fails closed.

All 46 routing tests passed before the final missing-receipt guard. The affected
20 tests passed again after that guard. Coverage includes conflicts, partial
publication, both resolution crash boundaries, preservation of later local edits,
and rejection of active/submission identity mismatch or failed remote results.
This evidence is local; real coding-agent conflict recovery remains part of the
final v0.1 evaluation.
