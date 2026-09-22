"""Admit on memory, share the CPU, and keep the answer across restarts.

The POC's `Admission` held its running set in a dict, which a worker restart
forgot (POC blocker 6). Here the ledger *is* the running set: held memory is the
sum of the reservations on live rows, so an engine that dies and comes back
admits against the same picture it left. Nothing else changes -- the reservation
arithmetic below is `admission.reserve`, unmodified.

Admission is serialised by one file lock. Two SSH calls arriving together must
not both read `held = 8 GiB` and both decide they fit.

The CPU hint is the other half of the POC's result and the easier half to get
wrong. `PANDORA_CPUS` is a *share* -- host cores divided by the number of
admitted runs -- not the core count. Telling every run it has four cores on a
four-core box cost 59% at two runs and 66% at three, because each run then sizes
its own worker pool for a machine it does not have to itself.
"""
import fcntl
import os
from contextlib import contextmanager
from pathlib import Path

from . import admission
from .ledger import LIVE

# A hint of 1 tells a runner to go single-file, which is right on a small box
# but wrong on a large one: 16 cores split 8 ways is still 2. The floor is a
# guess, flagged as such in the POC note, and untested above 4 cores.
MIN_CPUS = 1


@contextmanager
def gate(state):
    """Serialise admission across the engine's many short-lived processes."""
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
    def __init__(self, ledger, store, *, budget_mib, cores=None, max_running=8,
                 margin=admission.MARGIN, floor=admission.FLOOR_MIB):
        if not isinstance(budget_mib, int) or budget_mib < admission.FLOOR_MIB:
            raise admission.AdmissionError(
                'host budget must be an integer of at least %d MiB' % admission.FLOOR_MIB)
        self.ledger = ledger
        self.store = store
        self.budget_mib = budget_mib
        self.cores = cores or os.cpu_count() or 1
        self.max_running = max_running
        self.margin = margin
        self.floor = floor

    def reservation(self, repo, job, declared_class=None):
        """(reservation, ceiling, size_class) for the next run of this job.

        The repository declares a size class; the ceiling is the operator's
        number for that class and is also the run's cgroup `memory.max`. The
        reservation is learned from what this job actually peaked at, and a job
        with fewer than three observations reserves its whole ceiling, because
        the first runs of an unknown job are the ones most likely to surprise.
        """
        size_class = self.store.size_class(repo, job, default=declared_class or 'medium')
        peaks = self.store.peaks(repo, job)
        reserve = admission.reserve(peaks, size_class=size_class,
                                    margin=self.margin, floor=self.floor)
        return reserve, admission.ceiling_for(size_class), size_class, len(peaks)

    def live_rows(self):
        """Live attempts that occupy the box.

        A fan-out parent is excluded on purpose: it reserves no memory, holds no
        instance and runs no command, so counting it would divide the CPU hint
        by a run that is not using a core and would spend a slot on bookkeeping.
        """
        return [row for row in self.ledger.live()
                if row['state'] in LIVE and (row['role'] or 'single') != 'parent']

    def held_mib(self, exclude=None):
        return sum(row['reservation_mib'] or 0 for row in self.live_rows()
                   if row['reservation_mib'] and row['run_id'] != exclude)

    def lanes(self, including=None):
        names = {row['run_id'] for row in self.live_rows()
                 if row['state'] in ('admitted', 'running', 'collecting')}
        if including:
            names.add(including)
        return max(1, len(names))

    def cpus_hint(self, lanes):
        """Host cores split between the admitted runs, never below the floor."""
        return max(MIN_CPUS, self.cores // max(1, lanes))

    def admit(self, run_id, repo, job, declared_class=None):
        """Decide, and write the decision into the ledger under one lock."""
        reserve, ceiling, size_class, samples = self.reservation(repo, job, declared_class)
        cold = samples < admission.MIN_SAMPLES
        running = [row for row in self.live_rows()
                   if row['state'] in ('admitted', 'running', 'collecting')]
        if len(running) >= self.max_running:
            return {'admitted': False, 'reason': 'slots', 'reservation_mib': reserve,
                    'running': len(running), 'max_running': self.max_running}
        held = self.held_mib(exclude=run_id)
        if held + reserve > self.budget_mib:
            return {'admitted': False, 'reason': 'memory', 'reservation_mib': reserve,
                    'held_mib': held, 'budget_mib': self.budget_mib}
        lanes = self.lanes(including=run_id)
        hint = self.cpus_hint(lanes)
        self.ledger.update(run_id, state='admitted', reservation_mib=reserve,
                           ceiling_mib=ceiling, cpus_hint=hint, size_class=size_class)
        return {'admitted': True, 'run_id': run_id, 'reservation_mib': reserve,
                'ceiling_mib': ceiling, 'size_class': size_class, 'cold_start': cold,
                'cpus_hint': hint, 'lanes': lanes, 'held_mib': held + reserve,
                'budget_mib': self.budget_mib, 'samples': samples}

    def learn(self, row, peak_mib, outcome):
        """Record what the run actually used, and say what the next one reserves.

        An `oom` peak is stored and never learned from: a run killed at its
        ceiling only tells you it wanted more than the ceiling, which is an
        operator decision rather than a learned one.
        """
        mapped = {'passed': 'ok', 'command_failed': 'failed', 'oom': 'oom',
                  'timed_out': 'timeout', 'cancelled': 'lost',
                  'infra_failed': 'lost'}[outcome]
        self.store.record(row['repo'], row['job'], int(peak_mib), mapped)
        reserve, ceiling, size_class, samples = self.reservation(row['repo'], row['job'],
                                                                row['size_class'])
        return {'peak_mib': int(peak_mib), 'outcome': outcome,
                'reservation_mib': row['reservation_mib'], 'ceiling_mib': row['ceiling_mib'],
                'over_reservation': bool(row['reservation_mib']
                                         and peak_mib > row['reservation_mib']
                                         and peak_mib < (row['ceiling_mib'] or 1 << 30)),
                'over_ceiling': bool(row['ceiling_mib'] and peak_mib >= row['ceiling_mib'])
                                or outcome == 'oom',
                'next_reservation_mib': reserve, 'size_class': size_class,
                'samples': samples, 'learned': mapped in ('ok', 'failed')}

    def snapshot(self):
        rows = self.live_rows()
        return {'budget_mib': self.budget_mib, 'held_mib': self.held_mib(),
                'cores': self.cores, 'lanes': self.lanes(),
                'cpus_hint_now': self.cpus_hint(self.lanes()),
                'running': [{'run_id': row['run_id'], 'repo': row['repo'], 'job': row['job'],
                             'state': row['state'], 'reservation_mib': row['reservation_mib']}
                            for row in rows]}
