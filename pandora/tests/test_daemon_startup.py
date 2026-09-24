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
        err = io.StringIO()
        with mock.patch.object(daemon_module, 'Daemon', Recorder), \
                mock.patch.object(daemon_module.signal, 'signal',
                                  lambda number, handler: (order.append(number),
                                                           handlers.setdefault(number, handler))), \
                mock.patch.object(daemon_module.faulthandler, 'register',
                                  lambda number, **_: order.append(number)), \
                mock.patch.object(daemon_module, 'raise_accept_qos',
                                  lambda: order.append('qos') or True), \
                contextlib.redirect_stderr(err):
            self.assertEqual(daemon_module.main([]), 0)
        self.assertEqual(order[:4], [signal.SIGTERM, signal.SIGUSR1, 'qos', 'built'], order)
        self.assertEqual(order[-1], 'stopped')
        self.assertIn('accept thread QoS user-interactive', err.getvalue())

    def test_a_sigterm_while_starting_is_kept_and_the_daemon_never_serves(self):
        stopping = threading.Event()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        config = Path(home.name) / 'config.toml'
        config.write_text('[client]\nstate = "%s"\n[notify]\nenabled = false\n'
                          '[local.pause]\nenabled = false\n' % (Path(home.name) / 'state'))
        daemon = daemon_module.Daemon(config_path=str(config), stopping=stopping)
        self.addCleanup(daemon.budget.admission.store.close)
        stopping.set()                     # what the handler does, mid-start
        daemon.start()
        self.addCleanup(daemon.stop)
        served = threading.Thread(target=daemon.serve, daemon=True)
        served.start()
        served.join(timeout=5)
        self.assertFalse(served.is_alive(), 'a daemon told to stop while starting served')


class AcceptThreadQoS(unittest.TestCase):
    def libsystem(self, returns=0):
        calls = []

        class Function:
            def __call__(self, qos, relative):
                calls.append((qos, relative))
                return returns

        class Library:
            pthread_set_qos_class_self_np = Function()

        opened = []
        return calls, opened, lambda path: opened.append(path) or Library()

    def setUp(self):
        self.addCleanup(daemon_module.RAISED.clear)

    def test_on_darwin_the_call_is_made_with_user_interactive(self):
        calls, opened, cdll = self.libsystem()
        self.assertTrue(daemon_module.raise_accept_qos(platform='darwin', cdll=cdll))
        self.assertEqual(calls, [(0x21, 0)])
        self.assertEqual(opened, ['/usr/lib/libSystem.B.dylib'])
        self.assertTrue(daemon_module.RAISED.is_set())

    def test_elsewhere_nothing_is_loaded(self):
        calls, opened, cdll = self.libsystem()
        self.assertFalse(daemon_module.raise_accept_qos(platform='linux', cdll=cdll))
        self.assertEqual((calls, opened), ([], []))
        self.assertFalse(daemon_module.RAISED.is_set())

    def test_a_refusal_or_a_missing_library_is_silent(self):
        calls, _, cdll = self.libsystem(returns=22)
        self.assertFalse(daemon_module.raise_accept_qos(platform='darwin', cdll=cdll))

        def missing(path):
            raise OSError('no libSystem')
        self.assertFalse(daemon_module.raise_accept_qos(platform='darwin', cdll=missing))
        self.assertFalse(daemon_module.RAISED.is_set())

    def test_a_handler_thread_steps_back_to_the_default(self):
        daemon_module.RAISED.set()
        asked = []
        with mock.patch.object(daemon_module, 'set_qos', asked.append):
            daemon = object.__new__(daemon_module.Daemon)
            daemon.runs_lock, daemon.handlers = threading.Lock(), set()
            daemon._handle = lambda conn: None
            daemon.handle(None)
        self.assertEqual(asked, [daemon_module.QOS_CLASS_DEFAULT])


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
        # Its SQLite handle, closed here: collected later, its ResourceWarning
        # landed in another test's captured stderr.
        self.addCleanup(self.daemon.budget.admission.store.close)

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

    def test_a_stop_while_it_waits_ends_the_wait_quietly(self):
        self.hold(4321)
        threading.Timer(0.2, self.daemon.stopping.set).start()
        started = time.monotonic()
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            self.daemon.acquire_lock(wait=30, poll=0.05)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(stop.exception.code, 0)
        self.assertNotIn('already running', err.getvalue())
        self.assertIn('stopped while waiting', err.getvalue())


if __name__ == '__main__':
    unittest.main()
