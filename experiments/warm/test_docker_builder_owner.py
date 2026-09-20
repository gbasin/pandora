import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import builder_owner
import docker_cleanup
import docker_workflow
from docker_images import publish, resolve


class DockerBuilderOwner(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.attempt = self.root / 'runs' / ('a' * 32)
        (self.attempt / 'source').mkdir(parents=True)
        (self.attempt / 'source/Dockerfile').write_text('FROM scratch\n')
        handle = (self.attempt / 'attempt.lock').open('a')
        fcntl.flock(handle, fcntl.LOCK_EX)
        self.addCleanup(handle.close)
        self.submitted = {'source_digest': 'd' * 64, 'docker': {
            'worktree_key': 'b' * 64, 'request': {'kind': 'build', 'tag': 'app:test', 'dockerfile': 'Dockerfile'}}}
        self.old = {'image_id': 'sha256:old'}
        publish(self.root, 'b' * 64, 'app:test', self.old)
        runner = patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.run = runner.start()
        self.addCleanup(runner.stop)

    def execute(self):
        return docker_workflow.execute(self.attempt, self.submitted, {})

    def test_success_publishes_only_after_verified_builder_cleanup(self):
        with patch.object(docker_workflow, 'build', return_value=(0, 'sha256:new')):
            self.assertEqual(self.execute(), 0)
        self.assertEqual(resolve(self.root, 'b' * 64, 'app:test')['image_id'], 'sha256:new')
        self.assertTrue(json.loads((self.attempt / 'docker-cleanup.json').read_text())['verified'])
        self.assertFalse((self.attempt / 'docker-cleanup.pending').exists())
        self.assertIsNone(builder_owner.owner(builder_owner.paths(self.attempt, docker_cleanup.BUILDER)[1]))

    def test_failed_build_keeps_previous_mapping(self):
        with patch.object(docker_workflow, 'build', return_value=(1, None)):
            self.assertEqual(self.execute(), 1)
        self.assertEqual(resolve(self.root, 'b' * 64, 'app:test'), self.old)
        self.assertFalse((self.attempt / 'docker-cleanup.pending').exists())

    def test_unknown_cleanup_prevents_successful_build_publication(self):
        def build(*args):
            self.run.return_value = subprocess.CompletedProcess([], 1, '', 'daemon unavailable')
            return 0, 'sha256:new'
        with patch.object(docker_workflow, 'build', side_effect=build):
            self.assertEqual(self.execute(), 70)
        self.assertEqual(resolve(self.root, 'b' * 64, 'app:test'), self.old)
        self.assertTrue((self.attempt / 'docker-cleanup.pending').exists())

    def test_timeout_reaps_client_before_daemon_cleanup(self):
        child = Mock()
        child.poll.return_value = None
        events = []
        child.terminate.side_effect = lambda: events.append('terminate')
        child.wait.return_value = 0
        original = docker_cleanup.cleanup
        def cleanup(*args, **kwargs):
            events.append('cleanup')
            return original(*args, **kwargs)
        with patch.object(docker_workflow.subprocess, 'Popen', return_value=child), \
             patch.object(docker_workflow, 'wait', side_effect=TimeoutError('deadline')), \
             patch.object(docker_workflow, 'cleanup', side_effect=cleanup):
            with self.assertRaises(TimeoutError):
                self.execute()
        self.assertEqual(events, ['terminate', 'cleanup'])
        self.assertEqual(resolve(self.root, 'b' * 64, 'app:test'), self.old)
        self.assertFalse((self.attempt / 'docker-cleanup.pending').exists())

    def test_claim_marker_failure_is_recovered_without_stopping_other_owner(self):
        original = Path.replace
        def fail_marker(path, target):
            if Path(target).name == 'docker-cleanup.pending':
                raise OSError('marker write interrupted')
            return original(path, target)
        with patch.object(Path, 'replace', fail_marker):
            with self.assertRaises(OSError):
                self.execute()
        self.assertIsNone(builder_owner.owner(builder_owner.paths(self.attempt, docker_cleanup.BUILDER)[1]))
        self.assertEqual(resolve(self.root, 'b' * 64, 'app:test'), self.old)

    def test_crash_cleanup_respects_live_owner_then_recovers_dead_owner(self):
        lease = builder_owner.acquire(self.attempt, docker_cleanup.BUILDER, 'docker-cleanup.pending', marker_value='build')
        self.run.reset_mock()
        self.assertFalse(docker_cleanup.cleanup(self.attempt))
        self.assertFalse(any(c.args[0][2:4] == ['buildx', 'stop'] for c in self.run.call_args_list))
        lease.handle.close()
        self.assertTrue(docker_cleanup.cleanup(self.attempt))
        self.assertFalse((self.attempt / 'terminal.json').exists())
        self.assertFalse((self.attempt / 'docker-cleanup.pending').exists())


if __name__ == '__main__':
    unittest.main()
