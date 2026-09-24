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
import subprocess
import tempfile
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

    def test_a_drain_nobody_renews_ends_itself(self):
        self.ask({'op': 'drain'})
        renewed = self.daemon.drain_renewed
        self.daemon.check_lease(clock=lambda: renewed + drain.LEASE_SECONDS - 1)
        self.assertIsNotNone(self.daemon.draining)
        self.daemon.check_lease(clock=lambda: renewed + drain.LEASE_SECONDS + 1)
        self.assertIsNone(self.daemon.draining)
        self.assertFalse(drain.marker_path(self.state).exists())
        self.assertEqual(self.call(['pnpm', 'unit']).exit, 0)

    def test_the_serve_loop_checks_the_lease(self):
        self.ask({'op': 'drain'})
        self.daemon.drain_renewed -= drain.LEASE_SECONDS + 5
        self.assertTrue(wait_until(lambda: self.daemon.draining is None, 5))

    def test_a_stopping_daemon_keeps_its_drain_for_the_successor(self):
        self.ask({'op': 'drain'})
        self.daemon.stopping.set()
        self.daemon.check_lease(clock=lambda: self.daemon.drain_renewed + 3600)
        self.assertIsNotNone(self.daemon.draining)

    def test_each_drain_request_renews_the_lease_and_dates_the_marker(self):
        self.ask({'op': 'drain'})
        old = time.time() - 600
        os.utime(drain.marker_path(self.state), (old, old))
        first = self.daemon.drain_renewed
        time.sleep(0.01)
        self.ask({'op': 'drain'})
        self.assertGreater(self.daemon.drain_renewed, first)
        self.assertLess(drain.read_marker(self.state)['age'], 60)

    def test_a_run_admitted_as_the_drain_begins_is_withdrawn_not_started(self):
        admit = self.daemon.budget.admit

        def admitted_then_drained(*args, **kwargs):
            admission = admit(*args, **kwargs)
            self.daemon.drain()           # lands between the admission and `running`
            return admission
        self.daemon.budget.admit = admitted_then_drained
        frame = self.first_frame(self.run_request(['pnpm', 'slow']))
        self.assertEqual(frame['t'], 'draining')
        row = self.rows('slow')[0]
        self.assertEqual((row['state'], row.get('pgid')), ('withdrawn', None))
        self.assertEqual(self.daemon.budget.snapshot()['running'], [])

    def test_a_stop_right_after_the_drain_still_tells_a_queued_run_to_resubmit(self):
        self.daemon.budget.gate.closed = lambda: 'test pressure'   # nothing admits
        answers = {}

        def client():
            answers['frame'] = self.first_frame(self.run_request(['pnpm', 'slow']))
        thread = threading.Thread(target=client, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: any(row['state'] == 'queued'
                                               for row in self.rows('slow')), 15))
        with self.daemon.runs_lock:
            [run] = [run for run in self.daemon.pending.values() if run.lane == 'local']
        run.drained = True                # the drain's mark, then `--now`'s SIGTERM at once
        self.daemon.stop()
        thread.join(timeout=10)
        self.assertEqual(answers['frame']['t'], 'draining')

    def test_a_stop_removes_the_socket_before_it_closes_local_runs(self):
        seen = []
        close = self.daemon.close_local_runs
        self.daemon.close_local_runs = lambda: (seen.append(
            self.daemon.socket_path.exists()), close())[1]
        self.daemon.stop()
        self.assertEqual(seen, [False])

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
        self.addCleanup(successor.budget.admission.store.close)
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
        self.drop = 0           # connections to close unanswered, as a stopping daemon does
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
                if self.drop > 0:
                    self.drop -= 1
                    continue
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

    def test_a_live_daemon_still_draining_when_the_wait_runs_out_is_exit_75_not_a_local_run(self):
        os.environ[drain.WAIT_ENV] = '0.3'
        self.daemon(drains=10_000)
        code, err = self.shim(['unit'])
        self.assertEqual(code, 75)
        self.assertFalse(self.ran.exists(), 'ran unmanaged beside a live daemon')
        self.assertIn('still draining for a restart after 0.3s', err)
        self.assertNotIn('as if Pandora were not installed', err)

    def test_the_default_wait_outlasts_the_longest_restart_wait(self):
        self.assertGreaterEqual(drain.DEFAULT_CLIENT_WAIT,
                                drain.LONGEST_RESTART_WAIT + drain.SUCCESSOR_SECONDS)
        from pandora.client import install
        self.assertGreaterEqual(drain.LONGEST_RESTART_WAIT, install.WAIT_SECONDS)
        self.assertGreaterEqual(drain.LONGEST_RESTART_WAIT, drain.DEFAULT_RESTART_WAIT)

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

    def test_a_connection_dropped_unanswered_after_a_drain_began_is_asked_again(self):
        self.marker()
        fake = self.daemon(drains=0)
        fake.drop = 1
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.asked, ['run', 'run'])
        self.assertNotIn('execution is uncertain', err)

    def test_a_connection_dropped_before_the_drain_began_is_uncertain(self):
        drain.write_marker(self.state, {'since': time.time() + 60, 'pid': 1})
        fake = self.daemon(drains=0)
        fake.drop = 1
        code, err = self.shim(['unit'])
        self.assertEqual(code, INFRA)
        self.assertIn('execution is uncertain', err)

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

    def test_a_restart_that_never_ends_costs_the_wait_then_exits_75(self):
        os.environ[drain.WAIT_ENV] = '0.6'
        self.marker()
        code, err = self.shim(['unit'])
        self.assertEqual(code, 75)
        self.assertFalse(self.ran.exists())
        self.assertIn('was draining for a restart and did not come back', err)
        self.assertNotIn('pandora daemon --install', err)

    # -- the three states of an unanswered socket, end to end ----------------

    def installed(self):
        config = self.root / 'config.toml'
        config.write_text('[client]\nstate = "%s"\n' % self.state)
        os.environ['PANDORA_CONFIG'] = str(config)

    def test_a_fresh_marker_waits_even_where_the_daemon_is_installed(self):
        self.installed()
        os.environ[drain.WAIT_ENV] = '0.6'
        self.marker()
        started = time.monotonic()
        code, err = self.shim(['unit'])
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertEqual(code, 75)
        self.assertIn(drain.NOTICE, err)
        self.assertNotIn('installed on this Mac', err)

    def test_no_marker_and_installed_is_a_short_grace_then_70(self):
        self.installed()
        with mock.patch.object(shim, 'DAEMON_GRACE_SECONDS', 0.3):
            started = time.monotonic()
            code, err = self.shim(['unit'])
        self.assertGreater(time.monotonic() - started, 0.25)
        self.assertEqual(code, INFRA)
        self.assertFalse(self.ran.exists())
        self.assertIn('installed on this Mac', err)
        self.assertNotIn(drain.NOTICE, err)

    def test_no_marker_and_never_installed_passes_through(self):
        code, err = self.shim(['unit'])
        self.assertEqual(code, 0)
        self.assertTrue(self.ran.exists())
        self.assertIn('as if Pandora were not installed', err)

    def test_a_stale_marker_where_installed_is_the_grace_not_a_wait(self):
        self.installed()
        self.marker(age=drain.STALE_SECONDS + 60)
        with mock.patch.object(shim, 'DAEMON_GRACE_SECONDS', 0.1):
            code, err = self.shim(['unit'])
        self.assertEqual(code, INFRA)
        self.assertNotIn(drain.NOTICE, err)

    def test_the_slow_path_waits_too_on_the_same_budget(self):
        fake = self.daemon(drains=1)
        with mock.patch.object(shim, 'claimed_here', return_value=(True, False)):
            code, err = self.shim(['unit'], '--refresh')
        self.assertEqual(code, 0)
        self.assertEqual(fake.asked, ['claims', 'claims', 'run'])
        self.assertEqual(err.count(drain.NOTICE), 1, err)

    def test_the_slow_path_runs_an_unclaimed_command_at_once(self):
        fake = self.daemon(drains=10)
        with mock.patch.object(shim, 'claimed_here', return_value=(False, False)), \
                mock.patch.object(shim, 'unclaimed', return_value=0) as ran:
            code, err = self.shim(['lint'], '--refresh')
        self.assertEqual(code, 0)
        self.assertEqual(fake.asked, ['claims'])
        ran.assert_called_once()
        self.assertNotIn(drain.NOTICE, err)

    def test_an_attached_client_does_not_wait(self):
        from pandora import cli
        self.marker()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = time.monotonic()
            self.assertEqual(cli.attach(self.state / 'client.sock', 'x1'), INFRA)
        self.assertLess(time.monotonic() - started, 2)
        self.assertNotIn(drain.NOTICE, err.getvalue())


