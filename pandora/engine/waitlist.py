"""The worker's one admission queue: who waits, in what order, and for how long.

Before 2026-09-24 a plain remote run was admitted or refused at `submit`. That
day three `large` jobs were refused `admission-refused: memory` because other
sessions' runs held 7.8 of the worker's 14 GiB, and a large job never falls back,
so a busy worker meant exit 70 for every one of them. The owner's rulings:

* **A full worker queues; it does not refuse.** When memory or run slots are
  short, `submit` leaves the row `queued` and starts a waiter. The disk floor
  and a reservation larger than the whole budget still refuse, because waiting
  cannot fix them.
* **One queue, in arrival order.** No priority and no per-client share. The
  oldest waiting row is admitted first; a large row at the head blocks smaller
  ones behind it even when they would fit (`Scheduler.admit`, reason `queue`).
  Shards waiting for a lane stand in the same line.
* **The wait is bounded by the job's own history**, never by a config key:
  `max(120 s, min(1800 s, 3 x p50))` of how long this (repo, job)'s recent runs
  held their lane, and 600 s with fewer than three of them. Past the bound the
  row finishes `infra_failed`, cause `queue-timeout`, exit 70.

Who waits where: the engine. `submit` spawns a detached `wait` process per
queued row -- the same shape as the supervisor, and for the same reason: the
row lives in the worker's ledger, so a Mac-side daemon that restarts, or a
client that goes away, changes nothing about the queue. The waiter polls under
the admission gate, touches its row as a heartbeat, and on admission spawns the
ordinary supervisor and exits. No supervisor exists for a row until it is
admitted, so `supervisor_pid` keeps meaning "the command may be running".
"""
import statistics
import sys
import time

from . import admission, history, runner
from .ledger import Ledger
from .scheduler import Scheduler, gate

POLL = 2.0
BOUND_FLOOR = 120.0
BOUND_CEILING = 1800.0
BOUND_DEFAULT = 600.0
BOUND_FACTOR = 3.0


def bound(ledger, repo, job, *, role='single'):
    """Seconds a new row of this job may wait for admission.

    The p50 of its last few verdicts measured from admission to finish, so the
    time earlier runs spent queued does not stretch the next one's patience.
    """
    values = history.samples(ledger, repo, job, role=role, key='run')
    if len(values) < history.MIN_SAMPLES:
        return BOUND_DEFAULT
    return max(BOUND_FLOOR, min(BOUND_CEILING, BOUND_FACTOR * statistics.median(values)))


def running_rows(scheduler):
    return [row for row in scheduler.live_rows()
            if row['state'] in ('admitted', 'running', 'collecting')]


def estimate(ledger, running, ahead, *, now=None):
    """Seconds until a row behind `ahead` is likely admitted, or None.

    The soonest running row to free its lane, plus the typical run of every
    queued row ahead, one after another. Pessimistic where runs overlap and
    blind to memory arithmetic; None when any term has no history, rather than
    a number invented for it.
    """
    try:
        first = history.queue_eta(ledger, running, now=now) if running else 0.0
        if first is None:
            return None
        total = first
        for row in ahead:
            typical = history.typical(ledger, row['repo'], row['job'],
                                      role=row['role'] or 'single', key='run')
            if typical is None:
                return None
            total += typical
        return total
    except Exception:                               # noqa: BLE001 - a courtesy, never a verdict
        return None


def position(ledger, scheduler, run_id, *, now=None):
    """Where one queued row stands: {position, ahead, running, eta_seconds, ...}."""
    now = time.time() if now is None else now
    row = ledger.get(run_id)
    ahead = scheduler.ahead_of(run_id, now=now)
    running = running_rows(scheduler)
    deadline = row['queue_deadline'] if row is not None else None
    return {'position': len(ahead) + 1, 'ahead': len(ahead), 'running': len(running),
            'eta_seconds': estimate(ledger, running, ahead, now=now),
            'waited_seconds': round(now - (row['queued_at'] or now), 1) if row else 0.0,
            'bound_seconds': (round(deadline - row['queued_at'], 1)
                              if row is not None and deadline and row['queued_at'] else None),
            'deadline': deadline}


