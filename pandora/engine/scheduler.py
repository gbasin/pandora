"""Admit on memory, pin the CPU, and keep the answer across restarts.

The POC's `Admission` held its running set in a dict, which a worker restart
forgot (POC blocker 6). Here the ledger *is* the running set: held memory is the
sum of the reservations on live rows, so an engine that dies and comes back
admits against the same picture it left. Nothing else changes -- the reservation
arithmetic below is `admission.reserve`, unmodified.

Admission is serialized by one file lock. Two SSH calls arriving together must
not both read `held = 8 GiB` and both decide they fit.

The CPU number is the other half of the POC's result and the easier half to
get wrong. `PANDORA_CPUS` is a *pin*: the same count for every run, written as
the instance's `limits.cpu`, so a run sizes its own worker pool for the machine
it actually gets. The pin's width is in threads, what `nproc` reports; on an
SMT host the runner pins whole physical cores and `PANDORA_CPUS` counts those
(`runner.pin_cpus`, #201). The earlier form, host cores
divided by admitted runs, made a run's memory appetite a function of who else
was admitted -- the same command peaked at 3 GiB or 11 GiB -- which made its
size class unlearnable. A fixed pin makes the peak a property of the job, so
`learn` can trust it again.
"""
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import admission
from .ledger import LIVE

# A pin of 1 serializes everything a run does; two is the floor a bundler or a
# browser suite survives on.
MIN_CPUS = 2
# A queued row whose waiter has not touched it for this long is not in the
# queue. Its waiter died (a killed process, a rebooted worker before
# `reconcile`), and a row nobody is waiting on must not hold the head of the
# one queue for everybody behind it. Waiters touch their row every few seconds.
QUEUE_STALE = 30.0


