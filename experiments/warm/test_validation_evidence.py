import copy
import json
from pathlib import Path
import tempfile
import unittest

from validation_evidence import validate_result
from worker_config import demand
from test_worker_config import config


class ValidationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'results').mkdir()
        self.submitted = {'attempt': 'a' * 32, 'workflow': 'validation',
                          'validation': {'version': 1, 'suite': 'unit', 'args': []}}
        self.terminal = {'exit_code': 0}
        self.manifest = {'results/validation.json': '', 'results/unit.json': ''}
        self.report = {'version': 1, 'attempt': 'a' * 32, 'suite': 'unit', 'args': [],
                       'exit_code': 0, 'steps': [{'argv': ['pnpm', 'exec', 'vitest', 'run'],
                                                'exit_code': 0, 'test_count': 3,
                                                'report': 'unit.json'}]}

    def verify(self, report=None, terminal=None, manifest=None):
        (self.root / 'results/validation.json').write_text(json.dumps(report or self.report))
        return validate_result(self.root, self.submitted, terminal or self.terminal,
                               self.manifest if manifest is None else manifest)

    def test_identity_and_missing_report_cannot_pass(self):
        self.verify()
        for field, value in [('attempt', 'b' * 32), ('suite', 'tools'), ('args', ['other'])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(self.report | {field: value})
        with self.assertRaises(ValueError):
            self.verify(manifest={})

    def test_empty_failed_or_unreported_tests_cannot_pass(self):
        for change in [{'test_count': 0}, {'test_count': True}, {'exit_code': 1},
                       {'report': '../unit.json'}, {'report': 'missing.json'}]:
            report = copy.deepcopy(self.report)
            report['steps'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(report)
        with self.assertRaises(ValueError):
            self.verify(self.report | {'steps': []})

    def test_infrastructure_failure_preserves_completed_test_evidence(self):
        self.verify(terminal={'exit_code': 70})
        self.verify(terminal={'exit_code': 124}, manifest={})

    def test_admission_charges_services_only_where_required(self):
        base = self.submitted | {'worker_config': config()}
        for suite in ('postgres', 'browser-integration'):
            charged = demand(base | {'validation': {'suite': suite}})
            self.assertEqual(charged['memory_mib'], 7296)
        charged = demand(base)
        self.assertEqual(charged['memory_mib'], 6144)
        self.assertEqual(charged['cpu_millis'], 2000)


if __name__ == '__main__':
    unittest.main()
