"""A drain cancels a blocker that is doing nothing, and never one that is progressing.

On 2026-09-24 a local run sat at 0% CPU for 20 minutes, its pnpm processes
idle, and held a 25-minute drain; every new command waited, then exited 75.
"""
import json
import subprocess
import tempfile
import time
import unittest
from types import SimpleNamespace

from pandora.client import drain, local
from pandora.client.daemon import Run
from pandora.exits import CANCELED
from pandora.tests.test_drain import DrainCase, wait_until

BUSY = '''
[[jobs]]
id = "busy"
size = "small"
args = "none"
where = "local"
forms = [{ prefix = ["busy"] }]
run = { argv = ["sh", "-c", "echo started; i=0; while [ $i -lt 100000000 ]; do i=$((i+1)); done"] }
'''


class TheCpuColumn(unittest.TestCase):
    def test_both_platforms_formats(self):
        self.assertEqual(local.cpu_seconds('185:16.36'), 185 * 60 + 16.36)     # macOS
        self.assertEqual(local.cpu_seconds('0:00.03'), 0.03)
        self.assertEqual(local.cpu_seconds('01:02:03'), 3723)                   # Linux
        self.assertEqual(local.cpu_seconds('2-01:00:00'), 2 * 86400 + 3600)
        self.assertIsNone(local.cpu_seconds('-'))
        self.assertIsNone(local.cpu_seconds(''))

    def test_one_ps_gives_the_table_and_the_cpu(self):
        asked = []

        def run(argv, **_kwargs):
            asked.append(argv)
            return subprocess.CompletedProcess(argv, 0, '  10 1 10 2048 1:02.50\n'
                                                        '  11 10 10 1024 0:00.01\n', '')
        cpu = {}
        table = local.process_table(run, cpu=cpu)
        self.assertEqual(asked, [['ps', '-Ao', 'pid=,ppid=,pgid=,rss=,time=']])
        self.assertEqual(table, [(10, 1, 10, 2048), (11, 10, 10, 1024)])
        self.assertEqual(cpu, {10: 62.5, 11: 0.01})

    def test_without_cpu_the_table_is_what_it_was(self):
        def run(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 0, '10 1 10 2048\n', '')
        self.assertEqual(local.process_table(run), [(10, 1, 10, 2048)])


class TheSampler(unittest.TestCase):
    """`Supervisor.track` with a fake clock and fake `ps` readings."""

    def setUp(self):
        self.clock = [1000.0]
        self.heard = []
        self.supervisor = local.Supervisor(
            ['true'], cwd='.', env={}, timeout_seconds=0, on_log=lambda *_: None,
            on_activity=lambda cpu, at: self.heard.append((cpu, at)),
            clock=lambda: self.clock[0])

    def sample(self, cpu, after=1.0):
        self.clock[0] += after
        self.supervisor.track(set(cpu), dict(cpu))

    def idle(self):
        return self.clock[0] - self.supervisor.active_at

    def test_flat_cpu_is_idle_and_timer_ticks_do_not_count(self):
        self.sample({10: 5.0, 11: 1.0})
        self.assertEqual(self.idle(), 0)
        for step in range(1, 601):
            # An idle node process: a hundredth of a second now and then.
            self.sample({10: 5.0 + step * 0.001, 11: 1.0})
        self.assertAlmostEqual(self.idle(), 600)
        self.assertEqual(self.heard[-1], (round(5.6 + 1.0, 2), self.supervisor.active_at))

    def test_a_second_of_cpu_is_progress(self):
        self.sample({10: 5.0})
        self.sample({10: 5.5}, after=300)
        self.assertEqual(self.idle(), 300)
        self.sample({10: 6.0}, after=300)
        self.assertEqual(self.idle(), 0)

    def test_a_process_joining_or_leaving_is_progress_and_an_exit_keeps_its_cpu(self):
        self.sample({10: 5.0, 11: 3.0})
        self.sample({10: 5.0, 11: 3.0}, after=200)
        self.assertEqual(self.idle(), 200)
        self.sample({10: 5.0}, after=100)
        self.assertEqual(self.idle(), 0)
        self.assertEqual(self.supervisor.cpu_seconds, 8.0, 'an exited process kept its CPU')

    def test_output_is_progress(self):
        self.sample({10: 5.0})
        self.clock[0] += 500
        self.supervisor.active_at = self.clock[0]         # what `_pump` does per chunk
        self.sample({10: 5.0}, after=10)
        self.assertEqual(self.idle(), 10)

    def test_a_tree_ps_cannot_measure_is_never_idle(self):
        self.sample({10: 5.0})
        self.clock[0] += 900
        self.supervisor.track({10}, {})
        self.assertIsNone(self.supervisor.active_at)
        self.assertEqual(self.heard[-1], (None, None))


