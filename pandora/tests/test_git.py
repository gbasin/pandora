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

    def test_the_transfer_is_logged_from_start_to_its_end(self):
        from pandora.errors import TransferError
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            backend = worker_module.Worker.__new__(worker_module.Worker)
            backend.link, backend._root = None, '/engine'
            backend._bundle = {'path': '/engine/bundles/x'}
            backend.engine = lambda argv, stdin=None, timeout=None: {'ok': True, 'run_id': 'r1'}
            plan = {'repo': 'demo', 'secrets_exclude_globs': [], 'git': 'none'}
            lines = []

            def shipped(*args, on_send=None, stderr_path=None, **kwargs):
                on_send()
                return {'path': '/engine/src/demo/x', 'reused': False}

            def failed(*args, on_send=None, stderr_path=None, **kwargs):
                on_send()
                error = TransferError('rsync to h failed (255): unexpected end of file')
                error.rsync_exit = 255
                raise error
            with mock.patch.object(worker_module.transfer, 'send', side_effect=shipped):
                backend.submit(plan=plan, worktree=repo, request_id='q1', log=lines.append)
            with mock.patch.object(worker_module.transfer, 'send', side_effect=failed):
                with self.assertRaises(TransferError):
                    backend.submit(plan=plan, worktree=repo, request_id='q2', log=lines.append)
        self.assertRegex(lines[0], r'^transfer start: input \w+, 1 files, 1 KiB, from ')
        self.assertRegex(lines[1], r'^transfer done: input \w+, 1 files, 1 KiB, rsync exit 0, ')
        self.assertRegex(lines[3], r'^transfer failed: input \w+, syncing 1 files, 1 KiB, '
                                   r'rsync exit 255, after [\d.]+ s: rsync to h failed')

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
        proc = subprocess.run(['sh', '-c', IncusDriver.GIT_SCRIPT, 'git', str(root / 'work'),
                               str(lists), 'pandora abc'], check=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertFalse(lists.exists(), 'the lists are removed once used')
        self.printed = proc.stdout

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

    def test_the_tree_is_every_file_the_run_sees_untracked_included(self):
        import os
        import subprocess
        from pandora.executor.incus import tree_of
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.tree(root)
            # The same files, staged by hand the way a person would before
            # `git commit`: everything `git add -A` takes, plus the tracked
            # ignored file. That index's tree is what the verdict must name.
            expect = root / 'expect'
            env = dict(os.environ, GIT_CONFIG_SYSTEM=str(root / 'system.gitconfig'),
                       GIT_CONFIG_GLOBAL=str(root / 'global.gitconfig'))
            subprocess.run(['cp', '-R', str(root / 'work'), str(expect)], check=True)

            def plain(*args):
                return subprocess.run(['git', '-C', str(expect), *args], env=env, check=True,
                                      capture_output=True, text=True).stdout
            plain('init', '-q')
            plain('add', '-A')
            plain('add', '-f', 'forced.log')
            want = plain('write-tree').strip()
            git = self.build(root, {'untracked': ['scratch [1].md'],
                                    'ignored': ['forced.log']})
            tree = tree_of(self.printed)
            self.assertRegex(tree or '', r'^[0-9a-f]{40}$')
            self.assertEqual(tree, want)
            listed = git('ls-tree', '-r', '--name-only', tree).split('\n')[:-1]
            self.assertIn('scratch [1].md', listed)
            self.assertIn('forced.log', listed)
            self.assertNotIn('noise.log', listed)
            # The untracked file left the index after the tree was taken, so
            # the commit is still the caller's tracked set.
            self.assertNotEqual(git('rev-parse', 'HEAD^{tree}').strip(), tree)

    def test_the_tree_matches_a_local_write_tree_for_modes_links_and_crlf(self):
        # Pins git's normalization as the verdict sees it: the tree is git's
        # view of the bytes, not the bytes. Under `* text=auto` a CRLF file is
        # stored with LF, so the tree equals a local `git add -A; git write-tree`
        # over the same files but not a checkout whose bytes keep the CRLF. If
        # this ever changes, the verdict's tree changes with it.
        import os
        import subprocess
        from pandora.executor.incus import tree_of
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / 'work'
            (work / 'bin').mkdir(parents=True)
            (work / '.gitattributes').write_text('* text=auto\n')
            (work / 'bin' / 'run').write_text('#!/bin/sh\necho hi\n')
            os.chmod(work / 'bin' / 'run', 0o755)
            (work / 'dos.txt').write_bytes(b'one\r\ntwo\r\n')
            (work / 'link').symlink_to('bin/run')
            env = dict(os.environ, GIT_CONFIG_SYSTEM=str(root / 'system.gitconfig'),
                       GIT_CONFIG_GLOBAL=str(root / 'global.gitconfig'))
            expect = root / 'expect'
            subprocess.run(['cp', '-Rp', str(work), str(expect)], check=True)

            def plain(*args):
                return subprocess.run(['git', '-C', str(expect), *args], env=env, check=True,
                                      capture_output=True, text=True).stdout
            plain('init', '-q')
            plain('add', '-A')
            want = plain('write-tree').strip()
            git = self.build(root, {})
            tree = tree_of(self.printed)
            self.assertEqual(tree, want)
            entries = {line.split('\t')[1]: line.split()[0]
                       for line in git('ls-tree', '-r', tree).splitlines()}
            self.assertEqual(entries['bin/run'], '100755')
            self.assertEqual(entries['link'], '120000')
            self.assertEqual(entries['dos.txt'], '100644')
            self.assertEqual(git('cat-file', 'blob', '%s:link' % tree), 'bin/run')
            stored = subprocess.run(['git', '-C', str(work), 'cat-file', 'blob',
                                     '%s:dos.txt' % tree], env=env, check=True,
                                    capture_output=True).stdout
            self.assertEqual(stored, b'one\ntwo\n')
            self.assertEqual((work / 'dos.txt').read_bytes(), b'one\r\ntwo\r\n')

    def test_the_tree_is_taken_after_the_ignored_add_and_before_the_untracked_rm(self):
        from pandora.executor.incus import IncusDriver
        script = IncusDriver.GIT_SCRIPT
        added = script.index('add -f --pathspec-from-file="$2/ignored"')
        taken = script.index('git write-tree')
        removed = script.index('rm -q --cached')
        self.assertLess(script.index('git add -A'), added)
        self.assertLess(added, taken)
        self.assertLess(taken, removed)
        self.assertLess(removed, script.index('git commit'))

    def test_a_missing_tree_line_is_none(self):
        from pandora.executor.incus import tree_of
        self.assertIsNone(tree_of(''))
        self.assertIsNone(tree_of('pandora-tree xyz\n'))
        self.assertEqual(tree_of('noise\npandora-tree %s\n' % ('a' * 40)), 'a' * 40)
