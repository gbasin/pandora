"""Local smoke tests exercise the receiver lifecycle without SSH or live state."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(path))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


remote = load('network_remote', 'remote.py')
benchmark = load('network_benchmark', 'benchmark.py')


class NetworkScratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='network-test-')
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name).resolve()
        self.root = self.parent / 'pandora-cas-network-owned'
        self.root.mkdir(mode=0o700)
        (self.root / '.owner').write_text('nonce')

    def test_root_guard_rejects_wrong_nonce_foreign_parent_and_symlink(self):
        with mock.patch.object(remote.Path, 'home', return_value=self.parent):
            self.assertEqual(remote.checked_root(self.root, 'nonce'), self.root)
            with self.assertRaises(ValueError):
                remote.checked_root(self.root, 'other')
            link = self.parent / 'pandora-cas-network-link'
            link.symlink_to(self.root)
            with self.assertRaises(ValueError):
                remote.checked_root(link, 'nonce')
            with mock.patch.object(remote.Path, 'home', return_value=self.parent / 'elsewhere'):
                with self.assertRaises(ValueError):
                    remote.checked_root(self.root, 'nonce')
        self.assertTrue(self.root.exists())

    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_small_actual_rsync_plan_finalize_and_equal_audits_for_every_case_policy(self):
        source_root = self.parent / 'sources'
        source_root.mkdir()
        sources, manifests, details = benchmark.fixtures(source_root, 1, 120)
        shutil.copytree(sources['cold'], self.root / 'baseline', symlinks=True)
        initialized = remote.action(self.root, 'initialize', {'records': manifests['cold']})
        self.assertTrue(initialized['baseline_verified'])
        self.assertEqual(details['tiny_edit_bytes'], 16)
        original = sources['cold'] / 'large/075.bin'
        edited = sources['tiny_large_edit'] / 'large/075.bin'
        self.assertEqual(original.stat().st_size, edited.stat().st_size)
        self.assertEqual(original.stat().st_mtime_ns, edited.stat().st_mtime_ns)
        self.assertEqual(sum(left != right for left, right in zip(original.read_bytes(), edited.read_bytes())), 16)
        for case in benchmark.CASES:
            for method in benchmark.METHODS:
                with self.subTest(case=case, method=method):
                    sample = case + '_' + method
                    remote.action(self.root, 'setup', {'sample': sample, 'method': method, 'warm': case != 'cold'})
                    request = {'sample': sample, 'method': method}
                    if method != 'rsync':
                        request['records'] = manifests[case]
                    planned = remote.action(self.root, 'plan', request)
                    selected = manifests[case]
                    if method != 'rsync':
                        missing = set(planned['missing_paths'])
                        selected = [record for record in selected if record['path'] in missing]
                    if method == 'rsync' or selected:
                        argv = ['rsync', '-a', '--no-times', '--files-from=-', '--from0', '--no-whole-file']
                        if method == 'rsync':
                            argv += ['--checksum', '--delete']
                            if case != 'cold':
                                argv += ['--link-dest=' + str(self.root / 'baseline')]
                        argv += [str(sources[case]) + '/', planned['stage'] + '/']
                        names = b'\0'.join(record['path'].encode() for record in selected) + b'\0'
                        subprocess.run(argv, input=names, capture_output=True, check=True)
                    finalized = remote.action(self.root, 'finalize', {'sample': sample})
                    self.assertIn('publish', finalized['steps'])
                    if method == 'cas_rehash':
                        self.assertIn('verify_cached', finalized['steps'])
                    else:
                        self.assertNotIn('verify_cached', finalized['steps'])
                    audited = remote.action(self.root, 'audit', {'sample': sample, 'records': manifests[case]})
                    self.assertTrue(audited['verified'])
                    self.assertGreaterEqual(audited['output_audit_seconds'], 0)
                    self.assertGreaterEqual(audited['execution_copy_seconds'], 0)
                    self.assertGreaterEqual(audited['execution_audit_seconds'], 0)
                    self.assertFalse(remote.sample_dir(self.root, sample).exists())
        self.assertTrue(remote.action(self.root, 'baseline_audit', {'records': manifests['cold']})['baseline_immutable'])
        benchmark.PROFILE.verify(sources['cold'], manifests['cold'])

    def test_receiver_rejects_escape_paths_and_unknown_methods(self):
        for method in ('typo', '', None):
            with self.assertRaises(ValueError):
                remote.action(self.root, 'setup', {'sample': 'one', 'method': method, 'warm': False})
        with self.assertRaises(ValueError):
            remote.sample_dir(self.root, '../escape')
        with self.assertRaises(ValueError):
            remote.receiver(self.root, 'one', ['--server', '.', '/outside'])
        expected = remote.sample_dir(self.root, 'one') / 'stage'
        with self.assertRaises(ValueError):
            remote.receiver(self.root, 'one', ['--server', '--link-dest=/outside', '.', str(expected)])

    def test_archive_contains_only_declared_helper_code(self):
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(benchmark.code_archive())) as archive:
            self.assertEqual(set(archive.getnames()), {'experiments/cas-network/remote.py',
                                                       'experiments/cas-poc/cas.py',
                                                       'experiments/transfer-profile/benchmark.py'})

    def test_ssh_rsync_command_preserves_control_policy_and_missing_selection(self):
        link = benchmark.Link('worker', self.parent)
        link.root, link.token = '/home/owner/pandora-cas-network-owned', 'nonce'
        completed = subprocess.CompletedProcess([], 0, b'Total bytes sent: 123\n', b'')
        with mock.patch.object(benchmark.subprocess, 'run', return_value=completed) as run:
            benchmark.rsync_send(link, 'rsync', self.parent, link.root + '/stage',
                                 [{'path': 'space and\nnewline'}], sample='one', baseline=True, checksum=True)
        argv = run.call_args.args[0]
        self.assertIn('--checksum', argv)
        self.assertIn('--delete', argv)
        self.assertIn('--link-dest=' + link.root + '/baseline', argv)
        self.assertEqual(run.call_args.kwargs['input'], b'space and\nnewline\0')
        self.assertIn('ControlPersist=10m', link.rsh)
        self.assertIn('--rsync-path=', ' '.join(argv))


if __name__ == '__main__':
    unittest.main()
