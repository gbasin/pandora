import json
import os
import hashlib
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
        self.assertIn('pnpm journeys [--update] [--keep-going]', message)
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
                'action': 'run', 'shard_count': 6, 'selection': None, 'keep_going': True, 'update': False,
            })
            output = Path(captured[0][captured[0].index('--output') + 1])
            self.assertFalse(output.exists())

    def test_route_passes_a_private_validation_request_to_warm(self):
        captured = []

        class Child:
            def wait(self): return 64

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'
            environment = dict(os.environ, PANDORA_HOST='host', PANDORA_STATE=str(state),
                               PANDORA_SESSION='session')
            with patch.object(sys, 'argv', ['route.py', 'test:postgres', 'api', '--foundation-only']), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch.object(route.subprocess, 'check_output', return_value=str(Path.cwd())), \
                 patch.object(route.subprocess, 'Popen', side_effect=lambda command, **_kwargs: captured.append(command) or Child()):
                self.assertEqual(route.main(), 64)
            command = captured[0]
            self.assertEqual(command[command.index('--workflow') + 1], 'validation')
            request = Path(command[command.index('--validation-request') + 1])
            self.assertEqual(json.loads(request.read_text()), {
                'version': 1, 'suite': 'postgres', 'args': ['api', '--foundation-only'],
            })

    def test_surface_route_passes_private_surface_run_request_to_warm(self):
        captured = []

        class Child:
            def wait(self): return 64

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / 'state'
            environment = dict(os.environ, PANDORA_HOST='host', PANDORA_STATE=str(state),
                               PANDORA_SESSION='session', PANDORA_SUITE_SHARDS='6')
            argv = ['route.py', 'test:surface', 'desk', 'pipeline.spec.ts', '--keep-going']
            with patch.object(sys, 'argv', argv), patch.dict(os.environ, environment, clear=True), \
                 patch.object(route.subprocess, 'check_output', return_value=str(Path.cwd())), \
                 patch.object(route.subprocess, 'Popen', side_effect=lambda command, **_kwargs: captured.append(command) or Child()):
                self.assertEqual(route.main(), 64)
            command = captured[0]
            self.assertEqual(command[command.index('--workflow') + 1], 'surface-run')
            request = Path(command[command.index('--surface-suite-request') + 1])
            self.assertEqual(json.loads(request.read_text()), {
                'action': 'run', 'app': 'desk', 'selectors': ['pipeline.spec.ts'],
                'shard_count': 6, 'keep_going': True,
            })

    def test_suite_recovery_uses_original_request_when_shards_change(self):
        captured = []

        class Child:
            def wait(self):
                return 75

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve()
            state_root = repo / 'state'
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest()
            attempt = 'a' * 32
            output = state / attempt
            output.mkdir(parents=True)
            original = {'action': 'run', 'shard_count': 2, 'selection': None, 'keep_going': True}
            request = state / (attempt + '.suite-request.json')
            route.write(request, original)
            (output / 'submission.json').write_text(json.dumps({
                'attempt': attempt, 'workflow': 'suite-run', 'suite': original,
            }))
            route.write(state / 'active.json', {
                'state': 'active', 'tool': 'pnpm', 'output': str(output),
                'command': ['journeys', '--keep-going'], 'host': 'host',
                'attempt': attempt, 'suite_request': str(request),
            })
            environment = {'PANDORA_HOST': 'host', 'PANDORA_STATE': str(state_root),
                           'PANDORA_SESSION': 'session', 'PANDORA_SUITE_SHARDS': '9'}
            with patch.object(sys, 'argv', ['route.py', 'journeys', '--keep-going']), \
                 patch.dict(os.environ, environment, clear=True), \
                 patch.object(route.subprocess, 'check_output', return_value=str(repo)), \
                 patch.object(route.Path, 'cwd', return_value=repo), \
                 patch.object(route.subprocess, 'Popen', side_effect=lambda command, **_kwargs: captured.append(command) or Child()):
                self.assertEqual(route.main(), 75)
            self.assertEqual(len(captured), 1)
            self.assertIn('transport.py', str(captured[0]))
            self.assertNotIn('warm.py', str(captured[0]))
            self.assertEqual(json.loads(request.read_text()), original)
            self.assertEqual(json.loads((output / 'submission.json').read_text())['suite'], original)
            self.assertEqual(list(state.glob('*.suite-request.json')), [request])

    def test_real_route_rejects_each_legacy_suite_variable_before_creating_state(self):
        names = ('JOURNEY_FILTER', 'JOURNEY_SHARD', 'JOURNEY_CONCURRENCY',
                 'JOURNEY_REPLAY', 'JOURNEY_TEMPLATE', 'IKE_WORLD')
        for name in names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                state = Path(temp) / 'state'
                environment = {'PANDORA_HOST': 'host', 'PANDORA_STATE': str(state),
                               'PANDORA_SESSION': 'session', 'PANDORA_SUITE_SHARDS': '4', name: 'set'}
                with patch.object(sys, 'argv', ['route.py', 'journeys']), \
                     patch.dict(os.environ, environment, clear=True), \
                     patch.object(route.subprocess, 'check_output', side_effect=AssertionError('must not inspect repo')), \
                     patch.object(route.subprocess, 'Popen', side_effect=AssertionError('must not submit')):
                    self.assertEqual(route.main(), 64)
                self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()
