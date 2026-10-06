"""Saved run diagnosis works with no daemon or surviving repository."""
import base64
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import doctor


class RecordedRun(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.run_id = '123456789abc'
        self.directory = self.state / 'runs' / self.run_id
        self.directory.mkdir(parents=True)
        self.meta = {'repo': 'acme', 'job': 'unit', 'lane': 'remote',
                     'worktree': '/deleted/worktree', 'remote': 'rworker',
                     'argv': ['pnpm', 'unit'], 'state': 'infra_failed', 'exit_code': 70}
        self.save('meta.json', self.meta)

    def save(self, filename, value):
        (self.directory / filename).write_text(json.dumps(value))

    def report(self):
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('live probe')):
            return doctor.from_run(self.state, self.run_id)

    def test_source_preparation_points_at_the_job_instead_of_path(self):
        self.meta['phase'] = 'finished'
        self.save('meta.json', self.meta)
        self.save('result.json', {'outcome': 'infra_failed', 'hint': 'fix preparation',
                                 'evidence': {'cause': 'prepare-command-failed'}})
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        report = self.report()
        run = report['run']
        self.assertFalse(report['ok'])
        self.assertEqual(run['class'], 'prepare-command-failed')
        self.assertEqual(run['phase'], 'prepare_command')
        self.assertEqual(run['phase_source'], 'cause')
        self.assertEqual(run['last_reported_phase'], 'finished')
        self.assertFalse(run['retryable'])
        self.assertIn('job prepare_command', run['implicated'])
        self.assertEqual(run['hint'], 'fix preparation')
        self.assertIsNone(run['worker'], 'today\'s worker must not be passed off as recorded')
        self.assertEqual(before, {p: p.read_bytes() for p in self.directory.iterdir()})

    def test_legacy_clone_cause_and_retry_table_are_recovered(self):
        self.save('result.json', {'outcome': 'infra_failed',
                                 'evidence': {'error': 'CloneFailed: copy failed'}})
        run = self.report()['run']
        self.assertEqual(run['cause'], 'clone-failed')
        self.assertTrue(run['retryable'])
        self.assertIn('shares nothing', run['retry_reason'])

    def test_preparation_resource_failure_uses_its_evidence_phase(self):
        self.meta.update(state='oom', phase='finished')
        self.save('meta.json', self.meta)
        self.save('result.json', {'outcome': 'oom', 'job': 'unit', 'size_used': 'medium',
                                 'peak_mib': 2048, 'ceiling_mib': 2048,
                                 'evidence': {'preparation': {'outcome': 'oom'}}})
        run = self.report()['run']
        self.assertEqual(run['class'], 'oom')
        self.assertEqual(run['phase'], 'prepare_command')
        self.assertEqual(run['phase_source'], 'evidence')

    def test_preparation_resource_cause_does_not_claim_an_unknown_infra_cause(self):
        self.save('result.json', {'outcome': 'oom', 'evidence': {
            'cause': 'prepare-command-oom', 'preparation': {'outcome': 'oom'}}})
        run = self.report()['run']
        self.assertFalse(run['retryable'])
        self.assertIn('only to infra_failed', run['retry_reason'])
        self.assertIn('memory', run['implicated'])

    def test_refusal_and_partial_capture_survive_without_a_result(self):
        self.meta.update(phase='ship', refusal={'cause': 'worker-unreachable', 'detail': 'no route'},
                         pre_accept={'freeze': 2}, freeze_steps={'pass1.entries': 1})
        self.save('meta.json', self.meta)
        run = self.report()['run']
        self.assertEqual(run['phase'], 'ship')
        self.assertEqual(run['phase_source'], 'recorded')
        self.assertEqual(run['cause'], 'worker-unreachable')
        self.assertFalse(run['retryable'])
        self.assertEqual(run['freeze_steps'], {'pass1.entries': 1})

    def test_unknown_explicit_cause_is_not_replaced_by_engine_error(self):
        self.save('result.json', {'outcome': 'infra_failed', 'evidence': {'cause': 'future-cause'}})
        run = self.report()['run']
        self.assertEqual(run['cause'], 'future-cause')
        self.assertFalse(run['retryable'])
        self.assertIsNone(run['phase'])

    def test_invalid_nested_cause_evidence_reports_failure_instead_of_crashing(self):
        for evidence, verification in [({'cause': []}, {}), ({}, 'bad')]:
            with self.subTest(evidence=evidence, verification=verification):
                self.save('result.json', {'outcome': 'infra_failed', 'evidence': evidence,
                                         'verification': verification})
                report = self.report()
                self.assertFalse(report['ok'])
                self.assertTrue(any(check['name'] == 'saved cause' for check in report['checks']))

    def test_successful_fallback_does_not_inherit_the_remote_refusal(self):
        self.meta.update(state='passed', exit_code=0, lane='local',
                         refusal={'cause': 'worker-unreachable', 'detail': 'no route'})
        self.save('meta.json', self.meta)
        self.save('result.json', {'outcome': 'passed'})
        report = self.report()
        self.assertTrue(report['ok'])
        self.assertEqual(report['run']['class'], 'passed')
        self.assertIsNone(report['run']['cause'])
        self.assertIsNone(report['run']['implicated'])

    def test_local_missing_executable_does_not_blame_the_worker(self):
        self.meta.update(state='command_failed', exit_code=127, lane='local')
        self.save('meta.json', self.meta)
        self.save('result.json', {'outcome': 'command_failed', 'observed_exit': 127})
        self.save('log', {'t': 'log', 'b64': base64.b64encode(
            b'sh: playwright: command not found\n').decode()})
        run = self.report()['run']
        self.assertIn('playwright', run['log_tail'])
        self.assertIsNone(run['hint'])

    def test_log_tail_is_decoded_and_can_supply_a_saved_fact_hint(self):
        self.save('result.json', {'outcome': 'command_failed', 'observed_exit': 127})
        data = base64.b64encode(b'sh: playwright: command not found\n').decode()
        (self.directory / 'log').write_text('\n'.join([
            'truncated', '[]', '{"t":"log"}', '{"t":"log","b64":"?"}',
            json.dumps({'t': 'log', 'b64': data})]) + '\n')
        run = self.report()['run']
        self.assertIn('playwright', run['log_tail'])
        self.assertIn('not installed on the worker', run['hint'])

    def test_passing_run_reports_no_inferred_failure_phase(self):
        self.meta.update(state='passed', exit_code=0)
        self.save('meta.json', self.meta)
        self.save('result.json', {'outcome': 'passed', 'evidence': {}})
        report = self.report()
        self.assertTrue(report['ok'])
        self.assertIsNone(report['run']['retryable'])
        self.assertIsNone(report['run']['phase'])
        self.assertIn('no live checks', doctor.render(report))

    def test_active_run_does_not_claim_a_verdict(self):
        self.meta.update(state='running', exit_code=None, phase='execute')
        self.save('meta.json', self.meta)
        report = self.report()
        self.assertTrue(report['ok'])
        self.assertTrue(any(check['name'] == 'saved result' and check['status'] == 'warn'
                            for check in report['checks']))

    def test_missing_invalid_and_non_object_records_fail_without_creation(self):
        for filename, value in [('meta.json', '[1]'), ('result.json', '{broken')]:
            (self.directory / filename).write_text(value)
        self.assertFalse(self.report()['ok'])
        missing = self.state / 'never-created'
        self.assertFalse(doctor.from_run(missing, self.run_id)['ok'])
        self.assertFalse(missing.exists())
        for invalid in ('../outside', '/absolute', '..', 'a/b'):
            self.assertFalse(doctor.from_run(self.state, invalid)['ok'])

    def test_run_directory_symlink_is_refused(self):
        (self.state / 'runs' / 'linked').symlink_to(self.directory, target_is_directory=True)
        self.assertFalse(doctor.from_run(self.state, 'linked')['ok'])

    def test_cli_explicit_state_works_despite_bad_config_and_does_not_probe(self):
        bad_config = self.state / 'config.toml'
        bad_config.write_text('[broken')
        output = io.StringIO()
        with mock.patch.object(doctor, 'run', side_effect=AssertionError('live doctor')):
            with contextlib.redirect_stdout(output):
                code = cli.main(['--state', str(self.state), '--config', str(bad_config),
                                 'doctor', '--from-run', self.run_id, '--json'])
        self.assertEqual(code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report['mode'], 'recorded')
        self.assertEqual(report['run']['repo'], 'acme')
