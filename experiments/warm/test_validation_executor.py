import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import validation


class ValidationExecutorTests(unittest.TestCase):
    def setUp(self):
        self.request_module = types.ModuleType('validation_request')
        self.request_module.validate_request = lambda request: request
        self.modules = patch.dict(sys.modules, {'validation_request': self.request_module})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def attempt(self, root, suite='unit'):
        attempt = Path(root) / ('a' * 32)
        attempt.mkdir()
        (attempt / 'source').mkdir()
        (attempt / 'submission.json').write_text(json.dumps({
            'workflow': 'validation', 'validation': {'version': 1, 'suite': suite, 'args': []},
        }))
        (attempt / 'validation.mjs').write_text('// adapter')
        (attempt / 'validation-stack.mjs').write_text('// stack')
        (attempt / 'validation-artifacts.mjs').write_text('// artifacts')
        return attempt

    @staticmethod
    def child(status=0):
        process = Mock()
        process.wait.return_value = status
        process.poll.return_value = status
        return process

    @staticmethod
    def state(running=True, oom=False):
        return subprocess.CompletedProcess([], 0, json.dumps({'Running': running, 'OOMKilled': oom}), '')

    def test_private_container_has_validation_labels_limits_and_browser_shm(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = self.attempt(root)
            calls = []

            def docker(*args, **kwargs):
                calls.append(args)
                if args[0] == 'inspect':
                    return self.state()
                return subprocess.CompletedProcess([], 0, '', '')

            def run(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, '', '')

            with patch('validation.docker', side_effect=docker), \
                    patch('validation.subprocess.run', side_effect=run), \
                    patch('validation.subprocess.Popen', return_value=self.child()), \
                    patch('validation._copy_results'), \
                    patch('validation.cleanup', return_value=True):
                status = validation.execute(attempt, 'image-id', [], [], {})

            self.assertEqual(status, 0)
            main = next(call for call in calls if call[:2] == ('run', '-d'))
            self.assertIn('pandora.workflow=validation', main)
            self.assertIn('pandora.attempt=' + attempt.name, main)
            self.assertIn('--network', main)
            self.assertIn('--cap-drop=ALL', main)
            self.assertIn('--security-opt=no-new-privileges', main)
            self.assertIn('--shm-size=1g', main)
            self.assertIn('--pids-limit=512', main)
            self.assertIn('--memory=6144m', main)
            self.assertTrue(any(call[0] == 'network' and call[1] == 'create' for call in calls))
            check = next(call for call in calls if call[:3] == ('exec', 'pandora-warm-' + attempt.name, 'node'))
            self.assertEqual(check[-1], 'tools/check-worktree-deps.mjs')
            invocation = next(call for call in calls if call[:2] == ('cp', str(attempt / 'validation.mjs')))
            self.assertEqual(invocation[-1], 'pandora-warm-' + attempt.name + ':/workspace/source/pandora-validation.mjs')
            artifacts = next(call for call in calls if call[:2] == ('cp', str(attempt / 'validation-artifacts.mjs')))
            self.assertEqual(artifacts[-1], 'pandora-warm-' + attempt.name + ':/workspace/source/pandora-validation-artifacts.mjs')

    def test_postgres_suite_starts_service_roles_with_their_own_limits(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = self.attempt(root, 'postgres')
            calls = []

            def docker(*args, **kwargs):
                calls.append(args)
                if args[0] == 'inspect':
                    return self.state()
                return subprocess.CompletedProcess([], 0, '', '')

            def run(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, '', '')

            with patch('validation.docker', side_effect=docker), \
                    patch('validation.subprocess.run', side_effect=run), \
                    patch('validation.subprocess.Popen', return_value=self.child()), \
                    patch('validation._copy_results'), \
                    patch('validation.cleanup', return_value=True), \
                    patch('validation.time.sleep'):
                self.assertEqual(validation.execute(attempt, 'image-id', [], [], {}), 0)

            service_runs = [call for call in calls if call[:2] == ('run', '-d') and any(
                isinstance(value, str) and value.endswith('-db') for value in call)]
            self.assertEqual(len(service_runs), 1)
            self.assertIn('--memory=768m', service_runs[0])
            self.assertIn('pandora.workflow=validation', service_runs[0])
            self.assertEqual(sum(1 for call in calls if call[:2] == ('run', '-d')), 4)

    def test_cleanup_failure_overrides_passing_adapter_and_records_infrastructure_exit(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = self.attempt(root)

            def docker(*args, **kwargs):
                if args[0] == 'inspect':
                    return self.state()
                return subprocess.CompletedProcess([], 0, '', '')

            with patch('validation.docker', side_effect=docker), \
                    patch('validation.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                    patch('validation.subprocess.Popen', return_value=self.child()), \
                    patch('validation._copy_results'), \
                    patch('validation.cleanup', return_value=False):
                self.assertEqual(validation.execute(attempt, 'image-id', [], [], {}), 70)

            self.assertTrue((attempt / 'service-cleanup.pending').exists())
            self.assertEqual(json.loads((attempt / 'metrics.json').read_text())['exit_code'], 70)

    def test_rejects_non_validation_submission_before_resources_are_created(self):
        with tempfile.TemporaryDirectory() as root:
            attempt = self.attempt(root)
            (attempt / 'submission.json').write_text(json.dumps({'workflow': 'journey'}))
            with self.assertRaisesRegex(ValueError, 'workflow validation'):
                validation.execute(attempt, 'image-id', [], [], {})


if __name__ == '__main__':
    unittest.main()
