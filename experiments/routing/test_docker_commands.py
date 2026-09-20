import json
from pathlib import Path
import unittest
from docker_commands import classify, profile

CONFIG = {'dockerfiles': ['Dockerfile'], 'mounts': ['/workspace'],
          'outputs': [{'container': '/workspace/dist', 'workspace': 'dist'}]}


class DockerCommandsTests(unittest.TestCase):
    def test_supported_commands_preserve_argument_boundaries(self):
        root = Path('/tmp/worktree')
        self.assertEqual(classify(['build', '-t', 'app', '.'], root, CONFIG),
                         {'kind': 'build', 'tag': 'app:latest', 'dockerfile': 'Dockerfile'})
        request = classify(['run', '--rm', '-v', '/tmp/worktree:/workspace:ro', 'app:test', 'sh', '-c', 'echo one two'], root, CONFIG)
        self.assertEqual(request['command'], ['sh', '-c', 'echo one two'])
        self.assertTrue(request['mount']['readonly'])
        self.assertEqual(classify(['image', 'rm', 'app:test'], root, CONFIG)['kind'], 'remove')

    def test_unsupported_patterns_never_fall_back(self):
        for argv in (['ps'], ['run', '-d', 'app:test'], ['run', '--rm', '-p', '8080:80', 'app:test'],
                     ['run', '--rm', '-v', '/:/workspace', 'app:test'],
                     ['build', '-t', 'x', '-t', 'y', '.'], ['build', '--build-arg', 'X=y', '-t', 'x', '.'],
                     ['build', '-f', '../Dockerfile', '-t', 'x', '.'], ['run', 'app:test']):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                classify(argv, Path('/tmp/worktree'), CONFIG)

    def test_profile_rejects_overlapping_and_escaping_outputs(self):
        for outputs in ([{'container': '/workspace/dist', 'workspace': '../escape'}],
                        [{'container': '/workspace/dist', 'workspace': 'dist'},
                         {'container': '/workspace/other', 'workspace': 'dist/nested'}]):
            with self.assertRaises(ValueError):
                profile(json.dumps({**CONFIG, 'outputs': outputs}))
