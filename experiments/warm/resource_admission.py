"""Experimental multi-slot admission; not wired into the validation worker yet.

The ledger reserves declared CPU/RAM. Callers must enforce the same ceilings on
all execution resources and retain attempt locks through terminal publication.
"""
from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import re
import sqlite3
import time

from admission import alive, receipt
from scheduling_policy import validate_config, validate_demand, choose


class SchedulerUnavailable(RuntimeError):
    pass


class InvocationStopped(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__('Invocation stopped: ' + reason)


def identity(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('Expected an immutable 32-hex identity')
    return value


class Lease:
    def __init__(self, handle, ticket, queue_seconds):
        self.handle, self.ticket, self.queue_seconds = handle, ticket, queue_seconds

    def close(self):
        # Dropping a process lock is not evidence that its containers stopped.
        # The ledger reservation lasts until an explicit cleanup receipt exists.
        self.handle.close()


class Scheduler:
    def __init__(self, root, config, *, boot_id, clock=time.monotonic):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = validate_config(config)
        if not isinstance(boot_id, str) or not boot_id:
            raise ValueError('A worker boot identity is required')
        self.boot_id, self.clock = boot_id, clock

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.root / 'resources.sqlite3', timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS invocations (
                identity TEXT PRIMARY KEY, max_parallel INTEGER NOT NULL,
                budget REAL NOT NULL, waited REAL NOT NULL DEFAULT 0,
                turn INTEGER NOT NULL DEFAULT 0, stopped TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS requests (
                ticket INTEGER PRIMARY KEY AUTOINCREMENT, attempt TEXT UNIQUE NOT NULL,
                invocation TEXT NOT NULL, cpu_millis INTEGER NOT NULL, memory_mib INTEGER NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN ('waiting','running','finished','cancelled')))''')
            expected = {'config': json.dumps(self.config, sort_keys=True), 'boot_id': self.boot_id}
            for key, value in expected.items():
                row = db.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
                if row is None:
                    db.execute('INSERT INTO metadata VALUES (?,?)', (key, value))
                elif row['value'] != value:
                    raise SchedulerUnavailable('Scheduler configuration or worker boot changed; drain and reconcile existing work before migration')
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                raise SchedulerUnavailable('Invalid scheduler clock')
            previous = db.execute("SELECT value FROM metadata WHERE key='tick'").fetchone()
            try:
                before = now if previous is None else float(previous['value'])
            except (ValueError, TypeError) as error:
                raise SchedulerUnavailable('Invalid persisted scheduler clock') from error
            if not math.isfinite(before) or before < 0:
                raise SchedulerUnavailable('Invalid persisted scheduler clock')
            elapsed = now - before
            if not math.isfinite(elapsed) or elapsed < 0:
                raise SchedulerUnavailable('Scheduler clock moved backwards; existing reservations retained')
            # One elapsed interval per invocation, independent of shard count.
            db.execute('''UPDATE invocations SET waited=waited+? WHERE stopped IS NULL
                AND EXISTS (SELECT 1 FROM requests WHERE invocation=identity AND phase='waiting')
                AND NOT EXISTS (SELECT 1 FROM requests WHERE invocation=identity AND phase='running')''', (elapsed,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('tick',?)", (str(now),))
            db.execute("UPDATE invocations SET stopped='queue-timeout' WHERE stopped IS NULL AND waited>=budget")
            db.execute("UPDATE requests SET phase='cancelled' WHERE phase='waiting' AND invocation IN (SELECT identity FROM invocations WHERE stopped IS NOT NULL)")
            self.reconcile(db)
            yield db
            db.execute('COMMIT')
        except InvocationStopped:
            db.execute('COMMIT')
            raise
        except BaseException as error:
            if db.in_transaction:
                db.execute('ROLLBACK')
            if isinstance(error, sqlite3.Error):
                raise SchedulerUnavailable('Resource admission ledger unavailable; no new execution is authorized') from error
            raise
        finally:
            db.close()

    def reconcile(self, db):
        for row in db.execute("SELECT * FROM requests WHERE phase IN ('waiting','running')").fetchall():
            path = self.root / 'runs' / row['attempt']
            terminal = receipt(path / 'terminal.json', row['attempt'])
            if terminal:
                db.execute("UPDATE requests SET phase='finished' WHERE attempt=?", (row['attempt'],))
            elif not alive(path):
                if row['phase'] == 'waiting':
                    db.execute("UPDATE requests SET phase='cancelled' WHERE attempt=?", (row['attempt'],))
                elif receipt(path / 'admission-cleanup.json', row['attempt']):
                    db.execute("UPDATE requests SET phase='finished' WHERE attempt=?", (row['attempt'],))
                # A dead running owner without cleanup stays a global barrier.

    def register(self, invocation, *, queue_budget=900, max_parallel=1):
        identity(invocation)
        if type(max_parallel) is not int or not 1 <= max_parallel <= self.config['max_running']:
            raise ValueError('Invocation parallelism must fit the worker slot limit')
        if type(queue_budget) not in (int, float) or not math.isfinite(queue_budget) or not 0 < queue_budget <= 86400:
            raise ValueError('Invocation queue budget must be positive and at most 86400 seconds')
        with self.transaction() as db:
            row = db.execute('SELECT * FROM invocations WHERE identity=?', (invocation,)).fetchone()
            if row:
                if row['budget'] != queue_budget or row['max_parallel'] != max_parallel:
                    raise ValueError('Accepted invocation settings are immutable')
                return
            # New arrivals get a turn before the most recently served group,
            # but cannot forever jump older waiting groups by always using zero.
            turn = max(0, db.execute('SELECT COALESCE(MAX(turn),0)-1 FROM invocations').fetchone()[0])
            db.execute('INSERT INTO invocations(identity,max_parallel,budget,turn) VALUES (?,?,?,?)', (invocation,max_parallel,queue_budget,turn))

    def enqueue(self, attempt, invocation, demand):
        identity(attempt); identity(invocation)
        demand = validate_demand(demand, self.config)
        if not alive(self.root / 'runs' / attempt):
            raise ValueError('Admission requires a held attempt lock')
        with self.transaction() as db:
            group = db.execute('SELECT * FROM invocations WHERE identity=?', (invocation,)).fetchone()
            if group is None:
                raise ValueError('Register the invocation before its tasks')
            if group['stopped']:
                raise InvocationStopped(group['stopped'])
            if db.execute('SELECT 1 FROM requests WHERE attempt=?', (attempt,)).fetchone():
                raise ValueError('Attempt is already registered; no replacement ticket created')
            return db.execute('''INSERT INTO requests(attempt,invocation,cpu_millis,memory_mib,phase)
                VALUES (?,?,?,?,'waiting')''', (attempt,invocation,demand['cpu_millis'],demand['memory_mib'])).lastrowid

    def rows(self, db):
        requests = [dict(row) for row in db.execute("SELECT * FROM requests WHERE phase IN ('waiting','running') ORDER BY ticket")]
        groups = {row['identity']: {'max_parallel': row['max_parallel'], 'turn': row['turn'],
                                    'stopped': row['stopped'] is not None}
                  for row in db.execute('SELECT * FROM invocations')}
        return requests, groups

    def claim(self, attempt):
        identity(attempt)
        handle = None
        try:
            with self.transaction() as db:
                row = db.execute('SELECT * FROM requests WHERE attempt=?', (attempt,)).fetchone()
                if row is None:
                    raise ValueError('Attempt is not registered')
                group = db.execute('SELECT * FROM invocations WHERE identity=?', (row['invocation'],)).fetchone()
                if group['stopped']:
                    raise InvocationStopped(group['stopped'])
                if row['phase'] != 'waiting':
                    raise ValueError('Attempt already admitted or ended; never execute it twice')
                requests, groups = self.rows(db)
                blocked = [r for r in requests if r['phase'] == 'running' and not alive(self.root / 'runs' / r['attempt'])]
                if blocked or choose(self.config, requests, groups) != attempt:
                    return None
                # Shared leases coexist; the old exclusive worker blocks all of
                # them during migration or operator maintenance.
                handle = (self.root / 'worker.lock').open('a')
                try:
                    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    handle = None
                    return None
                if not alive(self.root / 'runs' / attempt):
                    raise ValueError('Attempt owner disappeared before admission')
                db.execute("UPDATE requests SET phase='running' WHERE attempt=?", (attempt,))
                turn = db.execute('SELECT COALESCE(MAX(turn),0)+1 FROM invocations').fetchone()[0]
                db.execute('UPDATE invocations SET turn=? WHERE identity=?', (turn,row['invocation']))
                lease = Lease(handle, row['ticket'], group['waited'])
            return lease  # COMMIT precedes any caller side effects.
        except BaseException:
            if handle is not None:
                handle.close()
            raise

    def stop(self, invocation, reason='cancelled'):
        identity(invocation)
        if reason not in ('cancelled', 'test-failure', 'infrastructure', 'deadline'):
            raise ValueError('Invalid invocation stopping reason')
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM invocations WHERE identity=?', (invocation,)).fetchone():
                raise ValueError('Unknown invocation')
            db.execute('UPDATE invocations SET stopped=COALESCE(stopped,?) WHERE identity=?', (reason,invocation))
            db.execute("UPDATE requests SET phase='cancelled' WHERE invocation=? AND phase='waiting'", (invocation,))
            # Running reservations remain charged; fail-fast does not kill them.

    def settle(self, attempt):
        """Call immediately after publishing cleanup evidence, before dropping ownership.

        The admitted interval ends at this ledger transition. Time is charged
        using the previous interval's state, never retroactively using the new
        state of a just-completed child. Polling waiters can also reconcile.
        """
        identity(attempt)
        with self.transaction() as db:
            row = db.execute('SELECT phase FROM requests WHERE attempt=?', (attempt,)).fetchone()
            if row is None or row['phase'] != 'finished':
                raise ValueError('Cannot release resources without verified cleanup evidence')

    def snapshot(self):
        with self.transaction() as db:
            return {'config': self.config,
                    'invocations': [dict(row) for row in db.execute('SELECT * FROM invocations ORDER BY identity')],
                    'requests': [dict(row) for row in db.execute('SELECT * FROM requests ORDER BY ticket')]}
