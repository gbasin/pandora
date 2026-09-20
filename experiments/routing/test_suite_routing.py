import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import route
from route import suite_environment_error, suite_shard_count


class SuiteRoutingTests(unittest.TestCase):
    def test_suite_shards_are_bounded(self):
        self.assertEqual(suite_shard_count('1'), 1)
        self.assertEqual(suite_shard_count('32'), 32)
        for value in ('0', '33', 'four', True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '1 through 32'):
                    suite_shard_count(value)

    def test_existing_suite_environment_is_rejected_instead_of_being_ignored(self):
        self.assertIsNone(suite_environment_error({
            'JOURNEY_FILTER': '', 'JOURNEY_SHARD': '', 'JOURNEY_CONCURRENCY': '',
            'JOURNEY_REPLAY': '', 'JOURNEY_TEMPLATE': '', 'IKE_WORLD': '',
        }))
        message = suite_environment_error({'JOURNEY_FILTER': 'S0-*', 'IKE_WORLD': 'staging'})
        self.assertIn('JOURNEY_FILTER, IKE_WORLD', message)
        self.assertIn('pnpm journeys [--keep-going]', message)
        self.assertIn('No validation started.', message)

    def test_route_passes_a_private_run_request_to_warm(self):
        captured = []

        class Child:
            def wait(self):
                return 64

        def popen(command, **_kwargs):
            captured.append(command)
            return Child()

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'
            environment = dict(os.environ, PANDORA_HOST='host', PANDORA_STATE=str(state),
                               PANDORA_SESSION='session', PANDORA_SUITE_SHARDS='6')
            with patch.object(sys, 'argv', ['route.py', 'journeys', '--keep-going']), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch.object(route.subprocess, 'check_output', return_value=str(Path.cwd())), \
                 patch.object(route.subprocess, 'Popen', side_effect=popen):
                self.assertEqual(route.main(), 64)
            self.assertIn('--workflow', captured[0])
            self.assertEqual(captured[0][captured[0].index('--workflow') + 1], 'suite-run')
            request = Path(captured[0][captured[0].index('--suite-request') + 1])
            self.assertTrue(request.is_file())
            self.assertEqual(request.parent.parent, state)
            self.assertEqual(json.loads(request.read_text()), {
                'action': 'run', 'shard_count': 6, 'selection': None, 'keep_going': True,
            })
            output = Path(captured[0][captured[0].index('--output') + 1])
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
