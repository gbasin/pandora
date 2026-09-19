from pathlib import Path
import subprocess
import tempfile
import unittest
from snapshot import freeze, verify


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


if __name__ == '__main__':
    unittest.main()
