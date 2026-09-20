import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from resource_admission import Scheduler, SchedulerUnavailable, InvocationStopped, initialize_ledger

CONFIG = {'version': 1, 'cpu_millis': 1000, 'memory_mib': 1024, 'max_running': 2, 'policy': 'fair'}
DEMAND = {'cpu_millis': 500, 'memory_mib': 512}
A, B = 'a' * 32, 'b' * 32


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 0
        self.handles, self.leases = {}, []
        self.scheduler = self.instance()

    def instance(self, **overrides):
        return Scheduler(self.root, overrides.get('config', CONFIG), boot_id=overrides.get('boot_id', 'boot-1'), clock=lambda: self.now)

    def tearDown(self):
        for lease in self.leases:
            lease.close()
        for handle in self.handles.values():
            handle.close()
        self.temp.cleanup()

    def queue(self, index, group=A, demand=DEMAND):
        attempt = f'{index:032x}'
        path = self.root / 'runs' / attempt
        path.mkdir(parents=True)
        handle = (path / 'attempt.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.handles[attempt] = handle
        self.scheduler.enqueue(attempt, group, demand)
        return attempt

    def claim(self, attempt):
        lease = self.scheduler.claim(attempt)
        if lease:
            self.leases.append(lease)
        return lease

    def finish(self, attempt):
        path = self.root / 'runs' / attempt / 'terminal.json'
        path.write_text(json.dumps({'attempt': attempt, 'cleanup_verified': True}))
        self.handles[attempt].close()
        self.scheduler.settle(attempt)

    def group(self, group=A):
        return next(row for row in self.scheduler.snapshot()['invocations'] if row['identity'] == group)

    def test_budget_counts_stalled_wall_time_once_and_survives_reconnect(self):
        self.scheduler.register(A, queue_budget=10, max_parallel=2)
        first, second = self.queue(1), self.queue(2)
        self.now = 3
        self.assertEqual(self.claim(first).queue_seconds, 3)
        self.now = 20
        self.assertEqual(self.group()['waited'], 3)  # Waiting sibling is not double-charged.
        self.finish(first)
        self.now = 22
        self.instance().register(A, queue_budget=10, max_parallel=2)
        self.assertEqual(self.group()['waited'], 5)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.instance().register(A, queue_budget=99, max_parallel=2)
        self.now = 27
        with self.assertRaises(InvocationStopped) as stopped:
            self.claim(second)
        self.assertEqual(stopped.exception.reason, 'queue-timeout')
        self.assertEqual(self.instance().snapshot()['invocations'][0]['waited'], 10)
        self.assertEqual(self.group()['stopped'], 'queue-timeout')

    def test_two_slots_capacity_and_later_focused_invocation_gets_next_turn(self):
        self.scheduler.register(A, max_parallel=2)
        first, second, third = self.queue(1), self.queue(2), self.queue(3)
        self.assertIsNotNone(self.claim(first))
        self.assertIsNotNone(self.claim(second))
        self.assertIsNone(self.claim(third))
        self.scheduler.register(B)
        focused = self.queue(4, B)
        self.finish(first)
        self.assertIsNone(self.claim(third))
        self.assertIsNotNone(self.claim(focused))
        rows = self.scheduler.snapshot()['requests']
        running = [row for row in rows if row['phase'] == 'running']
        self.assertEqual(sum(row['cpu_millis'] for row in running), 1000)
        self.assertEqual(sum(row['memory_mib'] for row in running), 1024)
        self.assertEqual({row['attempt'] for row in running}, {second, focused})

    def test_another_invocations_running_shard_does_not_pause_my_budget(self):
        self.scheduler.register(A)
        first = self.queue(1, demand={'cpu_millis': 1000, 'memory_mib': 1024})
        self.claim(first)
        self.scheduler.register(B, queue_budget=3)
        waiting = self.queue(2, B)
        self.now = 4
        with self.assertRaises(InvocationStopped): self.claim(waiting)
        self.assertEqual(self.group(B)['stopped'], 'queue-timeout')
        self.assertIsNone(self.group(A)['stopped'])
        self.assertEqual(self.group(A)['waited'], 0)
        self.assertEqual(self.scheduler.snapshot()['requests'][0]['phase'], 'running')

    def test_dead_owner_retains_reservation_until_cleanup_without_inventing_result(self):
        self.scheduler.register(A, max_parallel=2)
        first, second = self.queue(1), self.queue(2)
        self.claim(first)
        self.leases[0].close()
        self.handles[first].close()
        self.assertIsNone(self.claim(second))  # Even spare capacity is blocked by unknown cleanup.
        path = self.root / 'runs' / first
        (path / 'admission-cleanup.json').write_text(json.dumps({'attempt': first, 'cleanup_verified': True}))
        self.assertIsNotNone(self.claim(second))
        self.assertFalse((path / 'terminal.json').exists())

    def test_failfast_withdraws_waiters_without_interrupting_running_siblings(self):
        self.scheduler.register(A, max_parallel=2)
        first, second, third = self.queue(1), self.queue(2), self.queue(3)
        self.claim(first); self.claim(second)
        self.scheduler.stop(A, 'test-failure')
        with self.assertRaises(InvocationStopped): self.claim(third)
        rows = self.scheduler.snapshot()['requests']
        self.assertEqual([row['phase'] for row in rows], ['running', 'running', 'cancelled'])
        self.assertTrue(all(not (self.root / 'runs' / task / 'cancel.request').exists() for task in (first, second)))

    def test_exclusive_legacy_gate_and_duplicate_attempt_do_not_authorize_work(self):
        self.scheduler.register(A)
        task = self.queue(1)
        with (self.root / 'worker.lock').open('a') as gate:
            fcntl.flock(gate, fcntl.LOCK_EX)
            self.assertIsNone(self.claim(task))
        self.assertIsNotNone(self.claim(task))
        with self.assertRaisesRegex(ValueError, 'twice'): self.claim(task)
        with self.assertRaisesRegex(ValueError, 'already registered'):
            self.scheduler.enqueue(task, A, DEMAND)
        self.assertEqual(len(self.scheduler.snapshot()['requests']), 1)

    def test_new_arrivals_cannot_keep_jumping_an_older_waiting_invocation(self):
        self.scheduler.register(A)
        full = {'cpu_millis': 1000, 'memory_mib': 1024}
        first, second = self.queue(1, demand=full), self.queue(2, demand=full)
        self.claim(first)
        self.scheduler.register(B)
        focused = self.queue(3, B, full)
        self.finish(first)
        self.assertIsNotNone(self.claim(focused))
        self.scheduler.register('c' * 32)
        newcomer = self.queue(4, 'c' * 32, full)
        self.finish(focused)
        self.assertIsNone(self.claim(newcomer))
        self.assertIsNotNone(self.claim(second))

    def test_invalid_persisted_clock_and_boolean_clock_do_not_admit(self):
        self.scheduler.register(A)
        task = self.queue(1)
        self.now = True
        with self.assertRaises(SchedulerUnavailable): self.claim(task)
        self.now = 0
        for value in ('NaN', 'Infinity', '-1', 'broken'):
            with sqlite3.connect(self.root / 'resources.sqlite3') as db:
                db.execute("UPDATE metadata SET value=? WHERE key='tick'", (value,))
            with self.assertRaises(SchedulerUnavailable): self.claim(task)

    def test_boot_config_and_database_faults_fail_closed(self):
        self.scheduler.register(A)
        for other in (self.instance(boot_id='boot-2'), self.instance(config=CONFIG | {'max_running': 1})):
            with self.assertRaises(SchedulerUnavailable): other.snapshot()
        self.now = -1
        with self.assertRaises(SchedulerUnavailable): self.scheduler.snapshot()
        self.now = 0
        (self.root / 'resources.sqlite3').write_bytes(b'broken')
        with self.assertRaises(SchedulerUnavailable): self.scheduler.snapshot()

    def test_old_schema_fails_closed_without_rewriting_existing_ledger(self):
        with sqlite3.connect(self.root / 'resources.sqlite3') as db:
            db.execute('CREATE TABLE requests (ticket INTEGER PRIMARY KEY, attempt TEXT UNIQUE NOT NULL, '
                       'invocation TEXT NOT NULL, cpu_millis INTEGER NOT NULL, memory_mib INTEGER NOT NULL, '
                       "phase TEXT NOT NULL CHECK(phase IN ('waiting','running','finished','cancelled')))")
            db.execute("INSERT INTO requests VALUES (1, ?, ?, 500, 512, 'running')", (A, A))

        with self.assertRaisesRegex(SchedulerUnavailable, 'Old scheduler schema'):
            self.scheduler.snapshot()
        with sqlite3.connect(self.root / 'resources.sqlite3') as db:
            self.assertEqual(db.execute('SELECT attempt, phase FROM requests').fetchone(), (A, 'running'))

    def test_durable_exclusive_and_disk_reservations_block_later_claims(self):
        settings = CONFIG | {'disk_mib': 1000, 'disk_floor_mib': 100}
        self.scheduler = self.instance(config=settings)
        self.scheduler.register(A, max_parallel=2)
        self.scheduler.register(B, max_parallel=2)
        self.scheduler.register('c' * 32, max_parallel=2)
        first = self.queue(1, A, {'cpu_millis': 500, 'memory_mib': 512,
                                  'disk_mib': 600, 'exclusive': ['dependency-builder']})
        same_builder = self.queue(2, B, {'cpu_millis': 500, 'memory_mib': 512,
                                         'disk_mib': 100, 'exclusive': ['dependency-builder']})
        disk_overflow = self.queue(3, 'c' * 32, {'cpu_millis': 500, 'memory_mib': 512,
                                           'disk_mib': 500})
        with patch('resource_admission.shutil.disk_usage', return_value=type('Usage', (), {'free': 10_000 * 1024 * 1024})()):
            self.assertIsNotNone(self.claim(first))
            self.assertIsNone(self.claim(same_builder))
            self.scheduler.stop(B)
            self.assertIsNone(self.claim(disk_overflow))

    def test_disk_floor_prevents_claim_even_when_declared_capacity_fits(self):
        settings = CONFIG | {'disk_mib': 1000, 'disk_floor_mib': 300}
        self.scheduler = self.instance(config=settings)
        self.scheduler.register(A, max_parallel=2)
        self.scheduler.register(B, max_parallel=2)
        first = self.queue(1, A, {'cpu_millis': 500, 'memory_mib': 512, 'disk_mib': 400})
        pending = self.queue(2, B, {'cpu_millis': 500, 'memory_mib': 512, 'disk_mib': 200})
        usage = type('Usage', (), {'free': 2_000 * 1024 * 1024})()
        with patch('resource_admission.shutil.disk_usage', return_value=usage):
            self.assertIsNotNone(self.claim(first))
            usage.free = 899 * 1024 * 1024
            self.assertIsNone(self.claim(pending))

    def test_generation_change_after_old_transaction_commit_cannot_return_a_lease(self):
        marker = self.root / 'ledger-generation'
        marker.write_text('a' * 64 + '\n')
        scheduler = self.instance()
        with self.assertRaisesRegex(SchedulerUnavailable, 'generation changed'):
            with scheduler.transaction():
                marker.write_text('b' * 64 + '\n')

    def test_scheduler_constructed_between_in_place_reset_and_marker_write_is_rejected(self):
        settings = CONFIG | {'disk_mib': 1000, 'disk_floor_mib': 100}
        self.scheduler = self.instance(config=settings)
        self.scheduler.register(A)
        with sqlite3.connect(self.root / 'resources.sqlite3') as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('requests', 'invocations', 'metadata'):
                db.execute('DROP TABLE IF EXISTS ' + table)
            initialize_ledger(db, settings, 'boot-1', 'c' * 64, 0)
            db.commit()
        constructed_in_gap = self.instance(config=settings)
        with self.assertRaisesRegex(SchedulerUnavailable, 'generation changed'):
            constructed_in_gap.snapshot()


if __name__ == '__main__':
    unittest.main()
