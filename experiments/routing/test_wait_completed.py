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


COMMAND = ['test:surface', 'borrower-web']


class WaitCompletedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.state_root = self.repo / 'state'
        self.state = self.state_root / hashlib.sha256(str(self.repo).encode()).hexdigest()
        self.state.mkdir(parents=True)

    def record(self, attempt, state='active', *, tool='pnpm'):
        output = self.state / attempt
        output.mkdir()
        (output / 'submission.json').write_text(json.dumps({'attempt': attempt, 'source_digest': 'same'}))
        record = {'state': state, 'protocol': 2, 'tool': tool, 'output': str(output),
                  'command': COMMAND, 'host': 'unused', 'attempt': attempt}
        route.write(self.state / 'active.json', record)
        return output, record

    def waiting(self, attempt, **environment):
        env = {'PANDORA_STATE': str(self.state_root), 'PANDORA_HOST': 'unused'} | environment
        return patch.dict(os.environ, env), patch.object(sys, 'argv', ['wait.py', attempt]), \
            patch('wait.subprocess.check_output', return_value=str(self.repo)), \
            patch('wait.Path.cwd', return_value=self.repo)

    def test_completed_same_id_reports_current_verified_result_without_follow_or_publish(self):
        attempt = 'a' * 32
        output, _ = self.record(attempt, 'terminal')
        (output / 'terminal.json').write_text('{}')
        env, argv, git, cwd = self.waiting(attempt)
        with env, argv, git, cwd, patch('route.validate_evidence', return_value={'exit_code': 0}), \
             patch('route.current_digest', return_value='same'), patch('wait.follow') as follow, \
             patch('route.deliver') as deliver, patch('route.subprocess.Popen') as launch:
            self.assertEqual(wait.main(), 0)
        follow.assert_not_called(); deliver.assert_not_called(); launch.assert_not_called()

    def test_completed_same_id_rejects_stale_source_without_follow_or_publish(self):
        attempt = 'b' * 32
        output, _ = self.record(attempt, 'terminal')
        (output / 'terminal.json').write_text('{}')
        env, argv, git, cwd = self.waiting(attempt)
        with env, argv, git, cwd, patch('route.validate_evidence', return_value={'exit_code': 0}), \
             patch('route.current_digest', return_value='changed'), patch('wait.follow') as follow, \
             patch('route.deliver') as deliver:
            self.assertEqual(wait.main(), 75)
        follow.assert_not_called(); deliver.assert_not_called()

    def test_completed_same_id_reports_acknowledged_infrastructure_outcome(self):
        attempt = 'c' * 32
        _, record = self.record(attempt, 'infrastructurefailure')
        env, argv, git, cwd = self.waiting(attempt)
        with env, argv, git, cwd, patch('route.validate_operator_result', return_value={'attempt': attempt}), \
             patch('wait.follow') as follow:
            self.assertEqual(wait.main(), 70)
        follow.assert_not_called()
        self.assertEqual(json.loads((self.state / 'active.json').read_text()), record)

    def test_waits_for_live_capture_then_follows_after_submission(self):
        attempt = 'd' * 32
        output, _ = self.record(attempt)
        (output / 'submission.json').unlink()
        def capture_arrives(_seconds):
            (output / 'submission.json').write_text(json.dumps({'attempt': attempt, 'source_digest': 'same'}))
        env, argv, git, cwd = self.waiting(attempt)
        with env, argv, git, cwd, patch('wait.route.owner_is_busy', return_value=True), \
             patch('wait.time.sleep', side_effect=capture_arrives), patch('wait.follow', return_value=75) as follow:
            self.assertEqual(wait.main(), 75)
            self.assertTrue(follow.call_args.kwargs['registration_pending']())
        follow.assert_called_once()
        args, kwargs = follow.call_args
        self.assertEqual(args, ('unused', output))
        self.assertEqual(kwargs['artifact_delivery_limit_bytes'], 2147483648)

    def test_dead_capture_owner_returns_without_following_or_cancelling(self):
        attempt = 'e' * 32
        output, _ = self.record(attempt)
        (output / 'submission.json').unlink()
        env, argv, git, cwd = self.waiting(attempt)
        with env, argv, git, cwd, patch('wait.route.owner_is_busy', return_value=False), \
             patch('wait.follow') as follow, patch('route.control') as control:
            self.assertEqual(wait.main(), 75)
        follow.assert_not_called(); control.assert_not_called()

    def test_docker_wait_uses_current_profile_artifact_limit(self):
        attempt = 'f' * 32
        output, _ = self.record(attempt, tool='docker')
        profile_path = self.repo / 'docker-profile.json'
        profile_path.write_text('{}')
        env, argv, git, cwd = self.waiting(attempt, PANDORA_DOCKER_PROFILE_JSON=str(profile_path),
                                            PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES='99')
        with env, argv, git, cwd, patch('wait.route.owner_is_busy', return_value=False), \
             patch('docker_commands.profile', return_value={'artifact_delivery_limit_bytes': 17}), \
             patch('wait.follow', return_value=75) as follow:
            self.assertEqual(wait.main(), 75)
        follow.assert_called_once()
        args, kwargs = follow.call_args
        self.assertEqual(args, ('unused', output))
        self.assertEqual(kwargs['artifact_delivery_limit_bytes'], 17)
        self.assertFalse(kwargs['registration_pending']())


if __name__ == '__main__':
    unittest.main()
