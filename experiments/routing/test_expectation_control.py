import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import expectation_control
from tracked_outputs import PublicationConflict, publish, read_intent


class ExpectationControl(unittest.TestCase):
    def invoke(self, repo, state_root, attempt):
        return patch.dict(os.environ, {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}), \
            patch('sys.argv', ['pandora', 'resolve-expectations', attempt, '--keep-local']), \
            patch('expectation_control.subprocess.check_output', return_value=str(repo.resolve())), \
            patch('expectation_control.Path.cwd', return_value=repo.resolve())

    def test_keep_local_accepts_every_declared_value_and_closes_matching_active_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / 'repo'; repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'a').write_bytes(b'outside')
            (repo / 'b').write_bytes(b'old')
            state_root = root / 'state'
            key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
            attempt = 'a' * 32
            output = state_root / key / attempt
            output.mkdir(parents=True)
            declarations = {'a': {'base': b'old', 'target': b'new'},
                            'b': {'base': b'old', 'target': b'new'}}
            for name, item in declarations.items():
                target = output / 'results/updates' / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(item['target'])
            (output / 'artifacts.json').write_text(json.dumps({
                'results/updates/' + name: hashlib.sha256(item['target']).hexdigest()
                for name, item in declarations.items()
            }))
            with self.assertRaises(PublicationConflict):
                publish(repo, output, declarations)
            (repo / 'a').write_bytes(b'manual merge')
            active = output.parent / 'active.json'
            active.write_text(json.dumps({'state': 'active', 'attempt': attempt,
                                          'output': str(output)}))
            submitted = {'attempt': attempt, 'workflow': 'journey', 'selectors': ['S0-01', '--update']}
            (output / 'submission.json').write_text(json.dumps(submitted))
            terminal = {'attempt': attempt, 'exit_code': 0, 'cleanup_verified': True}
            environment = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            with patch.dict(os.environ, environment), \
                 patch('sys.argv', ['pandora', 'resolve-expectations', attempt, '--keep-local']), \
                 patch('expectation_control.subprocess.check_output', return_value=str(repo.resolve())), \
                 patch('expectation_control.validate_evidence', return_value=terminal), \
                 patch('expectation_control.journey_updates.declarations', return_value=declarations), \
                 patch('expectation_control.route.control', return_value={'cleanup_verified': True}), \
                 patch('expectation_control.Path.cwd', return_value=repo.resolve()):
                self.assertEqual(expectation_control.main(), 0)
            self.assertEqual((repo / 'a').read_bytes(), b'manual merge')
            self.assertEqual((repo / 'b').read_bytes(), b'old')
            self.assertEqual(read_intent(output).phase, 'resolved')
            self.assertEqual(json.loads(active.read_text())['state'], 'terminal')

    def test_retries_after_resolved_intent_before_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / 'repo'; repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'a').write_bytes(b'outside')
            state_root = root / 'state'; attempt = 'a' * 32
            state = state_root / hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
            output = state / attempt; output.mkdir(parents=True)
            declarations = {'a': {'base': b'old', 'target': b'new'}}
            target = output / 'results/updates/a'; target.parent.mkdir(parents=True); target.write_bytes(b'new')
            (output / 'artifacts.json').write_text(json.dumps({'results/updates/a': hashlib.sha256(b'new').hexdigest()}))
            with self.assertRaises(PublicationConflict):
                publish(repo, output, declarations)
            (output / 'submission.json').write_text(json.dumps({'attempt': attempt, 'workflow': 'journey', 'selectors': ['S0-01', '--update']}))
            active = state / 'active.json'; active.write_text(json.dumps({'state': 'active', 'attempt': attempt, 'output': str(output)}))
            terminal = {'attempt': attempt, 'exit_code': 0, 'cleanup_verified': True}
            contexts = self.invoke(repo, state_root, attempt)
            with contexts[0], contexts[1], contexts[2], contexts[3], \
                 patch('expectation_control.validate_evidence', return_value=terminal), \
                 patch('expectation_control.journey_updates.declarations', return_value=declarations), \
                 patch('expectation_control.route.complete', side_effect=OSError('complete crash')):
                with self.assertRaisesRegex(OSError, 'complete crash'):
                    expectation_control.main()
            self.assertEqual(read_intent(output).phase, 'resolved')
            (repo / 'a').write_bytes(b'later local edit')
            contexts = self.invoke(repo, state_root, attempt)
            with contexts[0], contexts[1], contexts[2], contexts[3], \
                 patch('expectation_control.validate_evidence', return_value=terminal), \
                 patch('expectation_control.journey_updates.declarations', return_value=declarations), \
                 patch('expectation_control.route.control', return_value={'cleanup_verified': True}):
                self.assertEqual(expectation_control.main(), 0)
            self.assertEqual((repo / 'a').read_bytes(), b'later local edit')

    def test_rejects_mismatched_active_submission_and_failed_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repo = root / 'repo'; repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            state_root = root / 'state'; attempt = 'a' * 32; other = 'b' * 32
            state = state_root / hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
            output = state / attempt; output.mkdir(parents=True)
            active = state / 'active.json'
            active.write_text(json.dumps({'state': 'active', 'attempt': other, 'output': str(output)}))
            contexts = self.invoke(repo, state_root, attempt)
            with contexts[0], contexts[1], contexts[2], contexts[3]:
                with self.assertRaisesRegex(ValueError, 'Attempt does not match'):
                    expectation_control.main()
            active.write_text(json.dumps({'state': 'active', 'attempt': attempt, 'output': str(output)}))
            (output / 'submission.json').write_text(json.dumps({'attempt': other, 'workflow': 'journey', 'selectors': ['S0-01', '--update']}))
            contexts = self.invoke(repo, state_root, attempt)
            with contexts[0], contexts[1], contexts[2], contexts[3], \
                 patch('expectation_control.validate_evidence', return_value={'attempt': attempt, 'exit_code': 0, 'cleanup_verified': True}):
                with self.assertRaisesRegex(ValueError, 'Submission evidence'):
                    expectation_control.main()
            contexts = self.invoke(repo, state_root, attempt)
            with contexts[0], contexts[1], contexts[2], contexts[3], \
                 patch('expectation_control.validate_evidence', return_value={'attempt': attempt, 'exit_code': 1, 'cleanup_verified': True}):
                with self.assertRaisesRegex(ValueError, 'Only a successful'):
                    expectation_control.main()


if __name__ == '__main__':
    unittest.main()
