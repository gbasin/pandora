"""Status stays responsive when admission or persistent history is slow."""
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client.daemon import Daemon, Run
from pandora.client.pressure import Gate
from pandora.client.protocol import Reader
from pandora.client.status import RECENT_LIMIT, RunStatus
from pandora.tests.test_cli import capture
from pandora.tests.test_fallback import DaemonCase, FakeWorker


class PublishedStatus(unittest.TestCase):
    def test_active_runs_survive_history_eviction_and_completion_moves_them_once(self):
        view = RunStatus()
        view.update({'id': 'old-active', 'started': 0, 'state': 'running'})
        for i in range(5000):
            view.update({'id': str(i), 'started': i + 1, 'state': 'done'})
        self.assertEqual(len(view.recent), RECENT_LIMIT)
        self.assertEqual([row['id'] for row in view.rows(2)], ['old-active', '4999', '4998'])
        view.update({'id': 'old-active', 'started': 0, 'state': 'done'})
        self.assertEqual(view.rows(0), [])
        self.assertEqual(len(view.rows(RECENT_LIMIT)), RECENT_LIMIT)

    def test_a_published_row_does_not_change_until_the_next_save(self):
        view = RunStatus()
        row = {'id': 'x', 'state': 'queued', 'attempts': []}
        view.update(row)
        row['attempts'].append({'remote': 'r1'})
        row['state'] = 'running'
        self.assertEqual(view.rows()[0]['attempts'], [])
        self.assertEqual(view.rows()[0]['state'], 'queued')
        view.update(row)
        self.assertEqual(view.rows()[0]['state'], 'running')

    def test_pressure_reports_age_without_sampling(self):
        clock = [10.0]
        reader = mock.Mock(return_value={'free_percent': 50})
        gate = Gate(clock=lambda: clock[0], reader=reader)
        self.assertIsNone(gate.state()['age_seconds'])
        self.assertTrue(gate.state()['stale'])
        reader.assert_not_called()
        gate.closed()
        self.assertFalse(gate.state()['stale'])
        clock[0] += 4
        self.assertEqual(gate.state()['age_seconds'], 4)
        self.assertTrue(gate.state()['stale'])
        reader.assert_called_once()


