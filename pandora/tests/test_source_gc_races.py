"""Source collection interleaved with reuse, admission, and cache enumeration."""
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from pandora.engine import runner
from pandora.engine.ledger import Ledger
from pandora.engine.scheduler import gate
from pandora.snapshot import transfer
from pandora.tests.test_engine import claim
from pandora.tests import test_transfer_concurrency as transport_tests

LocalLink = transport_tests.LocalLink


class SourceGCRaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = runner.Paths(self.tmp.name).ensure()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)
        now = time.time()
        self.repo = self.paths.src / 'repo'
        for n in range(6):
            directory = self.repo / ('s%d' % n)
            directory.mkdir(parents=True)
            (directory / 'file.txt').write_text('contents')
            os.utime(directory, (now - 7200 - n * 60,) * 2)
        (self.repo / 'latest').symlink_to(self.repo / 's0')
        self.source = self.repo / 's5'

    def test_cache_hit_renews_grace_before_submit(self):
        result = transfer.send(LocalLink(), [{'path': 'file.txt'}],
                               worktree=self.tmp.name, root=self.tmp.name,
                               repo='repo', input_id='s5')
        self.assertTrue(result['reused'])
        self.assertEqual(runner.gc_sources(self.paths, self.ledger), [])
        self.assertEqual(self.source.joinpath('file.txt').read_text(), 'contents')
        # No latest update is needed to keep a reused tree alive.
        self.assertEqual((self.repo / 'latest').resolve(), (self.repo / 's0').resolve())

    def test_gc_reads_live_rows_after_acquiring_admission_lock(self):
        waiting = threading.Event()
        errors, removed = [], []

        @contextmanager
        def observed_gate(root):
            waiting.set()
            with gate(root):
                yield

        def collect():
            ledger = Ledger(self.paths.ledger)
            try:
                removed.extend(runner.gc_sources(self.paths, ledger))
            except BaseException as exc:
                errors.append(exc)
            finally:
                ledger.close()

        with mock.patch.object(runner, 'gate', observed_gate):
            thread = threading.Thread(target=collect)
            try:
                with gate(self.paths.root):
                    thread.start()
                    self.assertTrue(waiting.wait(5), 'collector never reached the lock')
                    claim(self.ledger, input_id='s5', source_path=str(self.source))
            finally:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(removed, [])
        self.assertTrue(self.source.is_dir())

    def test_publication_renews_an_existing_tree_before_acknowledging(self):
        # A concurrent shipment fills the final path after our cache miss.
        self.source.rename(self.repo / 'held')
        source = Path(self.tmp.name) / 'worktree'
        source.mkdir()
        (source / 'file.txt').write_text('contents')
        transport = transport_tests.TransferConcurrencyTest()

        def concurrent_publish(argv, **kwargs):
            result = transport.fake_rsync(argv, **kwargs)
            if argv[0] == 'rsync':
                (self.repo / 'held').rename(self.source)
            return result

        with mock.patch.object(transfer.subprocess, 'run', side_effect=concurrent_publish):
            transfer.send(LocalLink(), [{'path': 'file.txt'}], worktree=source,
                          root=self.tmp.name, repo='repo', input_id='s5')
        # A subsequent publisher moves latest away before this run submits.
        (self.repo / 'latest').unlink()
        (self.repo / 'latest').symlink_to(self.repo / 's0')
        self.assertEqual(runner.gc_sources(self.paths, self.ledger), [])
        self.assertTrue(self.source.is_dir())

    def test_ship_skips_a_base_deleted_between_isdir_and_stat(self):
        victim = str(self.source)

        class DeletingScanLink(LocalLink):
            def feed(self, script, args=(), **kwargs):
                if 'entries = []' in script:
                    script = '''import os, shutil
_real_getmtime = os.path.getmtime
def deleting_getmtime(path):
    if path == %r:
        shutil.rmtree(path)
    return _real_getmtime(path)
os.path.getmtime = deleting_getmtime
''' % victim + script
                return super().feed(script, args, **kwargs)

        source = Path(self.tmp.name) / 'worktree'
        source.mkdir()
        (source / 'file.txt').write_text('new content')
        # Reuse the transport fake: remote helper programs execute normally;
        # only rsync is replaced with a local copy.
        transport = transport_tests.TransferConcurrencyTest()
        with mock.patch.object(transfer.subprocess, 'run', side_effect=transport.fake_rsync):
            result = transfer.send(DeletingScanLink(), [{'path': 'file.txt'}],
                                   worktree=source, root=self.tmp.name,
                                   repo='repo', input_id='new')
        self.assertFalse(result['reused'])
        self.assertNotIn(victim, result['link_dests'])
        self.assertFalse(self.source.exists())
        self.assertEqual((Path(result['path']) / 'file.txt').read_text(), 'new content')
