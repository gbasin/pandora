import fcntl
import json
import multiprocessing
from pathlib import Path
import queue
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import subprocess
from admission import acquire, database, enqueue, record_cleanup, QueueTimeout, QueueUnavailable


def worker(root, identity, events, release, timeout=5):
    attempt = Path(root) / 'runs' / identity
    attempt.mkdir(parents=True)
    with (attempt / 'attempt.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        lease = None
        def report(message, **kwargs):
            if 'admitted to FIFO' in message:
                events.put(('queued', identity))
        try:
            lease = acquire(attempt, timeout, poll=.01, report=report)
            events.put(('running', identity, lease.ticket))
            release.wait(5)
            status = 0
        except QueueTimeout:
            status = 75
        except KeyboardInterrupt:
            status = 130
        (attempt / 'terminal.json.tmp').write_text(json.dumps({'attempt': identity, 'cleanup_verified': True, 'exit_code': status}))
        (attempt / 'terminal.json.tmp').replace(attempt / 'terminal.json')
        if lease:
            lease.close()
        events.put(('terminal', identity, status))


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ctx = multiprocessing.get_context('spawn')
        self.events = self.ctx.Queue()
        self.children = []
        self.blocker = (self.root / 'worker.lock').open('a')
        fcntl.flock(self.blocker, fcntl.LOCK_EX)

    def tearDown(self):
        self.blocker.close()
        for child, release in self.children:
            if child.is_alive():
                release.set()
            child.join(2)
            if child.is_alive():
                child.kill()
                child.join()
        self.events.close()
        self.temp.cleanup()

    def start(self, char, timeout=5):
        identity = char * 32
        release = self.ctx.Event()
        child = self.ctx.Process(target=worker, args=(str(self.root), identity, self.events, release, timeout))
        child.start()
        self.children.append((child, release))
        self.assertEqual(self.events.get(timeout=3), ('queued', identity))
        return child, release

    def terminal(self, identity, status):
        self.assertEqual(self.events.get(timeout=3), ('terminal', identity, status))

    def running(self, identity):
        event = self.events.get(timeout=3)
        self.assertEqual(event[:2], ('running', identity))
        return event[2]

    def test_fifo_with_late_arrival_and_monotonic_tickets(self):
        _, a = self.start('a')
        _, b = self.start('b')
        self.blocker.close()
        first = self.running('a' * 32)
        _, c = self.start('c')
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=.1)
        a.set(); self.terminal('a' * 32, 0)
        second = self.running('b' * 32)
        b.set(); self.terminal('b' * 32, 0)
        third = self.running('c' * 32)
        c.set(); self.terminal('c' * 32, 0)
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_dead_waiter_does_not_block_followers(self):
        child, _ = self.start('a')
        _, b = self.start('b')
        child.kill(); child.join()
        self.blocker.close()
        self.running('b' * 32)
        b.set(); self.terminal('b' * 32, 0)
        self.assertFalse((self.root / 'runs' / ('a' * 32) / 'terminal.json').exists())

    def test_dead_execution_blocks_until_cleanup_receipt(self):
        child, _ = self.start('a')
        self.blocker.close()
        self.running('a' * 32)
        _, b = self.start('b')
        child.kill(); child.join()
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=.15)
        attempt = self.root / 'runs' / ('a' * 32)
        (attempt / 'admission-cleanup.json').write_text(json.dumps({'attempt': attempt.name, 'cleanup_verified': True}))
        self.running('b' * 32)
        b.set(); self.terminal('b' * 32, 0)
        self.assertFalse((attempt / 'terminal.json').exists())

    def test_cancel_head_and_nonhead_leave_other_jobs_intact(self):
        _, a = self.start('a')
        _, b = self.start('b')
        _, c = self.start('c')
        (self.root / 'runs' / ('b' * 32) / 'cancel.request').touch()
        self.terminal('b' * 32, 130)
        (self.root / 'runs' / ('a' * 32) / 'cancel.request').touch()
        self.terminal('a' * 32, 130)
        self.blocker.close()
        self.running('c' * 32)
        c.set(); self.terminal('c' * 32, 0)

    def test_timeout_does_not_execute_and_releases_its_place(self):
        self.start('a', timeout=.2)
        self.terminal('a' * 32, 75)
        _, b = self.start('b')
        self.blocker.close()
        self.running('b' * 32)
        b.set(); self.terminal('b' * 32, 0)

    def test_duplicate_attempt_cannot_get_a_new_ticket(self):
        self.start('a')
        with self.assertRaises(QueueUnavailable):
            enqueue(self.root / 'runs' / ('a' * 32))
        with database(self.root) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM requests').fetchone()[0], 1)

    def test_corrupt_queue_fails_closed(self):
        (self.root / 'admission.sqlite3').write_bytes(b'not a database')
        with self.assertRaisesRegex(QueueUnavailable, 'no tests started'):
            with database(self.root):
                self.fail('Corrupt queue was accepted')

    def test_cleanup_receipt_refuses_pending_or_running_resources(self):
        attempt = self.root / 'runs' / ('a' * 32)
        attempt.mkdir(parents=True)
        pending = attempt / 'dependency-cleanup.pending'
        pending.touch()
        with patch('admission.subprocess.run') as run:
            self.assertFalse(record_cleanup(attempt, True))
            run.assert_not_called()
        pending.unlink()
        with patch('admission.subprocess.run', return_value=subprocess.CompletedProcess([], 0, 'owned\n', '')):
            self.assertFalse(record_cleanup(attempt, True))
        self.assertFalse((attempt / 'admission-cleanup.json').exists())
        with patch('admission.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertTrue(record_cleanup(attempt, True))
        self.assertFalse((attempt / 'terminal.json').exists())


if __name__ == '__main__':
    unittest.main()
