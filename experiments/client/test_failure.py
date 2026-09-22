"""Side effect 4: the daemon is missing, dead, wedged, restarting or doubled.

Every case here asks the same question: was the command provably not executed?
If yes, run it locally.  If no, say so and stop.
"""
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import client as client_module
import protocol
from harness import Sandbox


class DaemonUnavailable(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def test_socket_missing_falls_back_locally(self):
        self.box.enrol()                       # no daemon started at all
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertIn(b'running locally instead', result.stderr)

    def test_socket_present_with_no_listener_falls_back_locally(self):
        self.box.enrol()
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(self.box.state / 'client.sock'))
        dead.close()                           # the file survives; nobody listens
        self.assertTrue((self.box.state / 'client.sock').exists())
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertIn(b'Connection refused', result.stderr)

    def test_a_permission_denied_connect_falls_back_locally(self):
        """Stands in for the sandbox case.

        notes/codex-sandbox-routing-2026-09-21.md measured that Codex's Seatbelt
        profile denies AF_UNIX connect with EPERM under `read-only` and under
        `workspace-write` without network, whatever directory the socket is in.
        Here the same errno is produced with a mode-0 directory, so the branch
        the sandbox would take is the branch under test.
        """
        self.box.start()
        self.box.enrol()
        walled = self.box.state / 'walled'
        walled.mkdir()
        (walled / 'client.sock').touch()
        walled.chmod(0o000)
        self.addCleanup(walled.chmod, 0o700)
        import enrolment
        marker = enrolment.parse((self.box.repo / '.git' / enrolment.MARKER).read_text())
        enrolment.write(self.box.repo / '.git', enrolment.render(
            socket_path=str(walled / 'client.sock'), repo='fake',
            claims=marker['claim'], heavy=marker['heavy'], strip_prefixes=marker['strip']))
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertIn(b'Permission denied', result.stderr)
        # The budget lives in the same unreachable directory.  The command must
        # still run, and the client must say the limit is not in force.
        self.assertIn(b'not limited', result.stderr)

    def test_a_wedged_daemon_is_abandoned_within_the_handshake_budget(self):
        self.box.start()
        self.box.enrol()
        self.box.reconfigure({'mode': 'hang'})
        started = time.monotonic()
        result = self.box.pnpm(['test:unit'])
        elapsed = time.monotonic() - started
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertLess(elapsed, 3.0)          # sh + python start-up dominates
        self.assertIn(b'did not answer within 300', result.stderr)

    def test_handshake_budget_is_300ms(self):
        self.assertEqual(client_module.HANDSHAKE_SECONDS, 0.3)

    def test_worker_unreachable_is_a_pre_accept_error(self):
        self.box.start()
        self.box.enrol()
        self.box.reconfigure({'mode': 'unreachable'})
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertIn(b'worker-unreachable', result.stderr)

    def test_queue_timeout_is_a_pre_accept_error(self):
        self.box.start()
        self.box.enrol()
        self.box.reconfigure({'mode': 'queue-timeout', 'accept_delay_ms': 50})
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'REAL test:unit\n')
        self.assertIn(b'queue-timeout', result.stderr)


class AfterAcceptance(unittest.TestCase):
    """Once accepted, the shim must never run the command a second time."""

    def test_accept_then_lose_the_connection_reattaches(self):
        box = Sandbox(backend={'mode': 'accept-then-drop', 'delay_ms': 1500,
                               'stdout': ['one\n', 'two\n']})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        result = box.pnpm(['test:unit'], timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'one\ntwo\n')
        self.assertNotIn(b'REAL', result.stdout)

    def test_reattach_does_not_duplicate_already_streamed_output(self):
        box = Sandbox(backend={'mode': 'accept-then-drop', 'delay_ms': 800,
                               'stdout': ['a\n', 'b\n', 'c\n']})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        result = box.pnpm(['test:unit'], timeout=60)
        self.assertEqual(result.stdout, b'a\nb\nc\n')

    def test_daemon_restart_mid_run_is_survived_by_reattach(self):
        box = Sandbox(backend={'mode': 'slow', 'delay_ms': 3000,
                               'stdout': ['a\n', 'b\n', 'c\n', 'd\n', 'e\n', 'f\n']})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        proc = box.popen(['test:unit'])
        deadline = time.time() + 5
        while time.time() < deadline and not list((box.state / 'runs').glob('*/meta.json')):
            time.sleep(0.05)
        time.sleep(0.6)
        box.stop()                              # the daemon goes away mid-run
        box.start()                             # ... and comes back
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out, b'a\nb\nc\nd\ne\nf\n')
        self.assertNotIn(b'REAL', out)

    def test_a_daemon_that_never_returns_ends_as_infrastructure_failure(self):
        box = Sandbox(backend={'mode': 'accept-then-drop', 'delay_ms': 60000,
                               'stdout': ['a\n'] * 40})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        proc = box.popen(['test:unit'])
        time.sleep(0.8)
        box.stop()                              # gone for good
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, protocol.INFRA_FAILURE)
        self.assertNotIn(b'REAL', out)
        self.assertIn(b'may still be executing', err)


class SingleOwner(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def test_a_second_daemon_refuses_the_lock(self):
        self.box.start()
        second = subprocess.run(
            [sys.executable, '-B', str(HERE / 'daemon.py'), '--state', str(self.box.state)],
            capture_output=True, timeout=30)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn(b'already running', second.stderr)
        self.assertEqual(self.box.ask({'v': protocol.VERSION, 'op': 'ping'})['t'], 'pong')

    def test_a_stale_socket_is_cleaned_up_on_start(self):
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(self.box.state / 'client.sock'))
        dead.close()
        self.box.start()
        self.assertEqual(self.box.ask({'v': protocol.VERSION, 'op': 'ping'})['t'], 'pong')

    def test_the_lock_is_released_when_the_daemon_exits(self):
        self.box.start()
        self.box.stop()
        self.box.start()
        self.assertEqual(self.box.ask({'v': protocol.VERSION, 'op': 'ping'})['t'], 'pong')


if __name__ == '__main__':
    unittest.main()
