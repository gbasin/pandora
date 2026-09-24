"""A daemon still starting is already a daemon: its signals and its lock.

Two things went wrong live on 2026-09-24. A SIGUSR1 sent to a daemon still
starting ended it, because the handler was registered after `start()`. And a
daemon launchd started while its predecessor was still stopping (`kickstart
-k` does not wait) exited "already running", so launchd relaunched it every
ten seconds and the restart took minutes.
"""
import contextlib
import fcntl
import io
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import daemon as daemon_module


class Startup(unittest.TestCase):
    def test_the_handlers_are_in_place_before_the_daemon_is_built(self):
        order = []

        class Recorder:
            def __init__(self, *args, **kwargs):
                order.append('built')
                self.stopping = kwargs['stopping']
                self.socket_path, self.code = 's', 'c' * 12
                self.config = {'worker': {'host': None}}

            def start(self):
                order.append('started')
                return self

            def serve(self):
                order.append('served')

            def stop(self):
                order.append('stopped')

        handlers = {}
        with mock.patch.object(daemon_module, 'Daemon', Recorder), \
                mock.patch.object(daemon_module.signal, 'signal',
                                  lambda number, handler: (order.append(number),
                                                           handlers.setdefault(number, handler))), \
                mock.patch.object(daemon_module.faulthandler, 'register',
                                  lambda number, **_: order.append(number)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(daemon_module.main([]), 0)
        self.assertEqual(order[:3], [signal.SIGTERM, signal.SIGUSR1, 'built'], order)
        self.assertEqual(order[-1], 'stopped')

    def test_a_sigterm_while_starting_is_kept_and_the_daemon_never_serves(self):
        stopping = threading.Event()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        config = Path(home.name) / 'config.toml'
        config.write_text('[client]\nstate = "%s"\n[notify]\nenabled = false\n'
                          '[local.pause]\nenabled = false\n' % (Path(home.name) / 'state'))
        daemon = daemon_module.Daemon(config_path=str(config), stopping=stopping)
        stopping.set()                     # what the handler does, mid-start
        daemon.start()
        self.addCleanup(daemon.stop)
        served = threading.Thread(target=daemon.serve, daemon=True)
        served.start()
        served.join(timeout=5)
        self.assertFalse(served.is_alive(), 'a daemon told to stop while starting served')


class TheLock(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = Path(home.name) / 'state'
        config = Path(home.name) / 'config.toml'
        config.write_text('[client]\nstate = "%s"\n[notify]\nenabled = false\n'
                          '[local.pause]\nenabled = false\n' % self.state)
        self.daemon = daemon_module.Daemon(config_path=str(config))
        self.addCleanup(lambda: self.daemon.lock_handle and self.daemon.lock_handle.close())

    def hold(self, pid):
        """The predecessor: it holds the lock and has written its pid."""
        handle = (self.state / 'daemon.lock').open('a+')
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write('%d\n' % pid)
        handle.flush()
        self.addCleanup(handle.close)
        return handle

    def test_it_waits_for_a_predecessor_that_is_still_stopping(self):
        predecessor = self.hold(4321)
        threading.Timer(0.6, predecessor.close).start()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            started = time.monotonic()
            self.daemon.acquire_lock(wait=10, poll=0.05)
        self.assertGreater(time.monotonic() - started, 0.5)
        self.assertEqual(err.getvalue().count('waiting for pid 4321 to stop'), 1, err.getvalue())
        self.assertEqual((self.state / 'daemon.lock').read_text().split(), [str(os.getpid())])

    def test_it_gives_up_when_the_holder_never_lets_go(self):
        self.hold(4321)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            self.daemon.acquire_lock(wait=0.3, poll=0.05)
        self.assertIn('already running', str(stop.exception))

    def test_a_stop_while_it_waits_ends_the_wait(self):
        self.hold(4321)
        threading.Timer(0.2, self.daemon.stopping.set).start()
        started = time.monotonic()
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.daemon.acquire_lock(wait=30, poll=0.05)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == '__main__':
    unittest.main()
