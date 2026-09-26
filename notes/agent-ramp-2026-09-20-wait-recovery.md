# Twelve-agent explicit-wait trial, 2026-09-20

Runtime: `dd933fa`, subsequently documented and merged in PR #57. Run:
`pandora-v01-twelve-recovery-20260920`. Twelve actual CLI sessions started together:
six Codex `gpt-5.6-terra` and six subscribed Claude Opus sessions. Each had an
independently bootstrapped worktree at evaluation seed `8ba797c843ff71c254494968233d913fa604eb6f`.
Six repaired the web title regression and six repaired the S0-01 milestone
projection. Briefs specified normal commands and scoped source edits, not Pandora
recovery strategies.

## Outcome

Eleven sessions completed failure, repair, and passing validation without a nudge.
The twelfth, Opus journey 3, ended its headless turn after backgrounding the repaired
validation. It accurately reported the result as pending. CLI teardown killed the
capture before submission. A neutral continuation, “Continue. Finish the requested
validation and report its completed result before ending,” resumed the same Claude
conversation. It followed the printed incomplete-capture recovery instruction and
obtained a genuine passing result. This is assisted completion, not unassisted 12/12.

Terra surface 1 and 3 independently used printed `pandora wait` commands for baseline
and repaired attempts. Multiple observers attached to the same requests. No duplicate
heavy execution resulted. Both final reports cited actual passing evidence.

Two initial requests failed before tests because image collection read another
request's partially uploaded submission JSON. They returned infrastructure exit 70
with verified cleanup. The agents retried without evaluator recovery instructions.
This exposed a real metadata-transfer race and requires a fix before readiness.

Independent transcript/diff auditing and direct artifact checksum verification found
12 seeded test failures, 12 repaired passes, and those two infrastructure failures.
Every successful submitted source digest matched the final local worktree. Only the
allowed product file changed in each lane. No commits, local heavy fallback, false
passing claim, duplicated test execution, or unverified cleanup was found. The aborted
pre-submission capture is additional to the 26 remote submissions.

| Lane | Baseline test failure | Repaired pass |
| --- | --- | --- |
| opus-journey-1 | `fb9edc5b32fd4f6698f87c2cb1469147` | `887215aeff4c4e8899a53267824c526d` |
| opus-journey-2 | `229bd0a03c7a451e8b7cc25fe679597d` | `c861676dcaca42efbf263d2925f16c51` |
| opus-journey-3 | `7f9da03df5db47b5bad0c12362eb605d` | `cb4550a5a2fb420f8d7c22c63040bd13` |
| opus-surface-1 | `a3d9c867b4af49b3a67d27f62d1d0cca` | `cd309a91b84d41dca92bfd8f6b818a0e` |
| opus-surface-2 | `1bf44064784e4a8fa589ae8d7ba52dd3` | `675d87ccc90f44faab2918c81868804f` |
| opus-surface-3 | `0c14517767034d8a82e97ead5f044360` | `fafe1849e45c4249a9ae7e86ae526745` |
| terra-journey-1 | `a3056bb714c8447bac89adebcb8405da` | `9c44373ec5a747b1a7e6248df7e97b7a` |
| terra-journey-2 | `4468274ac3904ef38f0a4b9426a556f7` | `99b68de1002640719b2acca170773b49` |
| terra-journey-3 | `935e7c87e4cc476cb0289288b19a904e` | `dde04418472142a0a4aee4672af3c719` |
| terra-surface-1 | `c6162608bbd1415c88a70fe35af1bcd6` | `6f7bc23008dd4f0cacbe2d6983712719` |
| terra-surface-2 | `e75f0e7e3f214732be50053415ebcc91` | `015fa4e8d7d74ee38d4ea5385ee768d4` |
| terra-surface-3 | `868c635f434249e48a38dfee23c8563d` | `58cc0ff745844464a725fb35286b3f4f` |

## Machine and queue observations

The dedicated Mac has 16 GiB RAM and ten logical CPUs. The Linux worker had two
admitted slots, a 3.5 CPU / 13,000 MiB reservation ceiling, and isolated per-run
services. The maximum recorded invocation queue wait was 199.7 seconds, below the
unchanged 900-second default. Twelve agent sessions did not mean twelve test slots.

Before launch, sampled empty-shell median latency was 4.4 ms. During the trial,
medians ranged from 4.2 to 10.1 ms, with a 72.6 ms maximum individual sample. Swap
use decreased from 4,459 MiB before launch to 4,155 MiB after. Reported free memory
was 41–58%; raw kernel memory-pressure level reached 2 during startup and returned
to 1. These are sparse host-wide observations, not causal attribution to Pandora.

Read-only full Finder state capture took 3,727 ms early, 782 ms mid-trial, and
3,076 ms after. All returned screenshots. This variable automation response is a
proxy, not a human typing, scrolling, or interactive-debugging measurement. No
claim of zero UI latency or sustained all-day responsiveness follows.

Raw evidence: `~/.local/state/pandora/v01-recovery-eval/`, including neutral briefs,
Mac samples, controller snapshots, and `verified-attempts.json`. Controller transcripts
remain under its `pandora-v01-twelve-recovery-20260920` run. The original incomplete
Opus transcript was preserved separately from its continuation.

This trial proves explicit wait usefulness and bounded twelve-session operation,
but its infrastructure race and assisted completion prevent calling it a clean
unassisted readiness sample. A later note must record the post-fix result.
