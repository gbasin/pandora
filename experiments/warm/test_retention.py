import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess
from retention import _remote_locked, candidates, local, PROFILE


class Retention(unittest.TestCase):
    def test_cleanup_verification_requires_true_boolean(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ('a' * 32)
            path.mkdir()
            (path / 'submission.json').write_text(json.dumps({'profile': PROFILE}))
            (path / 'terminal.json').write_text(json.dumps({'attempt': path.name, 'cleanup_verified': 1}))
            (path / 'completed.json').touch()
            self.assertEqual(candidates(root, 'completed.json', keep=0), [])

    def test_only_acknowledged_profile_runs_are_collectible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = []
            for index in range(15):
                path = root / f'{index:032x}'
                path.mkdir()
                (path / 'submission.json').write_text(json.dumps({'profile': PROFILE}))
                (path / 'terminal.json').write_text(json.dumps({'attempt': path.name, 'cleanup_verified': True}))
                (path / 'completed.json').write_text('{}')
                paths.append(path)
            (paths[0] / 'completed.json').unlink()  # unresolved delivery
            (paths[1] / 'submission.json').write_text('{}')  # earlier experiment
            (paths[2] / 'terminal.json').write_text('{"attempt":"ffffffffffffffffffffffffffffffff","cleanup_verified":true}')
            victims = candidates(root, 'completed.json', keep=1, protected=[paths[3]])
            self.assertFalse(set(paths[:4]) & set(victims))
            self.assertEqual(len(victims), 10)
            local(root, paths[3])
            self.assertTrue(all(path.exists() for path in paths[:4]))
            self.assertEqual(sum(path.exists() for path in paths), 14)

    def test_remote_removes_only_the_inspected_terminal_surface_container(self):
        with tempfile.TemporaryDirectory() as temp:
            root, path = self.remote_candidate(temp)
            name, container_id = 'pandora-warm-' + path.name, 'f' * 64
            listed = {'ID': container_id[:12]}
            inspected = {'Id': container_id, 'Name': '/' + name,
                         'State': {'Status': 'exited', 'Running': False},
                         'Config': {'Labels': {'pandora.experiment': 'warm-surface',
                                               'pandora.workflow': 'surface', 'pandora.attempt': path.name}}}
            with patch('retention.subprocess.run', side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps(listed) + '\n', ''),
                    subprocess.CompletedProcess([], 0, json.dumps(inspected), ''),
                    subprocess.CompletedProcess([], 0, '', '')]) as run:
                _remote_locked(root)
            self.assertFalse(path.exists())
            self.assertEqual(run.call_args_list[-1].args[0], ['sudo', 'docker', 'rm', container_id])

    def test_remote_preserves_unknown_or_replaced_container(self):
        with tempfile.TemporaryDirectory() as temp:
            root, path = self.remote_candidate(temp)
            name, container_id = 'pandora-warm-' + path.name, 'e' * 64
            inspected = {'Id': container_id, 'Name': '/' + name,
                         'State': {'Status': 'exited', 'Running': False},
                         'Config': {'Labels': {'pandora.experiment': 'warm-surface',
                                               'pandora.attempt': '0' * 32}}}
            with patch('retention.subprocess.run', side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps({'ID': container_id[:12]}) + '\n', ''),
                    subprocess.CompletedProcess([], 0, json.dumps(inspected), '')]) as run:
                _remote_locked(root)
            self.assertTrue(path.exists())
            self.assertEqual(run.call_count, 2)

    def test_remote_preserves_an_inspected_name_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root, path = self.remote_candidate(temp)
            container_id = 'c' * 64
            inspected = {'Id': container_id, 'Name': '/pandora-warm-' + ('b' * 32),
                         'State': {'Status': 'exited', 'Running': False},
                         'Config': {'Labels': {'pandora.experiment': 'warm-surface',
                                               'pandora.workflow': 'surface', 'pandora.attempt': path.name}}}
            with patch('retention.subprocess.run', side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps({'ID': container_id[:12]}) + '\n', ''),
                    subprocess.CompletedProcess([], 0, json.dumps(inspected), '')]) as run:
                _remote_locked(root)
            self.assertTrue(path.exists())
            self.assertEqual(run.call_count, 2)

    def test_remote_allows_exact_legacy_surface_label(self):
        with tempfile.TemporaryDirectory() as temp:
            root, path = self.remote_candidate(temp)
            name, container_id = 'pandora-warm-' + path.name, 'd' * 64
            inspected = {'Id': container_id, 'Name': '/' + name,
                         'State': {'Status': 'exited', 'Running': False},
                         'Config': {'Labels': {'pandora.experiment': 'warm-surface'}}}
            with patch('retention.subprocess.run', side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps({'ID': container_id[:12]}) + '\n', ''),
                    subprocess.CompletedProcess([], 0, json.dumps(inspected), ''),
                    subprocess.CompletedProcess([], 0, '', '')]):
                _remote_locked(root)
            self.assertFalse(path.exists())

    def test_remote_allows_inherited_nonpandora_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            root, path = self.remote_candidate(temp)
            name, container_id = 'pandora-warm-' + path.name, 'c' * 64
            inspected = {'Id': container_id, 'Name': '/' + name,
                         'State': {'Status': 'exited', 'Running': False},
                         'Config': {'Labels': {'pandora.experiment': 'warm-surface',
                                               'pandora.workflow': 'surface',
                                               'pandora.attempt': path.name,
                                               'org.opencontainers.image.source': 'https://example.invalid/image'}}}
            with patch('retention.subprocess.run', side_effect=[
                    subprocess.CompletedProcess([], 0, json.dumps({'ID': container_id[:12]}) + '\n', ''),
                    subprocess.CompletedProcess([], 0, json.dumps(inspected), ''),
                    subprocess.CompletedProcess([], 0, '', '')]):
                _remote_locked(root)
            self.assertFalse(path.exists())

    def remote_candidate(self, temp):
        root = Path(temp)
        path = root / 'runs' / ('a' * 32)
        path.mkdir(parents=True)
        (path / 'submission.json').write_text(json.dumps({'profile': PROFILE}))
        (path / 'terminal.json').write_text(json.dumps({'attempt': path.name, 'cleanup_verified': True}))
        (path / 'released').touch()
        os.utime(path / 'released', (1, 1))
        for index in range(10):
            newer = root / 'runs' / f'{index + 1:032x}'
            newer.mkdir()
            (newer / 'submission.json').write_text(json.dumps({'profile': PROFILE}))
            (newer / 'terminal.json').write_text(json.dumps({'attempt': newer.name, 'cleanup_verified': True}))
            (newer / 'released').touch()
            os.utime(newer / 'released', (index + 2, index + 2))
        return root, path


if __name__ == '__main__':
    unittest.main()
