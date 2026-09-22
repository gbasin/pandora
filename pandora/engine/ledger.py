"""The worker's memory: which requests exist, which attempts ran, how they ended.

SQLite, one file, `isolation_level=None` with explicit transactions, because the
engine is several short-lived processes (one per SSH call) plus one detached
supervisor per run, and they must not disagree about whether a run is running.

Two identities, deliberately distinct:

  request_id   the client's idea of "this invocation". Submitting the same
               request_id twice returns the first attempt instead of starting a
               second, which is what makes submission safe to retry over a
               connection that may have dropped after the worker read it.
  run_id       one attempt at that request. A request has exactly one attempt in
               the slice; the column exists so a retry policy can be added
               without a migration.

States are a line, not a graph: queued -> admitted -> running -> collecting ->
finished. Only `finished` carries an outcome, and the outcome vocabulary is
closed, because "some other string" is how a system ends up reporting a pass it
did not observe.
"""
import json
import sqlite3
import time

from ..errors import StaleRun

STATES = ('queued', 'admitted', 'running', 'collecting', 'finished')
OUTCOMES = ('passed', 'command_failed', 'oom', 'timed_out', 'cancelled', 'infra_failed')
LIVE = ('queued', 'admitted', 'running', 'collecting')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS attempts (
  run_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  repo TEXT NOT NULL,
  job TEXT NOT NULL,
  input_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  argv TEXT NOT NULL,
  env TEXT NOT NULL,
  cwd TEXT NOT NULL,
  outputs TEXT NOT NULL,
  size_class TEXT NOT NULL,
  state TEXT NOT NULL,
  outcome TEXT,
  exit_code INTEGER,
  instance TEXT,
  supervisor_pid INTEGER,
  reservation_mib INTEGER,
  ceiling_mib INTEGER,
  cpus_hint INTEGER,
  peak_mib INTEGER,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  role TEXT NOT NULL DEFAULT 'single',
  parent TEXT,
  shard_index INTEGER,
  shard_total INTEGER,
  same_input_as TEXT,
  durations TEXT NOT NULL DEFAULT '{}',
  evidence TEXT NOT NULL DEFAULT '{}',
  receipt TEXT,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  finished REAL
);
CREATE INDEX IF NOT EXISTS attempts_state ON attempts(state);
CREATE INDEX IF NOT EXISTS attempts_input ON attempts(repo, job, input_id);
'''
# The parent index is created by `migrate`, not here: on a ledger that predates
# sharding the table already exists, `CREATE TABLE IF NOT EXISTS` does nothing,
# and an index over a column that is one statement away from existing fails.

# `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
# worker whose ledger predates sharding needs the columns added by hand. Every
# one of them is nullable or defaulted, so an old row reads as what it was: a
# single, unsharded attempt.
ADDED = (('role', "TEXT NOT NULL DEFAULT 'single'"),
         ('parent', 'TEXT'),
         ('shard_index', 'INTEGER'),
         ('shard_total', 'INTEGER'))

# A parent holds the fan-out and runs nothing itself; `plan` is the build-once
# attempt a tier-2 parent runs before there are any shards to dispatch.
ROLES = ('single', 'parent', 'plan', 'shard')


def now():
    return time.time()


class Ledger:
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript(SCHEMA)
        self.migrate()

    def migrate(self):
        have = {row['name'] for row in self.db.execute('PRAGMA table_info(attempts)')}
        for name, declaration in ADDED:
            if name not in have:
                self.db.execute('ALTER TABLE attempts ADD COLUMN %s %s' % (name, declaration))
        self.db.execute('CREATE INDEX IF NOT EXISTS attempts_parent ON attempts(parent)')

    def close(self):
        self.db.close()

    # -- writing -----------------------------------------------------------

    def claim(self, request_id, run_id, *, repo, job, input_id, source_path, argv,
              env, cwd, outputs, size_class, role='single', parent=None,
              shard_index=None, shard_total=None):
        """Insert a queued attempt, or return the existing one for this request.

        Returns (row, created). `created` false means the caller is a duplicate
        submission and must attach rather than start anything.
        """
        if role not in ROLES:
            raise StaleRun('unknown role %r' % role)
        existing = self.by_request(request_id)
        if existing is not None:
            return existing, False
        # A shard is not "the same input as" its siblings: they share a source
        # tree and run different thirds of it, so linking them would make a
        # cache-hit claim Pandora has not earned. Only whole attempts compare.
        previous = None if role == 'shard' else self.db.execute(
            "SELECT run_id FROM attempts WHERE repo=? AND job=? AND input_id=? "
            "AND run_id<>? AND role IN ('single','parent') ORDER BY created DESC LIMIT 1",
            (repo, job, input_id, run_id)).fetchone()
        stamp = now()
        try:
            self.db.execute(
                'INSERT INTO attempts (run_id, request_id, repo, job, input_id, source_path,'
                ' argv, env, cwd, outputs, size_class, state, same_input_as, created, updated,'
                ' role, parent, shard_index, shard_total)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (run_id, request_id, repo, job, input_id, source_path,
                 json.dumps(argv), json.dumps(env), cwd, json.dumps(outputs),
                 size_class, 'queued', previous['run_id'] if previous else None, stamp, stamp,
                 role, parent, shard_index, shard_total))
        except sqlite3.IntegrityError:
            row = self.by_request(request_id)
            if row is None:
                raise
            return row, False
        return self.get(run_id), True

    def update(self, run_id, **fields):
        if not fields:
            return self.get(run_id)
        if 'state' in fields and fields['state'] not in STATES:
            raise StaleRun('unknown state %r' % fields['state'])
        if fields.get('outcome') is not None and fields['outcome'] not in OUTCOMES:
            raise StaleRun('unknown outcome %r' % fields['outcome'])
        for key in ('argv', 'env', 'outputs', 'durations', 'evidence', 'receipt'):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key])
        fields['updated'] = now()
        assignments = ', '.join('%s=?' % key for key in fields)
        cursor = self.db.execute('UPDATE attempts SET %s WHERE run_id=?' % assignments,
                                 (*fields.values(), run_id))
        if cursor.rowcount == 0:
            raise StaleRun('no attempt %s in the ledger' % run_id)
        return self.get(run_id)

    def finish(self, run_id, *, outcome, exit_code, peak_mib=None, durations=None,
               evidence=None, receipt=None):
        if outcome not in OUTCOMES:
            raise StaleRun('unknown outcome %r' % outcome)
        return self.update(run_id, state='finished', outcome=outcome, exit_code=exit_code,
                           peak_mib=peak_mib, finished=now(),
                           durations=durations if durations is not None else {},
                           evidence=evidence if evidence is not None else {},
                           receipt=receipt)

    def request_cancel(self, run_id):
        row = self.get(run_id)
        if row is None:
            raise StaleRun('no attempt %s in the ledger' % run_id)
        if row['state'] == 'finished':
            return row
        return self.update(run_id, cancel_requested=1)

    # -- reading -----------------------------------------------------------

    def get(self, run_id):
        return self.db.execute('SELECT * FROM attempts WHERE run_id=?', (run_id,)).fetchone()

    def by_request(self, request_id):
        return self.db.execute('SELECT * FROM attempts WHERE request_id=?',
                               (request_id,)).fetchone()

    def live(self):
        marks = ','.join('?' * len(LIVE))
        return self.db.execute('SELECT * FROM attempts WHERE state IN (%s) ORDER BY created'
                               % marks, LIVE).fetchall()

    def children(self, parent):
        return self.db.execute('SELECT * FROM attempts WHERE parent=? ORDER BY shard_index',
                               (parent,)).fetchall()

    def recent(self, limit=25):
        return self.db.execute('SELECT * FROM attempts ORDER BY created DESC LIMIT ?',
                               (limit,)).fetchall()

    def counts(self):
        rows = self.db.execute('SELECT state, outcome, COUNT(*) n FROM attempts '
                               'GROUP BY state, outcome').fetchall()
        return [{'state': row['state'], 'outcome': row['outcome'], 'count': row['n']}
                for row in rows]


def row_to_dict(row):
    if row is None:
        return None
    item = dict(row)
    for key in ('argv', 'env', 'outputs', 'durations', 'evidence', 'receipt'):
        if isinstance(item.get(key), str):
            try:
                item[key] = json.loads(item[key])
            except ValueError:
                pass
    return item