def blocker(run_id, idle, lane='local'):
    return {'id': run_id, 'lane': lane, 'state': 'running', 'argv': ['pnpm', run_id],
            'idle_seconds': idle, 'cpu_seconds': 1.0}


class TheRestarter(unittest.TestCase):
    """`drain_and_restart` against a scripted daemon."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = home.name
        self.said, self.asked, self.restarts = [], [], []

    def restart(self, answers, cancel_answer=None, **kwargs):
        answers = iter(answers)

        def ask(_sock, request, timeout=30.0):
            self.asked.append(request)
            if request['op'] == 'cancel':
                return cancel_answer or {'t': 'ok', 'run': request['run']}
            if request.get('cancel'):
                return {'t': 'drain', 'draining': False}
            return {'t': 'drain', 'draining': True, 'blockers': next(answers)}
        kwargs.setdefault('restart', lambda: self.restarts.append(1))
        kwargs.setdefault('held_lock', lambda: False)
        kwargs.setdefault('successor_seconds', 0)
        return drain.drain_and_restart(self.state, ask=ask, say=self.said.append,
                                       interval=0.001, **kwargs)

    def cancels(self):
        return [request for request in self.asked if request['op'] == 'cancel']

    def test_an_idle_blocker_is_cancelled_with_a_reason_and_the_drain_proceeds(self):
        code = self.restart([[blocker('a', 1300.0)], [blocker('a', 1301.0)], []], wait=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.restarts, [1])
        self.assertEqual(len(self.cancels()), 1, 'asked once, not every poll')
        self.assertEqual((self.cancels()[0]['run'], self.cancels()[0]['if_idle']), ('a', 600))
        text = '\n'.join(self.said)
        self.assertIn('canceling a: no CPU progress and no output for 21m', text)
        self.assertIn('a local running, idle 21m: pnpm a', text)

    def test_a_progressing_blocker_is_never_cancelled(self):
        code = self.restart([[blocker('a', 5.0)]] * 50, wait=0.02)
        self.assertEqual(code, 75)
        self.assertEqual(self.cancels(), [])
        self.assertNotIn('idle', '\n'.join(self.said))

    def test_an_unmeasured_or_remote_blocker_is_never_cancelled(self):
        rows = [blocker('a', None), dict(blocker('b', 5000.0), lane='remote', state='queued')]
        self.assertEqual(self.restart([rows] * 50, wait=0.02), 75)
        self.assertEqual(self.cancels(), [])

    def test_zero_disables_it(self):
        self.assertEqual(self.restart([[blocker('a', 5000.0)]] * 50, wait=0.02,
                                      idle_cancel=0), 75)
        self.assertEqual(self.cancels(), [])

    def test_the_daemon_refusing_because_it_woke_up_is_said(self):
        refusal = {'t': 'error', 'code': 'not-idle', 'msg': 'run a is not an idle local run'}
        self.assertEqual(self.restart([[blocker('a', 900.0)]] * 50, cancel_answer=refusal,
                                      wait=0.02), 75)
        self.assertIn('  not canceled: run a is not an idle local run', self.said)

    def test_the_blocker_list_is_said_again_only_when_it_changes_in_five_minute_steps(self):
        rows = [[blocker('a', 70.0 + step)] for step in range(40)] + [[]]
        self.restart(rows, wait=30, idle_cancel=0)
        headers = [line for line in self.said if line.startswith('draining: waiting')]
        self.assertEqual(len(headers), 1)


class TheDaemon(DrainCase):
    def setUp(self):
        super().setUp()
        with (self.repo / 'pandora.toml').open('a') as handle:
            handle.write(BUSY)

    def running(self, job):
        self.assertTrue(wait_until(lambda: any(row['state'] == 'running'
                                               for row in self.rows(job)), 15))
        return self.rows(job)[0]['id']

    def test_an_idle_local_run_is_cancelled_by_the_drain_and_says_why(self):
        thread, answer = self.in_background(['pnpm', 'slow'])      # sleep 30: no CPU
        run_id = self.running('slow')
        said = []
        code = drain.drain_and_restart(
            self.state, restart=lambda: drain.clear_marker(self.state), wait=30,
            say=said.append, interval=0.2, idle_cancel=2, successor_seconds=5)
        self.assertEqual(code, 0, said)
        thread.join(timeout=30)
        self.assertEqual(answer['value'].exit, CANCELED)
        self.assertIn(b'canceled by a restart drain', answer['value'].err)
        self.assertIn(b'no CPU progress', answer['value'].err)
        self.assertTrue(any(line.startswith('canceling %s' % run_id) for line in said), said)
        self.assertEqual(json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())
                         ['state'], 'cancelled')

    def test_ps_json_and_the_drain_carry_the_cpu_reading(self):
        thread, _answer = self.in_background(['pnpm', 'slow'])
        run_id = self.running('slow')
        self.assertTrue(wait_until(lambda: self.daemon.live(run_id).cpu_seconds is not None, 10))
        row = next(row for row in self.ask({'op': 'ps', 'limit': 5})['data']
                   if row['id'] == run_id)
        self.assertIsInstance(row['cpu_seconds'], float)
        self.assertIsInstance(row['idle_seconds'], float)
        self.assertIsInstance(row['last_active'], float)
        blockers = self.ask({'op': 'drain'})['blockers']
        self.assertIn('idle_seconds', blockers[0])
        self.ask({'op': 'drain', 'cancel': True})
        self.ask({'op': 'cancel', 'run': run_id})
        thread.join(timeout=30)

    def test_the_daemon_refuses_to_cancel_a_run_that_is_progressing(self):
        thread, answer = self.in_background(['pnpm', 'busy'])
        run_id = self.running('busy')
        self.assertTrue(wait_until(lambda: self.daemon.live(run_id).cpu_seconds is not None, 10))
        time.sleep(2.5)
        reply = self.ask({'op': 'cancel', 'run': run_id, 'if_idle': 2})
        self.assertEqual(reply.get('code'), 'not-idle', reply)
        self.assertFalse(self.daemon.live(run_id).canceled.is_set())
        self.ask({'op': 'cancel', 'run': run_id})
        thread.join(timeout=30)
        self.assertEqual(answer['value'].exit, CANCELED)

    def test_an_unmeasured_run_is_never_idle(self):
        run = Run(self.state, 'unmeasured', {'argv': ['pnpm', 'x']})
        run.lane, run.state = 'local', 'running'
        with self.daemon.runs_lock:
            self.daemon.runs[run.id] = run
        self.addCleanup(run.finish, 0)
        reply = self.ask({'op': 'cancel', 'run': run.id, 'if_idle': 1})
        self.assertEqual(reply.get('code'), 'not-idle')
        self.assertIn('unmeasured', reply['msg'])


class TheCommandLine(unittest.TestCase):
    def test_idle_cancel_goes_with_restart_install_and_upgrade(self):
        from pandora import cli
        from pandora.tests.test_cli import capture
        code, _out, err = capture(cli.main, ['daemon', '--idle-cancel', '5'])
        self.assertEqual(code, 64)
        self.assertIn('--idle-cancel', err)
        self.assertEqual(cli.idle_cancel_of(SimpleNamespace(idle_cancel=None)), 600)
        self.assertEqual(cli.idle_cancel_of(SimpleNamespace(idle_cancel=0)), 0)
        self.assertEqual(cli.idle_cancel_of(SimpleNamespace(idle_cancel=-3)), 0)


if __name__ == '__main__':
    unittest.main()
