from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import snapshot
from snapshot import encode, freeze, verify


def current_digest_for_test(repo):
    original_path = sys.path[:]
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'routing'))
        from route import current_digest
        return current_digest(repo)
    finally:
        sys.path[:] = original_path


class SnapshotTests(unittest.TestCase):
    def test_dirty_deleted_untracked_and_later_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'tracked').write_text('original')
            (repo / 'deleted').write_text('remove me')
            (repo / '.gitignore').write_text('ignored\n')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            (repo / 'tracked').write_text('dirty at submission')
            (repo / 'deleted').unlink()
            (repo / 'untracked').write_text('included')
            (repo / 'ignored').write_text('not included')
            (repo / '.env').write_text('not included either')
            index_before = (repo / '.git/index').read_bytes()
            frozen = root / 'snapshot'
            manifest, excluded = freeze(repo, frozen)
            self.assertEqual(index_before, (repo / '.git/index').read_bytes())
            self.assertEqual((frozen / 'tracked').read_text(), 'dirty at submission')
            self.assertEqual((frozen / 'untracked').read_text(), 'included')
            self.assertFalse((frozen / 'deleted').exists())
            self.assertFalse((frozen / 'ignored').exists())
            self.assertIn('.env', excluded)
            (repo / 'tracked').write_text('later edit')
            verify(frozen, manifest)
            self.assertEqual((frozen / 'tracked').read_text(), 'dirty at submission')
            (frozen / 'tracked').write_text('tampered')
            with self.assertRaises(RuntimeError):
                verify(frozen, manifest)

    def test_external_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            (repo / 'external').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                freeze(repo, Path(temp) / 'snapshot')

    def test_registered_nested_worktree_is_excluded_without_gitignore(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Snapshot test'], check=True)
            (repo / 'source.txt').write_text('source')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'seed'], check=True)
            nested = repo / 'nested'
            subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', str(nested), '-b', 'nested'], check=True)
            (nested / 'only-in-nested.txt').write_text('do not capture')

            manifest, exclusions = freeze(repo, root / 'snapshot')

            self.assertEqual([entry['path'] for entry in manifest], ['source.txt'])
            self.assertEqual(exclusions, ['nested/'])
            self.assertFalse((root / 'snapshot' / 'nested').exists())
            self.assertEqual(current_digest_for_test(repo), hashlib.sha256(encode(manifest)).hexdigest())

    def test_unregistered_nested_repository_remains_an_actionable_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            nested = repo / 'unknown'
            subprocess.run(['git', 'init', '-q', str(nested)], check=True)

            with self.assertRaisesRegex(ValueError, 'Unsupported source entry.*unknown/'):
                freeze(repo, root / 'snapshot')

    def test_worktree_registration_change_during_capture_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Snapshot test'], check=True)
            (repo / 'source.txt').write_text('source')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'seed'], check=True)
            original = snapshot.entry
            added = False

            def add_nested_worktree(capture_root, name):
                nonlocal added
                result = original(capture_root, name)
                if not added and capture_root.resolve() == repo.resolve() and name == 'source.txt':
                    added = True
                    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q',
                                    str(repo / 'nested'), '-b', 'nested'], check=True)
                return result

            with patch.object(snapshot, 'entry', side_effect=add_nested_worktree):
                with self.assertRaisesRegex(RuntimeError, 'Source changed during capture'):
                    freeze(repo, root / 'snapshot')

    def test_stale_registered_path_is_captured_as_ordinary_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'repo'
            repo.mkdir()
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'], check=True)
            subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Snapshot test'], check=True)
            (repo / 'source.txt').write_text('source')
            subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'seed'], check=True)
            nested = repo / 'nested'
            subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', str(nested), '-b', 'nested'], check=True)
            shutil.rmtree(nested)
            nested.mkdir()
            (nested / 'ordinary.txt').write_text('ordinary source')

            manifest, exclusions = freeze(repo, root / 'snapshot')

            self.assertEqual([entry['path'] for entry in manifest], ['nested/ordinary.txt', 'source.txt'])
            self.assertEqual(exclusions, [])


if __name__ == '__main__':
    unittest.main()
