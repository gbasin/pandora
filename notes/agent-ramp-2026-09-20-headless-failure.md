---
status: log
---

# Final agent ramp: headless early exits

On 2026-09-20, `pandora-v01-final-20260920` ran twelve fresh worktrees against
the seeded repair exercise. Ten lanes produced a completed seed failure, the
permitted source repair, and a completed pass. The result is **10/12 verified**,
not twelve successes.

The two exceptions were `opus-journey-3` and `opus-surface-3`. Each launched
its baseline validation as a Claude background Bash task, then ended while
stating it was waiting. Claude killed the background task at headless session
teardown. Neither lane edited source or recorded a product validation verdict.
The controller labelled both lanes succeeded because the CLI ended successfully;
that label is not completion evidence.

The final-eval routing records contain 23 accepted captures: 21 terminal records
(ten product failures, ten product passes, and one infrastructure exit 70) and
two unresolved captures from those early exits. The valid journeys changed only
`apps/api/src/closing/projections.ts`; valid surfaces changed only
`apps/web/index.html`.

## Other evidence

Terra surface 3 recovered its completed baseline with `pandora wait
659667c193b446fdbf068a70c1b6ac77` and its repaired pass with `pandora wait
6175e72357a949549ac7f9dcbcb89de6`.

Opus journey 1's first post-repair capture,
`82c3422fd20d4ed5b9be74edeffa99f4`, ended exit 70 before validation when the
remote worker reported unresolved Docker ownership from `sudo docker ps -a`.
Its fresh retry passed. This was infrastructure, not a product result.

A completed remote journey capture `dd9513a165e64fb5a139a2893504a5d4` exists
in the evidence store, but it has no agent repair loop and is not counted above.
The previously registered capture beginning `676` was never registered as a
completed result; `abandoned-upload-proof.json` records its remote abandonment.

## Machine samples

The recorded Mac samples are `mac-before.json`, `mac-start.json`, and
`mac-late.json` under the final-eval state directory. The late sample recorded
load averages `14.58 14.22 13.90`, 50% free memory, and 4107.25 MiB swap used.
These host signals are not attributable to an individual agent.
