"""The worker's one admission queue: who waits, in what order, and for how long.

Before 2026-09-24 a plain remote run was admitted or refused at `submit`. That
day three `large` jobs were refused `admission-refused: memory` because other
sessions' runs held 7.8 of the worker's 14 GiB, and a large job never falls back,
so a busy worker meant exit 70 for every one of them. The owner's rulings:

* **A full worker queues; it does not refuse.** When memory or run slots are
  short, `submit` leaves the row `queued` and starts a waiter. The disk floor
  and a reservation larger than the whole budget still refuse at `submit`.
* **A queued row waits on disk as it waits on memory** (issue #142). The pool
  can drop below the floor between `submit` and admission, so the waiter
  checks the floor before it asks the scheduler (`admit`), as a fan-out's
  shards do. Below the floor the row keeps its place and its deadline: a run
  that finishes frees its clone, and the source collection frees more. It says
  so in its log at most every `STILL_EVERY` seconds, and at the bound it ends
  `queue-timeout` with the pool's reading in its evidence.
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
import json
import statistics
import sys
import time
from pathlib import Path

from . import admission, history, runner
from .ledger import Ledger
from .scheduler import Scheduler, gate

POLL = 2.0
BOUND_FLOOR = 120.0
BOUND_CEILING = 1800.0
BOUND_DEFAULT = 600.0
BOUND_FACTOR = 3.0
# A row waiting on disk says so in its log once, then at most this often.
STILL_EVERY = 60.0


# One pool reading serves every admission site for this long. Each waiter is
# its own process polling every `POLL` seconds under the gate, and each probe
# is a `sudo btrfs filesystem usage`, so the reading is shared through a file
# in the engine root rather than taken once per waiter per poll.
DISK_CACHE_SECONDS = 10.0
DISK_CACHE = 'disk_reading.json'


def disk_reading(paths, *, now=None):
    """`runner.disk_headroom`, at most `DISK_CACHE_SECONDS` old."""
    now = time.time() if now is None else now
    cache = Path(paths.root) / DISK_CACHE
    try:
        cached = json.loads(cache.read_text())
        if 0 <= now - cached['at'] < DISK_CACHE_SECONDS and isinstance(cached['room'], dict):
            return cached['room']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    room = runner.disk_headroom(paths)
    try:
        runner.write_json(cache, {'at': now, 'room': room})
    except OSError:
        pass
    return room


def below_floor(paths):
    """A refusal verdict when the pool is below its disk floor, else None.

    The one disk check every admission site makes: `submit` refuses with it,
    and the waiter and a fan-out's shards wait on it. Disk has no reservation
    arithmetic, so it is checked before `Scheduler.admit`, and a row never
    holds a memory reservation for an instance the pool has no room to clone.

    A probe that fails (a `btrfs` timeout, a missing `sudo`) fails open, as the
    health poll's does: an exception here would end a waiter with its row
    still `queued` and nobody to admit it until the next reconcile.
    """
    try:
        room = disk_reading(paths)
    except Exception:                               # noqa: BLE001 - a probe, never a crash
        return None
    if room.get('ok'):
        return None
    return {'admitted': False, 'reason': 'disk-floor', 'capacity': room}


def admit(paths, scheduler, run_id, repo, job, size):
    """The disk floor, then the scheduler: a verdict like `Scheduler.admit`'s."""
    return below_floor(paths) or scheduler.admit(run_id, repo, job, size)


def disk_reason(verdict):
    return (verdict.get('capacity') or {}).get('reason') or 'the pool is below its floor'


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


def expire(paths, ledger, scheduler, run_id, *, now=None, capacity=None):
    """Close a row that waited its whole bound: `infra_failed`, `queue-timeout`.

    `capacity` is the pool's last reading when the row was last held back by
    the disk floor rather than by memory or slots. The cause stays
    `queue-timeout`: the row was queued, and a retry joins the same queue.
    """
    now = time.time() if now is None else now
    place = position(ledger, scheduler, run_id, now=now)
    row = ledger.get(run_id)
    note(paths, run_id, 'waited %s in the worker queue, its bound; not run (position %d, '
         '%d running%s)' % (history.fmt_seconds(place['waited_seconds']), place['position'],
                            place['running'],
                            '; the pool was below its disk floor' if capacity else ''))
    evidence = {'cause': 'queue-timeout', 'queue': place,
                'reservation_mib': scheduler.reservation(
                    row['repo'], row['job'], row['size_declared'] or row['size_class'])[0]}
    if capacity:
        evidence['capacity'] = capacity
    return runner.write_result(
        paths, ledger, run_id, outcome='infra_failed', layer='engine', exit_code=None,
        peak_mib=0, durations={'queue': place['waited_seconds']}, evidence=evidence,
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
    # The pool's reading while the floor holds this row back, else None; and
    # when the waiter last said so in the row's log.
    capacity, said = None, None
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
                                          max_running=runner.max_running_of(paths)[0],
                                          cpus_per_run=runner.cpus_per_run_of(paths)[0])
                    if clock() > (row['queue_deadline'] or 0):
                        expire(paths, ledger, scheduler, run_id, now=clock(),
                               capacity=capacity)
                        return 'queue-timeout'
                    verdict = admit(paths, scheduler, run_id, row['repo'], row['job'],
                                    row['size_declared'] or row['size_class'])
                finally:
                    store.close()
                if verdict.get('reason') == 'disk-floor':
                    capacity = verdict['capacity']
                    if said is None or clock() - said >= STILL_EVERY:
                        note(paths, run_id, 'queued run waits on disk: %s'
                             % disk_reason(verdict))
                        said = clock()
                else:
                    capacity = None
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
