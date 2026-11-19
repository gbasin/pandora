"""A freeze or ship that does not come back must not hold its row forever.

Seen on 2026-11-18: a remote run sat in `freezing` for 18 hours. Its freeze ran
in the connection thread and never returned, so nothing looked at the row:
`pandora cancel` set a flag nobody read, the caller's leaving went unnoticed,
the worktree was deleted under it, and every drained restart waited on it. The
daemon is real here; the worker is fake and blocks inside the phase a test
names until the test lets it go.
"""
import base64
import json
import shutil
import socket
import threading
import time
import unittest

from pandora.client.protocol import Reader, VERSION, dump
from pandora.tests.test_fallback import DaemonCase, FakeWorker, Submission


class HangingWorker(FakeWorker):
    """Enters each phase in turn and blocks in `hang_in` until `release` is set."""

    hang_in = 'freeze'
    release = None
    reached = []
    raises_in_freeze = None
    stop = None

    def submit(self, *, phase=None, stop=None, **kwargs):
        HangingWorker.stop = stop
        for name in ('freeze', 'ship', 'submit'):
            phase(name)
            HangingWorker.reached.append(name)
            if name == 'freeze' and HangingWorker.raises_in_freeze is not None:
                raise HangingWorker.raises_in_freeze
            if name == HangingWorker.hang_in:
                HangingWorker.release.wait(30)
        return Submission('r-hang')


class PreAcceptCase(DaemonCase):
    def setUp(self):
        super().setUp()
        HangingWorker.hang_in, HangingWorker.reached = 'freeze', []
        HangingWorker.raises_in_freeze = None
        HangingWorker.release = threading.Event()
        # Let the abandoned thread run out when the test ends.
        self.addCleanup(HangingWorker.release.set)
        worker = HangingWorker('fake@nowhere')
        self.daemon.worker_for = lambda repo: worker
        self.daemon.PRE_ACCEPT_POLL = 0.02

    def submit(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        self.addCleanup(sock.close)
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                           'argv': ['pnpm', 'surface'], 'env': {}, 'tty': False}))
        return sock

    def row(self, phase=None, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in (self.state / 'runs').glob('*/meta.json'):
                try:
                    row = json.loads(path.read_text())
                except ValueError:
                    continue
                if phase is None or row.get('phase') == phase:
                    return row
            time.sleep(0.01)
        self.fail('no row in phase %s' % phase)

    def settled(self, run_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())
            if row['state'] not in ('queued', 'running'):
                return row
            time.sleep(0.01)
        self.fail('run %s never settled' % run_id)

    @staticmethod
    def last_frame(sock):
        reader = Reader(sock)
        while True:
            frame = reader.line()
            if frame is None or frame.get('t') in ('error', 'exit', 'accepted'):
                return frame

    def cancel(self, run_id):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'cancel', 'run': run_id}))
        answer = Reader(sock).line()
        sock.close()
        return answer


class ACancelBeforeAcceptedSettlesTheRun(PreAcceptCase):
    def test_a_cancel_during_a_hung_freeze_ends_it_cancelled_at_130(self):
        sock = self.submit()
        run_id = self.row('freeze')['id']
        self.assertEqual(self.cancel(run_id)['t'], 'ok')
        frame = self.last_frame(sock)
        self.assertEqual((frame['t'], frame['code'], frame['exit']), ('error', 'canceled', 130))
        self.assertIn('during freeze', frame['msg'])
        row = self.settled(run_id)
        self.assertEqual((row['state'], row['exit_code']), ('cancelled', 130))
        # The freeze, when it does come back, never reaches the engine.
        HangingWorker.release.set()
        time.sleep(0.2)
        self.assertEqual(HangingWorker.reached, ['freeze'])
        self.assertIsNone(self.daemon.live(run_id))

    def test_a_cancel_during_ship_ends_it_too(self):
        HangingWorker.hang_in = 'ship'
        sock = self.submit()
        run_id = self.row('ship')['id']
        self.cancel(run_id)
        frame = self.last_frame(sock)
        self.assertEqual((frame['code'], frame['exit']), ('canceled', 130))
        # The transfer is told to stop at once, not left to finish its upload.
        self.assertTrue(HangingWorker.stop.is_set())
        HangingWorker.release.set()
        time.sleep(0.2)
        self.assertNotIn('submit', HangingWorker.reached)

    def test_a_cancel_once_the_engine_has_the_request_waits_for_its_answer(self):
        # In `submit` the engine may already have started the run: abandoning
        # it here could run the command with nobody following.
        HangingWorker.hang_in = 'submit'
        sock = self.submit()
        run_id = self.row('submit')['id']
        self.cancel(run_id)
        time.sleep(0.3)
        self.assertEqual(self.row()['state'], 'queued')
        HangingWorker.release.set()
        self.assertEqual(self.last_frame(sock)['t'], 'accepted')


class AFreezeThatCannotProceedFails(PreAcceptCase):
    def test_a_worktree_deleted_under_a_hung_freeze_fails_with_the_reason(self):
        sock = self.submit()
        run_id = self.row('freeze')['id']
        shutil.rmtree(self.repo)
        frame = self.last_frame(sock)
        self.assertEqual((frame['code'], frame['exit']), ('worktree-gone', 70))
        self.assertIn('no longer exists', frame['msg'])
        self.assertEqual(self.settled(run_id)['state'], 'infra_failed')

    def test_a_freeze_past_its_limit_fails_with_the_reason(self):
        self.daemon.FREEZE_LIMIT = 0.3
        sock = self.submit()
        run_id = self.row('freeze')['id']
        frame = self.last_frame(sock)
        self.assertEqual((frame['code'], frame['exit']), ('freeze-stalled', 70))
        self.assertIn('did not finish', frame['msg'])
        row = self.settled(run_id)
        self.assertEqual((row['state'], row['exit_code']), ('infra_failed', 70))
        self.assertIn('did not finish', self.log_text(run_id))

    def test_a_caller_that_leaves_during_a_hung_freeze_is_withdrawn(self):
        sock = self.submit()
        run_id = self.row('freeze')['id']
        sock.close()
        row = self.settled(run_id)
        self.assertEqual(row['state'], 'withdrawn')
        HangingWorker.release.set()
        time.sleep(0.2)
        self.assertEqual(HangingWorker.reached, ['freeze'])

    def test_a_freeze_thread_that_dies_closes_the_row(self):
        HangingWorker.raises_in_freeze = RuntimeError('freeze thread died')
        sock = self.submit()
        frame = self.last_frame(sock)
        self.assertEqual((frame['code'], frame['exit']), ('pre-accept-failed', 70))
        self.assertIn('freeze thread died', frame['msg'])
        row = self.settled(self.row()['id'])
        self.assertEqual(row['state'], 'infra_failed')

    def log_text(self, run_id):
        text = []
        for line in (self.state / 'runs' / run_id / 'log').read_text().splitlines():
            frame = json.loads(line)
            if frame.get('b64'):
                text.append(base64.b64decode(frame['b64']).decode())
        return ''.join(text)


if __name__ == '__main__':
    unittest.main()
