"""Side effect 5: many clients at once, big output, and faithful exit status."""
import concurrent.futures
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import Sandbox

CONCURRENT = 20
BIG = 50 * 1024 * 1024


class Concurrency(unittest.TestCase):
    def test_twenty_simultaneous_invocations_each_get_their_own_run(self):
        box = Sandbox(backend={'mode': 'slow', 'delay_ms': 200,
                               'stdout': ['line-1\n', 'line-2\n']})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        with concurrent.futures.ThreadPoolExecutor(CONCURRENT) as pool:
            results = list(pool.map(lambda _: box.pnpm(['test:unit'], timeout=120),
                                    range(CONCURRENT)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b'line-1\nline-2\n')
        runs = list((box.state / 'runs').glob('*/meta.json'))
        self.assertEqual(len(runs), CONCURRENT)

    def test_concurrent_clients_do_not_cross_output(self):
        """Each run has its own log file, so interleaving across runs is impossible."""
        box = Sandbox(backend={'mode': 'interleave', 'pairs': 6, 'delay_ms': 100})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: box.pnpm(['test:unit'], timeout=120), range(8)))
        expected = b''.join(b'out-%d\n' % index for index in range(6))
        for result in results:
            self.assertEqual(result.stdout, expected)


class LargeOutput(unittest.TestCase):
    def test_fifty_megabytes_streams_without_loss(self):
        box = Sandbox(backend={'mode': 'bytes', 'bytes': BIG, 'chunk': 65536})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        started = time.monotonic()
        result = box.pnpm(['test:unit'], timeout=300)
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr[-500:])
        self.assertEqual(len(result.stdout), BIG)
        self.assertEqual(result.stdout.count(b'\n'), BIG // 65536)
        print('\n50MB in %.2fs (%.1f MB/s)' % (elapsed, BIG / elapsed / 1e6))

    def test_daemon_memory_stays_flat_across_a_large_run(self):
        """The run log is a file; the daemon copies byte ranges out of it."""
        box = Sandbox(backend={'mode': 'bytes', 'bytes': BIG, 'chunk': 65536})
        self.addCleanup(box.close)
        box.start()
        box.enrol()
        before = _rss(box.proc.pid)
        box.pnpm(['test:unit'], timeout=300)
        after = _rss(box.proc.pid)
        print('\ndaemon RSS %.1f MB -> %.1f MB' % (before / 1e6, after / 1e6))
        self.assertLess(after - before, 40e6)


def _rss(pid):
    out = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or 0) * 1024


class ExitStatus(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_every_exit_code_survives(self):
        for code in (0, 1, 2, 42, 70, 75, 127, 128, 200, 254, 255):
            self.box.reconfigure({'mode': 'ok', 'exit_code': code, 'stdout': []})
            result = self.box.pnpm(['test:unit'])
            self.assertEqual(result.returncode, code, 'exit code %d' % code)

    def test_a_worker_signal_death_is_reproduced_as_a_signal_death(self):
        for number in (9, 15, 6):
            self.box.reconfigure({'mode': 'signal', 'signal': number, 'stdout': []})
            result = self.box.pnpm(['test:unit'])
            self.assertEqual(result.returncode, -number,
                             'signal %d became %d' % (number, result.returncode))

    def test_stdout_and_stderr_keep_their_streams(self):
        self.box.reconfigure({'mode': 'ok', 'stdout': ['to-out\n'], 'stderr': ['to-err\n'],
                              'exit_code': 0})
        result = self.box.pnpm(['test:unit'])
        self.assertEqual(result.stdout, b'to-out\n')
        self.assertEqual(result.stderr, b'to-err\n')

    def test_interleaving_order_within_one_run_is_preserved(self):
        """Both streams share one fd here, so the order is the worker's order."""
        self.box.reconfigure({'mode': 'interleave', 'pairs': 50})
        result = subprocess.run(['pnpm', 'test:unit'], cwd=str(self.box.repo),
                                env=self.box.env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=60)
        expected = [text for index in range(50)
                    for text in ('out-%d' % index, 'err-%d' % index)]
        self.assertEqual(result.stdout.decode().split(), expected)


class StandardInput(unittest.TestCase):
    """A routed command has no TTY.  The shim must say so, not pretend."""

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_tty_flag_is_reported_to_the_daemon(self):
        result = subprocess.run(['pnpm', 'test:unit'], cwd=str(self.box.repo),
                                env=self.box.env(), capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0)

    def test_closed_stdin_does_not_hang_the_client(self):
        proc = subprocess.Popen(['pnpm', 'test:unit'], cwd=str(self.box.repo),
                                env=self.box.env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.stdin.close()
        out, _ = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(out, b'hello\n')


if __name__ == '__main__':
    unittest.main()
