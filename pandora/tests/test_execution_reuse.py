import copy
import functools
import subprocess
import tempfile
import unittest
from pathlib import Path

from pandora.client.execution_reuse import evaluate, input_manifest


class ExecutionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git('init', '-q')
        (self.root / 'adapter.py').write_text('producer')
        (self.root / 'config.json').write_text('{}')
        (self.root / 'src').mkdir()
        (self.root / 'src/a.py').write_text('a')
        self.tree = self.snapshot()
        self.spec = {'root': False, 'paths': ['adapter.py', 'config.json', 'absent'],
                     'directories': ['src']}
        self.manifest = functools.partial(input_manifest, self.root)
        self.request = {'schema': 1, 'repo': 'owner/repo', 'family': 'unit', 'check_id': 'check',
                        'source_tree': self.tree, 'engine_identity': 'a' * 64,
                        'runtime': {'node': '22', 'platform': 'linux'},
                        'selection': {'flags': []}, 'units': [{'id': 'file-a'}, {'id': 'file-b'}]}
        self.evidence = dict(copy.deepcopy(self.request), inputs=self.spec,
                             input_manifest=self.manifest(self.tree, self.spec),
                             finished=100, complete=True, outcome='passed')
        self.evidence['units'] = [{'id': identity, 'complete': True, 'outcome': 'passed',
                                  'attempts': [{'ordinal': 0, 'outcome': 'passed'}]}
                                 for identity in ('file-a', 'file-b')]
        self.policy = {'schema': 1, 'repo': 'owner/repo', 'family': 'unit', 'check_id': 'check',
                       'inputs': self.spec, 'required_paths': ['adapter.py', 'config.json'],
                       'runtime': self.request['runtime'], 'approved_engines': ['a' * 64],
                       'max_age_seconds': 60}

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args]).decode().strip()

    def snapshot(self):
        self.git('add', '-A')
        return self.git('write-tree')

    def decision(self, **kwargs):
        return evaluate(self.evidence, self.request, self.policy, manifest=self.manifest,
                        now=110, **kwargs)

    def test_all_families_use_same_contract(self):
        for family in ('unit', 'surface', 'journey', 'arc', 'node', 'native', 'static'):
            for value in (self.request, self.evidence, self.policy):
                value['family'] = family
            self.assertTrue(self.decision()['whole_complete'])
            self.assertEqual(self.decision()['skip_units'], ['file-a', 'file-b'])

    def test_cross_tree_unrelated_change(self):
        (self.root / 'unrelated').write_text('new')
        self.request['source_tree'] = self.snapshot()
        self.assertTrue(self.decision()['reusable'])
        self.policy['inputs'] = {'root': True, 'paths': [], 'directories': []}
        self.evidence['inputs'] = self.policy['inputs']
        self.evidence['input_manifest'] = self.manifest(self.tree, self.policy['inputs'])
        self.assertEqual(self.decision()['reason'], 'inputs_changed')

    def test_input_add_remove_missing_mode_symlink(self):
        mutations = [lambda: (self.root / 'absent').write_text('new'),
                     lambda: (self.root / 'src/new').write_text('new'),
                     lambda: (self.root / 'src/a.py').unlink(),
                     lambda: (self.root / 'src/a.py').chmod(0o755),
                     lambda: ((self.root / 'src/a.py').unlink(),
                              (self.root / 'src/a.py').symlink_to('../config.json'))]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.git('read-tree', self.tree)
                self.git('checkout-index', '-a', '-f')
                for name in ('absent', 'src/new'):
                    (self.root / name).unlink(missing_ok=True)
                path = self.root / 'src/a.py'
                if path.is_symlink():
                    path.unlink()
                    self.git('checkout-index', '-a', '-f')
                path.chmod(0o644)
                mutate()
                self.request['source_tree'] = self.snapshot()
                self.assertEqual(self.decision()['reason'], 'inputs_changed')

    def test_independent_units_only_by_policy(self):
        self.evidence['units'][1]['attempts'].append({'ordinal': 1, 'outcome': 'passed'})
        self.assertFalse(self.decision()['reusable'])
        self.policy['granularity'] = 'independent-unit'
        result = self.decision()
        self.assertEqual(result['skip_units'], ['file-a'])
        self.assertEqual(result['remaining_units'], ['file-b'])
        self.assertFalse(result['whole_complete'])

    def test_skipped_incomplete_and_attempts_fail_closed(self):
        for field, value in [('outcome', 'skipped'), ('complete', False),
                             ('attempts', []), ('attempts', [{'ordinal': False, 'outcome': 'passed'}]), ('attempts', [{'ordinal': 0, 'outcome': 'expected'}])]:
            with self.subTest(field=field):
                unit = self.evidence['units'][0]
                saved = copy.deepcopy(unit)
                unit[field] = value
                self.assertFalse(self.decision()['reusable'])
                self.evidence['units'][0] = saved

    def test_canary_and_force_full(self):
        for option in ('canary', 'force_full'):
            result = self.decision(**{option: True})
            self.assertTrue(result['whole_complete'])
            self.assertEqual(result['eligible_units'], ['file-a', 'file-b'])
            self.assertEqual(result['skip_units'], [])
            self.assertFalse(result['reusable'])

    def test_malformed_and_context(self):
        mutations = [('schema', True), ('family', 'unknown'), ('engine_identity', 'b' * 64),
                     ('runtime', {'node': '23'}), ('finished', 111), ('finished', 0),
                     ('input_manifest', 'wrong'), ('units', [{'id': 'a'}, {'id': 'a'}]),
                     ('selection', {'flags': ['only']})]
        for field, value in mutations:
            with self.subTest(field=field):
                saved = copy.deepcopy(self.evidence)
                self.evidence[field] = value
                self.assertFalse(self.decision()['reusable'])
                self.evidence = saved

    def test_runtime_types_and_engine_policy(self):
        self.request['runtime'] = {'flag': 1}
        self.policy['runtime'] = {'flag': 1}
        self.evidence['runtime'] = {'flag': True}
        self.assertEqual(self.decision()['reason'], 'runtime_mismatch')
        self.evidence['runtime'] = {'flag': 1}
        self.policy['approved_engines'] = 'a' * 64
        self.assertEqual(self.decision()['reason'], 'invalid_engine_policy')

    def test_unsafe_paths(self):
        for path in ('../a', '/a', 'a//b', 'a/./b', 'a\\b'):
            spec = {'root': False, 'paths': [path], 'directories': []}
            with self.assertRaises(ValueError):
                self.manifest(self.tree, spec)

    def test_policy_requires_producer_config_closure(self):
        self.policy['required_paths'].append('outside')
        self.assertEqual(self.decision()['reason'], 'missing_producer_config_inputs')


if __name__ == '__main__':
    unittest.main()
