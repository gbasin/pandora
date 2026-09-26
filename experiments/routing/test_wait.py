import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import route
import wait


class WaitRecovery(unittest.TestCase):
    def test_wait_finalizes_local_terminal_while_owner_is_busy(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve()
            state_root = repo / 'state'
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest()
            attempt = 'b' * 32
            output = state / attempt
            output.mkdir(parents=True)
            (output / 'submission.json').write_text(json.dumps({'attempt': attempt, 'source_digest': 'same'}))
            (output / 'artifacts.json').write_text('{}')
            (output / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'exit_code': 1, 'cleanup_verified': True}))
            record = {'state': 'active', 'protocol': 2, 'tool': 'pnpm', 'output': str(output),
                      'command': ['test:surface', 'web'], 'host': 'unused', 'attempt': attempt}
            route.write(state / 'active.json', record)
            owner = route.locked(route.owner_path(output))
            env = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['wait.py', attempt]), \
                 patch('wait.subprocess.check_output', return_value=str(repo)), \
                 patch('wait.Path.cwd', return_value=repo), patch('route.Path.cwd', return_value=repo), \
                 patch('route.subprocess.check_output', return_value=str(repo)), \
                 patch('route.current_digest', return_value='same'):
                self.assertEqual(wait.main(), 1)
            self.assertEqual(json.loads((state / 'active.json').read_text())['state'], 'terminal')
            owner.close()

    def test_late_wait_never_allocates_a_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve(); state_root = repo / 'state'
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest(); state.mkdir(parents=True)
            route.write(state / 'active.json', {'state': 'terminal', 'attempt': 'c' * 32})
            env = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['route.py', 'test:surface', 'web']), \
                 patch('route.subprocess.check_output', return_value=str(repo)), patch('route.Path.cwd', return_value=repo), \
                 patch('route.subprocess.Popen', side_effect=AssertionError('late wait must not submit')):
                self.assertEqual(route.main(expected_attempt='b' * 32, observer=True), 75)
            self.assertEqual(json.loads((state / 'active.json').read_text())['attempt'], 'c' * 32)

    def test_wait_observes_submitted_attempt_before_launch_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve(); state_root = repo / 'state'
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest(); attempt = 'd' * 32
            output = state / attempt; output.mkdir(parents=True)
            (output / 'submission.json').write_text(json.dumps({'attempt': attempt}))
            route.write(state / 'active.json', {'state': 'active', 'protocol': 2, 'attempt': attempt,
                                                'output': str(output), 'host': 'unused', 'command': []})
            owner = route.locked(route.owner_path(output))
            with patch.dict(os.environ, {'PANDORA_STATE': str(state_root), 'PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES': '20'}), patch.object(sys, 'argv', ['wait.py', attempt]), \
                 patch('wait.subprocess.check_output', return_value=str(repo)), patch('wait.Path.cwd', return_value=repo), \
                 patch('wait.follow', return_value=75) as follow:
                self.assertEqual(wait.main(), 75)
            self.assertEqual(follow.call_args.args, ('unused', output))
            self.assertEqual(follow.call_args.kwargs['artifact_delivery_limit_bytes'], 20)
            self.assertTrue(follow.call_args.kwargs['registration_pending']())
            owner.close()

    def test_wait_finalizer_interrupt_never_cancels_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve(); state_root = repo / 'state'
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest(); attempt = 'e' * 32
            output = state / attempt; output.mkdir(parents=True)
            (output / 'submission.json').write_text(json.dumps({'attempt': attempt, 'source_digest': 'same'}))
            record = {'state': 'active', 'protocol': 2, 'tool': 'pnpm', 'attempt': attempt,
                      'output': str(output), 'host': 'unused', 'command': ['test:surface', 'web']}
            route.write(state / 'active.json', record)
            env = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            with patch.dict(os.environ, env), patch.object(sys, 'argv', ['route.py', *record['command']]), \
                 patch('route.subprocess.check_output', return_value=str(repo)), patch('route.Path.cwd', return_value=repo), \
                 patch('route.subprocess.Popen', side_effect=KeyboardInterrupt), patch('route.control') as control:
                self.assertEqual(route.main(expected_attempt=attempt, observer=True), 130)
            control.assert_not_called()
            self.assertEqual(json.loads((state / 'active.json').read_text())['attempt'], attempt)



if __name__ == '__main__':
    unittest.main()
