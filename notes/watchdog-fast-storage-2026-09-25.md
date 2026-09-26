---
status: log
---
# The memory watchdog on fast storage, 2026-09-25

Why the canary's `oom verdict inside 60s` flaked on the live worker
(fast CPU, btrfs on local enterprise NVMe, kernel 7.0.0-31-generic)
and what the threshold numbers look like there, measured with the
replay script from the filing session (#150).

## What the canary saw

`file-cache thrash is killed as oom` kept passing; only the bound
failed. Three verdicts on 2026-09-25: **79.9 s** (cold golden, FAIL),
**57.8 s** (warm, bare pass), and a later canary's hog inside ~40 s.

## What the replay measured

The canary's own `file` hog replayed against the reused acme golden
on the worker, printing every sample. Unpatched driver: verdict in
43.9 s. Patched: 24.0 s.

Two of the three thrash legs are never marginal on this hardware:

- `pinned` holds from the first sample (`memory.current` rides at the
  482 MiB `memory.high` wall).
- Refused charges run **700-900/s** against the 500/s bar (the same
  loop-file pool measured 1,700-4,000/s; the rate is *lower* on fast
  storage, though the margin is still comfortable).

`psi_full10` is the fragile leg:

| | loop-file pool (b3-16) | local NVMe (pandora-rbx) |
| --- | --- | --- |
| hog PSI full avg10 | 5.4-8.3 % | ramps 0 -> ~2 over ~25 s, plateau 1.96-2.29 |
| verdict lag after wedge | ~21 s | 43-80 s, one run under the 2.0 bar entirely |

On NVMe page-ins resolve fast enough that a fully wedged cgroup barely
stalls. The signal hugs the old 2.0 threshold: any dip restarts the
unbroken-15 s sustain clock, and the second replay plateaued at **1.96,
below the threshold** — no verdict at all, only the 120 s wall. Also
noted: for the single-task hog cgroup `psi_some == psi_full`, so the
`full`/`some` distinction contributes nothing in the canary's case.

## The fix (#157)

- `thrash_psi` 2.0 -> **1.0**, half the measured NVMe plateau, still
  well above a healthy run's 0.0.
- The sustain is counted by `IncusDriver.stalled_seconds`: wedged time
  accumulated inside a trailing window twice the bar (30 s), not one
  unbroken streak. A dip pauses the clock instead of zeroing it — the
  same trick `thrash_window` already gave the rate leg.
- Samples now carry a per-sample `stalled` flag in the evidence.

Patched replay verdict: **24.0 s**, vs the old VM's ~21.5 s. The 60 s
canary bound is unchanged and now has real margin again.

## Residual watch item

`thrash_rate` is now the thinnest margin (700-900/s measured vs 500/s
bar). The windowed counting tolerates dips, but storage slow enough to
hold the rate under 500/s sustained would still verdict `timeout`
rather than `oom`. Worth re-reading the samples on the next worker.
