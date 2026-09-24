"""Real rsync cache reuse across fresh worktree timestamps."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.snapshot import transfer
from pandora.tests.test_transfer_concurrency import LocalLink

_REAL_RUN = subprocess.run
_OPENRSYNC = bool(shutil.which('rsync')) and 'openrsync' in subprocess.check_output(
    ['rsync', '--version'], text=True)


@unittest.skipUnless(shutil.which('rsync'), 'rsync required')
class WorktreeMetadata(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.file = self.source / 'file.txt'
        self.file.write_text('original')
        self.file.chmod(0o644)

    def send(self, input_id):
        def local_rsync(argv, **kwargs):
            if argv[0] == 'rsync':
                argv = [*argv[:-1], argv[-1].removeprefix('local:')]
            return _REAL_RUN(argv, **kwargs)
        with mock.patch.object(transfer.subprocess, 'run', side_effect=local_rsync):
            result = transfer.send(LocalLink(), [{'path': 'file.txt'}],
                                   worktree=self.source, root=str(self.root / 'worker'),
                                   repo='repo', input_id=input_id)
        return Path(result['path']) / 'file.txt'

    def test_published_root_is_readable_by_isolated_worker(self):
        uploaded = self.send('first')
        self.assertEqual(uploaded.parent.stat().st_mode & 0o777, 0o755)

    def test_timestamp_only_changes_reuse_the_cached_inode(self):
        first = self.send('first')
        os.utime(self.file, (1, 1))
        second = self.send('second')
        self.assertEqual(first.read_bytes(), self.file.read_bytes())
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)

    def test_a_divergent_latest_still_dedupes_against_an_older_base(self):
        first = self.send('first')
        self.file.write_text('divergent')
        second = self.send('second')
        self.file.write_text('original')
        third = self.send('third')
        self.assertEqual(first.stat().st_ino, third.stat().st_ino)
        self.assertNotEqual(second.stat().st_ino, third.stat().st_ino)

    def test_same_size_and_timestamp_content_change_is_transferred(self):
        first = self.send('first')
        stamp = self.file.stat().st_mtime_ns
        self.file.write_text('modified')
        os.utime(self.file, ns=(stamp, stamp))
        second = self.send('second')
        self.assertEqual(first.read_text(), 'original')
        self.assertEqual(second.read_text(), 'modified')
        self.assertNotEqual(first.stat().st_ino, second.stat().st_ino)

    @unittest.skipIf(_OPENRSYNC, 'openrsync local receiver ignores mode changes; GNU worker verified live')
    def test_executable_mode_change_does_not_mutate_the_old_cache(self):
        first = self.send('first')
        self.file.chmod(0o755)
        second = self.send('second')
        self.assertEqual(first.stat().st_mode & 0o777, 0o644)
        self.assertEqual(second.stat().st_mode & 0o777, 0o755)
