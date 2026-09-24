"""Memory-admitted scheduling for the Incus executor.

Admit on memory, CPU soft. A run's *reservation* is learned from what that
(repo, job) actually used: p95 of observed peaks times a margin, clamped to a
floor and to its size class's ceiling. The ceiling is also the run's hard cap,
so the two numbers mean different things and are enforced in different places:

  reservation  what the scheduler holds against the host budget
  ceiling      what the instance cgroup refuses to exceed

Between them is slack on purpose. A run that outgrows its reservation but
stays under its ceiling finishes and teaches the next one; a run that reaches
its ceiling is killed with outcome `oom`. Nothing here talks to Incus, and it
is not the v0.1.1 ledger: no locks, no attempt identities, no receipts.
"""
import json
import math
import sqlite3
import time

# Size classes exist so that one runaway job cannot learn its way to the whole
# box. The ceiling is an operator decision; the reservation is learned.
CLASSES = {'small': 1024, 'medium': 4096, 'large': 8192, 'xlarge': 12288}
FLOOR_MIB = 512
MARGIN = 1.25
HISTORY = 50          # peaks kept per (repo, job)
MIN_SAMPLES = 3       # below this, a job is still cold


class AdmissionError(ValueError):
    pass


def ceiling_for(size_class):
    if size_class not in CLASSES:
        raise AdmissionError('unknown size class ' + repr(size_class))
    return CLASSES[size_class]


def percentile(values, q):
    """Nearest-rank percentile. Deterministic, and defined for one sample."""
    if not values:
        raise AdmissionError('percentile of no samples')
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def reserve(peaks, *, size_class, floor=FLOOR_MIB, margin=MARGIN, min_samples=MIN_SAMPLES):
    """The reservation for a (repo, job) given its observed peaks, in MiB.

    Cold start — fewer than `min_samples` peaks — reserves the class ceiling.
    That is deliberately pessimistic: the first runs of an unknown job are the
    ones most likely to surprise, and over-reserving delays a run whereas
    under-reserving oversubscribes the host and slows every run on it.
    """
    ceiling = ceiling_for(size_class)
    if floor > ceiling:
        raise AdmissionError('floor %d exceeds class ceiling %d' % (floor, ceiling))
    if margin < 1:
        raise AdmissionError('margin must be at least 1')
    usable = [p for p in peaks if isinstance(p, int) and not isinstance(p, bool) and p > 0]
    if len(usable) < min_samples:
        return ceiling
    return int(min(ceiling, max(floor, math.ceil(percentile(usable, 95) * margin))))


def classify(peaks, *, current='medium', learned=False):
    """Suggest a size class from history.

    By default it never lowers below `current` and reads the largest peak: the
    suggestion a person is shown after an `oom`. With `learned=True` it is the
    rule the worker applies by itself (ruled 2026-09-24): the smallest class
    whose ceiling holds p95 of the peaks times the margin, up *or* down, from
    `small` to `xlarge`. p95 rather than the maximum, so one outlier does not
    pin a job to a class its other runs never needed; the margin, so a class is
    not chosen that the p95 run would fill to the brim.
    """
    if not peaks:
        return current
    observed = percentile(peaks, 95) if learned else max(peaks)
    for name in sorted(CLASSES, key=CLASSES.get):
        if CLASSES[name] >= observed * MARGIN:
            if learned:
                return name
            return name if CLASSES[name] >= CLASSES[current] else current
    return max(CLASSES, key=CLASSES.get)


