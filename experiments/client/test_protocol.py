"""Side effect 6 and the protocol half of 4: who may talk, and in what version."""
import os
from pathlib import Path
import socket
import stat
import sys
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import protocol
from protocol import dump
from harness import Sandbox


class Handshake(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_ping_reports_version_and_backend(self):
        frame = self.box.ask({'v': protocol.VERSION, 'op': 'ping'})
        self.assertEqual(frame['t'], 'pong')
        self.assertEqual(frame['backend'], 'ok')

    def test_version_skew_is_refused_before_acceptance(self):
        frame = self.box.ask({'v': protocol.VERSION + 99, 'op': 'run', 'argv': ['pnpm', 'test:unit']})
        self.assertEqual(frame['t'], 'error')
        self.assertEqual(frame['code'], 'version')
        self.assertIn('version', protocol.PRE_ACCEPT_ERRORS)

    def test_a_shim_from_the_future_falls_back_rather_than_failing(self):
        """A version-skewed shim must still run the user's command."""
        import client
        original = protocol.VERSION
        try:
            protocol.VERSION = original + 99
            code = client.main(['--sock', str(self.box.state / 'client.sock'),
                                '--real', str(self.box.real), '--', 'test:unit'])
        finally:
            protocol.VERSION = original
        self.assertEqual(code, 0)

    def test_unknown_op_is_refused(self):
        frame = self.box.ask({'v': protocol.VERSION, 'op': 'teleport'})
        self.assertEqual(frame['code'], 'rejected')

    def test_unclaimed_argv_is_refused_before_acceptance(self):
        frame = self.box.ask({'v': protocol.VERSION, 'op': 'run', 'argv': ['pnpm', 'lint:fast'],
                              'cwd': str(self.box.repo)})
        self.assertEqual(frame['t'], 'error')
        self.assertEqual(frame['code'], 'rejected')


class LocalCallerAuth(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_state_directory_is_0700(self):
        mode = stat.S_IMODE(os.stat(self.box.state).st_mode)
        self.assertEqual(mode, 0o700)

    def test_socket_is_0600(self):
        mode = stat.S_IMODE(os.stat(self.box.state / 'client.sock').st_mode)
        self.assertEqual(mode, 0o600)

    def test_peer_credentials_are_readable_on_this_platform(self):
        """LOCAL_PEERCRED on macOS, SO_PEERCRED on Linux.  If this returns None
        the uid check silently does nothing, so the test asserts it works."""
        import daemon as daemon_module
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.box.state / 'client.sock'))
        try:
            self.assertEqual(daemon_module.peer_uid(sock), os.getuid())
        finally:
            sock.close()

    def test_token_is_enforced_when_configured(self):
        self.box.reconfigure(require_token=True, token='secret')
        frame = self.box.ask({'v': protocol.VERSION, 'op': 'ping'})
        self.assertEqual(frame['code'], 'unauthorized')
        frame = self.box.ask({'v': protocol.VERSION, 'op': 'ping', 'token': 'secret'})
        self.assertEqual(frame['t'], 'pong')

    def test_a_wrong_token_still_lets_the_command_run_locally(self):
        self.box.reconfigure(require_token=True, token='secret')
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertIn(b'unauthorized', result.stderr)


class CancelDetachAttach(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(backend={'mode': 'slow', 'delay_ms': 4000,
                                    'stdout': ['a\n', 'b\n', 'c\n', 'd\n']})
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def _run_id(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            metas = sorted((self.box.state / 'runs').glob('*/meta.json'))
            if metas:
                return metas[-1].parent.name
            time.sleep(0.05)
        self.fail('no run appeared')

    def test_client_disconnect_detaches_and_the_run_survives(self):
        proc = self.box.popen(['test:unit'])
        run = self._run_id()
        time.sleep(0.4)
        proc.kill()                              # SIGKILL: the harshest disconnect
        proc.wait(5)
        deadline = time.time() + 15
        while time.time() < deadline:
            frame = self.box.ask({'v': protocol.VERSION, 'op': 'ping'})
            import json
            meta = json.loads((self.box.state / 'runs' / run / 'meta.json').read_text())
            if meta['state'] == 'done':
                self.assertEqual(meta['exit_code'], 0)
                return
            time.sleep(0.1)
        self.fail('run did not finish after the client was killed')

    def test_wait_reattaches_to_a_detached_run_and_replays_from_the_start(self):
        proc = self.box.popen(['test:unit'])
        run = self._run_id()
        time.sleep(0.3)
        proc.kill()
        proc.wait(5)
        import cli
        code = cli.main(['--state', str(self.box.state), 'wait', run])
        self.assertEqual(code, 0)

    def test_explicit_cancel_stops_the_run(self):
        proc = self.box.popen(['test:unit'])
        run = self._run_id()
        time.sleep(0.4)
        proc.send_signal(2)                      # SIGINT, as Ctrl-C in the agent's shell
        code = proc.wait(15)
        self.assertEqual(code, 130)
        import json
        meta = json.loads((self.box.state / 'runs' / run / 'meta.json').read_text())
        self.assertEqual(meta['state'], 'cancelled')

    def test_sigterm_detaches_and_names_the_run(self):
        proc = self.box.popen(['test:unit'])
        run = self._run_id()
        time.sleep(0.4)
        proc.send_signal(15)
        proc.wait(15)
        self.assertIn(b'pandora wait ' + run.encode(), proc.stderr.read())
        import json
        deadline = time.time() + 15
        while time.time() < deadline:
            meta = json.loads((self.box.state / 'runs' / run / 'meta.json').read_text())
            if meta['state'] == 'done':
                return
            time.sleep(0.1)
        self.fail('SIGTERM cancelled the run instead of detaching')


if __name__ == '__main__':
    unittest.main()
