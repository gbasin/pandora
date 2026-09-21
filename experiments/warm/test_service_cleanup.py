import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from service_cleanup import cleanup


class CleanupTests(unittest.TestCase):
    @staticmethod
    def container(name, identity, workflow, resource_id):
        return {'Name': '/' + name, 'Id': resource_id,
                'Config': {'Labels': {'pandora.attempt': identity,
                                      'pandora.workflow': workflow}},
                'State': {'Status': 'exited', 'Running': False}}

    @staticmethod
    def network(name, identity, resource_id):
        return {'Name': name, 'Id': resource_id,
                'Labels': {'pandora.attempt': identity}}

    def test_partial_creation_is_removed_by_reserved_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('a' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            calls = []
            resource_ids = iter(('1' * 64, '2' * 64, '3' * 64, '4' * 64))
            resources = {
                name: self.container(name, attempt.name, 'journey', next(resource_ids))
                for name, suffix in ((f'pandora-warm-{attempt.name}-proxy', 'proxy'),
                                     (f'pandora-warm-{attempt.name}-pool', 'pool'),
                                     (f'pandora-warm-{attempt.name}-db', 'db'),
                                     (f'pandora-warm-{attempt.name}', 'main'))}
            resources[f'pandora-warm-{attempt.name}-network'] = self.network(
                f'pandora-warm-{attempt.name}', attempt.name, '5' * 64)

            def run(argv, **kwargs):
                calls.append(argv)
                if argv[2:4] == ['container', 'inspect']:
                    value = resources.get(argv[4])
                    return subprocess.CompletedProcess(argv, 0 if value else 1,
                                                       json.dumps(value) if value else '',
                                                       '' if value else 'No such object: ' + argv[4])
                if argv[2:4] == ['network', 'inspect']:
                    value = resources.get(argv[4] + '-network')
                    return subprocess.CompletedProcess(argv, 0 if value else 1,
                                                       json.dumps(value) if value else '',
                                                       '' if value else 'No such network: ' + argv[4])
                if argv[2:3] == ['rm']:
                    resources.pop(next(name for name, value in resources.items()
                                      if value.get('Id') == argv[-1]), None)
                if argv[2:4] == ['network', 'rm']:
                    resources.pop(f'pandora-warm-{attempt.name}-network', None)
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch('service_cleanup.subprocess.run', side_effect=run):
                self.assertTrue(cleanup(attempt))
            removed = [a[-1] for a in calls if a[2:3] == ['rm']]
            self.assertEqual(len(removed), 4)
            self.assertTrue(all(len(value) == 64 for value in removed))
            self.assertIn('5' * 64, [a[-1] for a in calls if a[2:4] == ['network', 'rm']])
            self.assertFalse((attempt / 'service-cleanup.pending').exists())
            self.assertTrue(json.loads((attempt / 'service-cleanup.json').read_text())['verified'])

    def test_unknown_docker_state_keeps_cleanup_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('b' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            with patch('service_cleanup.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'daemon unavailable')):
                self.assertFalse(cleanup(attempt))
            self.assertTrue((attempt / 'service-cleanup.pending').exists())

    def test_worker_death_cleans_a_validation_attempt_with_pending_service_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('f' * 32)
            attempt.mkdir()
            (attempt / 'submission.json').write_text(json.dumps({'workflow': 'validation'}))
            (attempt / 'service-cleanup.pending').touch()
            names = [f'pandora-warm-{attempt.name}' + suffix
                     for suffix in ('-proxy', '-pool', '-db', '')]
            resources = {name: self.container(name, attempt.name, 'validation', str(index) * 64)
                         for index, name in enumerate(names, 1)}
            resources['network'] = self.network(f'pandora-warm-{attempt.name}', attempt.name, '9' * 64)

            def run(argv, **_kwargs):
                if argv[2:4] == ['container', 'inspect']:
                    value = resources.get(argv[4])
                    return subprocess.CompletedProcess(argv, 0 if value else 1,
                                                       json.dumps(value) if value else '', '')
                if argv[2:4] == ['network', 'inspect']:
                    value = resources.get('network')
                    return subprocess.CompletedProcess(argv, 0 if value else 1,
                                                       json.dumps(value) if value else '', '')
                if argv[2:3] == ['rm']:
                    resources.pop(next(key for key, value in resources.items()
                                       if value.get('Id') == argv[-1]), None)
                if argv[2:4] == ['network', 'rm']:
                    resources.pop('network', None)
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch('service_cleanup.subprocess.run', side_effect=run):
                self.assertTrue(cleanup(attempt))
            self.assertFalse(resources)
            self.assertFalse((attempt / 'service-cleanup.pending').exists())

    def test_validation_cleanup_fails_closed_for_a_foreign_workflow_label(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('1' * 32)
            attempt.mkdir()
            (attempt / 'submission.json').write_text(json.dumps({'workflow': 'validation'}))
            (attempt / 'service-cleanup.pending').touch()
            name = f'pandora-warm-{attempt.name}'
            foreign = self.container(name, attempt.name, 'journey', '2' * 64)

            def run(argv, **_kwargs):
                if argv[2:4] == ['container', 'inspect'] and argv[4] == name:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(foreign), '')
                return subprocess.CompletedProcess(argv, 1, '', 'No such object')

            with patch('service_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(cleanup(attempt))
            self.assertFalse(any(call.args[0][2:3] == ['rm'] for call in mocked.call_args_list))
            self.assertTrue((attempt / 'service-cleanup.pending').exists())

    def test_foreign_same_name_container_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('c' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            name = f'pandora-warm-{attempt.name}'
            foreign = self.container(name, 'd' * 32, 'journey', 'd' * 64)

            def run(argv, **kwargs):
                if argv[2:4] == ['container', 'inspect'] and argv[4] == name:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(foreign), '')
                return subprocess.CompletedProcess(argv, 1, '', 'No such object')

            with patch('service_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(cleanup(attempt))
            self.assertFalse(any(call.args[0][2:3] == ['rm'] and call.args[0][-1] == 'd' * 64
                                 for call in mocked.call_args_list))
            self.assertTrue((attempt / 'service-cleanup.pending').exists())

    def test_foreign_same_name_network_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('d' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            name = f'pandora-warm-{attempt.name}'
            foreign = self.network(name, 'e' * 32, 'e' * 64)

            def run(argv, **kwargs):
                if argv[2:4] == ['network', 'inspect']:
                    return subprocess.CompletedProcess(argv, 0, json.dumps(foreign), '')
                return subprocess.CompletedProcess(argv, 1, '', 'No such object')

            with patch('service_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(cleanup(attempt))
            self.assertFalse(any(call.args[0][2:4] == ['network', 'rm']
                                 and call.args[0][-1] == 'e' * 64
                                 for call in mocked.call_args_list))
            self.assertTrue((attempt / 'service-cleanup.pending').exists())

    def test_name_replacement_after_inspection_is_not_removed_or_verified(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('e' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            name = f'pandora-warm-{attempt.name}'
            owned = self.container(name, attempt.name, 'journey', 'a' * 64)
            foreign = self.container(name, 'f' * 32, 'journey', 'f' * 64)
            inspections = 0

            def run(argv, **kwargs):
                nonlocal inspections
                if argv[2:4] == ['container', 'inspect'] and argv[4] == name:
                    inspections += 1
                    return subprocess.CompletedProcess(argv, 0,
                                                       json.dumps(owned if inspections == 1 else foreign), '')
                if argv[2:3] == ['rm'] and argv[-1] == 'a' * 64:
                    return subprocess.CompletedProcess(argv, 1, '', 'No such container: ' + ('a' * 64))
                return subprocess.CompletedProcess(argv, 1, '', 'No such object')

            with patch('service_cleanup.subprocess.run', side_effect=run) as mocked:
                self.assertFalse(cleanup(attempt))
            self.assertIn('a' * 64, [call.args[0][-1] for call in mocked.call_args_list
                                       if call.args[0][2:3] == ['rm']])
            self.assertNotIn('f' * 64, [call.args[0][-1] for call in mocked.call_args_list
                                             if call.args[0][2:3] == ['rm']])
            self.assertTrue((attempt / 'service-cleanup.pending').exists())
