"""Local filesystem races in source-cache publication, without an SSH worker."""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.errors import TransferError
from pandora.snapshot import transfer


_REAL_RUN = subprocess.run


class LocalLink:
    host = 'local'
    rsh = 'ssh'

    def run(self, argv, *, stdin=None, timeout=120, check=True):
        return self._execute(argv, stdin, timeout, check)

    def feed(self, script, args=(), *, stdin=b'', timeout=600, check=True):
        return self._execute(['python3', '-c', script, *map(str, args)],
                             stdin, timeout, check)

    @staticmethod
    def _execute(argv, stdin, timeout, check):
        proc = _REAL_RUN(argv, input=stdin, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, timeout=timeout)
        out = proc.stdout.decode('utf-8', 'replace')
        err = proc.stderr.decode('utf-8', 'replace')
        if check and proc.returncode:
            raise TransferError('%s failed (%d): %s' % (argv[0], proc.returncode, err))
        return proc.returncode, out, err


class PausedPublicationLink(LocalLink):
    """Pause the first publisher after it creates its latest symlink."""

    def __init__(self, marker, release):
        self.marker = Path(marker)
        self.release = Path(release)

    def run(self, argv, **kwargs):
        # The former shell publisher has one command for rename, symlink, and
        # replacement. Split at its last shell boundary to expose the race.
        if argv[:2] == ['sh', '-c'] and 'ln -sfn' in argv[2] and '/one ' in argv[2]:
            before, after = argv[2].rsplit(' && ', 1)
            super().run(['sh', '-c', before], **kwargs)
            self.marker.touch()
            self.wait_for_release()
            return super().run(['sh', '-c', after], **kwargs)
        return super().run(argv, **kwargs)

    def feed(self, script, args=(), **kwargs):
        if len(args) == 3 and str(args[1]).endswith('/one'):
            pause = ("import os as _os, time as _time\n"
                     "_real_replace = _os.replace\n"
                     "def _paused_replace(source, target):\n"
                     "    open(%r, 'w').close()\n"
                     "    deadline = _time.monotonic() + 5\n"
                     "    while not _os.path.exists(%r):\n"
                     "        if _time.monotonic() > deadline: raise RuntimeError('release timed out')\n"
                     "        _time.sleep(0.01)\n"
                     "    return _real_replace(source, target)\n"
                     "_os.replace = _paused_replace\n" % (str(self.marker), str(self.release)))
            script = pause + script
        return super().feed(script, args, **kwargs)

    def wait_for_release(self):
        deadline = time.monotonic() + 5
        while not self.release.exists():
            if time.monotonic() > deadline:
                raise AssertionError('second publication did not finish')
            time.sleep(0.01)


class TransferConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.link = LocalLink()

    def source(self, name, content):
        path = self.root / name
        path.mkdir()
        (path / 'file.txt').write_text(content)
        return path

    def send(self, source, input_id):
        return transfer.send(self.link, [{'path': 'file.txt'}], worktree=source,
                             root=str(self.root / 'worker'), repo='repo', input_id=input_id)

    def fake_rsync(self, argv, **kwargs):
        if argv[0] != 'rsync':
            return _REAL_RUN(argv, **kwargs)
        source = Path(argv[-2])
        target = Path(argv[-1].split(':', 1)[1])
        shutil.copyfile(source / 'file.txt', target / 'file.txt')
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    def test_same_input_uploads_keep_first_published_tree(self):
        first = self.source('first', 'first')
        second = self.source('second', 'second')
        first_staged = threading.Event()
        release_first = threading.Event()
        errors = []
        results = []

        def staged_rsync(argv, **kwargs):
            result = self.fake_rsync(argv, **kwargs)
            if argv[0] == 'rsync' and Path(argv[-2]) == first:
                first_staged.set()
                if not release_first.wait(5):
                    raise AssertionError('second upload did not finish')
            return result

        def upload_first():
            try:
                results.append(self.send(first, 'same'))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(transfer.subprocess, 'run', side_effect=staged_rsync):
            thread = threading.Thread(target=upload_first)
            thread.start()
            try:
                self.assertTrue(first_staged.wait(5))
                results.append(self.send(second, 'same'))
            finally:
                release_first.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        paths = transfer.cache_paths(str(self.root / 'worker'), 'repo', 'same')
        final = Path(paths['final'])
        self.assertEqual((final / 'file.txt').read_text(), 'second')
        self.assertEqual([p.name for p in final.iterdir()], ['file.txt'])
        self.assertEqual(list(final.parent.glob('same.partial*')), [])

    def test_different_inputs_replace_latest_with_complete_symlink(self):
        first = self.source('first', 'first')
        second = self.source('second', 'second')
        marker = self.root / 'first_symlink_ready'
        release = self.root / 'release_first'
        self.link = PausedPublicationLink(marker, release)
        errors = []

        def upload_first():
            try:
                self.send(first, 'one')
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(transfer.subprocess, 'run', side_effect=self.fake_rsync):
            thread = threading.Thread(target=upload_first)
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), errors)
                self.send(second, 'two')
            finally:
                release.touch()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        paths = transfer.cache_paths(str(self.root / 'worker'), 'repo', 'one')
        base = Path(paths['base'])
        self.assertEqual((base / 'one' / 'file.txt').read_text(), 'first')
        self.assertEqual((base / 'two' / 'file.txt').read_text(), 'second')
        self.assertEqual(os.readlink(paths['latest']), str(base / 'one'))
        self.assertEqual(list(base.glob('latest.tmp*')), [])
        self.assertEqual(list(base.glob('*.partial*')), [])

    def test_failed_rsync_never_publishes_and_cleans_its_staging(self):
        source = self.source('first', 'first')

        def failed_rsync(argv, **kwargs):
            result = self.fake_rsync(argv, **kwargs)
            if argv[0] == 'rsync':
                return subprocess.CompletedProcess(argv, 23, b'', b'copy failed')
            return result

        with mock.patch.object(transfer.subprocess, 'run', side_effect=failed_rsync):
            with self.assertRaisesRegex(TransferError, 'rsync to local failed'):
                self.send(source, 'failed')
        paths = transfer.cache_paths(str(self.root / 'worker'), 'repo', 'failed')
        self.assertFalse(Path(paths['final']).exists())
        self.assertEqual(list(Path(paths['base']).glob('failed.partial*')), [])


    def test_an_rsync_timeout_is_a_transfer_error_and_the_stage_is_cleaned(self):
        source = self.source('first', 'first')

        def slow_rsync(argv, **kwargs):
            if argv[0] == 'rsync':
                raise subprocess.TimeoutExpired(argv, kwargs.get('timeout'))
            return self.fake_rsync(argv, **kwargs)

        with mock.patch.object(transfer.subprocess, 'run', side_effect=slow_rsync):
            with self.assertRaisesRegex(TransferError, 'rsync to local timed out after 7 s'):
                transfer.send(self.link, [{'path': 'file.txt'}], worktree=source,
                              root=str(self.root / 'worker'), repo='repo', input_id='slow',
                              timeout=7)
        paths = transfer.cache_paths(str(self.root / 'worker'), 'repo', 'slow')
        self.assertEqual(list(Path(paths['base']).glob('slow.partial*')), [])

    def test_a_failing_cleanup_is_logged_and_never_replaces_the_cause(self):
        source = self.source('first', 'first')
        logged = []

        class CleanupFails(LocalLink):
            def feed(self, script, args=(), **kwargs):
                if 'rmtree' in script:
                    raise TransferError('ssh died during cleanup')
                return super().feed(script, args, **kwargs)

        def failed_rsync(argv, **kwargs):
            if argv[0] == 'rsync':
                return subprocess.CompletedProcess(argv, 255, b'', b'unexpected end of file')
            return _REAL_RUN(argv, **kwargs)

        for rsync, expect in ((failed_rsync, 'unexpected end of file'), (self.fake_rsync, None)):
            logged.clear()
            with mock.patch.object(transfer.subprocess, 'run', side_effect=rsync):
                call = lambda: transfer.send(  # noqa: E731
                    CleanupFails(), [{'path': 'file.txt'}], worktree=source,
                    root=str(self.root / 'worker'), repo='repo', input_id='c-%s' % bool(expect),
                    log=logged.append)
                if expect:
                    with self.assertRaisesRegex(TransferError, expect):
                        call()
                else:
                    self.assertFalse(call()['reused'])
            self.assertEqual(len(logged), 1, logged)
            self.assertIn('ssh died during cleanup', logged[0])


if __name__ == '__main__':
    unittest.main()
