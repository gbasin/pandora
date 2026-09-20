import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import docker_cleanup


class DockerCleanupIdentityTests(unittest.TestCase):
    def container(self, name, identity, resource_id):
        return {'Name': '/' + name, 'Id': resource_id,
                'Config': {'Labels': {'pandora.attempt': identity,
                                      'pandora.workflow': 'docker'}},
                'State': {'Status': 'exited', 'Running': False}}

    def attempt(self, temp, identity='a' * 32):
        attempt = Path(temp) / identity
        attempt.mkdir()
        (attempt / 'docker-cleanup.pending').write_text('run')
        return attempt

    def test_missing_container_is_harmless(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(temp)
            def missing(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    return subprocess.CompletedProcess(argv, 1, '', 'No such container: ' + argv[4])
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch('docker_cleanup.subprocess.run', side_effect=missing) as mocked:
                self.assertTrue(docker_cleanup.cleanup(attempt))
            self.assertFalse(any(call.args[0][2:3] == ['rm'] for call in mocked.call_args_list))
            self.assertFalse((attempt / 'docker-cleanup.pending').exists())

    def test_foreign_same_name_container_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(temp)
            name = 'pandora-warm-' + attempt.name
            foreign = self.container(name, 'b' * 32, 'b' * 64)

            def run(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(foreign), '')
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('docker_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(docker_cleanup.cleanup(attempt))
            self.assertFalse(any(call.args[0][-1] == 'b' * 64 for call in mocked.call_args_list
                                 if call.args[0][2:3] == ['rm']))
            self.assertTrue((attempt / 'docker-cleanup.pending').exists())

    def test_replacement_after_inspection_keeps_barrier_and_foreign_container(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(temp)
            name = 'pandora-warm-' + attempt.name
            owned = self.container(name, attempt.name, 'a' * 64)
            foreign = self.container(name, 'b' * 32, 'b' * 64)
            inspections = 0

            def run(argv, **kwargs):
                nonlocal inspections
                if argv[2:4] == ['container', 'inspect']:
                    inspections += 1
                    return subprocess.CompletedProcess(argv, 0,
                                                       json.dumps(owned if inspections == 1 else foreign), '')
                if argv[2:3] == ['rm']:
                    return subprocess.CompletedProcess(argv, 1, '', 'No such container: ' + ('a' * 64))
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('docker_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(docker_cleanup.cleanup(attempt))
            removed = [call.args[0][-1] for call in mocked.call_args_list if call.args[0][2:3] == ['rm']]
            self.assertEqual(removed, ['a' * 64])
            self.assertTrue((attempt / 'docker-cleanup.pending').exists())

    def test_inherited_oci_labels_do_not_prevent_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(temp)
            name = 'pandora-warm-' + attempt.name
            owned = self.container(name, attempt.name, 'a' * 64)
            owned['Config']['Labels']['org.opencontainers.image.source'] = 'https://example.invalid/image'
            inspected = 0

            def run(argv, **kwargs):
                nonlocal inspected
                if argv[2:4] == ['container', 'inspect']:
                    inspected += 1
                    if inspected == 1:
                        return subprocess.CompletedProcess(argv, 0, json.dumps(owned), '')
                    return subprocess.CompletedProcess(argv, 1, '', 'No such container: ' + name)
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('docker_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertTrue(docker_cleanup.cleanup(attempt))
            self.assertIn('a' * 64, [call.args[0][-1] for call in mocked.call_args_list
                                     if call.args[0][2:3] == ['rm']])

    def test_invalid_inspected_id_is_not_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(temp)
            name = 'pandora-warm-' + attempt.name
            invalid = self.container(name, attempt.name, 'not-an-immutable-docker-id')

            def run(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(invalid), '')
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('docker_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(docker_cleanup.cleanup(attempt))
            self.assertFalse(any(call.args[0][2:3] == ['rm'] for call in mocked.call_args_list))
            self.assertTrue((attempt / 'docker-cleanup.pending').exists())

    def test_daemon_context_not_found_is_not_treated_as_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(temp)
            with patch('docker_cleanup.subprocess.run', return_value=subprocess.CompletedProcess(
                    [], 1, '', 'Current Docker context not found')):
                self.assertFalse(docker_cleanup.cleanup(attempt))
            self.assertTrue((attempt / 'docker-cleanup.pending').exists())


if __name__ == '__main__':
    unittest.main()