class ARestart(DrainCase):
    """`drain_and_restart`: what `pandora daemon --restart` and `pandora upgrade` run."""

    def setUp(self):
        super().setUp()
        self.said, self.restarts = [], []

    def successor(self):
        """What launchd's kickstart does: stop this daemon, start the next on the same state."""
        self.restarts.append(time.monotonic())
        self.daemon.stop()
        successor = daemon_module.Daemon(config_path=str(self.root / 'config.toml'))
        self.addCleanup(successor.budget.admission.store.close)
        successor.worker_factory = FakeWorker
        successor.start()
        self.addCleanup(successor.stop)
        threading.Thread(target=successor.serve, daemon=True).start()
        self.next = successor

    def restart(self, **kwargs):
        kwargs.setdefault('restart', self.successor)
        kwargs.setdefault('interval', 0.1)
        return drain.drain_and_restart(self.state, say=self.said.append, **kwargs)

    def test_it_waits_for_the_executing_run_then_restarts_and_the_successor_clears_the_marker(self):
        thread, answer = self.in_background(['pnpm', 'brief'])
        self.assertTrue(wait_until(lambda: any(row['state'] == 'running'
                                               for row in self.rows('brief')), 15))
        self.assertEqual(self.restart(wait=30), 0)
        thread.join(timeout=10)
        self.assertEqual(answer['value'].exit, 0, 'the restart ended the run it waited for')
        self.assertEqual(len(self.restarts), 1)
        self.assertEqual(self.rows('brief')[0]['state'], 'passed')
        self.assertTrue(any('waiting up to' in line for line in self.said), self.said)
        self.assertTrue(any('brief' in line for line in self.said), self.said)
        self.assertFalse(drain.marker_path(self.state).exists())
        self.assertIsNone(self.next.draining)

    def test_nothing_running_restarts_at_once(self):
        self.assertEqual(self.restart(wait=30), 0)
        self.assertEqual(len(self.restarts), 1)
        self.assertIn('the new daemon is up and admitting runs', self.said)

    def test_a_timeout_undrains_and_exits_75_naming_what_still_runs(self):
        thread, answer = self.in_background(['pnpm', 'slow'])
        self.assertTrue(wait_until(lambda: any(row['state'] == 'running'
                                               for row in self.rows('slow')), 15))
        self.assertEqual(self.restart(wait=0.5), 75)
        self.assertEqual(self.restarts, [])
        self.assertIsNone(self.daemon.draining)
        self.assertFalse(drain.marker_path(self.state).exists())
        self.assertTrue(any('gave up' in line for line in self.said), self.said)
        self.assertTrue(any('pnpm slow' in line for line in self.said), self.said)
        self.assertEqual(self.call(['pnpm', 'unit']).exit, 0, 'still refusing after undrain')

    def test_now_restarts_when_the_wait_runs_out_and_the_run_ends_70(self):
        thread, answer = self.in_background(['pnpm', 'slow'])
        self.assertTrue(wait_until(lambda: any(row['state'] == 'running'
                                               for row in self.rows('slow')), 15))
        self.assertEqual(self.restart(wait=0.5, now=True), 0)
        thread.join(timeout=10)
        self.assertEqual(answer['value'].exit, INFRA)
        self.assertEqual(len(self.restarts), 1)
        self.assertIn(drain.NOW_NOTE, self.said)
        # The executor says its piece after the child dies; let it, before the
        # temporary directory goes.
        self.assertTrue(wait_until(lambda: not any(
            'execute_local' in thread.name and thread.is_alive()
            for thread in threading.enumerate()), 10))

    def test_a_restart_that_fails_leaves_the_daemon_admitting(self):
        def refused():
            raise RuntimeError('kickstart failed')
        with self.assertRaises(RuntimeError):
            self.restart(wait=5, restart=refused)
        self.assertIsNone(self.daemon.draining)
        self.assertFalse(drain.marker_path(self.state).exists())

    def test_before_restart_runs_once_nothing_blocks(self):
        order = []
        self.restart(wait=5, before_restart=lambda: order.append(
            ('flip', self.daemon.draining is not None)),
            restart=lambda: (order.append(('restart', None)), self.successor()))
        self.assertEqual(order, [('flip', True), ('restart', None)])

    def test_the_marker_is_dated_at_the_restart_not_at_the_drain(self):
        seen = []

        def restart():
            seen.append(drain.read_marker(self.state)['age'])
            self.successor()
        self.ask({'op': 'drain'})
        os.utime(drain.marker_path(self.state), (time.time() - 600, time.time() - 600))
        self.restart(wait=5, restart=restart)
        self.assertLess(seen[0], 5)


