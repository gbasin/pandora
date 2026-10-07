"""Real tiny-fixture probes for missing-only CAS with readonly rsync delta bases."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


benchmark = load('hybrid_benchmark', 'benchmark.py')
remote = load('hybrid_remote', 'remote.py')


@unittest.skipUnless(shutil.which('rsync'), 'rsync required')
class HybridDelta(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hybrid-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.sender = self.root / 'sender'
        self.sender.mkdir()
        selected = [*benchmark.CASES, *benchmark.EXTRA_CASES]
        self.sources, self.manifests, self.details = benchmark.fixtures(self.sender, 4, 120, cases=selected)
        self.worker = self.root / 'worker'
        self.worker.mkdir()
        shutil.copytree(self.sources['cold'], self.worker / 'baseline', symlinks=True)
        remote.action(self.worker, 'initialize', {'records': self.manifests['cold']})

    def upload(self, case, method, sample):
        remote.action(self.worker, 'setup', {'sample': sample, 'method': method, 'warm': True})
        plan = remote.action(self.worker, 'plan', {'sample': sample, 'method': method, 'records': self.manifests[case]})
        missing = set(plan['missing_paths'])
        records = [record for record in self.manifests[case] if record['path'] in missing]
        stage = Path(plan['stage'])
        for record in records:
            self.assertFalse(os.path.lexists(stage / record['path']), 'no incorrect basis inode in stage')
        argv = ['rsync', '-a', '--no-times', '--stats', '--ignore-times', '--no-whole-file',
                '--files-from=-', '--from0', '--link-dest=' + str(self.worker / 'baseline'),
                str(self.sources[case]) + '/', str(stage) + '/']
        names = b'\0'.join(record['path'].encode() for record in records) + b'\0'
        proc = subprocess.run(argv, input=names, capture_output=True, check=True, env={**os.environ, 'LC_ALL': 'C'})
        return records, stage, benchmark.PROFILE.parse_stats(proc.stdout.decode())

    def finish(self, case, method, sample):
        final = remote.action(self.worker, 'finalize', {'sample': sample})
        self.assertEqual('verify_cached' in final['steps'], method.endswith('_rehash'))
        output = remote.sample_dir(self.worker, sample) / 'output'
        benchmark.PROFILE.verify(output, self.manifests[case])
        remote.action(self.worker, 'audit', {'sample': sample, 'records': self.manifests[case]})
        self.assertTrue(remote.action(self.worker, 'baseline_audit', {'records': self.manifests['cold']})['baseline_immutable'])

    def test_sixteen_byte_same_size_mtime_edit_uses_matched_delta_for_both_policies(self):
        original = self.sources['cold'] / 'large/075.bin'
        edited = self.sources['tiny_large_edit'] / 'large/075.bin'
        self.assertEqual(self.details['tiny_edit_file_bytes'], edited.stat().st_size,
                         'optional mode/new-path fixtures cannot overwrite tiny-edit byte metadata')
        self.assertGreater(self.details['tiny_edit_file_bytes'], 128)
        self.assertEqual(original.stat().st_size, edited.stat().st_size)
        self.assertEqual(original.stat().st_mtime_ns, edited.stat().st_mtime_ns)
        self.assertEqual(sum(a != b for a, b in zip(original.read_bytes(), edited.read_bytes())), 16)
        for method in benchmark.HYBRID_METHODS:
            with self.subTest(method=method):
                records, stage, stats = self.upload('tiny_large_edit', method, method)
                self.assertEqual([record['path'] for record in records], ['large/075.bin'])
                received = stage / 'large/075.bin'
                basis = self.worker / 'baseline/large/075.bin'
                self.assertNotEqual(received.stat().st_ino, basis.stat().st_ino)
                self.assertGreater(stats['matched_bytes'], original.stat().st_size / 2)
                self.assertLess(stats['literal_bytes'], original.stat().st_size / 2)
                self.assertGreater(stats['literal_bytes'], 0)
                self.finish('tiny_large_edit', method, method)

    def test_new_path_sends_full_literal_bytes_without_a_basis(self):
        for method in benchmark.HYBRID_METHODS:
            with self.subTest(method=method):
                sample = 'new_' + method
                records, _, stats = self.upload('new_path', method, sample)
                self.assertEqual([record['path'] for record in records], ['small/new-path.txt'])
                self.assertEqual(stats['literal_bytes'], 128)
                self.assertEqual(stats['matched_bytes'], 0)
                self.finish('new_path', method, sample)

    def test_mode_only_identity_uses_independent_inode_and_keeps_executable_basis(self):
        basis = self.worker / 'baseline/small/00000.txt'
        original_inode = basis.stat().st_ino
        for method in benchmark.HYBRID_METHODS:
            with self.subTest(method=method):
                sample = 'mode_' + method
                records, stage, _ = self.upload('mode_change', method, sample)
                self.assertEqual([record['path'] for record in records], ['small/00000.txt'])
                received = stage / 'small/00000.txt'
                self.assertEqual(received.stat().st_mode & 0o777, 0o444)
                self.assertNotEqual(received.stat().st_ino, original_inode)
                self.assertEqual(basis.stat().st_ino, original_inode)
                self.assertEqual(basis.stat().st_mode & 0o777, 0o555)
                self.finish('mode_change', method, sample)

    def test_new_received_corruption_is_rejected_in_both_integrity_policies(self):
        for method in benchmark.HYBRID_METHODS:
            with self.subTest(method=method):
                sample = 'corrupt_' + method
                _, stage, _ = self.upload('tiny_large_edit', method, sample)
                path = stage / 'large/075.bin'
                replacement = stage / 'bad-replacement'
                replacement.write_bytes(b'corrupt receiver bytes')
                replacement.chmod(0o444)
                os.replace(replacement, path)
                with self.assertRaisesRegex(ValueError, 'bytes differ from manifest'):
                    remote.action(self.worker, 'finalize', {'sample': sample})
                self.assertFalse((remote.sample_dir(self.worker, sample) / 'output').exists())
                remote.action(self.worker, 'baseline_audit', {'records': self.manifests['cold']})

    def test_reused_corruption_is_rejected_by_rehash_or_independent_trusted_audit(self):
        records = self.manifests['warm_unchanged']
        record = next(record for record in records if record['path'] == 'large/075.bin')
        template = remote.CAS.CasStore(self.worker / 'template').blob(record)
        template_inode, template_bytes = template.stat().st_ino, template.read_bytes()
        for method in benchmark.HYBRID_METHODS:
            with self.subTest(method=method):
                sample = 'reused_corrupt_' + method
                remote.action(self.worker, 'setup', {'sample': sample, 'method': method, 'warm': True})
                directory = remote.sample_dir(self.worker, sample)
                blob = remote.CAS.CasStore(directory / 'store').blob(record)
                self.assertEqual(blob.stat().st_ino, template_inode)
                # Detach the sample's entry before corrupting it. Never chmod
                # or write its shared template/baseline inode.
                replacement = directory / 'private-corrupt-blob'
                replacement.write_bytes(b'corrupted private cached bytes')
                replacement.chmod(0o444)
                os.replace(replacement, blob)
                self.assertNotEqual(blob.stat().st_ino, template_inode)
                plan = remote.action(self.worker, 'plan',
                                     {'sample': sample, 'method': method, 'records': records})
                self.assertEqual(plan['missing_paths'], [])
                output = directory / 'output'
                if method.endswith('_rehash'):
                    with self.assertRaisesRegex(ValueError, 'cached bytes differ from manifest'):
                        remote.action(self.worker, 'finalize', {'sample': sample})
                    self.assertFalse(output.exists())
                else:
                    remote.action(self.worker, 'finalize', {'sample': sample})
                    self.assertTrue(output.exists())
                    with self.assertRaisesRegex(RuntimeError, 'materialized source differs'):
                        remote.action(self.worker, 'audit', {'sample': sample, 'records': records})
                self.assertEqual(template.stat().st_ino, template_inode)
                self.assertEqual(template.read_bytes(), template_bytes)
                self.assertEqual(template.stat().st_mode & 0o777, 0o444)
                self.assertTrue(remote.action(self.worker, 'baseline_audit',
                                             {'records': self.manifests['cold']})['baseline_immutable'])

    def test_full_rewrite_control_retains_same_size_mtime_with_different_bytes(self):
        original = self.sources['cold'] / 'large/075.bin'
        rewritten = self.sources['full_large_rewrite'] / 'large/075.bin'
        self.assertEqual(original.stat().st_size, rewritten.stat().st_size)
        self.assertEqual(original.stat().st_mtime_ns, rewritten.stat().st_mtime_ns)
        self.assertNotEqual(benchmark.PROFILE.digest(original), benchmark.PROFILE.digest(rewritten))
        records, _, stats = self.upload('full_large_rewrite', 'cas_hybrid_rehash', 'rewrite')
        self.assertEqual(len(records), 1)
        self.assertEqual(stats['literal_bytes'], rewritten.stat().st_size)
        self.assertEqual(stats['matched_bytes'], 0)
        self.finish('full_large_rewrite', 'cas_hybrid_rehash', 'rewrite')


class HybridSelection(unittest.TestCase):
    def test_original_defaults_are_unchanged_and_selection_is_validated_before_ssh(self):
        self.assertEqual(benchmark.selection(), (list(benchmark.CASES), list(benchmark.METHODS)))
        cases, methods = benchmark.selection(hybrid=True, warm_only=True)
        self.assertNotIn('cold', cases)
        self.assertEqual(methods, [*benchmark.METHODS, *benchmark.HYBRID_METHODS])
        self.assertEqual(benchmark.selection(cases=['mode_change'])[0], ['mode_change'])
        for options in ({'cases': []}, {'cases': ['tiny_large_edit', 'tiny_large_edit']},
                        {'cases': ['typo']}, {'cases': ['cold'], 'warm_only': True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                benchmark.selection(**options)

    def test_hybrid_sender_flags_force_delta_without_mutating_shared_bases(self):
        link = benchmark.Link('worker', Path('/tmp/control'))
        link.root, link.token = '/home/owner/pandora-cas-network-owned', 'nonce'
        completed = subprocess.CompletedProcess([], 0, b'', b'')
        with mock.patch.object(benchmark.subprocess, 'run', return_value=completed) as run:
            benchmark.rsync_send(link, 'rsync', Path('/source'), link.root + '/stage',
                                 [{'path': 'large/075.bin'}], sample='one', baseline=True, hybrid=True)
        argv = run.call_args.args[0]
        self.assertIn('--ignore-times', argv)
        self.assertIn('--no-whole-file', argv)
        self.assertIn('--link-dest=' + link.root + '/baseline', argv)
        for forbidden in ('--checksum', '--delete', '--inplace', '--append', '--append-verify'):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(run.call_args.kwargs['input'], b'large/075.bin\0')


if __name__ == '__main__':
    unittest.main()
