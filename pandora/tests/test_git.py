"""The synthetic repository: declared per job, built from the caller's tracked set."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import worker as worker_module
from pandora.errors import ConfigError
from pandora.tests.test_config import MINIMAL, load_text
from pandora.tests.test_snapshot import make_repo


class GitKeyTest(unittest.TestCase):
    def test_the_default_is_no_repository(self):
        self.assertEqual(load_text(MINIMAL)['jobs'][0]['git'], 'none')

    def test_synthetic_is_accepted_and_anything_else_is_refused(self):
        self.assertEqual(load_text(MINIMAL + 'git = "synthetic"\n')['jobs'][0]['git'],
                         'synthetic')
        with self.assertRaisesRegex(ConfigError, 'git must be one of'):
            load_text(MINIMAL + 'git = "mirror"\n')

    def test_a_local_job_already_has_a_real_checkout(self):
        with self.assertRaisesRegex(ConfigError, 'already runs in a real checkout'):
            load_text(MINIMAL + 'git = "synthetic"\nwhere = "local"\n')


class SubmitTest(unittest.TestCase):
    def submit(self, plan_git):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            (repo / 'scratch.md').write_text('x\n')
            sent = {}
            backend = worker_module.Worker.__new__(worker_module.Worker)
            backend.link, backend._root = None, '/engine'

            def engine(argv, stdin=None, timeout=None):
                sent.update(json.loads(stdin))
                return {'ok': True, 'run_id': 'r1'}

            backend.engine = engine
            plan = {'repo': 'demo', 'secrets_exclude_globs': [], 'git': plan_git}
            with mock.patch.object(worker_module.transfer, 'send',
                                   return_value={'path': '/engine/src/demo/x'}):
                backend.submit(plan=plan, worktree=repo, request_id='q1')
            return sent

    def test_the_exception_lists_travel_only_when_the_job_asks_for_git(self):
        self.assertEqual(self.submit('synthetic')['git_marks'],
                         {'untracked': ['scratch.md'], 'ignored': []})
        self.assertNotIn('git_marks', self.submit('none'))


if __name__ == '__main__':
    unittest.main()
