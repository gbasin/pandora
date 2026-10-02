---
status: log
---

# Learned ceiling split from the reservation, 2026-10-02

On 2026-10-02 the size learner stepped eichler's `check` job down from `xlarge`
to `large`, and every cold run after it was killed. This log records the
evidence (gbasin/pandora#194), the owner's ruling, and the rule as coded.

Branch `fix/learner-ceiling-194`. Nothing here ran on the live worker. The unit
tests are the only proof; see "Unverified".

## What happened

`check` is declared `large`. Its peaks are bimodal: warm runs 2 to 4 GiB, cold
runs (a Turbo typecheck cache miss) 10 to 11 GiB. The learner raised it to
`xlarge` on 2026-09-26, and cold runs passed for six days. At about 04:22 on
2026-10-02 it stepped the job down to `large`. 13 runs were killed as `oom`
that day, 6 worktrees inside 9 minutes (geteichler/eichler#1939, #1930, #1718).

Measured from the run records:

* Of 452 clean runs since 2026-09-26, 108 (24%) were cold.
* The 50-run window at the step-down (2026-10-01 15:37 to 2026-10-02 04:23,
  evening into overnight) held 2 cold runs. Its top five peaks were 10780,
  10422, 6009, 5952 and 5750 MiB. The newest peak was 3723 MiB.
* p95 is nearest-rank: the 48th of 50, which discards the top two. p95 was
  6009 MiB. The old rule (ruled 2026-09-24, `notes/v0.2-worker-queue-2026-09-24.md`)
  took `max(p95, newest) x 1.25 = max(6009, 3723) x 1.25 = 7511 MiB`, which
  fits `large`.
* So the step-down fired whenever two or fewer cold runs remained in the newest
  50, whatever the job needed when cold.

It could not recover. An `oom` resets the class to the declared one (`large`,
the class that had just failed) and restarts the window, and `oom` peaks never
feed learning. Stepping up needed 3 clean runs and one clean peak above
6553 MiB. A cold run at `large` ooms instead of finishing clean, and a killed
run uploads no build cache, so the next worktree on the same main is cold too.

## Ruling (Gary, 2026-10-02)

Split the ceiling from the reservation.

1. **The reservation** that admission charges against the worker's budget is
   unchanged: `p95 x 1.25` of the job's recent clean peaks, capped at the
   ceiling, and the whole ceiling for the first 3 runs.
2. **The ceiling** (the class, the kill limit on the run's cgroup) is never
   learned below the class the repository declares. It rises above the
   declared class to the class that fits the largest clean peak in the window
   times the margin.

Not ruled in, and left open in #194: an `oom` stepping the class above the
declared one, and an automatic retry at a larger class. The thrash thresholds
and the hint rules are unchanged, beyond keeping the hints truthful.

## The rule as coded

`admission.classify(peaks, current=declared)`: the smallest class whose
ceiling is at least `max(peaks) x 1.25` and not below `declared`; `xlarge` when
none fits. `peaks` is `Store.clean_since_oom`: the newest 50 (`HISTORY`) clean
peaks (`passed` or `command_failed`) recorded under the current declaration
after the job's last `oom`.

`Scheduler.learn`, at the end of every supervised run:

* `oom`: the peak is stored and never learned from. The class is reset to the
  declared one. The line says `(an oom resets it to the declared class)` when
  that is a change.
* `passed` or `command_failed`: with at least 3 (`MIN_SAMPLES`) peaks in that
  window, the class becomes `classify(window, current=declared)`. A change
  prints `pandora: size for <job>: <old> -> <new> (largest clean peak N MiB
  over K runs)`. The result's `learned.size_change` gains `max_mib` beside the
  existing `p95_mib` and `samples`.
* `timed_out`, `cancelled`, `infra_failed`: nothing about size.

`admission.reserve` is untouched. It still reads `Store.peaks` (the newest 50
clean peaks of the job, any declaration) and caps at the class's ceiling. For a
job whose peaks never needed more than its declared class, the class stays the
declared one and the reservation is the same number as before.

A fan-out's plan step and its shards keep separate histories and classes
(`admission.peak_key`), so each gets the same rule. The local lane does not
learn classes and is unchanged.

The ceiling follows the window's maximum, so one old outlier keeps the class
up until that run leaves the 50-run window. That is intended: a loose ceiling
costs no budget, and only the reservation does. There is no decay.

### State already on the worker

A store written by the old rule can hold a learned class below the declared
one. `Scheduler.size_class` reads the stored class through
`admission.at_least(stored, declared)`, so admission gives the declared class
from the next run. The stored row itself is rewritten by `learn` once 3 clean runs have been
recorded under the current declaration; until then it stays as it was and the
floor applies on every read. The ceiling is right from the first run. No manual
reset is needed. A row stored before `declared` was recorded (NULL)
is floored the same way.

### The eichler window under the new rule

| | Old rule | New rule |
|---|---|---|
| Class basis | `max(p95 6009, newest 3723) x 1.25 = 7511` | `max 10780 x 1.25 = 13475` |
| Ceiling | `large`, 8192 MiB | `xlarge`, 12288 MiB |
| Reservation | `min(8192, ceil(6009 x 1.25)) = 7512` | `min(12288, 7512) = 7512` |
| Cold run (10 to 11 GiB) | killed | fits |

The class stays `xlarge` until both cold peaks leave the window: 48 more warm
runs in the test's reconstruction. Then it returns to `large`, the declared
floor, and never below.

### The oom hint

The hint's branch for a run that used a learned class below the declared one
("the learned class for job X was Y; declared Z applies again from the next
run") can no longer fire for a new run. It stays, because result records
written before this change carry that case and `pandora result` renders their
hint again. `size_line` keeps the old `(p95 N MiB over K runs)` text for a
`size_change` that has no `max_mib`.

`pandora stats` now passes the row's declared class, not its learned one, when
it computes the reservation it reports.

## Residual exposure

The ruling closes the step-down that killed `check`. It does not close the
loop after an `oom`: if the class does return to the declared one (50 warm runs
in a row) and a cold run then ooms, the class stays declared, the window
restarts, and only a clean run above the declared class can raise it. That is
the part of #194 the ruling left open (oom steps up, retry at a larger class).

## Tests

`pandora/tests/test_admission.py` (`Classify`, `CeilingAndReservationSplit`)
and `pandora/tests/test_worker_queue.py` (`LearnedSizeClasses`,
`TheRunUsesTheLearnedClass`):

* the eichler window, reconstructed with the measured top five, p95 6009 and
  newest 3723: ceiling `xlarge`, reservation 7512, and the step back to `large`
  only after both cold peaks leave;
* declared `large`, every peak 1 GiB: ceiling `large`, reservation 1280;
* a clean peak above the declared class raises it;
* an `oom` resets to declared, the pre-oom peaks do not count, and the max rule
  applies again after 3 clean runs;
* a stored learned class below declared, with and without a recorded
  declaration, for a whole run and for a shard, runs at the declared class;
* plan step and shard classes learn separately;
* the size-change line, new and old forms.

## Unverified

Nothing ran on the live worker or against the live daemon. Not measured: the
class and reservation the live worker picks for `check` after upgrade, and the
correction of its stored rows on the first run.
