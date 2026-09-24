"""A draining daemon admits nothing new and lets what it started finish.

`pandora daemon --restart` drains before it restarts, so a restart under steady
traffic ends no run: a new request is told `draining` and asked to come back, a
local run executing here finishes, and a remote row before `accepted` reaches
it. The successor removes the marker once it has settled every row.
"""
import json
import os
import socket
import threading
import time

from pandora import cli
from pandora.client import daemon as daemon_module
from pandora.client import drain
from pandora.client.protocol import Reader, VERSION, dump
from pandora.exits import INFRA
from pandora.tests.test_cli import capture
from pandora.tests.test_fallback import DaemonCase, FakeWorker, Submission

JOBS = '''
[[jobs]]
id = "brief"
size = "small"
args = "none"
where = "local"
forms = [{ prefix = ["brief"] }]
run = { argv = ["sh", "-c", "echo started; sleep 1.5; echo finished"] }
[[jobs]]
id = "slow"
size = "small"
args = "none"
where = "local"
forms = [{ prefix = ["slow"] }]
run = { argv = ["sh", "-c", "echo started; sleep 30"] }
'''


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class GatedWorker(FakeWorker):
    """Holds `submit` until the test opens the gate: a remote row before `accepted`."""

    gate = None
    entered = None

    def submit(self, **kwargs):
        GatedWorker.entered.set()
        GatedWorker.gate.wait(20)
        return Submission('r-gated')


