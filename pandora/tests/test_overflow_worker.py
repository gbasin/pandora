"""Frozen packet and once-only claims without an Incus or Boat dependency."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pandora.engine import overflow_worker as worker
from pandora.engine.ledger import Ledger
from pandora.snapshot import freeze


class OverflowWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'isolated-engine'
        self.source = self.root / 'src' / 'repo' / 'frozen-input'
        self.source.mkdir(parents=True)
        (self.source / 'hello.py').write_text('print("hello")\n')
        self.manifest = [freeze.entry(self.source, 'hello.py')]
        self.packet = {
            'run_id': 'r-original-123', 'manifest': self.manifest,
            'limits': {'reservation_mib': 1024, 'ceiling_mib': 4096, 'cpus_hint': 4},
            'request': {
                'request_id': 'original-request', 'input_id': freeze.input_id(self.manifest),
                'client': 'owner@mac', 'source_path': '/original-ovh/source',
                'git_marks': {'untracked': [], 'ignored': []},
                'plan': {
                    'repo': 'demo', 'job': 'unit', 'size': 'medium', 'where': 'remote',
                    'argv': ['python3', 'hello.py'], 'cwd': '.', 'env': {},
                    'outputs': [{'kind': 'artifacts', 'paths': ['reports']}],
                    'worker': {'base_image': 'images:ubuntu/26.04', 'packages': [],
                               'pins': {'base_image': 'caller-must-not-pin'},
                               'pin_notes': ['caller-must-not-choose'], 'built_from': 'bad'},
                },
            },
        }
        self.allowed = {('demo', 'unit')}
        self.fake = Mock(side_effect=self.finish)
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('PANDORA_CPUS_PER_RUN', None)

    def finish(self, root, run_id):
        result = {'run_id': run_id, 'request_id': self.packet['request']['request_id'],
                  'input_id': self.packet['request']['input_id'], 'outcome': 'passed',
                  'cli_exit': 0, 'verdict': None, 'verdict_skipped': 'worker_not_ready'}
        ledger = Ledger(root / 'ledger.db')
        try:
            ledger.finish(run_id, outcome='passed', exit_code=0)
        finally:
            ledger.close()
        (root / 'runs' / run_id / 'result.json').write_text(json.dumps(result))
        return result

    def execute(self, **overrides):
        kwargs = dict(source=self.source, root=self.root, allowed=self.allowed,
                      supervise=self.fake)
        kwargs.update(overrides)
        return worker.execute(self.packet, **kwargs)

    def test_original_ids_limits_and_client_are_preserved(self):
        result = self.execute()
        self.assertEqual(result['run_id'], 'r-original-123')
        self.fake.assert_called_once_with(self.root, 'r-original-123')
        ledger = Ledger(self.root / 'ledger.db')
        try:
            row = ledger.get('r-original-123')
            self.assertEqual(row['request_id'], 'original-request')
            self.assertEqual(row['input_id'], self.packet['request']['input_id'])
            self.assertEqual(row['client'], 'owner@mac')
            self.assertEqual(row['source_path'], str(self.source))
            self.assertEqual((row['reservation_mib'], row['ceiling_mib'], row['cpus_hint']),
                             (1024, 4096, 4))
        finally:
            ledger.close()
        self.assertEqual((self.root / 'cpus_per_run').read_text(), '4')
        toolchain = json.loads((self.root / 'runs/r-original-123/toolchain.json').read_text())
        self.assertNotIn('pins', toolchain)
        self.assertNotIn('pin_notes', toolchain)
        self.assertNotIn('built_from', toolchain)
        self.assertIsNone(result['verdict'])

    def test_learned_ceiling_does_not_replace_declared_size(self):
        self.packet['limits']['ceiling_mib'] = 12288
        self.execute()
        ledger = Ledger(self.root / 'ledger.db')
        try:
            row = ledger.get(self.packet['run_id'])
            self.assertEqual(row['size_class'], 'xlarge')
            self.assertEqual(row['size_declared'], 'medium')
        finally:
            ledger.close()

    def test_exact_duplicate_returns_finished_result_without_execution(self):
        result = self.execute()
        self.assertEqual(self.execute(), result)
        self.assertEqual(self.fake.call_count, 1)

    def test_first_and_duplicate_results_use_identical_persisted_json_types(self):
        def tuple_receipt(root, run_id):
            result = self.finish(root, run_id)
            result['receipt'] = {'leftovers': ()}
            (root / 'runs' / run_id / 'result.json').write_text(json.dumps(result))
            return result  # Real runner returns tuples that JSON persists as lists.

        self.fake.side_effect = tuple_receipt
        result = self.execute()
        self.assertEqual(result['receipt']['leftovers'], [])
        self.assertEqual(result, self.execute())
        self.assertEqual(self.fake.call_count, 1)

    def test_supervisor_return_does_not_prove_completion_or_result_existence(self):
        self.fake.side_effect = None
        self.fake.return_value = {'outcome': 'passed'}
        with self.assertRaises(worker.AttemptUncertain):
            self.execute()
        ledger = Ledger(self.root / 'ledger.db')
        try:
            ledger.finish(self.packet['run_id'], outcome='passed', exit_code=0)
        finally:
            ledger.close()
        # A finished ledger without its result file also cannot fabricate a pass.
        with self.assertRaises(worker.AttemptUncertain):
            self.execute()
        self.assertEqual(self.fake.call_count, 1)

    def test_persisted_result_must_match_original_ids_and_ledger_outcome(self):
        for field in ('run_id', 'request_id', 'input_id', 'outcome'):
            with self.subTest(field=field):
                self.packet['run_id'] = 'run-' + field
                self.packet['request']['request_id'] = 'request-' + field

                def mismatched(root, run_id):
                    result = self.finish(root, run_id)
                    result[field] = 'mismatched'
                    (root / 'runs' / run_id / 'result.json').write_text(json.dumps(result))
                    return result

                self.fake.side_effect = mismatched
                with self.assertRaises(worker.PacketRejected):
                    self.execute()

    def test_unfinished_claim_is_uncertain_and_never_reexecuted(self):
        self.fake.side_effect = RuntimeError('worker transport or process lost')
        with self.assertRaises(RuntimeError):
            self.execute()
        with self.assertRaises(worker.AttemptUncertain):
            self.execute()
        self.assertEqual(self.fake.call_count, 1)

    def test_partial_intent_is_uncertain_and_never_executed(self):
        marker = self.root / 'runs' / self.packet['run_id'] / 'overflow-packet.json'
        marker.parent.mkdir(parents=True)
        marker.write_text(worker.canonical({'packet': self.packet, 'source': str(self.source)}))
        with self.assertRaises(worker.AttemptUncertain):
            self.execute()
        self.fake.assert_not_called()

    def test_duplicate_with_changed_command_is_rejected(self):
        self.execute()
        self.packet['request']['plan']['argv'] = ['different-command']
        with self.assertRaises(worker.PacketRejected):
            self.execute()
        self.assertEqual(self.fake.call_count, 1)

    def test_changed_bytes_mode_extra_or_missing_files_are_rejected(self):
        for change in ('bytes', 'mode', 'extra', 'missing'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                source = self.root / 'src' / Path(temporary).name
                source.mkdir()
                path = source / 'hello.py'
                path.write_text('print("hello")\n')
                if change == 'bytes':
                    path.write_text('different bytes')
                elif change == 'mode':
                    path.chmod(0o755)
                elif change == 'extra':
                    (source / 'extra.txt').write_text('extra')
                else:
                    path.unlink()
                with self.assertRaises(worker.PacketRejected):
                    self.execute(source=source)
        self.fake.assert_not_called()

    def test_manifest_path_traversal_absolute_duplicate_and_noncanonical_rejected(self):
        for name in ('../outside', '/absolute', 'a/../hello.py', './hello.py', 'a//b'):
            with self.subTest(name=name):
                self.packet['manifest'] = [{'path': name, 'sha256': 'bad', 'executable': False}]
                with self.assertRaises(worker.PacketRejected):
                    self.execute()
        self.packet['manifest'] = self.manifest * 2
        with self.assertRaises(worker.PacketRejected):
            self.execute()
        self.fake.assert_not_called()

    def test_symlink_file_and_parent_escape_are_rejected(self):
        outside = Path(self.temporary.name) / 'outside'
        outside.mkdir()
        (outside / 'file').write_text('not frozen')
        (self.source / 'link').symlink_to(outside, target_is_directory=True)
        for name in ('link', 'link/file'):
            self.packet['manifest'] = [{'path': name, 'link': '../outside'}]
            with self.assertRaises(worker.PacketRejected):
                self.execute()
        self.fake.assert_not_called()

    def test_input_id_and_git_marks_are_bound_to_manifest(self):
        self.packet['request']['input_id'] = 'wrong'
        with self.assertRaises(worker.PacketRejected):
            self.execute()
        self.packet['request']['input_id'] = freeze.input_id(self.manifest)
        self.packet['request']['git_marks']['untracked'] = ['hello.py']
        with self.assertRaises(worker.PacketRejected):
            self.execute()

    def test_source_must_be_strictly_inside_engine_source_tree(self):
        for source in (self.root, self.root / 'src', Path(self.temporary.name)):
            with self.subTest(source=source), self.assertRaises(worker.PacketRejected):
                self.execute(source=source)
        (self.root / 'src/escape').symlink_to(self.root)
        with self.assertRaises(worker.PacketRejected):
            self.execute(source=self.root / 'src/escape')

    def test_only_explicit_allowed_remote_single_jobs_are_supported(self):
        with self.assertRaises(worker.PacketRejected):
            self.execute(allowed=set())
        original = copy.deepcopy(self.packet)
        for field, value in (('shards', {'default': 1}), ('singleton', True),
                             ('session', 'existing'), ('where', 'local'),
                             ('writeback', True)):
            self.packet = copy.deepcopy(original)
            self.packet['request']['plan'][field] = value
            with self.subTest(field=field), self.assertRaises(worker.PacketRejected):
                self.execute()
        self.packet = copy.deepcopy(original)
        self.packet['role'] = 'parent'
        with self.assertRaises(worker.PacketRejected):
            self.execute()
        self.packet = copy.deepcopy(original)
        self.packet['request']['plan']['outputs'] = [{'kind': 'writeback', 'paths': ['fixture']}]
        with self.assertRaises(worker.PacketRejected):
            self.execute()
        self.fake.assert_not_called()

    def test_host_headroom_and_cpu_limit_are_enforced(self):
        original = copy.deepcopy(self.packet['limits'])
        for key, value in (('ceiling_mib', 16384), ('reservation_mib', 8192),
                           ('cpus_hint', 9), ('cpus_hint', True), ('ceiling_mib', 3072)):
            self.packet['limits'] = {**original, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(worker.PacketRejected):
                self.execute()
        self.fake.assert_not_called()

    def test_frozen_ceiling_cannot_lower_the_declared_size_floor(self):
        self.packet['request']['plan']['size'] = 'xlarge'
        self.packet['limits']['ceiling_mib'] = 4096
        with self.assertRaisesRegex(worker.PacketRejected, 'below the declared size'):
            self.execute()
        self.fake.assert_not_called()
        self.assertFalse((self.root / 'ledger.db').exists())

    def test_conflicting_worker_cpu_override_is_rejected(self):
        os.environ['PANDORA_CPUS_PER_RUN'] = '8'
        with self.assertRaises(worker.PacketRejected):
            self.execute()
        self.fake.assert_not_called()

    def test_another_live_attempt_prevents_claiming_shared_root(self):
        self.fake.side_effect = RuntimeError('unfinished')
        with self.assertRaises(RuntimeError):
            self.execute()
        self.packet['run_id'] = 'different-run'
        self.packet['request']['request_id'] = 'different-request'
        with self.assertRaisesRegex(worker.PacketRejected, 'another live attempt'):
            self.execute()
        self.assertEqual(self.fake.call_count, 1)


if __name__ == '__main__':
    unittest.main()
