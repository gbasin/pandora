"""Durable FIFO tickets for one worker; a dead execution remains a cleanup barrier."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time


class QueueTimeout(TimeoutError):
    pass


class QueueUnavailable(RuntimeError):
    pass


@contextmanager
def database(root):
    db = sqlite3.connect(root / 'admission.sqlite3', timeout=5, isolation_level=None)
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS requests (
            ticket INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt TEXT NOT NULL UNIQUE,
            phase TEXT NOT NULL CHECK (phase IN ('waiting', 'running'))
        )''')
        yield db
        db.execute('COMMIT')
    except sqlite3.Error as error:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise QueueUnavailable(f'FIFO admission store unavailable ({error}); no tests started. '
                               'Queue state retained; operator inspection required.') from error
    except BaseException:
        if db.in_transaction:
            db.execute('ROLLBACK')
        raise
    finally:
        db.close()


def alive(attempt):
    """The registered worker owns this lock until exit; no PID/reboot guessing."""
    try:
        handle = (attempt / 'attempt.lock').open('r+')
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def receipt(path, identity):
    try:
        data = json.loads(path.read_text())
        return data.get('attempt') == identity and data.get('cleanup_verified') is True
    except FileNotFoundError:
        return False
    # Corruption is not proof that cleanup finished. Fail closed.


def prune(db, root):
    for ticket, identity, phase in db.execute('SELECT ticket, attempt, phase FROM requests').fetchall():
        attempt = root / 'runs' / identity
        if receipt(attempt / 'terminal.json', identity):
            db.execute('DELETE FROM requests WHERE ticket = ?', (ticket,))
        elif not alive(attempt):
            if phase == 'waiting' or receipt(attempt / 'admission-cleanup.json', identity):
                db.execute('DELETE FROM requests WHERE ticket = ?', (ticket,))


def enqueue(attempt):
    if not re.fullmatch('[0-9a-f]{32}', attempt.name):
        raise ValueError('Invalid attempt identity')
    if not alive(attempt):
        raise RuntimeError('Admission requires the held attempt lock')
    with database(attempt.parent.parent) as db:
        # A duplicate worker must never gain another ticket or execute twice.
        cursor = db.execute("INSERT INTO requests (attempt, phase) VALUES (?, 'waiting')", (attempt.name,))
        return cursor.lastrowid


class Lease:
    def __init__(self, handle, ticket, waited):
        self.handle = handle
        self.ticket = ticket
        self.waited = waited

    def close(self):
        self.handle.close()


def acquire(attempt, timeout=900, *, poll=1, report=print):
    attempt = Path(attempt)
    root = attempt.parent.parent
    started = time.monotonic()
    ticket = enqueue(attempt)
    report(f'[pandora] admitted to FIFO queue; ticket {ticket}; queue limit {timeout}s', flush=True)
    last_message = float('-inf')
    last_ahead = None
    while True:
        if (attempt / 'cancel.request').exists():
            raise KeyboardInterrupt
        waited = time.monotonic() - started
        if waited >= timeout:
            raise QueueTimeout(f'Queue deadline reached after {timeout}s; no tests started')
        handle = None
        try:
            with database(root) as db:
                prune(db, root)
                row = db.execute('SELECT ticket, phase FROM requests WHERE attempt = ?', (attempt.name,)).fetchone()
                if row != (ticket, 'waiting'):
                    raise RuntimeError('FIFO request is missing or already executing; no replacement started')
                ahead = db.execute('SELECT COUNT(*) FROM requests WHERE ticket < ?', (ticket,)).fetchone()[0]
                head = db.execute('SELECT attempt, phase FROM requests ORDER BY ticket LIMIT 1').fetchone()
                blocked = head[1] == 'running' and not alive(root / 'runs' / head[0])
                if ahead == 0:
                    handle = (root / 'worker.lock').open('a')
                    try:
                        # Never block on the resource lock inside the database transaction.
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        handle.close()
                        handle = None
                    if handle is not None:
                        if (attempt / 'cancel.request').exists():
                            raise KeyboardInterrupt
                        if time.monotonic() - started >= timeout:
                            raise QueueTimeout(f'Queue deadline reached after {timeout}s; no tests started')
                        db.execute("UPDATE requests SET phase = 'running' WHERE ticket = ?", (ticket,))
            if handle is not None:
                # COMMIT has succeeded. No resource side effect can precede this point.
                if (attempt / 'cancel.request').exists():
                    raise KeyboardInterrupt
                return Lease(handle, ticket, time.monotonic() - started)
        except BaseException:
            if handle is not None:
                handle.close()
            raise
        if ahead != last_ahead or time.monotonic() - last_message >= 10:
            suffix = f'; cleanup unresolved for {head[0]}; operator reconciliation required' if blocked else ''
            report(f'[pandora] queued; worker occupied; ticket {ticket}; {ahead} request(s) ahead; '
                   f'waited {waited:.0f}s of {timeout}s; no local validation started{suffix}', flush=True)
            last_message, last_ahead = time.monotonic(), ahead
        time.sleep(poll)


def record_cleanup(attempt, verified):
    """ExecStopPost can unblock a dead execution, but never invent a test result."""
    attempt = Path(attempt)
    if not verified or any((attempt / name).exists() for name in (
            'service-cleanup.pending', 'docker-cleanup.pending', 'dependency-cleanup.pending')):
        return False
    check = subprocess.run(['sudo', 'docker', 'ps', '--filter',
                            'name=^/pandora-warm-' + attempt.name + '$', '--format', '{{.Names}}'],
                           capture_output=True, text=True, timeout=30)
    if check.returncode or check.stdout.strip():
        return False
    temporary = attempt / 'admission-cleanup.json.tmp'
    temporary.write_text(json.dumps({'attempt': attempt.name, 'cleanup_verified': True}) + '\n')
    temporary.replace(attempt / 'admission-cleanup.json')
    return True
