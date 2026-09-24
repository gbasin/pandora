"""A daemon still starting is already a daemon: its signals and its lock.

Two things went wrong live on 2026-09-24. A SIGUSR1 sent to a daemon still
starting ended it, because the handler was registered after `start()`. And a
daemon launchd started while its predecessor was still stopping (`kickstart
-k` does not wait) exited "already running", so launchd relaunched it every
ten seconds and the restart took minutes.
"""
import io
import contextlib
import signal
import tempfile
import threading
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


if __name__ == '__main__':
    unittest.main()
