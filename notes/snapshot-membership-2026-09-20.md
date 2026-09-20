---
status: log
---

# Snapshot membership, 2026-09-20

A real Git fixture confirmed that a frozen snapshot contains dirty tracked
content, new files, and the new side of a rename. Tracked deletions and the old
rename path are absent. Later source edits do not change the accepted snapshot.

A registered worktree nested under the submitted worktree is excluded even when
no ignore rule covers it. The source enumerator accepts that exclusion only when
the nested path has Git's worktree file and resolves to the same common Git
directory. A changed registration during capture rejects the snapshot. A stale
registry path reused as an ordinary directory remains source input; an
unregistered nested repository still fails as an unsupported entry.

Git-ignored files remain outside the frozen source. In a local `FROM scratch`
Docker build, an ignored file required by `COPY` produced BuildKit's missing-path
failure. The snapshot records policy exclusions and registered worktree prefixes,
but does not enumerate Git-ignored inputs. The Docker client should direct a
failed build with a missing required input to `git check-ignore -v PATH`.