class ARestartWithoutADrain(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = home.name
        self.said, self.restarts = [], []

    def restart(self, ask, **kwargs):
        kwargs.setdefault('restart', lambda: self.restarts.append(1))
        kwargs.setdefault('held_lock', lambda: False)
        return drain.drain_and_restart(self.state, ask=ask, say=self.said.append,
                                       interval=0.01, **kwargs)

    def test_a_refused_socket_with_the_lock_held_is_a_silent_daemon_not_an_absent_one(self):
        def ask(_sock, request, timeout=30.0):
            raise ConnectionRefusedError('backlog full')
        self.assertEqual(self.restart(ask, wait=5, held_lock=lambda: True), 75)
        self.assertEqual(self.restarts, [])
        self.assertTrue(any('holds' in line for line in self.said), self.said)
        self.assertEqual(self.restart(ask, wait=5, now=True, held_lock=lambda: True), 0)
        self.assertEqual(self.restarts, [1])

    def test_a_daemon_that_vanishes_mid_drain_with_the_lock_held_still_blocks(self):
        calls = []

        def ask(_sock, request, timeout=30.0):
            calls.append(request)
            if len(calls) == 1:
                return {'t': 'drain', 'draining': True,
                        'blockers': [{'id': 'a', 'lane': 'local', 'state': 'running'}]}
            if request.get('cancel'):
                return {'t': 'drain', 'draining': False}
            raise ConnectionRefusedError('backlog full')
        self.assertEqual(self.restart(ask, wait=0.2, held_lock=lambda: True), 75)
        self.assertEqual(self.restarts, [])

    def test_a_pre_drain_daemon_that_admits_a_run_during_the_flip_puts_it_back(self):
        answers = iter([[], [{'id': 'late', 'lane': 'local', 'state': 'running',
                              'argv': ['pnpm', 'x']}], [], []])
        order = []

        def ask(_sock, request, timeout=30.0):
            if request['op'] == 'drain':
                return {'t': 'error', 'code': 'rejected', 'msg': "unknown op 'drain'"}
            return {'t': 'ps', 'data': next(answers)}
        self.assertEqual(self.restart(ask, wait=5, before_restart=lambda: order.append('flip'),
                                      undo_before_restart=lambda: order.append('undo'),
                                      restart=lambda: order.append('restart')), 0)
        self.assertEqual(order, ['flip', 'undo', 'flip', 'restart'])
        self.assertTrue(any('late local running' in line for line in self.said), self.said)

    def test_a_failed_end_is_said_and_reported(self):
        def ask(_sock, request, timeout=30.0):
            if request.get('cancel'):
                raise TimeoutError('timed out')
            return {'t': 'drain', 'draining': True,
                    'blockers': [{'id': 'a', 'lane': 'local', 'state': 'running'}]}
        report = {}
        self.assertEqual(self.restart(ask, wait=0.05, report=report), 75)
        self.assertIs(report['undrained'], False)
        self.assertTrue(any('could not end the drain' in line for line in self.said))
        self.assertFalse(any('admitting runs again' in line for line in self.said))

    def test_sigterm_or_sighup_ends_the_drain_on_the_way_out(self):
        import signal
        for number in (signal.SIGTERM, signal.SIGHUP):
            asked = []
            before = signal.getsignal(number)

            def ask(_sock, request, timeout=30.0):
                asked.append(request)
                if request.get('cancel'):
                    return {'t': 'drain', 'draining': False}
                return {'t': 'drain', 'draining': True,
                        'blockers': [{'id': 'a', 'lane': 'local', 'state': 'running'}]}
            with self.assertRaises(drain.Interrupted):
                self.restart(ask, wait=30, sleep=lambda _s: signal.raise_signal(number))
            self.assertIn({'op': 'drain', 'cancel': True}, asked)
            self.assertIs(signal.getsignal(number), before)

    def test_an_old_daemon_still_answering_after_the_restart_is_undrained(self):
        drain.write_marker(self.state, {'since': 0, 'daemon': 555})
        asked, clock = [], [0.0]

        def ask(_sock, request, timeout=30.0):
            asked.append(request)
            if request['op'] == 'ping':
                return {'t': 'pong', 'pid': 555}
            if request.get('cancel'):
                return {'t': 'drain', 'draining': False}
            return {'t': 'drain', 'draining': True, 'blockers': []}

        def sleep(seconds):
            clock[0] += seconds
        self.assertEqual(self.restart(ask, wait=5, clock=lambda: clock[0], sleep=sleep), 1)
        self.assertIn({'op': 'drain', 'cancel': True}, asked)
        self.assertTrue(any('launchd did not restart it' in line for line in self.said))

    def test_no_daemon_is_restarted_at_once(self):
        self.assertEqual(self.restart(drain.ask, wait=5), 0)
        self.assertEqual(self.restarts, [1])

    def test_a_daemon_from_before_drain_is_waited_on_through_ps(self):
        answers = iter([{'t': 'ps', 'data': [{'id': 'a', 'lane': 'local', 'state': 'running',
                                              'argv': ['pnpm', 'x']}]},
                        {'t': 'ps', 'data': [{'id': 'a', 'lane': 'local', 'state': 'passed'}]},
                        {'t': 'ps', 'data': []}])

        def ask(_sock, request, timeout=30.0):
            if request['op'] == 'drain':
                return {'t': 'error', 'code': 'rejected', 'msg': "unknown op 'drain'"}
            return next(answers)
        self.assertEqual(self.restart(ask, wait=5), 0)
        self.assertEqual(self.restarts, [1])
        self.assertTrue(any('predates drain' in line for line in self.said))

    def test_a_silent_daemon_is_restarted_only_with_now(self):
        asked = []

        def ask(_sock, request, timeout=30.0):
            asked.append(request)
            raise TimeoutError('timed out')
        self.assertEqual(self.restart(ask, wait=5), 75)
        self.assertEqual(self.restarts, [])
        self.assertIn({'op': 'drain', 'cancel': True}, asked)
        self.assertEqual(self.restart(ask, wait=5, now=True), 0)
        self.assertEqual(self.restarts, [1])


class TheCommand(DrainCase):
    def pandora(self, *argv):
        return capture(cli.main, ['--state', str(self.state),
                                  '--config', str(self.root / 'config.toml'), *argv])

    def test_wait_and_now_go_with_restart_or_install_only(self):
        self.assertEqual(self.pandora('daemon', '--uninstall', '--now')[0], 64)
        self.assertEqual(self.pandora('daemon', '--stop', '--wait', '5')[0], 64)
        self.assertEqual(self.pandora('daemon', '--wait', '5')[0], 64)

    def launchd(self, loaded):
        from pandora.client import launchd
        from pandora.tests.test_launchd import FakeLaunchd
        fake = FakeLaunchd(loaded)
        patches = [mock.patch.object(launchd, 'launchctl',
                                     lambda args, run=None: fake([launchd.LAUNCHCTL, *args])),
                   mock.patch.object(cli.sys, 'platform', 'darwin')]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return fake

    def test_a_lock_held_by_a_daemon_launchd_did_not_start_is_refused_before_any_drain(self):
        fake = self.launchd({'com.pandora.daemon': 100})     # this process holds the lock
        code, _, err = self.pandora('daemon', '--restart')
        self.assertEqual(code, 1)
        self.assertIn('would not reach it', err)
        self.assertIsNone(self.daemon.draining)
        self.assertNotIn('kickstart', [call[0] for call in fake.calls])

    def test_enroll_does_not_call_a_draining_daemon_old(self):
        self.ask({'op': 'drain'})
        _, _, err = capture(cli.check_daemon_knows_claims, self.daemon.socket_path, self.repo)
        self.assertNotIn('predates', err)
        self.assertIn('draining', err)

    def test_an_agent_launchd_does_not_run_is_refused_before_any_drain(self):
        fake = self.launchd({})
        code, _, err = self.pandora('daemon', '--restart')
        self.assertEqual(code, 1)
        self.assertIn('not loaded in launchd', err)
        self.assertIsNone(self.daemon.draining)
        self.assertNotIn('kickstart', [call[0] for call in fake.calls])

    def test_restart_drains_then_kickstarts(self):
        fake = self.launchd({'com.pandora.daemon': os.getpid()})
        original = fake.__call__

        def kickstart(argv, **kwargs):
            if argv[1] == 'kickstart':
                self.assertIsNotNone(self.daemon.draining, 'kickstarted before draining')
                drain.clear_marker(self.state)            # the successor, in one line
            return original(argv, **kwargs)
        fake_call = mock.patch.object(type(fake), '__call__',
                                      lambda self_, argv, **kw: kickstart(argv, **kw))
        fake_call.start()
        self.addCleanup(fake_call.stop)
        code, _, err = self.pandora('daemon', '--restart', '--wait', '5')
        self.assertEqual(code, 0, err)
        self.assertIn(['kickstart', '-k', 'gui/%d/com.pandora.daemon' % os.getuid()],
                      fake.calls)
        self.assertIn('drained', err)

    def test_install_over_a_loaded_agent_drains_before_the_bootout(self):
        fake = self.launchd({'com.pandora.daemon': os.getpid()})
        original = fake.__call__
        order = []

        def call(argv, **kwargs):
            if argv[1] in ('bootout', 'bootstrap'):
                order.append((argv[1], self.daemon.draining is not None))
            if argv[1] == 'bootout':
                drain.clear_marker(self.state)            # the successor, in one line
            return original(argv, **kwargs)
        patch = mock.patch.object(type(fake), '__call__', lambda self_, argv, **kw: call(argv, **kw))
        patch.start()
        self.addCleanup(patch.stop)
        code, _, err = self.pandora('daemon', '--install', '--wait', '5')
        self.assertEqual(code, 0, err)
        self.assertEqual(order, [('bootout', True), ('bootstrap', True)])
        self.assertIn('drained', err)
        self.assertIn('the new daemon is up', err)

    def test_install_waits_for_an_executing_run_and_gives_up_without_a_bootout(self):
        fake = self.launchd({'com.pandora.daemon': os.getpid()})
        thread, _answer = self.in_background(['pnpm', 'slow'])
        self.assertTrue(wait_until(lambda: any(row['state'] == 'running'
                                               for row in self.rows('slow')), 15))
        code, _, err = self.pandora('daemon', '--install', '--wait', '0.5')
        self.assertEqual(code, 75, err)
        self.assertIn('pandora daemon --install --now', err)
        self.assertNotIn('bootout', fake.verbs())
        self.assertIsNone(self.daemon.draining)
        for row in self.rows('slow'):
            self.ask({'op': 'cancel', 'run': row['id']})
        thread.join(timeout=30)

    def test_install_with_nothing_loaded_and_no_daemon_loads_at_once(self):
        self.daemon.stop()
        fake = self.launchd({})
        code, _, err = self.pandora('daemon', '--install')
        self.assertEqual(code, 0, err)
        self.assertNotIn('drain', err)
        self.assertEqual(fake.verbs()[:3], ['print', 'print', 'bootstrap'])


class Doctor(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = home.name

    def test_no_marker_no_line(self):
        from pandora.client import doctor
        self.assertIsNone(doctor.check_drain(self.state))

    def test_a_fresh_marker_is_reported_as_a_restart_in_progress(self):
        from pandora.client import doctor
        drain.write_marker(self.state, {'since': time.time(), 'pid': 77})
        item = doctor.check_drain(self.state)
        self.assertEqual(item['status'], doctor.INFO)
        self.assertIn('pid 77', item['detail'])

    def test_a_stale_marker_is_a_warning_and_is_left_where_it_is(self):
        from pandora.client import doctor
        drain.write_marker(self.state, {'since': 0, 'pid': 77})
        stamp = time.time() - drain.STALE_SECONDS - 120
        os.utime(drain.marker_path(self.state), (stamp, stamp))
        item = doctor.check_drain(self.state)
        self.assertEqual(item['status'], doctor.WARN)
        self.assertIn('never finished', item['detail'])
        self.assertTrue(drain.marker_path(self.state).exists(), 'doctor changed something')

    def test_the_report_carries_it(self):
        from pandora.client import doctor
        drain.write_marker(self.state, {'since': 0, 'pid': 77})
        stamp = time.time() - drain.STALE_SECONDS - 120
        os.utime(drain.marker_path(self.state), (stamp, stamp))
        from pandora.tests.test_launchd import FakeLaunchd

        def failed(argv, **_):
            return subprocess.CompletedProcess(argv, 1, '', '')
        report = doctor.run(state=self.state, env={'PATH': '/nonexistent'}, cwd=self.state,
                            runner=failed, launchctl=FakeLaunchd())
        names = [item['name'] for item in report['checks']]
        self.assertEqual(names[-1], 'restart drain', names)


class TheDecision(unittest.TestCase):
    """No daemon answers: wait for a restart, a short grace then 70, or pass through."""

    def test_a_fresh_marker_is_a_restart_whether_or_not_installed(self):
        for installed in (True, False):
            self.assertEqual(drain.when_unanswered({'age': 5}, installed, budget=660),
                             drain.WAIT_FOR_RESTART)

    def test_no_marker_installed_is_the_grace_then_70(self):
        self.assertEqual(drain.when_unanswered(None, True), drain.GRACE_THEN_REFUSE)

    def test_no_marker_never_installed_passes_through(self):
        self.assertEqual(drain.when_unanswered(None, False), drain.PASS_THROUGH)

    def test_a_marker_older_than_the_wait_or_stale_is_no_marker(self):
        self.assertEqual(drain.when_unanswered({'age': 700}, True, budget=660),
                         drain.GRACE_THEN_REFUSE)
        self.assertEqual(drain.when_unanswered({'age': drain.STALE_SECONDS}, False,
                                               budget=10 ** 6), drain.PASS_THROUGH)
        self.assertEqual(drain.when_unanswered({'age': 0}, True, budget=0),
                         drain.GRACE_THEN_REFUSE)

    def test_a_wait_already_begun_stays_a_wait(self):
        self.assertEqual(drain.when_unanswered(None, False, waited=True),
                         drain.WAIT_FOR_RESTART)


class TheWaiter(unittest.TestCase):
    def test_a_zero_budget_never_waits_and_never_says_so(self):
        said = []
        waiter = drain.Waiter('/nonexistent', budget=0, say=said.append,
                              sleep=lambda _: self.fail('slept'))
        self.assertFalse(waiter.draining(2))
        self.assertEqual(said, [])

    def test_the_notice_is_said_once_across_asks(self):
        said, slept = [], []
        waiter = drain.Waiter('/nonexistent', budget=10, say=said.append, sleep=slept.append,
                              clock=lambda: 0.0)
        self.assertTrue(waiter.draining(2))
        self.assertTrue(waiter.draining('junk'))
        self.assertEqual((said, slept), ([drain.NOTICE], [2.0, drain.RETRY_AFTER]))
