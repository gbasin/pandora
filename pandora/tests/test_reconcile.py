"""A stopping daemon closes the local runs it drives, and a closed row stays closed.

The restart sweep (`resume_interrupted`) settles what a dead daemon left. This
is the other half: the daemon that is about to die closes its own local runs
first, so the caller hears exit 70 now rather than after the successor starts,
and nothing it closes can be reopened by a thread that was still on its way.
"""
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest

from pandora.client import daemon as daemon_module
from pandora.client.local import Supervisor
from pandora.client.protocol import Reader, VERSION, dump
from pandora.exits import CANCELED, INFRA
from pandora.tests.test_fallback import DaemonCase

SLOW = '''
[[jobs]]
id = "slow"
size = "small"
args = "none"
where = "local"
forms = [{ prefix = ["slow"] }]
run = { argv = ["sh", "-c", "echo started; sleep 30"] }
'''


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class AStoppingDaemon(DaemonCase):
    def setUp(self):
        super().setUp()
        with (self.repo / 'pandora.toml').open('a') as handle:
            handle.write(SLOW)

    def meta(self, run_id):
        return json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())

    def rows(self, job):
        return [json.loads(meta.read_text()) for meta in (self.state / 'runs').glob('*/meta.json')
                if json.loads(meta.read_text()).get('job') == job]

    def start_client(self, argv):
        answer = {}

        def client():
            answer['value'] = self.call(argv, timeout=30)

        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        return thread, answer

    def test_a_stop_kills_the_run_in_flight_and_the_caller_hears_exit_70(self):
        thread, answer = self.start_client(['pnpm', 'slow'])
        self.assertTrue(wait_until(lambda: any(row.get('pgid') for row in self.rows('slow')), 15),
                        'the slow job never spawned')
        row = next(row for row in self.rows('slow') if row.get('pgid'))
        pid = row['pgid']
        self.assertTrue(alive(pid))
        self.daemon.stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), 'the caller is still waiting after the stop')
        self.assertEqual(answer['value'].exit, INFRA)
        self.assertIn(b'stopped while this run was executing', answer['value'].err)
        closed = self.meta(row['id'])
        self.assertEqual((closed['state'], closed['exit_code']), ('infra_failed', INFRA))
        self.assertTrue(wait_until(lambda: not alive(pid), 5), 'the child outlived the daemon')
        # One exit frame, the daemon's, whatever the executor said afterwards.
        frames = [json.loads(line) for line in
                  (self.state / 'runs' / row['id'] / 'log').read_bytes().splitlines() if line]
        self.assertEqual([f['code'] for f in frames if f.get('t') == 'exit'], [INFRA])

    def test_a_stop_while_queued_withdraws_the_run_and_nothing_starts(self):
        self.daemon.budget.gate.closed = lambda: 'test pressure'   # the lane is paused
        thread, answer = self.start_client(['pnpm', 'slow'])
        self.assertTrue(wait_until(lambda: any(row['state'] == 'queued'
                                               for row in self.rows('slow')), 15))
        self.daemon.stop()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(answer['value'].error['code'], 'daemon-stopping')
        self.assertEqual(answer['value'].exit, INFRA)
        time.sleep(0.5)
        row = self.rows('slow')[0]
        self.assertEqual((row['state'], row['exit_code'], row.get('pgid')),
                         ('withdrawn', INFRA, None))

    def test_a_request_that_arrives_while_stopping_is_refused_not_run(self):
        self.daemon.stopping.set()
        answer = self.call(['pnpm', 'slow'])
        self.assertEqual(answer.error['code'], 'daemon-stopping')
        self.assertEqual(answer.exit, INFRA)
        self.assertEqual(self.rows('slow'), [])

    def test_a_remote_run_is_left_for_the_successor(self):
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0)
        self.assertEqual(self.daemon.close_local_runs(), [])


class TheSupervisor(unittest.TestCase):
    def test_a_cancel_that_lands_before_the_spawn_starts_nothing(self):
        started = []
        supervisor = Supervisor(['sh', '-c', 'echo started; sleep 30'], cwd='.',
                                env=dict(os.environ), timeout_seconds=30,
                                on_log=lambda *_: None, on_start=started.append,
                                spawn_lock=threading.Lock())
        self.assertEqual(supervisor.run(lambda: True), ('cancelled', None))
        self.assertEqual(started, [])
        self.assertIsNone(supervisor.proc)


class AClosedRow(unittest.TestCase):
    """Once closed, a row keeps the daemon's last word whatever a late thread says."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.run = daemon_module.Run(self.home.name, 'r0000000000x',
                                     {'argv': ['pnpm', 'unit'], 'cwd': self.home.name})

    def meta(self):
        return json.loads(self.run.meta.read_text())

    def test_a_late_save_does_not_reopen_it(self):
        self.run.finish(INFRA, state='withdrawn')
        self.run.state = 'running'
        self.run.remote = 'w-9'
        self.run.save()
        self.assertEqual((self.meta()['state'], self.meta()['exit_code']), ('withdrawn', INFRA))

    def test_a_second_finish_is_not_heard(self):
        self.run.finish(INFRA, state='infra_failed')
        self.run.finish(0, state='done')
        self.assertEqual((self.meta()['state'], self.meta()['exit_code']), ('infra_failed', INFRA))

    def test_two_finishes_at_once_write_one_exit_frame(self):
        gate = threading.Barrier(2)

        def close(code, state):
            gate.wait()
            self.run.finish(code, state=state)

        threads = [threading.Thread(target=close, args=(INFRA, 'infra_failed')),
                   threading.Thread(target=close, args=(0, 'done'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        frames = [json.loads(line) for line in self.run.log.read_bytes().splitlines() if line]
        exits = [frame for frame in frames if frame.get('t') == 'exit']
        self.assertEqual(len(exits), 1, exits)
        self.assertEqual(exits[0]['code'], self.meta()['exit_code'])


if __name__ == '__main__':
    unittest.main()
