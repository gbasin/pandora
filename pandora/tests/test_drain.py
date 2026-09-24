"""A draining daemon admits nothing new and lets what it started finish.

`pandora daemon --restart` drains before it restarts, so a restart under steady
traffic ends no run: a new request is told `draining` and asked to come back, a
local run executing here finishes, and a remote row before `accepted` reaches
it. The successor removes the marker once it has settled every row.
"""
import contextlib
import io
import json
import os
import socket
import threading
import time
import unittest
from unittest import mock

from pandora import cli
from pandora.client import daemon as daemon_module
from pandora.client import drain, shim
from pandora.client.protocol import Reader, VERSION, dump
from pandora.exits import INFRA
from pandora.tests.test_cli import capture
from pandora.tests import test_fallback
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


class FlippingDaemon:
    """A socket that answers `draining` a number of times, then like a daemon that runs it."""

    def __init__(self, path, drains=0, retry_after=0.05):
        self.path, self.drains, self.retry_after = str(path), drains, retry_after
        self.asked = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                request = Reader(conn).line() or {}
                self.asked.append(request.get('op'))
                if self.drains > 0:
                    self.drains -= 1
                    conn.sendall(dump({'v': VERSION, 't': 'draining',
                                       'retry_after': self.retry_after,
                                       'reason': 'restarting'}))
                elif request.get('op') == 'claims':
                    conn.sendall(dump({'v': VERSION, 't': 'claims', 'claimed': True,
                                       'heavy': False}))
                else:
                    conn.sendall(dump({'v': VERSION, 't': 'accepted', 'run': 'd1',
                                       'remote': 'w1'}))
                    conn.sendall(dump({'t': 'exit', 'code': 0, 'run': 'd1'}))

    def close(self):
        self.server.close()


class AClientDuringARestart(unittest.TestCase):
    """The shim waits for a restart, and only for a restart, and only so long."""

    POLICY = [{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto', 'writeback': False}]
    enroll = test_fallback.WithoutADaemon.enroll

    def setUp(self):
        test_fallback.WithoutADaemon.setUp(self)
        self.enroll(self.POLICY)
        environ = mock.patch.dict(os.environ, {drain.WAIT_ENV: '5'})
        environ.start()
        self.addCleanup(environ.stop)

    def daemon(self, **kwargs):
        fake = FlippingDaemon(self.state / 'client.sock', **kwargs)
        self.addCleanup(fake.close)
        return fake

    def shim(self, command, *extra):
        real = self.root / 'fake-pnpm'
        real.write_text('#!/bin/sh\necho ran > %s\n' % self.ran)
        real.chmod(0o755)
        code, _, err = capture(shim.main, ['--sock', str(self.state / 'client.sock'),
                                           '--real', str(real), '--state', str(self.state),
                                           *extra, '--', *command])
        return code, err

    def marker(self, age=0.0):
        drain.write_marker(self.state, {'since': time.time() - age, 'pid': 1})
        stamp = time.time() - age
        os.utime(drain.marker_path(self.state), (stamp, stamp))

    def test_a_draining_answer_is_waited_out_and_the_run_submitted_once_it_flips(self):
        fake = self.daemon(drains=2)
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertEqual(fake.asked, ['run', 'run', 'run'])
        self.assertEqual(err.count(drain.NOTICE), 1, err)
        self.assertFalse(self.ran.exists(), 'it ran here as well')

    def test_the_budget_runs_out_and_the_command_runs_as_if_pandora_were_absent(self):
        os.environ[drain.WAIT_ENV] = '0.3'
        self.daemon(drains=10_000)
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertTrue(self.ran.exists())
        self.assertIn('did not come back within 0.3s', err)
        self.assertIn('as if Pandora were not installed', err)
        self.assertIn('daemon-draining', (self.state / 'passthrough.jsonl').read_text())

    def test_the_restart_gap_is_waited_out_while_a_fresh_marker_says_one_is_coming(self):
        self.marker()
        fakes = []
        timer = threading.Timer(0.6, lambda: fakes.append(self.daemon()))
        timer.start()
        self.addCleanup(timer.cancel)
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertEqual(fakes[0].asked, ['run'])
        self.assertIn(drain.NOTICE, err)
        self.assertFalse(self.ran.exists())

    def test_a_stale_marker_is_ignored(self):
        self.marker(age=drain.STALE_SECONDS + 60)
        os.environ[drain.WAIT_ENV] = str(drain.STALE_SECONDS * 2)
        started = time.monotonic()
        code, err = self.shim(['unit'])
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(code, 0)
        self.assertTrue(self.ran.exists())
        self.assertNotIn(drain.NOTICE, err)

    def test_a_marker_older_than_the_wait_is_ignored(self):
        self.marker(age=30)
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertTrue(self.ran.exists())
        self.assertNotIn(drain.NOTICE, err)

    def test_no_marker_means_no_wait(self):
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertNotIn(drain.NOTICE, err)

    def test_a_restart_that_never_ends_costs_the_wait_then_runs_here(self):
        os.environ[drain.WAIT_ENV] = '0.6'
        self.marker()
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertTrue(self.ran.exists())
        self.assertIn('did not come back', err)
        self.assertIn('daemon-unreachable', (self.state / 'passthrough.jsonl').read_text())

    def test_the_slow_path_waits_too_on_the_same_budget(self):
        fake = self.daemon(drains=1)
        code, err = self.shim(['unit'], '--refresh')
        self.assertEqual(code, 0)
        self.assertEqual(fake.asked, ['claims', 'claims', 'run'])
        self.assertEqual(err.count(drain.NOTICE), 1, err)

    def test_an_attached_client_does_not_wait(self):
        from pandora import cli
        self.marker()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = time.monotonic()
            self.assertEqual(cli.attach(self.state / 'client.sock', 'x1'), INFRA)
        self.assertLess(time.monotonic() - started, 2)
        self.assertNotIn(drain.NOTICE, err.getvalue())