def derived_cpus_per_run(threads):
    """The pin a host earns, in threads: a quarter of them, never below the floor.

    A quarter leaves about four heavy runs of headroom before cores are
    oversubscribed; `cpu.weight` arbitrates when they are. The manifest's
    `cpus_per_run` is the operator's override of this guess.
    """
    return max(MIN_CPUS, int(threads or 0) // 4)


@contextmanager
def gate(state):
    """Serialize admission across the engine's many short-lived processes."""
    path = Path(state) / 'admission.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


class Scheduler:
    def __init__(self, ledger, store, *, budget_mib, cores=None, max_running=None,
                 cpus_per_run=None, margin=admission.MARGIN,
                 floor=admission.FLOOR_MIB):
        if not isinstance(budget_mib, int) or budget_mib < admission.FLOOR_MIB:
            raise admission.AdmissionError(
                'host budget must be an integer of at least %d MiB' % admission.FLOOR_MIB)
        self.ledger = ledger
        self.store = store
        self.budget_mib = budget_mib
        self.cores = cores or os.cpu_count() or 1
        # None derives from the cores this scheduler sees; the engine passes the
        # worker's configured cap (`runner.max_running_of`).
        self.max_running = max_running if max_running else max(2, self.cores // 2)
        # None derives a quarter of the cores this scheduler sees; the engine
        # passes the worker's configured pin (`runner.cpus_per_run_of`). The pin
        # can never exceed the box: a manifest naming 8 on a 4-core host pins 4.
        self.cpus_per_run = max(1, min(cpus_per_run or derived_cpus_per_run(self.cores),
                                     self.cores))
        self.margin = margin
        self.floor = floor

    def reservation(self, repo, job, declared_class=None, role='single'):
        """(reservation, ceiling, size_class) for the next run of this job.

        The repository declares a size class, and `learn` may since have raised
        it, never lowered it; the ceiling is the operator's number for the
        class in use and is also the run's cgroup `memory.max`. The
        reservation is learned from what this job actually peaked at, and a job
        with fewer than three observations reserves its whole ceiling, because
        the first runs of an unknown job are the ones most likely to surprise.
        """
        key = admission.peak_key(job, role)
        size_class = self.size_class(repo, key, declared_class)
        peaks = self.store.peaks(repo, key)
        reserve = admission.reserve(peaks, size_class=size_class,
                                    margin=self.margin, floor=self.floor)
        return reserve, admission.ceiling_for(size_class), size_class, len(peaks)

    def size_class(self, repo, key, declared_class=None):
        """The class a run of this job gets: the learned one, never below the
        declared one. A class a store learned below the declaration before
        2026-10-02 reads as the declared class here, so the next run already
        has the room the repository asked for. The stored row is rewritten by `learn`
        after `MIN_SAMPLES` clean runs; until then this floor applies on every read."""
        declared = declared_class or 'medium'
        stored = self.store.size_class(repo, key, default=declared)
        return admission.at_least(stored, declared) if declared_class else stored

    def live_rows(self):
        """Live attempts that occupy the box.

        A fan-out parent is excluded on purpose: it reserves no memory, holds no
        instance and runs no command, so counting it would divide the CPU hint
        by a run that is not using a core and would spend a slot on bookkeeping.
        """
        return [row for row in self.ledger.live()
                if row['state'] in LIVE and (row['role'] or 'single') != 'parent']

    def queue(self, now=None):
        """The rows waiting for admission, in the order they will be admitted.

        One queue for everything, in arrival order (ruled 2026-09-24): a plain
        run's waiter and a fan-out's shard waiting for a lane stand in the same
        line. A row joins it when it first waits (`queued_at`), not when it is
        claimed, so a shard its parent has not tried to dispatch yet -- or never
        will, after a sibling failed -- holds no place.
        """
        now = time.time() if now is None else now
        rows = [row for row in self.ledger.live()
                if row['state'] == 'queued' and row['queued_at'] is not None
                and (row['role'] or 'single') != 'parent'
                and (row['updated'] or 0) >= now - QUEUE_STALE]
        return sorted(rows, key=lambda row: (row['queued_at'], row['created'], row['run_id']))

    def ahead_of(self, run_id, now=None):
        """Queued rows that must be admitted before `run_id`: all of them if it
        has not joined the queue yet."""
        rows = self.queue(now)
        names = [row['run_id'] for row in rows]
        return rows[:names.index(run_id)] if run_id in names else rows

    def held_mib(self, exclude=None):
        return sum(row['reservation_mib'] or 0 for row in self.live_rows()
                   if row['reservation_mib'] and row['run_id'] != exclude)

    def lanes(self, including=None):
        names = {row['run_id'] for row in self.live_rows()
                 if row['state'] in ('admitted', 'running', 'collecting')}
        if including:
            names.add(including)
        return max(1, len(names))

    def cpus_hint(self, lanes=None):
        """The run's pin width in threads: one number for every run, independent
        of lanes. The runner turns it into physical cores when it pins a cpuset."""
        return self.cpus_per_run

    def admit(self, run_id, repo, job, declared_class=None):
        """Decide, and write the decision into the ledger under one lock.

        Refusal reasons, in the order they are checked: `state` (the row is
        not `queued`: it was admitted, withdrawn or expired by someone else, and
        admitting it again would start a second supervisor), `memory` with
        `never` (the reservation is larger than the whole budget, so waiting
        cannot help), `queue` (older rows are waiting, and the oldest goes first
        even if this one would fit: a large row at the head blocks the smaller
        ones behind it, by ruling), then `slots` and `memory`. Every reason but
        `state` and `never` is waited out in the one queue.
        """
        row = self.ledger.get(run_id)
        if row is not None and row['state'] != 'queued':
            return {'admitted': False, 'reason': 'state', 'state': row['state']}
        role = (row['role'] if row is not None else None) or 'single'
        reserve, ceiling, size_class, samples = self.reservation(repo, job, declared_class, role)
        cold = samples < admission.MIN_SAMPLES
        if reserve > self.budget_mib:
            return {'admitted': False, 'reason': 'memory', 'never': True,
                    'reservation_mib': reserve, 'held_mib': self.held_mib(exclude=run_id),
                    'budget_mib': self.budget_mib, 'size_class': size_class}
        held = self.held_mib(exclude=run_id)
        ahead = self.ahead_of(run_id)
        if ahead:
            return {'admitted': False, 'reason': 'queue', 'ahead': len(ahead),
                    'reservation_mib': reserve, 'held_mib': held,
                    'budget_mib': self.budget_mib, 'size_class': size_class}
        running = [item for item in self.live_rows()
                   if item['state'] in ('admitted', 'running', 'collecting')]
        if len(running) >= self.max_running:
            return {'admitted': False, 'reason': 'slots', 'reservation_mib': reserve,
                    'running': len(running), 'max_running': self.max_running}
        if held + reserve > self.budget_mib:
            return {'admitted': False, 'reason': 'memory', 'reservation_mib': reserve,
                    'held_mib': held, 'budget_mib': self.budget_mib,
                    'size_class': size_class}
        lanes = self.lanes(including=run_id)
        hint = self.cpus_hint()
        self.ledger.update(run_id, state='admitted', reservation_mib=reserve,
                           ceiling_mib=ceiling, cpus_hint=hint, size_class=size_class,
                           admitted_at=time.time())
        return {'admitted': True, 'run_id': run_id, 'reservation_mib': reserve,
                'ceiling_mib': ceiling, 'size_class': size_class, 'cold_start': cold,
                'size_declared': declared_class or size_class,
                'cpus_hint': hint, 'lanes': lanes, 'held_mib': held + reserve,
                'budget_mib': self.budget_mib, 'samples': samples}

    def learn(self, row, peak_mib, outcome):
        """Record what the run actually used, and say what the next one reserves.

        An `oom` peak is stored and never learned from: a run killed at its
        ceiling only tells you it wanted more than the ceiling. It does reset
        the learned class to the declared one, so the next run gets the room
        the repository asked for.

        The class is the ceiling, and it is split from the reservation (ruled
        2026-10-02, gbasin/pandora#194): after `MIN_SAMPLES` clean peaks since
        the last `oom` under the current declaration, it is the smallest class
        that holds the largest of them (the newest `HISTORY`) times the margin,
        and never below the declared class (`admission.classify`). The
        reservation stays p95 x margin of the recent peaks, capped at that
        ceiling (`admission.reserve`).
        """
        mapped = {'passed': 'ok', 'command_failed': 'failed', 'oom': 'oom',
                  'timed_out': 'timeout', 'cancelled': 'lost',
                  'infra_failed': 'lost'}[outcome]
        repo, keys = row['repo'], row.keys()
        role = (row['role'] if 'role' in keys else None) or 'single'
        job = admission.peak_key(row['job'], role)
        declared = ((row['size_declared'] if 'size_declared' in keys else None)
                    or row['size_class'] or 'medium')
        before = self.size_class(repo, job, declared)
        self.store.record(repo, job, int(peak_mib), mapped, declared=declared)
        change = None
        if mapped == 'oom':
            if before != declared:
                change = {'reason': 'oom'}
            self.store.set_class(repo, job, declared, declared=declared)
        elif mapped in ('ok', 'failed'):
            clean = self.store.clean_since_oom(repo, job, declared=declared)
            if len(clean) >= admission.MIN_SAMPLES:
                chosen = admission.classify(clean, current=declared)
                if chosen != before:
                    change = {'reason': 'learned', 'max_mib': max(clean),
                              'p95_mib': admission.percentile(clean, 95),
                              'samples': len(clean)}
                self.store.set_class(repo, job, chosen, declared=declared)
        reserve, ceiling, size_class, samples = self.reservation(row['repo'], row['job'],
                                                                declared, role)
        if change is not None:
            change.update({'job': row['job'], 'from': before, 'to': size_class})
        return {'peak_mib': int(peak_mib), 'outcome': outcome,
                'reservation_mib': row['reservation_mib'], 'ceiling_mib': row['ceiling_mib'],
                'over_reservation': bool(row['reservation_mib']
                                         and peak_mib > row['reservation_mib']
                                         and peak_mib < (row['ceiling_mib'] or 1 << 30)),
                'over_ceiling': bool(row['ceiling_mib'] and peak_mib >= row['ceiling_mib'])
                                or outcome == 'oom',
                'next_reservation_mib': reserve, 'size_class': size_class,
                'size_declared': declared, 'size_change': change,
                'samples': samples, 'learned': mapped in ('ok', 'failed')}

    def snapshot(self):
        rows = self.live_rows()
        return {'budget_mib': self.budget_mib, 'held_mib': self.held_mib(),
                'queued': len(self.queue()),
                'cores': self.cores, 'lanes': self.lanes(),
                'cpus_hint_now': self.cpus_hint(),
                'running': [{'run_id': row['run_id'], 'repo': row['repo'], 'job': row['job'],
                             'state': row['state'], 'reservation_mib': row['reservation_mib']}
                            for row in rows]}


def size_line(change):
    """`size for check: large -> xlarge (largest clean peak 10780 MiB over 50 runs)`,
    or None. The ceiling moves on the window's largest clean peak, so that is
    the number the line gives; the reservation's p95 is in `size_change`."""
    if not change:
        return None
    if change.get('reason') == 'oom':
        why = 'an oom resets it to the declared class'
    elif change.get('max_mib') is not None:
        why = 'largest clean peak %d MiB over %d runs' % (change['max_mib'], change['samples'])
    else:
        # A change recorded before the ceiling moved on the maximum (2026-10-02).
        why = 'p95 %d MiB over %d runs' % (change['p95_mib'], change['samples'])
    return 'size for %s: %s -> %s (%s)' % (change['job'], change['from'], change['to'], why)