class StatusRequests(DaemonCase):
    def ps(self, *args):
        return capture(cli.main, ['--state', str(self.state), '--config',
                                  str(self.root / 'config.toml'), 'ps', *args])

    def test_concurrent_status_does_not_wait_for_or_multiply_a_stalled_probe(self):
        entered, release = threading.Event(), threading.Event()

        def probe():
            entered.set()
            release.wait(5)
            return {'free_percent': 50}

        reader = mock.Mock(side_effect=probe)
        self.daemon.gate.reader = reader
        self.daemon.gate.config['enabled'] = True
        sampling = threading.Thread(target=self.daemon.gate.closed)
        sampling.start()
        try:
            self.assertTrue(entered.wait(1))
            # No history reads, configuration reloads, or probes on this path.
            with mock.patch.object(Path, 'glob', side_effect=AssertionError('disk scan')), \
                    mock.patch.object(self.daemon, 'refresh', side_effect=AssertionError('reload')):
                with ThreadPoolExecutor(max_workers=8) as pool:
                    replies = list(pool.map(lambda _: cli.ask(
                        self.daemon.socket_path, {'op': 'ps'}, deadline_seconds=1), range(16)))
            self.assertTrue(all(reply['t'] == 'ps' for reply in replies))
            self.assertTrue(all(reply['pause']['age_seconds'] is None for reply in replies))
            self.assertFalse(release.is_set())
            reader.assert_called_once()
        finally:
            release.set()
            sampling.join(2)

    def test_saves_publish_the_lifecycle_and_limit_never_hides_active_runs(self):
        run = Run(self.state, 'active', {'argv': ['pnpm', 'check']},
                  on_save=self.daemon.status.update)
        run.save()
        self.assertEqual(json.loads(self.ps('--json', '--limit', '0')[1])['runs'][0]['state'],
                         'queued')
        run.state = 'running'
        run.save()
        self.assertEqual(json.loads(self.ps('--json', '--limit', '0')[1])['runs'][0]['state'],
                         'running')
        run.finish(0)
        # A late executor save after finish must not resurrect the active row.
        run.state = 'running'
        run.save()
        self.assertEqual(json.loads(self.ps('--json', '--limit', '0')[1])['runs'], [])
        self.assertEqual(json.loads(self.ps('--json')[1])['runs'][0]['exit_code'], 0)

    def test_a_real_submission_publishes_its_terminal_status(self):
        answer = self.call(['unit'])
        self.assertEqual(answer.exit, 0)
        rows = json.loads(self.ps('--json')[1])['runs']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['exit_code'], 0)

    def test_startup_rebuilds_recent_history_and_reconciles_interrupted_runs(self):
        self.daemon.stop()
        self.thread.join(2)
        for i in range(300):
            run = Run(self.state, 'old%03d' % i, {})
            run.started = i
            run.finish(0)
        run = Run(self.state, 'interrupted', {})
        run.lane = 'local'
        run.save()
        successor = Daemon(config_path=str(self.root / 'config.toml'))
        successor.worker_factory = FakeWorker
        self.addCleanup(successor.stop)
        successor.start()
        rows = successor.ps(RECENT_LIMIT)
        self.assertEqual(len(rows), RECENT_LIMIT)
        interrupted = next(row for row in rows if row['id'] == 'interrupted')
        self.assertEqual(interrupted['state'], 'infra_failed')
        self.assertEqual(interrupted['exit_code'], 70)
        self.assertEqual(rows[1]['id'], 'old299')

    def test_no_daemon_reports_unknown_and_never_reads_historical_rows(self):
        self.daemon.stop()
        self.thread.join(2)
        with mock.patch.object(Path, 'glob', side_effect=AssertionError('disk scan')):
            code, out, _ = self.ps('--json')
        self.assertEqual(code, 70)
        reply = json.loads(out)
        self.assertFalse(reply['daemon']['responding'])
        self.assertEqual(reply['runs'], [])


class RequestDeadline(unittest.TestCase):
    def test_missing_or_invalid_reply_is_unknown_not_idle(self):
        for reply in (None, {'t': 'error', 'msg': 'incompatible daemon'},
                      {'t': 'ps', 'data': None}):
            with self.subTest(reply=reply), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(cli, 'state_of', return_value=(Path(tmp), {})), \
                    mock.patch.object(cli, 'ask', return_value=reply):
                code, out, _ = capture(cli.main, ['ps', '--json'])
                self.assertEqual(code, 70)
                self.assertFalse(json.loads(out)['daemon']['responding'])

    def test_fragmented_response_cannot_extend_the_total_request_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'client.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(server.close)
            server.bind(path)
            server.listen(1)
            stop = threading.Event()

            def trickle():
                conn, _ = server.accept()
                with conn:
                    Reader(conn).line()
                    try:
                        while not stop.wait(.02):
                            conn.sendall(b' ')
                    except OSError:
                        pass

            thread = threading.Thread(target=trickle)
            thread.start()
            try:
                start = time.monotonic()
                with mock.patch.object(cli, 'PS_SECONDS', .15), \
                        mock.patch.object(cli, 'state_of', return_value=(Path(tmp), {})), \
                        mock.patch.object(Path, 'glob', side_effect=AssertionError('disk scan')):
                    code, out, _ = capture(cli.main, ['ps'])
                self.assertEqual(code, 70)
                self.assertIn('unresponsive', out)
                self.assertIn('run status is unknown', out)
                self.assertLess(time.monotonic() - start, 1)
            finally:
                stop.set()
                thread.join(2)
