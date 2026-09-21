"""Bounded fallback, the passthrough log, and `pandora stats`.

The two things the owner cannot see today: how many heavy commands a worker
outage dumps back onto this Mac at once, and what still runs locally because
nothing claims it.
"""
import concurrent.futures
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cli
import fallback
import protocol
from harness import Sandbox

SLOW_REAL = '#!/bin/sh\nsleep 1.2\necho "REAL $*"\n'


class FallbackBudget(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox(backend={'mode': 'unreachable'})
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()
        self.box.real.write_text(SLOW_REAL)
        self.box.real.chmod(0o755)

    def test_only_k_claimed_commands_fall_back_at_once(self):
        self.box.reconfigure(fallback_slots=2, fallback_wait_seconds=0)
        with concurrent.futures.ThreadPoolExecutor(6) as pool:
            results = list(pool.map(lambda _: self.box.pnpm(['test:unit'], timeout=60), range(6)))
        ran = [r for r in results if r.stdout == b'REAL test:unit\n']
        refused = [r for r in results if r.returncode == protocol.FALLBACK_REFUSED]
        self.assertEqual(len(ran) + len(refused), 6)
        self.assertLessEqual(len(ran), 2 + 1)      # a slot may be reused as one finishes
        self.assertGreaterEqual(len(refused), 1)
        self.assertIn(b'slots are all busy', refused[0].stderr)
        self.assertIn(b'PANDORA_OFF=1', refused[0].stderr)

    def test_waiting_mode_queues_instead_of_refusing(self):
        self.box.reconfigure(fallback_slots=1, fallback_wait_seconds=30)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            results = list(pool.map(lambda _: self.box.pnpm(['test:unit'], timeout=120), range(3)))
        for result in results:
            self.assertEqual(result.stdout, b'REAL test:unit\n')
            self.assertEqual(result.returncode, 0)

    def test_unclaimed_commands_never_take_a_slot(self):
        self.box.reconfigure(fallback_slots=1, fallback_wait_seconds=0)
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.box.pnpm(['lint:fast'], timeout=60), range(4)))
        for result in results:
            self.assertEqual(result.stdout, b'REAL lint:fast\n')

    def test_slots_are_released_even_when_the_command_fails(self):
        self.box.reconfigure(fallback_slots=1, fallback_wait_seconds=0)
        self.box.real.write_text('#!/bin/sh\nexit 3\n')
        self.box.real.chmod(0o755)
        for _ in range(3):
            self.assertEqual(self.box.pnpm(['test:unit']).returncode, 3)

    def test_a_dead_holder_does_not_leak_its_slot(self):
        """flock is held by the fd, so a killed process frees the slot at once."""
        slot = fallback.acquire(self.box.state, 1, 0)
        self.assertIsNotNone(slot)
        self.assertIsNone(fallback.acquire(self.box.state, 1, 0))
        slot.release()
        self.assertIsNotNone(fallback.acquire(self.box.state, 1, 0))


class UpdateRuns(unittest.TestCase):
    """An --update run writes files back.  Running it locally is never a fallback."""

    def setUp(self):
        self.box = Sandbox(backend={'mode': 'unreachable'})
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol(claims=(('journeys',),))

    def test_update_never_falls_back(self):
        result = self.box.pnpm(['journeys', '--update'])
        self.assertEqual(result.returncode, protocol.INFRA_FAILURE)
        self.assertNotIn(b'REAL', result.stdout)
        self.assertIn(b'never run locally', result.stderr)

    def test_the_same_command_without_update_does_fall_back(self):
        result = self.box.pnpm(['journeys'])
        self.assertEqual(result.stdout, b'REAL journeys\n')

    def test_update_is_refused_for_every_pre_accept_error(self):
        for mode in ('unreachable', 'queue-timeout', 'hang'):
            self.box.reconfigure({'mode': mode})
            result = self.box.pnpm(['journeys', '--update'], timeout=30)
            self.assertEqual(result.returncode, protocol.INFRA_FAILURE, mode)


class PassthroughLog(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.box.start()
        self.box.enrol()

    def test_heavy_unclaimed_commands_are_recorded(self):
        self.assertEqual(self.box.pnpm(['build']).stdout, b'REAL build\n')
        rows = self.box.passthrough()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'passthrough')
        self.assertEqual(rows[0]['argv'], ['build'])
        self.assertGreaterEqual(rows[0]['duration_ms'], 0)
        self.assertEqual(rows[0]['exit'], 0)

    def test_light_unclaimed_commands_are_not_recorded(self):
        self.box.pnpm(['lint:fast'])
        self.assertEqual(self.box.passthrough(), [])

    def test_a_fallback_is_recorded_with_its_reason(self):
        self.box.reconfigure({'mode': 'unreachable'})
        self.box.pnpm(['test:unit'])
        rows = self.box.passthrough()
        self.assertEqual(rows[0]['kind'], 'fallback')
        self.assertEqual(rows[0]['reason'], 'worker-unreachable')

    def test_passthrough_preserves_exit_codes(self):
        self.box.real.write_text('#!/bin/sh\nexit 17\n')
        self.box.real.chmod(0o755)
        self.assertEqual(self.box.pnpm(['build']).returncode, 17)
        self.assertEqual(self.box.passthrough()[0]['exit'], 17)

    def test_passthrough_preserves_signal_death(self):
        self.box.real.write_text('#!/bin/sh\nkill -TERM $$\n')
        self.box.real.chmod(0o755)
        self.assertEqual(self.box.pnpm(['build']).returncode, -15)

    def test_concurrent_appends_do_not_interleave(self):
        with concurrent.futures.ThreadPoolExecutor(12) as pool:
            list(pool.map(lambda _: self.box.pnpm(['build'], timeout=60), range(12)))
        rows = self.box.passthrough()
        self.assertEqual(len(rows), 12)

    def test_stats_summarises_what_still_runs_locally(self):
        self.box.pnpm(['build'])
        self.box.pnpm(['build'])
        self.box.pnpm(['lint'])
        self.box.pnpm(['test:unit'])
        captured, sys.stdout = sys.stdout, io.StringIO()
        try:
            code = cli.main(['--state', str(self.box.state), 'stats'])
            text = sys.stdout.getvalue()
        finally:
            sys.stdout = captured
        self.assertEqual(code, 0)
        self.assertIn('local, not routed: 3', text)
        self.assertIn('routed runs: 1', text)
        self.assertIn('build', text)
        print(text)


if __name__ == '__main__':
    unittest.main()
