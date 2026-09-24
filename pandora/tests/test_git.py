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
        self.assertEqual(load_text(MINIMAL)['jobs']['suite']['git'], 'none')

    def test_synthetic_is_accepted_and_anything_else_is_refused(self):
        self.assertEqual(load_text(MINIMAL + 'git = "synthetic"\n')['jobs']['suite']['git'],
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
            backend._bundle = {'path': '/engine/bundles/x'}   # submit resolves it first

            def engine(argv, stdin=None, timeout=None):
                sent.update(json.loads(stdin))
                return {'ok': True, 'run_id': 'r1'}

            backend.engine = engine
            plan = {'repo': 'demo', 'secrets_exclude_globs': [], 'git': plan_git}
            with mock.patch.object(worker_module.transfer, 'send',
                                   return_value={'path': '/engine/src/demo/x'}):
                backend.submit(plan=plan, worktree=repo, request_id='q1')
            return sent

    def test_each_step_is_announced_and_a_failure_keeps_the_timings_so_far(self):
        from pandora.errors import TransferError
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            backend = worker_module.Worker.__new__(worker_module.Worker)
            backend.link, backend._root = None, '/engine'
            steps = []
            plan = {'repo': 'demo', 'secrets_exclude_globs': [], 'git': 'none'}
            with mock.patch.object(worker_module.transfer, 'send',
                                   side_effect=TransferError('rsync timed out')):
                with self.assertRaises(TransferError) as caught:
                    backend.submit(plan=plan, worktree=repo, request_id='q1',
                                   phase=steps.append)
        self.assertEqual(steps, ['freeze', 'ship'])
        self.assertEqual(set(caught.exception.pre_accept), {'freeze', 'ship'})

    def test_the_exception_lists_travel_only_when_the_job_asks_for_git(self):
        self.assertEqual(self.submit('synthetic')['git_marks'],
                         {'untracked': ['scratch.md'], 'ignored': []})
        self.assertNotIn('git_marks', self.submit('none'))


if __name__ == '__main__':
    unittest.main()


class ScriptTest(unittest.TestCase):
    """The shell the driver runs inside an instance, run here against a real git."""

    def build(self, root, marks):
        import os
        import subprocess
        from pandora.executor.incus import IncusDriver
        lists = root / 'marks'
        lists.mkdir()
        for flag in ('untracked', 'ignored'):
            (lists / flag).write_bytes(b''.join(p.encode() + b'\0' for p in marks.get(flag, [])))
        env = dict(os.environ, GIT_CONFIG_SYSTEM=str(root / 'system.gitconfig'),
                   GIT_CONFIG_GLOBAL=str(root / 'global.gitconfig'))
        subprocess.run(['sh', '-c', IncusDriver.GIT_SCRIPT, 'git', str(root / 'work'),
                        str(lists), 'pandora abc'], check=True, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.assertFalse(lists.exists(), 'the lists are removed once used')

        def git(*args):
            return subprocess.run(['git', '-C', str(root / 'work'), *args], env=env,
                                  check=True, capture_output=True, text=True).stdout
        return git

    def tree(self, root):
        work = root / 'work'
        (work / 'docs').mkdir(parents=True)
        (work / '.gitignore').write_text('*.log\n')
        (work / 'docs/a.md').write_text('status: gold\n')
        (work / 'scratch [1].md').write_text('no status\n')
        (work / 'forced.log').write_text('kept\n')
        (work / 'noise.log').write_text('ignored\n')

    def test_the_index_is_the_callers_tracked_set_and_head_is_deterministic(self):
        heads = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.tree(root)
                git = self.build(root, {'untracked': ['scratch [1].md'],
                                        'ignored': ['forced.log']})
                self.assertEqual(git('ls-files').split('\n')[:-1],
                                 ['.gitignore', 'docs/a.md', 'forced.log'])
                self.assertEqual(git('ls-files', '--others', '--exclude-standard'),
                                 'scratch [1].md\n')
                self.assertEqual(git('diff', 'HEAD', '--stat'), '')
                heads.append(git('rev-parse', 'HEAD'))
        self.assertEqual(heads[0], heads[1])
