import fcntl
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from resource_admission import Scheduler, SchedulerUnavailable, InvocationStopped

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


if __name__ == '__main__':
    unittest.main()
