"""Freezing a worktree: what travels, what does not, and what the id means."""
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from pandora.errors import SnapshotError
from pandora.snapshot import freeze as snapshot


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo(root, files):
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.email', 'test@example.com')
    git(root, 'config', 'user.name', 'Test')
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'first')
    return root


class ExcludeTest(unittest.TestCase):
    def test_secret_shapes_are_excluded_by_name(self):
        for name in ('.env', '.env.local', 'apps/web/.dev.vars', 'certs/server.pem',
                     'keys/id_ed25519', 'a/.ssh/config', 'node_modules/x/index.js'):
            self.assertTrue(snapshot.excluded(name), name)

    def test_credential_files_a_repository_may_track_are_excluded(self):
        # Each of these was shipped when tracked or not gitignored. Suffixes
        # match in any case: a `.PEM` is the same key as a `.pem`.
        for name in ('.envrc', 'tools/.envrc', '.netrc', '.git-credentials', '.pypirc',
                     'keys/id_ecdsa', 'keys/id_ed25519', 'keys/id_rsa', 'certs/SERVER.PEM',
                     'certs/tls.Key', 'dist/Signing.P12'):
            self.assertTrue(snapshot.excluded(name), name)
        for name in ('src/keyboard.ts', 'docs/pem-format.md', 'id_rsa.pub', '.envrc.example'):
            self.assertFalse(snapshot.excluded(name), name)

    def test_an_example_env_file_is_not_a_secret(self):
        for name in ('.env.example', '.env.sample', 'config/.dev.vars.template'):
            self.assertFalse(snapshot.excluded(name), name)

    def test_the_repository_may_widen_the_list_but_the_builtins_always_apply(self):
        self.assertTrue(snapshot.excluded('secrets/thing.txt', ['secrets/*']))
        self.assertTrue(snapshot.excluded('.env', []))


