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


LFS_SPEC = 'version https://git-lfs.github.com/spec/v1'


def lfs_pointer(data):
    import hashlib
    return ('%s\noid sha256:%s\nsize %d\n'
            % (LFS_SPEC, hashlib.sha256(data).hexdigest(), len(data))).encode()


class LfsTreeTest(unittest.TestCase):
    """A Git LFS path is stored as its pointer in a real repository's tree.

    The worker's synthetic repository has no LFS filter, so `git add -A`
    stores the real bytes. The tree it prints must still equal the commit's.
    No git-lfs binary is needed: the pointer blob is written by hand, as the
    clean filter would.
    """

    def setUp(self):
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_CONFIG_SYSTEM=str(self.root / 'system.gitconfig'),
                        GIT_CONFIG_GLOBAL=str(self.root / 'global.gitconfig'),
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@localhost',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@localhost')

    def git(self, repo, *args, stdin=None):
        import subprocess
        return subprocess.run(['git', '-C', str(repo), *args], env=self.env, check=True,
                              input=stdin, capture_output=True).stdout

    def committed(self, files, stored):
        """A real repository whose commit holds `files`, with `stored` overriding
        the blob of a path (the pointer git-lfs would store). Returns HEAD^{tree}."""
        import os
        real = self.root / 'real'
        real.mkdir()
        self.git(real, 'init', '-q', '-b', 'main')
        for name, data in files.items():
            path = real / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(stored.get(name, data))
            if name.endswith('.sh'):
                os.chmod(path, 0o755)
        self.git(real, 'add', '-A')
        self.git(real, 'commit', '-qm', 'first')
        return self.git(real, 'rev-parse', 'HEAD^{tree}').decode().strip()

    def synthetic(self, files):
        """Run GIT_SCRIPT over a work tree holding `files`; returns (tree, run git)."""
        import os
        import subprocess
        from pandora.executor.incus import IncusDriver, tree_of
        work = self.root / 'work'
        for name, data in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            if name.endswith('.sh'):
                os.chmod(path, 0o755)
        lists = self.root / 'marks'
        lists.mkdir()
        for flag in ('untracked', 'ignored'):
            (lists / flag).write_bytes(b'')
        proc = subprocess.run(['sh', '-c', IncusDriver.GIT_SCRIPT, 'git', str(work),
                               str(lists), 'pandora abc'], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(lists.exists())
        return tree_of(proc.stdout), work

    ATTRS = b'*.bin filter=lfs diff=lfs merge=lfs -text\n'

    def test_the_tree_stores_the_pointer_for_real_bytes(self):
        data = bytes(range(256)) * 40 + b'\0tail'
        tool = b'#!/bin/sh\n' + bytes(range(256))
        files = {'.gitattributes': self.ATTRS, 'assets/model.bin': data,
                 'assets/run.sh.bin': tool, 'readme.md': b'hi\n',
                 'dir with space/a b.bin': b'spaced\n'}
        want = self.committed(files, {name: lfs_pointer(body) for name, body in files.items()
                                      if name.endswith('.bin')})
        tree, work = self.synthetic(files)
        self.assertEqual(tree, want)
        # The working bytes are what the run needs, and stay untouched.
        self.assertEqual((work / 'assets/model.bin').read_bytes(), data)
        # The commit keeps the real bytes, so `git status` in the run is clean
        # without git-lfs installed.
        self.assertEqual(self.git(work, 'status', '--porcelain'), b'')
        self.assertEqual(self.git(work, 'cat-file', 'blob', 'HEAD:assets/model.bin'), data)

    def test_the_mode_of_an_lfs_entry_is_kept(self):
        import os
        data = b'\x7fELF' + bytes(100)
        real = self.root / 'real'
        real.mkdir()
        self.git(real, 'init', '-q', '-b', 'main')
        (real / '.gitattributes').write_bytes(self.ATTRS)
        (real / 'tool.bin').write_bytes(lfs_pointer(data))
        os.chmod(real / 'tool.bin', 0o755)
        self.git(real, 'add', '-A')
        self.git(real, 'commit', '-qm', 'first')
        want = self.git(real, 'rev-parse', 'HEAD^{tree}').decode().strip()
        work = self.root / 'work'
        work.mkdir()
        (work / 'tool.bin').write_bytes(data)
        os.chmod(work / 'tool.bin', 0o755)
        tree, work = self.synthetic({'.gitattributes': self.ATTRS})
        self.assertEqual(tree, want)
        listing = self.git(work, 'ls-tree', tree, 'tool.bin').decode()
        self.assertTrue(listing.startswith('100755 blob '), listing)

    def test_a_file_already_a_pointer_is_not_pointed_at_again(self):
        # Checked out without git-lfs: the work tree holds the pointer itself.
        pointer = lfs_pointer(b'the real content\n')
        files = {'.gitattributes': self.ATTRS, 'big.bin': pointer}
        want = self.committed(files, {})
        tree, _work = self.synthetic(files)
        self.assertEqual(tree, want)

    def test_a_repository_without_lfs_is_unchanged(self):
        files = {'.gitattributes': b'*.txt text\n', 'a.bin': bytes(range(256)),
                 'b.txt': b'b\n', 'run.sh': b'#!/bin/sh\n'}
        want = self.committed(files, {})
        tree, work = self.synthetic(files)
        self.assertEqual(tree, want)
        self.assertEqual(self.git(work, 'rev-parse', 'HEAD^{tree}').decode().strip(), want)
