import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from docker_commands import DEFAULT_QUEUE_TIMEOUT_SECONDS, MAX_QUEUE_TIMEOUT_SECONDS, profile
from route import effective_artifact_delivery_limit, effective_queue_timeout


ROOT = Path(__file__).resolve().parent


class QueueConfigurationTests(unittest.TestCase):
    def test_artifact_limit_defaults_in_launcher_and_docker_profile_overrides_only_docker(self):
        base = {'dockerfiles': [], 'mounts': [], 'outputs': []}
        for value in (0, -1, True, 1.5, '2'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Artifact delivery limit'):
                profile(json.dumps({**base, 'artifact_delivery_limit_bytes': value}))
        config = profile(json.dumps({**base, 'artifact_delivery_limit_bytes': 123}))
        self.assertEqual(effective_artifact_delivery_limit(456, 'docker', config), 123)
        self.assertEqual(effective_artifact_delivery_limit(456, 'pnpm', config), 456)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'environment.py'
            target.write_text('import os\nprint(os.environ["PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES"])\n')
            env = dict(os.environ, PANDORA_REAL_PNPM='/bin/true')
            command = ['python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state')]
            self.assertEqual(subprocess.check_output([*command, '--', 'python3', str(target)], env=env, text=True).strip(),
                             str(2 * 1024 ** 3))
            self.assertEqual(subprocess.check_output([*command, '--artifact-delivery-limit-bytes', '123', '--', 'python3', str(target)], env=env, text=True).strip(), '123')

    def test_profile_accepts_bounded_positive_integer_only(self):
        base = {'dockerfiles': [], 'mounts': [], 'outputs': []}
        for value in (0, -1, MAX_QUEUE_TIMEOUT_SECONDS + 1, True, False, 1.5, '900'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Queue timeout'):
                profile(json.dumps({**base, 'queue_timeout_seconds': value}))
        self.assertEqual(profile(json.dumps({**base, 'queue_timeout_seconds': 1}))['queue_timeout_seconds'], 1)
        self.assertEqual(profile(json.dumps({**base, 'queue_timeout_seconds': MAX_QUEUE_TIMEOUT_SECONDS}))['queue_timeout_seconds'], MAX_QUEUE_TIMEOUT_SECONDS)

    def test_launcher_timeout_remains_the_session_default_with_a_docker_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'environment.py'
            target.write_text('import os\nprint(os.environ["PANDORA_QUEUE_TIMEOUT_SECONDS"])\n')
            docker_profile = root / 'profile.json'
            docker_profile.write_text(json.dumps({'dockerfiles': [], 'mounts': [], 'outputs': [], 'queue_timeout_seconds': 1234}))
            env = dict(os.environ, PANDORA_REAL_PNPM='/bin/true')
            result = subprocess.check_output([
                'python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state'),
                '--queue-timeout-seconds', '60', '--docker-profile', str(docker_profile), '--', 'python3', str(target)],
                env=env, text=True)
            self.assertEqual(result.strip(), '60')

    def test_docker_profile_timeout_overrides_only_docker_workflows(self):
        config = {'queue_timeout_seconds': 120}
        self.assertEqual(effective_queue_timeout(300, 'docker', config), 120)
        self.assertEqual(effective_queue_timeout(300, 'pnpm', config), 300)

    def test_launcher_default_and_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'environment.py'
            target.write_text('import os\nprint(os.environ["PANDORA_QUEUE_TIMEOUT_SECONDS"])\n')
            env = dict(os.environ, PANDORA_REAL_PNPM='/bin/true')
            command = ['python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state')]
            self.assertEqual(subprocess.check_output([*command, '--', 'python3', str(target)], env=env, text=True).strip(), str(DEFAULT_QUEUE_TIMEOUT_SECONDS))
            for value in ('0', '-1', str(MAX_QUEUE_TIMEOUT_SECONDS + 1)):
                with self.subTest(value=value):
                    result = subprocess.run([*command, '--queue-timeout-seconds', value, '--', 'true'], env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('Queue timeout', result.stderr)

    def test_launcher_exports_bounded_suite_shard_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / 'environment.py'
            target.write_text('import os\nprint(os.environ["PANDORA_SUITE_SHARDS"])\n')
            env = dict(os.environ, PANDORA_REAL_PNPM='/bin/true')
            command = ['python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state')]
            self.assertEqual(subprocess.check_output([*command, '--', 'python3', str(target)], env=env, text=True).strip(), '4')
            self.assertEqual(subprocess.check_output([*command, '--suite-shards', '12', '--', 'python3', str(target)], env=env, text=True).strip(), '12')
            for value in ('0', '33'):
                with self.subTest(value=value):
                    result = subprocess.run([*command, '--suite-shards', value, '--', 'true'], env=env, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn('Suite shards', result.stderr)


if __name__ == '__main__':
    unittest.main()
