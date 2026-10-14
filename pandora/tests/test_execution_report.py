import json
import tempfile
import unittest
from pathlib import Path

from pandora.engine import execution_report


class ExecutionReportTests(unittest.TestCase):
    def test_only_declared_run_bound_complete_report_is_collected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / execution_report.PATH
            path.parent.mkdir()
            outputs = [{'kind': 'artifacts', 'paths': [execution_report.PATH]}]
            report = {'kind': 'pandora-execution-report', 'schema': 1, 'worker_run': 'run1',
                      'execution': {'complete': True, 'outcome': 'passed'}}
            path.write_text(json.dumps(report))
            self.assertEqual(execution_report.load(root, [], 'run1'), (None, None))
            artifact, reason = execution_report.load(root, outputs, 'run1')
            self.assertIsNone(reason)
            self.assertEqual(artifact['bytes'], len(path.read_bytes()))
            self.assertIsNone(execution_report.load(root, outputs, 'other')[0])
            report['execution']['complete'] = False
            path.write_text(json.dumps(report))
            self.assertIsNone(execution_report.load(root, outputs, 'run1')[0])

    def test_symlink_output_never_collects_foreign_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / execution_report.PATH
            path.parent.mkdir()
            foreign = root / 'foreign.json'
            foreign.write_text('{}')
            path.symlink_to(foreign)
            outputs = [{'kind': 'artifacts', 'paths': [execution_report.PATH]}]
            self.assertIsNone(execution_report.load(root, outputs, 'run1')[0])

    def test_preparation_cannot_receive_report_identity_or_output(self):
        env = {'KEEP': 'yes', 'PANDORA_EXECUTION_REPORT': '/report', 'PANDORA_EXECUTION_RUN': 'run1',
               'PANDORA_TEST_EVIDENCE': '/old', 'PANDORA_TEST_EVIDENCE_RUN': 'run1'}
        self.assertEqual(execution_report.prepare_environment(env), {'KEEP': 'yes'})
        self.assertEqual(env['PANDORA_EXECUTION_RUN'], 'run1')

    def test_cleanup_and_integrity_must_be_explicitly_proven(self):
        from types import SimpleNamespace
        instance = SimpleNamespace(name='clone')
        absent = SimpleNamespace()
        self.assertFalse(execution_report.fresh_output(absent, instance))
        self.assertFalse(execution_report.source_integrity(absent, instance, {}))
        failing = SimpleNamespace(clear_execution_report=lambda *args: 1,
                                  source_integrity=lambda *args: 1)
        self.assertFalse(execution_report.fresh_output(failing, instance))
        self.assertFalse(execution_report.source_integrity(failing, instance, {}))
        good = SimpleNamespace(clear_execution_report=lambda *args: True,
                               source_integrity=lambda *args: True)
        self.assertTrue(execution_report.fresh_output(good, instance))
        self.assertTrue(execution_report.source_integrity(good, instance, {}))