class FreezeTest(unittest.TestCase):
    def test_tracked_and_untracked_source_travels_and_secrets_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', 'src/b.js': 'b\n'})
            (repo / 'untracked.txt').write_text('u\n')
            (repo / '.env').write_text('TOKEN=hunter2\n')
            (repo / 'ignored.log').write_text('x\n')
            (repo / '.gitignore').write_text('ignored.log\n')
            manifest, dropped, input_id = snapshot.freeze(repo)
            names = {record['path'] for record in manifest}
            self.assertIn('a.txt', names)
            self.assertIn('src/b.js', names)
            self.assertIn('untracked.txt', names)
            self.assertNotIn('.env', names)
            self.assertNotIn('ignored.log', names)
            self.assertIn('.env', dropped)
            self.assertEqual(len(input_id), 64)

    def test_a_tracked_credential_file_does_not_travel(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', '.envrc': 'export X=1\n',
                                                  'deploy/KEY.PEM': 'k\n', '.netrc': 'm\n'})
            manifest, dropped, _ = snapshot.freeze(repo, exclude_globs=['extra/*'])
            names = {record['path'] for record in manifest}
            self.assertEqual(names, {'a.txt'})
            self.assertTrue({'.envrc', 'deploy/KEY.PEM', '.netrc'} <= set(dropped))

    def test_what_git_would_answer_differently_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', 'forced.log': 'f\n'})
            (repo / '.gitignore').write_text('*.log\n')
            (repo / 'scratch.md').write_text('no status\n')
            manifest, _, _ = snapshot.freeze(repo)
            marks = {record['path']: record.get('git') for record in manifest}
            self.assertIsNone(marks['a.txt'])
            self.assertEqual(marks['scratch.md'], 'untracked')
            self.assertEqual(marks['forced.log'], 'ignored')
            self.assertEqual(snapshot.git_marks(manifest),
                             {'untracked': ['.gitignore', 'scratch.md'],
                              'ignored': ['forced.log']})

    def test_the_tracked_set_is_part_of_the_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = make_repo(Path(tmp) / 'one', {'a.txt': 'a\n', 'b.txt': 'b\n'})
            two = make_repo(Path(tmp) / 'two', {'a.txt': 'a\n'})
            (two / 'b.txt').write_text('b\n')
            self.assertNotEqual(snapshot.freeze(one)[2], snapshot.freeze(two)[2])

    def test_equal_content_in_two_worktrees_is_one_input_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = make_repo(Path(tmp) / 'one', {'a.txt': 'a\n'})
            two = make_repo(Path(tmp) / 'two', {'a.txt': 'a\n'})
            self.assertEqual(snapshot.freeze(one)[2], snapshot.freeze(two)[2])

    def test_one_changed_byte_is_a_different_input_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            before = snapshot.freeze(repo)[2]
            (repo / 'a.txt').write_text('b\n')
            self.assertNotEqual(before, snapshot.freeze(repo)[2])

    def test_an_executable_bit_is_part_of_the_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'run.sh': '#!/bin/sh\n'})
            before = snapshot.freeze(repo)[2]
            os.chmod(repo / 'run.sh', 0o755)
            self.assertNotEqual(before, snapshot.freeze(repo)[2])

    def test_a_symlink_out_of_the_worktree_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            (repo / 'escape').symlink_to('/etc/hosts')
            with self.assertRaises(SnapshotError):
                snapshot.freeze(repo)

    def test_an_internal_symlink_travels_as_a_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            (repo / 'alias').symlink_to('a.txt')
            manifest, _, _ = snapshot.freeze(repo)
            record = next(item for item in manifest if item['path'] == 'alias')
            self.assertEqual(record['link'], 'a.txt')

    def test_a_credential_bearing_npmrc_is_refused_rather_than_shipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            (repo / '.npmrc').write_text('//registry/:_authToken=secret\n')
            # It is excluded by the eichler glob, but a repository without that
            # glob must still not ship it.
            with self.assertRaises(SnapshotError):
                snapshot.freeze(repo)

    def test_a_nested_registered_worktree_does_not_travel(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            git(repo, 'worktree', 'add', '-q', '-b', 'side', str(repo / 'inner'))
            manifest, dropped, _ = snapshot.freeze(repo)
            self.assertFalse([item for item in manifest if item['path'].startswith('inner/')],
                             'a nested worktree would ship the repository twice')
            self.assertIn('inner/', dropped)

    def test_verify_accepts_a_materialized_tree_and_rejects_a_tampered_one(self):
        # `verify` is for the tree the worker materialized, which contains the
        # manifest and nothing else -- not for the worktree it came from, which
        # also has .git in it.
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', 'src/b.js': 'b\n'})
            manifest, _, _ = snapshot.freeze(repo)
            materialized = Path(tmp) / 'shipped'
            for record in manifest:
                target = materialized / record['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(repo / record['path'], target)
            snapshot.verify(materialized, manifest)
            (materialized / 'a.txt').write_text('tampered\n')
            with self.assertRaises(SnapshotError):
                snapshot.verify(materialized, manifest)


class TransferPathTest(unittest.TestCase):
    def test_the_cache_is_addressed_by_input_id(self):
        from pandora.snapshot import transfer
        paths = transfer.cache_paths('/home/w/engine', 'eichler', 'abc123')
        self.assertEqual(paths['final'], '/home/w/engine/src/eichler/abc123')
        self.assertEqual(paths['partial'], '/home/w/engine/src/eichler/abc123.partial')
        self.assertEqual(paths['latest'], '/home/w/engine/src/eichler/latest')

    def test_the_control_socket_is_per_state_directory(self):
        from pandora.snapshot import transfer
        with tempfile.TemporaryDirectory() as tmp:
            link = transfer.Link('user@host', Path(tmp) / 'ssh')
            self.assertIn('ControlMaster=auto', link.options)
            self.assertIn('ControlPersist=10m', link.options)
            self.assertTrue(any(item.endswith('/ssh-%C') for item in link.options))
            self.assertEqual(oct(os.stat(link.control_dir).st_mode & 0o777), '0o700')
            # A unix socket path is capped near 104 bytes and ssh appends its own
            # suffix, so the whole control path must stay well short of that.
            path = next(item for item in link.options if item.endswith('/ssh-%C'))
            self.assertLess(len(path) + 40 + 20, 104 + 40,
                            'the control path would overflow a unix socket name')
            self.assertLess(len(str(link.control_dir)), 45)


if __name__ == '__main__':
    unittest.main()


class DigestsTest(unittest.TestCase):
    def aged(self, repo):
        """As if every file were older than the racy window, as a worktree's are.
        (`os.utime` cannot age a ctime, so the window is closed instead.)"""
        patch = mock.patch.object(snapshot.Digests, 'RACY_NS', 0)
        patch.start()
        self.addCleanup(patch.stop)

    def counting(self):
        calls = []
        real = snapshot.digest

        def counted(path):
            calls.append(path.name)
            return real(path)
        return calls, mock.patch.object(snapshot, 'digest', counted)

    def test_an_unchanged_file_is_not_read_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', 'b.txt': 'b\n'})
            self.aged(repo)
            cache = Path(tmp) / 'cache'
            first = snapshot.freeze(repo, cache=cache)
            calls, patch = self.counting()
            with patch:
                second = snapshot.freeze(repo, cache=cache)
            self.assertEqual(second, first)
            self.assertEqual(calls, [])

    def test_a_changed_file_is_read_and_changes_the_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', 'b.txt': 'b\n'})
            self.aged(repo)
            cache = Path(tmp) / 'cache'
            first = snapshot.freeze(repo, cache=cache)
            (repo / 'a.txt').write_text('A\n')          # same size, new bytes
            calls, patch = self.counting()
            with patch:
                second = snapshot.freeze(repo, cache=cache)
            self.assertNotEqual(second[2], first[2])
            self.assertIn('a.txt', calls)
            self.assertNotIn('b.txt', calls)

    def test_a_file_written_just_now_is_never_trusted_to_its_stat(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            cache = Path(tmp) / 'cache'
            snapshot.freeze(repo, cache=cache)       # a.txt is seconds old: racy
            calls, patch = self.counting()
            with patch:
                snapshot.freeze(repo, cache=cache)
            self.assertIn('a.txt', calls)
