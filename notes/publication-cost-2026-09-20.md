---
status: log
---
# Catalog publication metadata cost

The full catalog update `975c177b87164fd1b3b6d9eb56e4695d` returned 199 declared
paths. Its v1 recovery document was 24,610,096 bytes. Rewriting that document after
each path produced approximately 4.9 GB of metadata writes. The observed interval
from the first retained backup to the final intent was 40.5427 seconds.

[PR #55](https://github.com/gbasin/pandora/pull/55) stores immutable declarations
once and binds compact progress receipts to their SHA-256. Recovery still reads
v1 records. V2 retains the conflict, interrupted-publication, explicit-resolution,
and externally-reverted-file checks.

A disposable local replay decoded the actual v1 declarations, created the
captured bases in a temporary repository, and verified every returned target.
It published all 199 paths in 0.9804 seconds. The declaration document was
24,599,306 bytes. All 201 progress and final receipts together were 38,191 bytes.
The final intent was 191 bytes. The original evidence and worktree were read-only
inputs to this replay.

The warm disposable replay is not a direct controlled comparison with the
original end-to-end run. The write-volume reduction is deterministic. The
measured time suggests the repeated metadata serialization caused most of the
observed local delay. Sixty-one routing tests passed, including a 200-file
write-volume regression, v1 recovery, and v2 declaration tampering.
