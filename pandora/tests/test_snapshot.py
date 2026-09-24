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


class IndexTest(unittest.TestCase):
    """A file git vouches for is not read, and the manifest cannot tell."""

    def both_ways(self, repo, cache):
        """Freeze by reading everything, then from the index cold and warm."""
        legacy = snapshot.freeze(repo, index=False)
        cold, warm = {}, {}
        self.assertEqual(snapshot.freeze(repo, cache=cache, counts=cold), legacy)
        self.assertEqual(snapshot.freeze(repo, cache=cache, counts=warm), legacy)
        return legacy, cold, warm

    def fixture(self, root):
        repo = make_repo(root, {
            'clean.txt': 'clean\n', 'src/deep.js': 'deep\n', 'modified.txt': 'old\n',
            'restaged.txt': 'one\n', 'deleted.txt': 'gone\n', 'assumed.txt': 'kept\n',
            'crlf.txt': 'a\nb\n', 'big.bin': 'pointer\n',
            '.gitattributes': 'crlf.txt eol=crlf\n*.bin filter=fake\n',
            '.gitignore': '*.log\n'})
        (repo / 'run.sh').write_text('#!/bin/sh\n')
        os.chmod(repo / 'run.sh', 0o755)
        (repo / 'later.sh').write_text('#!/bin/sh\n')
        (repo / 'alias').symlink_to('clean.txt')
        (repo / 'forced.log').write_text('tracked though ignored\n')
        git(repo, 'add', '-f', 'run.sh', 'later.sh', 'alias', 'forced.log')
        git(repo, 'commit', '-qm', 'second')
        (repo / 'modified.txt').write_text('new\n')
        (repo / 'restaged.txt').write_text('two\n')
        git(repo, 'add', 'restaged.txt')
        (repo / 'restaged.txt').write_text('three\n')
        (repo / 'deleted.txt').unlink()
        os.chmod(repo / 'later.sh', 0o755)
        (repo / 'untracked.txt').write_text('u\n')
        (repo / 'ignored.log').write_text('i\n')
        git(repo, 'update-index', '--assume-unchanged', 'assumed.txt')
        (repo / 'assumed.txt').write_text('edited, and git was told not to look\n')
        # Checked out again under eol=crlf: the bytes on disk are not the blob's.
        (repo / 'crlf.txt').unlink()
        git(repo, 'checkout', '--', 'crlf.txt')
        git(repo, 'worktree', 'add', '-q', '-b', 'side', str(repo / 'inner'))
        return repo

    def test_every_kind_of_entry_freezes_exactly_as_when_every_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self.fixture(Path(tmp) / 'repo')
            vouched = set(snapshot.index_blobs(repo))
            self.assertEqual(vouched, {'clean.txt', 'src/deep.js', 'run.sh', 'forced.log',
                                       '.gitattributes', '.gitignore'})
            (manifest, dropped, _), cold, warm = self.both_ways(repo, Path(tmp) / 'cache')
            paths = {record['path'] for record in manifest}
            self.assertIn('untracked.txt', paths)
            self.assertIn('inner/', dropped)
            self.assertFalse({'deleted.txt', 'ignored.log'} & paths)
            self.assertFalse([path for path in paths if path.startswith('inner/')])
            # Six vouched files over two passes: read once, then known everywhere.
            self.assertEqual(cold['index'], 6)
            self.assertEqual(warm['index'], 12)
            self.assertEqual(warm['read'] + warm['stat'] + 6, cold['read'] + cold['stat'])

    def test_a_same_size_rewrite_in_the_index_s_own_second_is_seen(self):
        # Racy git: with only mtime seconds and size compared, the stat still
        # matches. Git must compare content itself, and the freeze must not
        # vouch for the old blob.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            git(root, 'init', '-q', '-b', 'main')
            git(root, 'config', 'core.trustctime', 'false')
            git(root, 'config', 'core.checkStat', 'minimal')
            repo = make_repo(root, {'racy.txt': 'aaaa\n', 'other.txt': 'o\n'})
            before = os.stat(repo / 'racy.txt')
            (repo / 'racy.txt').write_text('bbbb\n')
            os.utime(repo / 'racy.txt', ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertNotIn('racy.txt', snapshot.index_blobs(repo))
            manifest, _, _ = self.both_ways(repo, Path(tmp) / 'cache')[0]
            record = next(item for item in manifest if item['path'] == 'racy.txt')
            self.assertEqual(record['sha256'], snapshot.digest(repo / 'racy.txt'))

    def test_a_fresh_worktree_at_a_known_commit_reads_no_tracked_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n', 'src/b.js': 'b\n'})
            cache = Path(tmp) / 'cache'
            first = snapshot.freeze(repo, cache=cache)
            git(repo, 'worktree', 'add', '-q', '--detach', str(Path(tmp) / 'fresh'))
            counts = {}
            second = snapshot.freeze(Path(tmp) / 'fresh', cache=cache, counts=counts)
            self.assertEqual(second[2], first[2])
            self.assertEqual(counts, {'read': 0, 'index': 4, 'stat': 0})

    def test_the_index_is_never_written(self):
        # A status that refreshed the index would take index.lock, and the
        # agent's own `git add` in this worktree would fail on it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            (repo / 'a.txt').touch()                 # stat-dirty: a refresh would rewrite
            index = repo / '.git' / 'index'
            before = (index.read_bytes(), index.stat().st_mtime_ns)
            snapshot.freeze(repo)
            self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)

    def test_bytes_that_are_not_the_blob_never_enter_the_map(self):
        # The file moved between git's answer and our read: remembering that
        # sha256 under the old blob id would mislead every later worktree.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            real = snapshot.index_blobs
            with mock.patch.object(snapshot, 'index_blobs',
                                   lambda root: {'a.txt': '0' * 40} if real(root) else {}):
                snapshot.freeze(repo, cache=Path(tmp) / 'cache')
            self.assertEqual(snapshot.Blobs(Path(tmp) / 'cache' / 'blobs.json').table, {})
            sha, same = snapshot.digest_blob(repo / 'a.txt', real(repo)['a.txt'], 2)
            self.assertTrue(same)
            self.assertEqual(sha, snapshot.digest(repo / 'a.txt'))

    def test_without_git_answers_every_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            real = snapshot._git

            def broken(root, *args, **kwargs):
                if 'status' in args:
                    raise SnapshotError('git status failed')
                return real(root, *args, **kwargs)
            counts = {}
            with mock.patch.object(snapshot, '_git', broken):
                result = snapshot.freeze(repo, counts=counts)
            self.assertEqual(result, snapshot.freeze(repo, index=False))
            self.assertEqual(counts['index'], 0)

    def test_the_map_forgets_what_no_freeze_has_used_for_a_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'blobs.json'
            old = snapshot.Blobs(path)
            old.today -= snapshot.Blobs.KEEP_DAYS + 1
            old.put('a' * 40, 1, 'x' * 64)
            old.save()
            new = snapshot.Blobs(path)
            new.put('b' * 40, 2, 'y' * 64)
            new.save()
            self.assertEqual(set(snapshot.Blobs(path).table), {'b' * 40})

    def test_concurrent_saves_merge_rather_than_overwrite(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'blobs.json'
            maps = [snapshot.Blobs(path) for _ in range(8)]
            for number, blobs in enumerate(maps):
                blobs.put('%040x' % number, number, '%064x' % number)
            threads = [threading.Thread(target=blobs.save) for blobs in maps]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(len(snapshot.Blobs(path).table), 8)
            self.assertEqual([item.name for item in Path(tmp).iterdir()], ['blobs.json'])


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

    def test_the_operator_cli_never_shares_the_daemons_master(self):
        import argparse
        from pandora.snapshot import transfer
        from pandora.worker import cli
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.toml'
            config.write_text('[worker]\nhost = "u@h"\n')
            args = argparse.Namespace(config=str(config), host=None, state=tmp,
                                      engine_root=None)
            _, _, control = cli.target(args)
            self.assertNotEqual(control, Path(tmp) / 'ssh')
            self.assertNotEqual(transfer.control_dir_for(control),
                                transfer.control_dir_for(Path(tmp) / 'ssh'))


class LinkOwnershipTest(unittest.TestCase):
    """`close()` exits a master only when this Link started it."""

    def link(self, tmp, master_running):
        from pandora.snapshot import transfer
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            if '-O' in argv:
                verb = argv[argv.index('-O') + 1]
                return subprocess.CompletedProcess(argv, 0 if master_running and verb == 'check'
                                                   else 255, b'', b'')
            return subprocess.CompletedProcess(argv, 0, b'ok', b'')
        patch = mock.patch.object(transfer.subprocess, 'run', side_effect=fake)
        patch.start()
        self.addCleanup(patch.stop)
        return transfer.Link('user@host', Path(tmp) / 'ssh'), calls

    @staticmethod
    def verbs(calls):
        return [argv[argv.index('-O') + 1] for argv in calls if '-O' in argv]

    def test_a_link_that_started_the_master_exits_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            link, calls = self.link(tmp, master_running=False)
            link.run(['true'])
            link.feed('pass')
            link.close()
            self.assertEqual(self.verbs(calls), ['check', 'exit'])

    def test_a_master_another_process_started_is_left_running(self):
        # Its in-flight rsyncs ride on it; `-O exit` would cut them off.
        with tempfile.TemporaryDirectory() as tmp:
            link, calls = self.link(tmp, master_running=True)
            link.run(['true'])
            link.close()
            self.assertEqual(self.verbs(calls), ['check'])

    def test_an_ssh_call_that_times_out_is_an_unreachable_worker(self):
        from pandora.errors import WorkerUnreachable
        from pandora.snapshot import transfer
        with tempfile.TemporaryDirectory() as tmp:
            link = transfer.Link('user@host', Path(tmp) / 'ssh')
            link.owns_master = False

            def hang(argv, **kwargs):
                raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
            with mock.patch.object(transfer.subprocess, 'run', side_effect=hang):
                with self.assertRaisesRegex(WorkerUnreachable, 'no answer within 60 s'):
                    link.run(['true'], timeout=60)
                with self.assertRaises(WorkerUnreachable):
                    link.feed('pass', timeout=5)

    def test_the_operator_cli_never_exits_a_master(self):
        # Concurrent `pandora worker` verbs share `ssh-cli`; ControlPersist reaps it.
        from pandora.worker.remote import Remote
        with tempfile.TemporaryDirectory() as tmp:
            link, calls = self.link(tmp, master_running=False)
            remote = Remote('user@host', control_dir=Path(tmp) / 'ssh-cli')
            remote.link = link
            remote.link.run(['true'])
            remote.close()
            self.assertEqual(self.verbs(calls), ['check'])

    def test_a_link_that_never_called_exits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            link, calls = self.link(tmp, master_running=False)
            link.close()
            self.assertEqual(calls, [])


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
        # Untracked, so the stat cache is its only shortcut: git vouches for
        # nothing it does not track.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / 'repo', {'a.txt': 'a\n'})
            (repo / 'new.txt').write_text('n\n')
            cache = Path(tmp) / 'cache'
            snapshot.freeze(repo, cache=cache)       # new.txt is seconds old: racy
            calls, patch = self.counting()
            with patch:
                snapshot.freeze(repo, cache=cache)
            self.assertIn('new.txt', calls)
