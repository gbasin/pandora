import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from source_cache import repository_key, prepare, publish, protected


class SourceCacheTests(unittest.TestCase):
    def test_worktrees_share_cache_but_other_repositories_do_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ['repo', 'other']:
                subprocess.run(['git', 'init', '-q', str(root / name)], check=True)
            subprocess.run(['git', '-C', str(root / 'repo'), '-c', 'user.name=Test',
                            '-c', 'user.email=test@example.com', 'commit', '--allow-empty',
                            '-qm', 'seed'], check=True)
            nested = root / 'repo' / 'nested'
            subprocess.run(['git', '-C', str(root / 'repo'), 'worktree', 'add', '-qb',
                            'nested', str(nested)], check=True)
            self.assertEqual(repository_key(root / 'repo'), repository_key(nested))
            self.assertNotEqual(repository_key(root / 'repo'), repository_key(root / 'other'))

    def test_private_seed_survives_pointer_change_and_attempt_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b, c = [root / 'runs' / (x * 32) for x in 'abc']
            for attempt in [a, b, c]:
                (attempt / 'source').mkdir(parents=True)
            (a / 'source' / 'file').write_text('original')
            (a / 'source' / 'link').symlink_to('file')
            publish(root, 'a' * 64, a)
            publish(root, 'b' * 64, b)
            seed = Path(prepare(root, 'a' * 64, c))
            self.assertEqual(os.stat(seed / 'file').st_ino, os.stat(a / 'source' / 'file').st_ino)
            self.assertTrue((seed / 'link').is_symlink())
            self.assertIn(a.resolve(), protected(root))
            publish(root, 'a' * 64, b)
            shutil.rmtree(a)
            self.assertEqual((seed / 'file').read_text(), 'original')
            publish(root, 'a' * 64, c)
            self.assertFalse(seed.exists())
            self.assertEqual(prepare(root, 'c' * 64, b), '')


if __name__ == '__main__':
    unittest.main()