class Store:
    """Peaks per (repo, job). SQLite so a worker restart does not forget."""

    def __init__(self, path=':memory:'):
        # `check_same_thread=False` because the client daemon admits from one
        # thread per connection and serializes every touch of this store behind
        # its own lock; SQLite's assertion would protect nothing and cost the
        # local lane its learned peaks.
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS peaks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, job TEXT NOT NULL,
            peak_mib INTEGER NOT NULL, outcome TEXT NOT NULL, at REAL NOT NULL)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS classes (
            repo TEXT NOT NULL, job TEXT NOT NULL, size_class TEXT NOT NULL,
            PRIMARY KEY (repo, job))''')
        # Which declared class a learned one was learned from. A store written
        # before learning existed has no such column; its rows read as NULL,
        # which is "not learned from anything", and are honored as before.
        have = {row['name'] for row in self.db.execute('PRAGMA table_info(classes)')}
        if 'declared' not in have:
            try:
                self.db.execute('ALTER TABLE classes ADD COLUMN declared TEXT')
            except sqlite3.OperationalError as error:
                if 'duplicate column' not in str(error):
                    raise

    def size_class(self, repo, job, default='medium'):
        """The class this job runs in: the learned one, else `default`.

        A class learned from a declaration the repository has since changed is
        stale: the new `size` in `pandora.toml` is the new starting point, and
        learning begins again from it rather than from the old answer.
        """
        row = self.db.execute('SELECT size_class, declared FROM classes WHERE repo=? AND job=?',
                              (repo, job)).fetchone()
        if row is None:
            return default
        if row['declared'] is not None and default and row['declared'] != default:
            return default
        return row['size_class']

    def set_class(self, repo, job, size_class, declared=None):
        ceiling_for(size_class)
        self.db.execute('INSERT OR REPLACE INTO classes (repo, job, size_class, declared) '
                        'VALUES (?,?,?,?)', (repo, job, size_class, declared))

    def clean_since_oom(self, repo, job):
        """Clean peaks recorded after this job's last `oom`, newest first.

        What the learned class is decided from. An `oom` resets the class to
        the declared one, and the peaks from before it are what chose the class
        that was killed: learning from them again would choose it again.
        """
        last = self.db.execute("SELECT MAX(id) m FROM peaks WHERE repo=? AND job=? "
                               "AND outcome='oom'", (repo, job)).fetchone()['m'] or 0
        rows = self.db.execute(
            'SELECT peak_mib FROM peaks WHERE repo=? AND job=? AND outcome IN ("ok","failed") '
            'AND id>? ORDER BY id DESC LIMIT ?', (repo, job, last, HISTORY)).fetchall()
        return [row['peak_mib'] for row in rows]

    def peaks(self, repo, job):
        rows = self.db.execute(
            'SELECT peak_mib FROM peaks WHERE repo=? AND job=? AND outcome IN ("ok","failed") '
            'ORDER BY id DESC LIMIT ?', (repo, job, HISTORY)).fetchall()
        return [row['peak_mib'] for row in rows]

    def close(self):
        self.db.close()

    def record(self, repo, job, peak_mib, outcome, at=None):
        """A run's observed peak.

        `oom` peaks are stored for the record but never feed a reservation: a
        run killed at its ceiling only tells you it wanted more than the
        ceiling, which is an operator decision, not a learned one.
        """
        if outcome not in ('ok', 'failed', 'oom', 'timeout', 'lost'):
            raise AdmissionError('unknown outcome ' + repr(outcome))
        if not isinstance(peak_mib, int) or isinstance(peak_mib, bool) or peak_mib < 0:
            raise AdmissionError('peak must be a non-negative integer of MiB')
        self.db.execute('INSERT INTO peaks(repo,job,peak_mib,outcome,at) VALUES (?,?,?,?,?)',
                        (repo, job, peak_mib, outcome, at if at is not None else time.time()))


class Admission:
    """Admit while the sum of held reservations fits the host budget."""

    def __init__(self, *, budget_mib, store=None, max_running=8, margin=MARGIN, floor=FLOOR_MIB):
        if not isinstance(budget_mib, int) or budget_mib < FLOOR_MIB:
            raise AdmissionError('host budget must be an integer of at least %d MiB' % FLOOR_MIB)
        self.budget_mib, self.max_running = budget_mib, max_running
        self.margin, self.floor = margin, floor
        self.store = store if store is not None else Store()
        self.running = {}     # run_id -> {'repo','job','reservation','ceiling'}

    def reservation(self, repo, job):
        size_class = self.store.size_class(repo, job)
        return (reserve(self.store.peaks(repo, job), size_class=size_class,
                        margin=self.margin, floor=self.floor),
                ceiling_for(size_class), size_class)

    def held(self):
        return sum(item['reservation'] for item in self.running.values())

    def admit(self, run_id, repo, job):
        """Return the limits for an admitted run, or a refusal with a reason."""
        if run_id in self.running:
            raise AdmissionError('run %r is already admitted' % run_id)
        reservation, ceiling, size_class = self.reservation(repo, job)
        cold = len(self.store.peaks(repo, job)) < MIN_SAMPLES
        if len(self.running) >= self.max_running:
            return {'admitted': False, 'reason': 'slots', 'reservation_mib': reservation}
        if self.held() + reservation > self.budget_mib:
            return {'admitted': False, 'reason': 'memory',
                    'reservation_mib': reservation, 'held_mib': self.held(),
                    'budget_mib': self.budget_mib}
        self.running[run_id] = {'repo': repo, 'job': job, 'reservation': reservation,
                                'ceiling': ceiling}
        return {'admitted': True, 'run_id': run_id, 'reservation_mib': reservation,
                'ceiling_mib': ceiling, 'size_class': size_class, 'cold_start': cold,
                'held_mib': self.held(), 'budget_mib': self.budget_mib}

    def finish(self, run_id, peak_mib, outcome):
        """Release the reservation and learn from what the run actually used."""
        held = self.running.pop(run_id, None)
        if held is None:
            raise AdmissionError('run %r was not admitted' % run_id)
        self.store.record(held['repo'], held['job'], peak_mib, outcome)
        over_reservation = peak_mib > held['reservation']
        over_ceiling = peak_mib >= held['ceiling'] or outcome == 'oom'
        return {'run_id': run_id, 'peak_mib': peak_mib, 'outcome': outcome,
                'reservation_mib': held['reservation'], 'ceiling_mib': held['ceiling'],
                # Under the ceiling, outgrowing the reservation is not an error:
                # the cgroup never refused anything and the next run reserves more.
                'over_reservation': over_reservation and not over_ceiling,
                'over_ceiling': over_ceiling,
                'next_reservation_mib': self.reservation(held['repo'], held['job'])[0]}

    def snapshot(self):
        return {'budget_mib': self.budget_mib, 'held_mib': self.held(),
                'running': dict(sorted(self.running.items()))}


if __name__ == '__main__':
    import sys
    admission = Admission(budget_mib=int(sys.argv[1]) if len(sys.argv) > 1 else 12288)
    print(json.dumps(admission.snapshot(), sort_keys=True))
