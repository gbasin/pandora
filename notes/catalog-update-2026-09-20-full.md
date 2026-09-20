---
status: log
---

# Full catalog update, 2026-09-20

The ordinary routed command `pnpm journeys --update`, with the launcher configured
for eight shards, completed as `975c177b87164fd1b3b6d9eb56e4695d`. All 200
journeys passed. The parent verified eight shard reports and cleanup before the
client published 198 ledger fixtures and the merged route manifest. The invocation
spent 132.4 seconds waiting with no admitted shard while focused coding-agent
requests also used the two-slot worker.

The command returned through the normal local route. Its evidence lives at
`~/.local/state/pandora/v01-catalog-routing/83c602863bc75ce2a3381959764e90b48dcc10952759b0b969f6c4f0ace9710e/975c177b87164fd1b3b6d9eb56e4695d`.
The tracked-publication intent records all 199 declared paths as published.

This run exposed a local publication cost: the recovery intent contained
24,610,096 bytes of base and target declarations and was rewritten after each
file. Backup creation timestamps span 39.43 seconds; the final intent was written
40.54 seconds after the first backup. Repeatedly serializing that payload adds
local work unrelated to remote test execution. A separate change will retain one
immutable declaration record and update compact progress receipts.

The full update is verified. Ordinary full-catalog validation of the returned
bytes and the twelve-session operating limit remain separate acceptance evidence.
