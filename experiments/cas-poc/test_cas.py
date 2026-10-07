"""CAS correctness probes over private tiny fixtures, never live source caches."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('cas_poc', Path(__file__).with_name('cas.py'))
cas = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cas)


class CasPrototype(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='cas-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.store = cas.CasStore(self.root / 'store')

    def file(self, name, payload=b'fixture contents\n', mode=0o444):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
        return {'path': name, 'sha256': hashlib.sha256(payload).hexdigest(), 'mode': mode}

    def replace(self, name, payload, mode=0o444):
        path = self.source / name
        replacement = path.with_name(path.name + '.edit')
        replacement.write_bytes(payload)
        replacement.chmod(mode)
        os.replace(replacement, path)

    def test_same_bytes_different_modes_have_distinct_immutable_inodes(self):
        records = [self.file('plain'), self.file('duplicate'), self.file('exec', mode=0o555)]
        self.store.seed(self.source, records)
        snapshot = self.root / 'snapshot'
        with mock.patch.object(cas._BENCHMARK, 'digest', wraps=cas._BENCHMARK.digest) as digest:
            result = self.store.transfer(self.source, snapshot, records)
        self.assertEqual(digest.call_count, 2, 'one read per unique cached sha/mode')
        self.assertEqual(result['missing_files'], 0)
        self.assertIn('verify_cached', result['steps'])
        self.assertNotIn('rsync', result['steps'])
        plain, duplicate, executable = [snapshot / record['path'] for record in records]
        self.assertEqual(plain.stat().st_ino, duplicate.stat().st_ino)
        self.assertNotEqual(plain.stat().st_ino, executable.stat().st_ino)
        self.assertEqual(plain.stat().st_mode & 0o777, 0o444)
        self.assertEqual(executable.stat().st_mode & 0o777, 0o555)
        self.assertEqual(plain.stat().st_ino, self.store.blob(records[0]).stat().st_ino)

    def test_independent_writable_execution_copy_cannot_change_store_or_other_snapshot(self):
        records = [self.file('plain'), self.file('exec', mode=0o555)]
        self.store.seed(self.source, records)
        first, second = self.root / 'first', self.root / 'second'
        self.store.transfer(self.source, first, records)
        self.store.transfer(self.source, second, records)
        execution = self.root / 'execution'
        cas.writable_copy(first, execution)
        self.assertNotEqual((execution / 'plain').stat().st_ino, (first / 'plain').stat().st_ino)
        self.assertEqual((execution / 'plain').stat().st_mode & 0o777, 0o644)
        self.assertEqual((execution / 'exec').stat().st_mode & 0o777, 0o755)
        (execution / 'plain').write_bytes(b'changed by dependency preparation')
        (execution / 'exec').chmod(0o600)
        expected = sorted(records, key=lambda value: value['path'])
        cas._BENCHMARK.verify(first, expected)
        cas._BENCHMARK.verify(second, expected)
        self.assertEqual(self.store.blob(records[0]).read_bytes(), b'fixture contents\n')
        with self.assertRaises(FileExistsError):
            cas.writable_copy(first, execution)

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_missing_only_transfer_verifies_modes_links_and_odd_names(self):
        known = self.file('known')
        records = [known, self.file('space and\nnewline', b'odd fixture data'), self.file('exec', b'new bytes', 0o555)]
        self.store.seed(self.source, [known])
        (self.source / 'internal-link').symlink_to('known')
        records.append({'path': 'internal-link', 'link': 'known'})
        snapshot = self.root / 'snapshot'
        real_run = subprocess.run
        observed = []

        def rsync(argv, **kwargs):
            observed.append((argv, kwargs))
            return real_run(argv, **kwargs)

        with mock.patch.object(cas.subprocess, 'run', side_effect=rsync):
            result = self.store.transfer(self.source, snapshot, records)
        self.assertEqual(result['missing_files'], 2)
        self.assertEqual(result['missing_bytes'], len(b'odd fixture data') + len(b'new bytes'))
        self.assertNotIn('--checksum', observed[0][0])
        self.assertEqual(observed[0][1]['input'], b'space and\nnewline\0exec\0')
        cas._BENCHMARK.verify(snapshot, sorted(records, key=lambda value: value['path']))
        reused = self.root / 'reused'
        with mock.patch.object(cas.subprocess, 'run', side_effect=AssertionError('no rsync expected')):
            again = self.store.transfer(self.source, reused, records)
        self.assertEqual(again['missing_files'], 0)
        cas._BENCHMARK.verify(reused, sorted(records, key=lambda value: value['path']))

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_source_mutation_refuses_publication_and_cleans_partial_trees(self):
        records = [self.file('file', b'before')]
        self.replace('file', b'after!')
        snapshot = self.root / 'snapshot'
        with self.assertRaisesRegex(ValueError, 'bytes differ from manifest'):
            self.store.transfer(self.source, snapshot, records)
        self.assertFalse(snapshot.exists())
        self.assertEqual(list(self.root.glob('snapshot.partial.*')), [])
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_invalid_seed_does_not_publish_a_blob(self):
        record = self.file('file')
        record['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'bytes differ from manifest'):
            self.store.seed(self.source, [record])
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_corrupted_reused_blob_is_rejected_before_snapshot_publication(self):
        records = [self.file('file')]
        self.store.seed(self.source, records)
        blob = self.store.blob(records[0])
        blob.chmod(0o644)
        blob.write_bytes(b'corrupted bytes')
        blob.chmod(0o444)
        with self.assertRaisesRegex(ValueError, 'reused blob differs from manifest'):
            self.store.transfer(self.source, self.root / 'snapshot', records)
        self.assertFalse((self.root / 'snapshot').exists())
        self.assertEqual(list(self.root.glob('snapshot.partial.*')), [])

    def test_trusted_policy_requires_external_audit_to_detect_store_corruption(self):
        records = [self.file('file')]
        self.store.seed(self.source, records)
        blob = self.store.blob(records[0])
        blob.chmod(0o644)
        blob.write_bytes(b'corrupted bytes')
        blob.chmod(0o444)
        snapshot = self.root / 'snapshot'
        result = self.store.transfer(self.source, snapshot, records, integrity='trusted')
        self.assertNotIn('verify_cached', result['steps'])
        self.assertEqual(result['integrity'], 'trusted')
        with self.assertRaisesRegex(RuntimeError, 'materialized source differs'):
            cas._BENCHMARK.verify(snapshot, records)

    def test_concurrent_verified_insertions_never_replace_the_shared_blob(self):
        record = self.file('file')
        barrier = threading.Barrier(2)
        real_link = os.link

        def simultaneous_link(source, destination, **kwargs):
            if Path(destination) == self.store.blob(record):
                barrier.wait(timeout=5)
            return real_link(source, destination, **kwargs)

        with mock.patch.object(cas.os, 'link', side_effect=simultaneous_link):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.store.seed, self.source, [record]) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)
        original_inode = self.store.blob(record).stat().st_ino
        snapshot = self.root / 'snapshot'
        self.store.transfer(self.source, snapshot, [record])
        self.store.seed(self.source, [record])
        self.assertEqual(self.store.blob(record).stat().st_ino, original_inode)
        self.assertEqual((snapshot / 'file').stat().st_ino, original_inode)
        self.assertEqual(self.store.blob(record).stat().st_mode & 0o777, 0o444)
        self.assertEqual(list(self.store.blobs.glob('.insert-*')), [])
        cas._BENCHMARK.verify(snapshot, [record])

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_corrupt_insertion_winner_cannot_bypass_verification_in_either_policy(self):
        record = self.file('file')
        real_link = os.link
        for policy in ('rehash', 'trusted'):
            with self.subTest(policy=policy):
                self.store = cas.CasStore(self.root / ('store-' + policy))
                target = self.root / ('snapshot-' + policy)

                def raced_link(source, destination, **kwargs):
                    if Path(destination) == self.store.blob(record):
                        winner = self.store.blob(record)
                        winner.write_bytes(b'corrupt concurrent winner')
                        winner.chmod(0o444)
                    return real_link(source, destination, **kwargs)

                with mock.patch.object(cas.os, 'link', side_effect=raced_link):
                    with self.assertRaisesRegex(ValueError, 'insertion winner differs'):
                        self.store.transfer(self.source, target, [record], integrity=policy)
                self.assertFalse(target.exists())
                self.assertEqual(list(self.root.glob(target.name + '.partial.*')), [])
                self.assertEqual(list(self.store.blobs.glob('.insert-*')), [])

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_interrupted_blob_publication_never_publishes_target_or_leaves_partials(self):
        record = self.file('file')
        real_link = os.link

        def interrupted_link(source, destination, **kwargs):
            if Path(destination) == self.store.blob(record):
                raise OSError('interrupted insertion')
            return real_link(source, destination, **kwargs)

        with mock.patch.object(cas.os, 'link', side_effect=interrupted_link):
            with self.assertRaisesRegex(OSError, 'interrupted insertion'):
                self.store.transfer(self.source, self.root / 'snapshot', [record])
        self.assertFalse((self.root / 'snapshot').exists())
        self.assertEqual(list(self.root.glob('snapshot.partial.*')), [])
        self.assertEqual(list(self.store.blobs.iterdir()), [])

    def test_invalid_paths_modes_links_and_ancestor_conflicts_never_stage(self):
        good = self.file('file')
        invalid = [
            [{**good, 'path': '../escape'}], [{**good, 'path': '/absolute'}],
            [{**good, 'path': 'nested/../escape'}], [{**good, 'path': 'nul\0path'}],
            [good, good], [good, {**good, 'path': 'file/child'}],
            [{'path': 'link', 'link': '/outside'}], [{'path': 'link', 'link': '../outside'}],
            [{'path': 'link', 'link': 'file'}, {**good, 'path': 'link/child'}],
            [{**good, 'mode': 0o644}], [{**good, 'mode': True}], [{**good, 'mode': float(0o444)}],
        ]
        for records in invalid:
            with self.subTest(records=records):
                with self.assertRaises(ValueError):
                    self.store.transfer(self.source, self.root / 'snapshot', records)
                self.assertFalse((self.root / 'snapshot').exists())
                self.assertEqual(list(self.root.glob('snapshot.partial.*')), [])
        with self.assertRaisesRegex(ValueError, 'cyclic'):
            self.store.transfer(self.source, self.root / 'snapshot',
                                [{'path': 'one', 'link': 'two'}, {'path': 'two', 'link': 'one'}])
        self.assertEqual(list(self.root.glob('snapshot.partial.*')), [])

    def test_source_ancestor_symlink_is_refused_and_fresh_target_is_required(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'file').write_bytes(b'fixture contents\n')
        (outside / 'file').chmod(0o444)
        (self.source / 'parent').symlink_to(outside)
        record = {'path': 'parent/file', 'mode': 0o444,
                  'sha256': hashlib.sha256(b'fixture contents\n').hexdigest()}
        with self.assertRaisesRegex(ValueError, 'parent is a symlink'):
            self.store.seed(self.source, [record])
        (self.root / 'snapshot').mkdir()
        with self.assertRaises(FileExistsError):
            self.store.transfer(self.source, self.root / 'snapshot', [])
        with self.assertRaisesRegex(ValueError, 'outside the CAS'):
            self.store.transfer(self.source, self.store.root / 'snapshot', [])


if __name__ == '__main__':
    unittest.main()