def enqueue(paths, ledger, scheduler, run_id, repo, job, *, now=None):
    """Put a claimed row in the queue with its deadline. Under the gate."""
    now = time.time() if now is None else now
    seconds = bound(ledger, repo, job)
    ledger.update(run_id, queued_at=now, queue_deadline=now + seconds)
    return seconds


def withdraw(paths, ledger, run_id, why):
    """Close a queued row as `cancelled`: nothing ran. Under the gate."""
    row = ledger.get(run_id)
    waited = round(time.time() - (row['queued_at'] or time.time()), 2)
    return runner.write_result(paths, ledger, run_id, outcome='cancelled', layer='engine',
                               exit_code=None, peak_mib=0, durations={'queue': waited},
                               evidence={'reason': why, 'withdrawn': True},
                               receipt={'clean': True, 'note': 'no instance was created'})


def expire(paths, ledger, scheduler, run_id, *, now=None):
    """Close a row that waited its whole bound: `infra_failed`, `queue-timeout`."""
    now = time.time() if now is None else now
    place = position(ledger, scheduler, run_id, now=now)
    row = ledger.get(run_id)
    note(paths, run_id, 'waited %s in the worker queue, its bound; not run (position %d, '
         '%d running)' % (history.fmt_seconds(place['waited_seconds']), place['position'],
                          place['running']))
    return runner.write_result(
        paths, ledger, run_id, outcome='infra_failed', layer='engine', exit_code=None,
        peak_mib=0, durations={'queue': place['waited_seconds']},
        evidence={'cause': 'queue-timeout', 'queue': place,
                  'reservation_mib': scheduler.reservation(
                      row['repo'], row['job'], row['size_declared'] or row['size_class'])[0]},
        receipt={'clean': True, 'note': 'no instance was created'})


def note(paths, run_id, text):
    try:
        with paths.log(run_id).open('a') as handle:
            handle.write('pandora: ' + text + '\n')
    except OSError:
        pass


def wait(root, run_id, *, python=None, poll=POLL, clock=time.time, sleep=time.sleep):
    """The waiter: hold one queued row until it is admitted, withdrawn or expired.

    Returns what became of it: `admitted`, `cancelled`, `queue-timeout`, or the
    state it was already in when this started (a second waiter, a row that
    finished meanwhile). Every decision is taken under the admission gate, so a
    `cancel` either withdraws the row before it is admitted or finds it
    admitted and asks its supervisor to stop -- never neither.
    """
    paths = runner.Paths(root).ensure()
    ledger = Ledger(paths.ledger)
    try:
        while True:
            with gate(paths.root):
                row = ledger.get(run_id)
                if row is None or row['state'] != 'queued':
                    return row['state'] if row is not None else 'stale'
                if not row['queue_deadline'] or (row['role'] or 'single') != 'single':
                    # Not a row `submit` queued: a shard its parent is admitting,
                    # or one with no bound. Not this waiter's to admit or expire.
                    return 'not-waitable'
                if row['cancel_requested']:
                    withdraw(paths, ledger, run_id, 'canceled while queued; nothing ran')
                    return 'cancelled'
                store = admission.Store(str(paths.peaks))
                try:
                    scheduler = Scheduler(ledger, store, budget_mib=runner.budget_of(paths),
                                          max_running=runner.max_running_of(paths)[0])
                    if clock() > (row['queue_deadline'] or 0):
                        expire(paths, ledger, scheduler, run_id, now=clock())
                        return 'queue-timeout'
                    verdict = scheduler.admit(run_id, row['repo'], row['job'],
                                              row['size_declared'] or row['size_class'])
                finally:
                    store.close()
                if verdict['admitted']:
                    waited = clock() - (row['queued_at'] or clock())
                    note(paths, run_id, 'admitted after %s in the worker queue'
                         % history.fmt_seconds(waited))
                    pid = runner.spawn(paths.root, run_id, python=python or sys.executable)
                    ledger.update(run_id, supervisor_pid=pid)
                    return 'admitted'
                # The heartbeat: a row whose waiter stops touching it leaves the
                # queue (`scheduler.QUEUE_STALE`) instead of blocking its head.
                ledger.update(run_id, queued_at=row['queued_at'])
            sleep(poll)
    finally:
        ledger.close()
