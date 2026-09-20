import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from surface_cleanup import cleanup


class SurfaceCleanupTests(unittest.TestCase):
    @staticmethod
    def container(name, identity, resource_id):
        return {
            'Name': '/' + name,
            'Id': resource_id,
            'Config': {'Labels': {
                'pandora.attempt': identity,
                'pandora.workflow': 'surface',
                'pandora.experiment': 'warm-surface',
            }},
            'State': {'Status': 'created', 'Running': False},
        }

    def test_pre_create_intent_is_cleaned_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / ('a' * 32)
            attempt.mkdir()
            (attempt / 'surface-cleanup.pending').touch()

            def run(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    return subprocess.CompletedProcess(argv, 1, '', 'No such container: ' + argv[4])
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('surface_cleanup.subprocess.run', side_effect=run):
                self.assertTrue(cleanup(attempt))
                self.assertTrue(cleanup(attempt))
            self.assertFalse((attempt / 'surface-cleanup.pending').exists())

    def test_created_owned_container_is_removed_after_crash_intent(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / ('b' * 32)
            attempt.mkdir()
            (attempt / 'surface-cleanup.pending').touch()
            name = 'pandora-warm-' + attempt.name
            resources = {name: self.container(name, attempt.name, '1' * 64)}

            def run(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    value = resources.get(argv[4])
                    return subprocess.CompletedProcess(argv, 0 if value else 1, json.dumps(value) if value else '',
                                                       '' if value else 'No such container: ' + argv[4])
                if argv[2:3] == ['rm']:
                    resources.pop(name)
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('surface_cleanup.subprocess.run', side_effect=run):
                self.assertTrue(cleanup(attempt))
            self.assertEqual(resources, {})
            self.assertFalse((attempt / 'surface-cleanup.pending').exists())

    def test_foreign_same_name_container_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / ('c' * 32)
            attempt.mkdir()
            (attempt / 'surface-cleanup.pending').touch()
            name = 'pandora-warm-' + attempt.name
            foreign = self.container(name, 'd' * 32, '2' * 64)

            def run(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(foreign), '')
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('surface_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(cleanup(attempt))
            self.assertFalse(any(call.args[0][2:3] == ['rm'] for call in mocked.call_args_list))
            self.assertTrue((attempt / 'surface-cleanup.pending').exists())


if __name__ == '__main__':
    unittest.main()
