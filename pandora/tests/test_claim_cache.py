"""Each worktree routes by a claim cache derived from its own pandora.toml."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from pandora.client import enrollment



def age(path, seconds=60):
    """Date a file `seconds` ago; returns the mtime it now has."""
    stamp = time.time_ns() - seconds * 10**9
    os.utime(path, ns=(stamp, stamp))
    return Path(path).stat().st_mtime_ns


class Paths(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()

    def test_the_cache_lives_in_each_worktrees_own_git_dir(self):
        main = self.root / 'main'
        (main / '.git' / 'worktrees' / 'b').mkdir(parents=True)
        branch = self.root / 'b'
        branch.mkdir()
        (branch / '.git').write_text('gitdir: %s\n' % (main / '.git' / 'worktrees' / 'b'))
        (main / '.git' / 'worktrees' / 'b' / 'gitdir').write_text(str(branch / '.git') + '\n')
        self.assertEqual(enrollment.cache_path(main), main / '.git' / 'pandora-claims')
        self.assertEqual(enrollment.cache_path(branch),
                         main / '.git' / 'worktrees' / 'b' / 'pandora-claims')
        self.assertEqual(enrollment.caches_of(main / '.git'),
                         [(str(main), main / '.git' / 'pandora-claims'),
                          (str(branch), main / '.git' / 'worktrees' / 'b' / 'pandora-claims')])

    def test_a_cache_is_dated_as_its_newest_source(self):
        one, two, cache = self.root / 'one', self.root / 'two', self.root / 'cache'
        one.write_text('1')
        two.write_text('2')
        age(one, 120)
        newest = age(two, 60)
        self.assertTrue(enrollment.write_cache(cache, 'x\n', [one, two, None]))
        self.assertEqual(cache.stat().st_mtime_ns, newest)
        self.assertFalse(enrollment.write_cache(cache, 'x\n', [one, two]))   # nothing to do

    def test_a_source_edited_just_now_leaves_the_cache_dated_before_it(self):
        source, cache = self.root / 'source', self.root / 'cache'
        source.write_text('1')
        enrollment.write_cache(cache, 'x\n', [source])
        self.assertLessEqual(cache.stat().st_mtime_ns,
                             source.stat().st_mtime_ns - enrollment.SETTLE_NS)
        # Once it settles, the same text is re-dated exactly: a rewrite, not a no-op.
        stamp = age(source, 10)
        self.assertTrue(enrollment.write_cache(cache, 'x\n', [source]))
        self.assertEqual(cache.stat().st_mtime_ns, stamp)
