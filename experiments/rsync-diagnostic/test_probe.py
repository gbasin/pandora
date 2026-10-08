"""Small local rsync lifecycle tests, with no SSH or production-state access."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HERE = Path(__file__).parent
probe = load('rsync_diagnostic_probe', HERE / 'probe.py')
remote = load('diagnostic_remote', HERE.parent / 'cas-network' / 'remote.py')
REAL_RUN = subprocess.run


class LocalLink:
    host = 'local'
    rsh = 'ssh'
    token = 'nonce'
    deadline = None

    def __init__(self, root):
        self.root = str(root)
        self.actions = []

    def call(self, action, request):
        self.actions.append(action)
        if action == 'initialize':
            raise AssertionError('diagnostic must not initialize a CAS store')
        result = remote.action(Path(self.root), action, request)
        return result, len(json.dumps(request)), len(json.dumps(result))

    def command(self, argv):
        if argv != ['true']:
            raise AssertionError('only private true is expected')
        return b''

    def timeout(self, limit):
        return limit


def local_rsync(argv, **kwargs):
    rewritten = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == '-e':
            skip = True
        elif arg.startswith('--rsync-path=') or arg.startswith('--remote-option='):
            continue
        else:
            rewritten.append(arg.removeprefix('local:'))
    # Local rsync defaults to whole-file, unlike production's remote transport.
    rewritten.insert(1, '--no-whole-file')
    return REAL_RUN(rewritten, **kwargs)


@unittest.skipUnless(shutil.which('rsync'), 'rsync required')
class DiagnosticProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rsync-diagnostic-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        sources = self.root / 'sources'
        sources.mkdir()
        self.base, self.tiny, self.empty, self.original, self.records, self.details = probe.fixtures(sources, 1, 103)
        worker = self.root / 'worker'
        worker.mkdir()
        shutil.copytree(self.base, worker / 'baseline', symlinks=True)
        self.link = LocalLink(worker)

    def test_all_variants_audit_the_correct_scope_without_any_cas_store(self):
        original_path = self.base / 'large/075.bin'
        edited_path = self.tiny / 'large/075.bin'
        self.assertEqual(original_path.stat().st_size, edited_path.stat().st_size)
        self.assertEqual(original_path.stat().st_mtime_ns, edited_path.stat().st_mtime_ns)
        self.assertEqual(sum(a != b for a, b in zip(original_path.read_bytes(), edited_path.read_bytes())), 16)
        for variant in probe.VARIANTS:
            source, records = (self.empty, []) if variant in ('empty', 'ssh_true') else (self.base, self.original)
            with self.subTest(variant=variant), \
                    mock.patch.object(probe.subprocess, 'run', side_effect=local_rsync), \
                    mock.patch.object(remote.CAS, 'CasStore', side_effect=AssertionError('no CAS store')):
                result = probe.sample(self.link, 'rsync', source, records, variant, variant)
            self.assertTrue(result['verified'])
            self.assertEqual(result['verified_full_source'], variant == 'normal')
            self.assertEqual(result['verified_empty_stage'], variant != 'normal')
            self.assertEqual(set(result['outside_timer_seconds']), {'setup', 'plan', 'finalize', 'audit'})
            self.assertFalse(remote.sample_dir(Path(self.link.root), variant).exists())
            if variant == 'normal':
                self.assertEqual(result['stats']['literal_bytes'], 0)
                self.assertEqual(result['stats']['regular_files_transferred'], 0)
            elif variant == 'ssh_true':
                self.assertIsNone(result['stats']['sent_bytes'])
        probe.PROFILE.verify(Path(self.link.root) / 'baseline', self.original)
        probe.PROFILE.verify(self.base, self.original)
        probe.PROFILE.verify(self.tiny, self.records)
        self.assertNotIn('initialize', self.link.actions)
        self.assertFalse((Path(self.link.root) / 'template').exists())

    def test_separate_edit_probe_audits_actual_tiny_edit(self):
        with mock.patch.object(probe.subprocess, 'run', side_effect=local_rsync):
            result = probe.sample(self.link, 'rsync', self.tiny, self.records, 'normal', 'edit_probe')
        self.assertTrue(result['verified_full_source'])
        self.assertTrue(result['verified'])
        self.assertGreater(result['stats']['matched_bytes'], 0)
        self.assertGreater(result['stats']['literal_bytes'], 0)
        probe.PROFILE.verify(Path(self.link.root) / 'baseline', self.original)

    def test_debug_probe_audits_unchanged_source(self):
        with mock.patch.object(probe.subprocess, 'run', side_effect=local_rsync):
            result = probe.sample(self.link, 'rsync', self.base, self.original, 'normal', 'debug', debug=True)
        self.assertTrue(result['verified_full_source'])
        self.assertTrue(result['verified'])
        self.assertIsInstance(result['stdout'], str)

    def test_flags_keep_basis_dry_run_and_debug_distinct_without_inplace(self):
        for variant in probe.VARIANTS:
            argv = probe.rsync_argv(self.link, 'rsync', self.tiny, self.link.root + '/stage', 'one', variant)
            self.assertEqual('--dry-run' in argv, variant in ('dry_basis', 'dry_no_basis'))
            self.assertEqual(any(arg.startswith('--link-dest=') for arg in argv), variant in ('normal', 'dry_basis'))
            self.assertFalse(any(arg.startswith('--debug=') for arg in argv))
            for unsafe in ('--inplace', '--append', '--append-verify'):
                self.assertNotIn(unsafe, argv)

    def test_stats_unknown_stays_unknown_and_file_list_timings_are_optional(self):
        stats = probe.parse_stats('Total bytes sent: 123\nFile list generation time: 0.001 seconds\n')
        self.assertEqual(stats['sent_bytes'], 123)
        self.assertEqual(stats['file_list_generation_seconds'], .001)
        self.assertIsNone(stats['matched_bytes'])
        self.assertIsNone(stats['file_list_transfer_seconds'])


if __name__ == '__main__':
    unittest.main()