class DrainCase(DaemonCase):
    def setUp(self):
        super().setUp()
        with (self.repo / 'pandora.toml').open('a') as handle:
            handle.write(JOBS)

    def ask(self, request, timeout=10):
        return drain.ask(self.daemon.socket_path, request, timeout=timeout)

    def first_frame(self, request, timeout=30):
        """The first frame that is not a notice or `queued`: the daemon's answer."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.daemon.socket_path))
        try:
            sock.sendall(dump(dict({'v': VERSION}, **request)))
            reader = Reader(sock)
            while True:
                frame = reader.line()
                if frame is None or frame.get('t') not in ('notice', 'queued', 'working'):
                    return frame
        finally:
            sock.close()

    def run_request(self, argv):
        return {'op': 'run', 'cwd': str(self.repo), 'argv': argv, 'env': {}, 'tty': False}

    def rows(self, job):
        out = []
        for meta in (self.state / 'runs').glob('*/meta.json'):
            row = json.loads(meta.read_text())
            if row.get('job') == job:
                out.append(row)
        return out

    def in_background(self, argv):
        answer = {}
        thread = threading.Thread(
            target=lambda: answer.setdefault('value', self.call(argv, timeout=30)), daemon=True)
        thread.start()
        return thread, answer


class ADrainingDaemon(DrainCase):
    def test_a_new_run_is_answered_draining_and_opens_no_row(self):
        reply = self.ask({'op': 'drain', 'pid': 4242})
        self.assertEqual((reply['t'], reply['draining'], reply['blockers']), ('drain', True, []))
        frame = self.first_frame(self.run_request(['pnpm', 'unit']))
        self.assertEqual(frame['t'], 'draining')
        self.assertEqual(frame['reason'], 'restarting')
        self.assertEqual(frame['retry_after'], drain.RETRY_AFTER)
        self.assertEqual(self.rows('unit'), [])
        self.assertFalse(self.marker.exists(), 'the command ran')

    def test_a_claims_question_is_answered_draining(self):
        self.ask({'op': 'drain'})
        frame = self.first_frame({'op': 'claims', 'cwd': str(self.repo), 'argv': ['unit']})
        self.assertEqual(frame['t'], 'draining')

    def test_the_marker_names_the_requester_and_undrain_removes_it(self):
        self.ask({'op': 'drain', 'pid': 4242})
        marker = drain.read_marker(self.state)
        self.assertEqual((marker['pid'], marker['daemon']), (4242, os.getpid()))
        reply = self.ask({'op': 'drain', 'cancel': True})
        self.assertEqual((reply['t'], reply['draining']), ('drain', False))
        self.assertIsNone(drain.read_marker(self.state))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)

    def test_an_executing_local_run_finishes_and_blocks_until_it_does(self):
        thread, answer = self.in_background(['pnpm', 'brief'])
        self.assertTrue(wait_until(lambda: any(row['state'] == 'running'
                                               for row in self.rows('brief')), 15))
        reply = self.ask({'op': 'drain'})
        self.assertEqual([row['lane'] for row in reply['blockers']], ['local'])
        self.assertEqual((reply['local'], reply['pre_accept']), (1, 0))
        thread.join(timeout=20)
        self.assertEqual(answer['value'].exit, 0)
        self.assertIn(b'finished', answer['value'].out)
        self.assertEqual(self.ask({'op': 'drain'})['blockers'], [])

    def test_a_queued_local_run_is_withdrawn_and_told_to_ask_again(self):
        self.daemon.budget.gate.closed = lambda: 'test pressure'   # nothing admits
        answers = {}

        def client():
            answers['frame'] = self.first_frame(self.run_request(['pnpm', 'slow']))

        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: any(row['state'] == 'queued'
                                               for row in self.rows('slow')), 15))
        reply = self.ask({'op': 'drain'})
        self.assertEqual(reply['blockers'], [])
        thread.join(timeout=10)
        self.assertEqual(answers['frame']['t'], 'draining')
        row = self.rows('slow')[0]
        self.assertEqual((row['state'], row['exit_code'], row.get('pgid')),
                         ('withdrawn', INFRA, None))

    def test_ps_cancel_and_attach_are_still_served(self):
        done = self.call(['pnpm', 'unit'])
        self.assertEqual(done.exit, 0)
        run_id = done.accepted['run']
        self.ask({'op': 'drain', 'pid': 7})
        ps = self.ask({'op': 'ps'})
        self.assertEqual(ps['t'], 'ps')
        self.assertEqual(ps['draining']['pid'], 7)
        self.assertIn(run_id, [row['id'] for row in ps['data']])
        self.assertEqual(self.ask({'op': 'cancel', 'run': run_id})['t'], 'ok')
        attached = self.first_frame({'op': 'attach', 'run': run_id, 'from': 0})
        self.assertEqual((attached['t'], attached['run']), ('accepted', run_id))
        self.assertEqual(self.ask({'op': 'ping'})['t'], 'pong')

    def test_ps_says_draining_on_its_first_line_and_not_after_undrain(self):
        argv = ['--state', str(self.state), '--config', str(self.root / 'config.toml'), 'ps']
        self.ask({'op': 'drain', 'pid': 99})
        code, out, _ = capture(cli.main, argv)
        self.assertEqual(code, 0)
        self.assertTrue(out.splitlines()[0].startswith('daemon: draining'), out)
        self.assertIn('pid 99', out.splitlines()[0])
        self.ask({'op': 'drain', 'cancel': True})
        self.assertNotIn('draining', capture(cli.main, argv)[1])

    def test_ps_in_the_restart_gap_says_so_from_the_marker(self):
        self.ask({'op': 'drain'})
        self.daemon.stop()
        argv = ['--state', str(self.state), '--config', str(self.root / 'config.toml'), 'ps']
        out = capture(cli.main, argv)[1]
        self.assertTrue(out.splitlines()[0].startswith('daemon: draining'), out)
        self.assertIn('no daemon answers yet', out.splitlines()[0])

    def test_a_request_during_a_drained_stop_is_told_to_wait_not_to_rerun(self):
        self.ask({'op': 'drain'})
        self.daemon.stopping.set()
        self.assertEqual(self.first_frame(self.run_request(['pnpm', 'slow']))['t'], 'draining')

    def test_the_successor_clears_the_marker_after_settling(self):
        self.ask({'op': 'drain'})
        self.assertIsNotNone(drain.read_marker(self.state))
        self.daemon.stop()
        self.assertTrue(drain.marker_path(self.state).exists(),
                        'the stopping daemon removed the marker its successor needs')
        successor = daemon_module.Daemon(config_path=str(self.root / 'config.toml'))
        successor.worker_factory = FakeWorker
        settled = []
        successor.resume_interrupted = lambda: settled.append(
            drain.marker_path(self.state).exists())
        successor.start()
        self.addCleanup(successor.stop)
        self.assertEqual(settled, [True], 'the marker went before the rows were settled')
        self.assertFalse(drain.marker_path(self.state).exists())
        self.assertIsNone(successor.draining)


class ARemoteRowBeforeAccepted(DrainCase):
    def setUp(self):
        GatedWorker.gate, GatedWorker.entered = threading.Event(), threading.Event()
        super().setUp()
        self.daemon.worker_factory = GatedWorker
        with self.daemon.workers_lock:
            self.daemon.workers.clear()        # the health poll may have built one already
        self.addCleanup(GatedWorker.gate.set)

    def test_blocks_the_drain_until_it_is_accepted(self):
        thread, answer = self.in_background(['pnpm', 'unit'])
        self.assertTrue(GatedWorker.entered.wait(15))
        reply = self.ask({'op': 'drain'})
        self.assertEqual([(row['lane'], row['state']) for row in reply['blockers']],
                         [('remote', 'queued')])
        self.assertEqual(reply['pre_accept'], 1)
        GatedWorker.gate.set()
        thread.join(timeout=20)
        self.assertEqual(answer['value'].exit, 0)
        self.assertEqual(answer['value'].accepted['remote'], 'r-gated')
        self.assertEqual(self.ask({'op': 'drain'})['blockers'], [])
