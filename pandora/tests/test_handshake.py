"""Pre-accept: a slow freeze is not a dead daemon, and a caller who left gets nothing run."""
import json
import socket
import time
import unittest

from pandora.client import daemon as daemon_module
from pandora.client.protocol import VERSION, dump
from pandora.tests.test_fallback import DaemonCase, FakeWorker, Submission


class SlowWorker(FakeWorker):
    """Takes `delay` seconds to freeze, ship and submit, like a loaded Mac does."""
    delay = 2.0
    canceled = []

    def submit(self, **kwargs):
        time.sleep(SlowWorker.delay)
        return Submission('r-slow')

    def cancel(self, run_id):
        SlowWorker.canceled.append(run_id)
        return {'ok': True}


class HandshakeTest(DaemonCase):
    def setUp(self):
        super().setUp()
        SlowWorker.canceled = []
        self.daemon.worker_factory = SlowWorker
        with self.daemon.workers_lock:   # the health thread may be building one
            self.daemon.workers.clear()
        self.every = daemon_module.Heartbeat.EVERY
        daemon_module.Heartbeat.EVERY = 0.1
        self.addCleanup(setattr, daemon_module.Heartbeat, 'EVERY', self.every)

    def test_a_submission_slower_than_the_silence_timeout_is_still_accepted(self):
        # The client gives up after 1 s of silence; the submission takes 2 s.
        answer = self.call(['pnpm', 'unit'], timeout=1.0)
        self.assertIsNotNone(answer.accepted, answer.error)
        self.assertEqual(answer.accepted['remote'], 'r-slow')
        self.assertEqual(SlowWorker.canceled, [])

    def test_a_caller_that_left_before_accepted_gets_its_submission_withdrawn(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                           'argv': ['pnpm', 'unit'], 'env': {}, 'tty': False}))
        time.sleep(0.3)
        sock.close()                       # what a shim that timed out does
        deadline = time.monotonic() + 10
        while not SlowWorker.canceled and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(SlowWorker.canceled, ['r-slow'])
        # The row is finished just after the cancel returns; wait for it rather
        # than race it.
        while time.monotonic() < deadline:
            metas = [json.loads(path.read_text())
                     for path in (self.state / 'runs').glob('*/meta.json')]
            if all(meta['state'] != 'queued' for meta in metas):
                break
            time.sleep(0.05)
        self.assertEqual([(m['state'], m['remote']) for m in metas], [('withdrawn', 'r-slow')])


if __name__ == '__main__':
    unittest.main()
