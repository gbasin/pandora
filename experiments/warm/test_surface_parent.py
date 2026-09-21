"""Authenticate surface parents against real retained child file receipts."""
import json
from pathlib import Path
import tempfile
import unittest

from evidence import validate_evidence
from snapshot import digest
from surface_parent import validate_result
from surface_parent_evidence import summarize, output_conflicts
from surface_suite import outputs_manifest, plan_digest
from test_surface_suite import plan, report
from worker_config import identity as config_identity


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def receipt(path, metadata, code):
    terminal = {'attempt': metadata['attempt'], 'workflow': metadata['workflow'],
                'exit_code': code, 'cleanup_verified': True}
    (path / 'results').mkdir(exist_ok=True)
    (path / 'results/exit-code').write_text(str(code))
    write(path / 'submission.json', metadata)
    write(path / 'terminal.json', terminal)
    manifest = {str(file.relative_to(path)): digest(file) for file in path.rglob('*')
                if file.is_file() and str(file.relative_to(path)) not in ('artifacts.json', 'terminal.json', 'submission.json')}
    write(path / 'artifacts.json', manifest)
    return terminal, manifest


class SurfaceParentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name)
        self.config = json.loads(Path(__file__).with_name('worker-config.example.json').read_text())

    def fixture(self, codes=(0, 0, 0), reason=None, planner_code=0, keep=False):
        frozen = plan(); frozen['keep_going'] = keep
        children = [str(i) * 32 for i in range(1, 5)]
        request = {key: frozen[key] for key in ('app', 'selectors', 'shard_count', 'keep_going')} | {'action': 'run'}
        submitted = {'attempt': frozen['parent_attempt'], 'workflow': 'surface-run',
                     'source_digest': frozen['source_digest'], 'surface_app': frozen['app'],
                     'selectors': frozen['selectors'], 'surface_suite': request,
                     'worker_config': self.config, 'queue_timeout_seconds': 900}
        planner = self.stage / 'results/attempts' / children[0]
        for name in ('apps/borrower-web/dist/x', 'apps/borrower-web/e2e/dist/y'):
            path = planner / 'results/outputs' / name
            path.parent.mkdir(parents=True, exist_ok=True); path.write_text(name)
        frozen['build'] = outputs_manifest(planner / 'results/outputs', frozen['app'])
        frozen['plan_id'] = plan_digest(frozen)
        write(planner / 'results/surface-plan.json', frozen)
        completed, reports = [], []
        for index, code in enumerate((planner_code, *(() if planner_code else codes))):
            child = self.stage / 'results/attempts' / children[index]
            task = request | {'action': 'plan'} if index == 0 else {'action': 'shard', 'plan': frozen, 'shard': index}
            metadata = submitted | {'attempt': children[index], 'workflow': 'surface',
                                    'parent_attempt': submitted['attempt'], 'surface_suite': task}
            write(child / 'results/surface.json', {'app': frozen['app']})
            if index and code != 75:
                value = report(frozen, index, code=code)
                write(child / 'results/surface-shard.json', value); reports.append(value)
            receipt(child, metadata, code)
            completed.append(children[index])
        if planner_code:
            reason = 'deadline' if planner_code == 124 else 'planning-failed'
            code = 75 if planner_code == 124 else planner_code
            write(self.stage / 'results/surface-error.json', {'version': 1, 'parent_attempt': submitted['attempt'],
                  'source_digest': submitted['source_digest'], 'plan_attempt': children[0], 'reason': reason, 'exit_code': planner_code})
        else:
            result = summarize(frozen, reports, keep_going=keep, stop_reason=reason)
            code, reason = result['exit_code'], result['stop_reason']
            write(self.stage / 'results/surface-plan.json', frozen)
            write(self.stage / 'results/surface-run.json', result)
        write(self.stage / 'children.json', {'version': 1, 'parent_attempt': submitted['attempt'], 'children': children})
        write(self.stage / 'suite-state.json', {'version': 2, 'reserved': children, 'dispatched': completed,
              'completed': list(reversed(completed)), 'queue_seconds': 1.25, 'stop_reason': reason})
        write(self.stage / 'queue.json', {'mode': 'resource', 'invocation': submitted['attempt'],
              'waited': 1.25, 'config_digest': config_identity(self.config)})
        terminal, manifest = receipt(self.stage, submitted, code)
        return submitted, terminal, manifest, children

    def test_pass_uses_every_retained_report_and_accepts_unordered_completion(self):
        submitted, _, _, _ = self.fixture()
        self.assertEqual(validate_evidence(self.stage, submitted['attempt'])['exit_code'], 0)

    def test_fail_fast_can_withdraw_waiting_child_without_faking_a_report(self):
        submitted, terminal, manifest, _ = self.fixture(codes=(1, 75), reason='test-failure')
        validate_result(self.stage, submitted, terminal, manifest)
        self.assertEqual(terminal['exit_code'], 1)

    def test_keep_going_collects_all_failures(self):
        submitted, terminal, manifest, _ = self.fixture(codes=(1, 1, 0), keep=True)
        validate_result(self.stage, submitted, terminal, manifest)
        self.assertEqual(terminal['exit_code'], 1)

    def test_planning_failure_has_a_verified_child_and_no_dispatched_shards(self):
        submitted, terminal, manifest, _ = self.fixture(planner_code=70)
        validate_result(self.stage, submitted, terminal, manifest)
        self.assertEqual(terminal['exit_code'], 70)

    def test_deadline_is_stopped_even_after_a_passing_shard(self):
        submitted, terminal, manifest, _ = self.fixture(codes=(0, 124), reason='deadline')
        validate_result(self.stage, submitted, terminal, manifest)
        self.assertEqual(terminal['exit_code'], 75)

    def test_summary_cannot_replace_retained_failed_report_with_pass(self):
        submitted, terminal, manifest, _ = self.fixture(codes=(1, 0, 0))
        path = self.stage / 'results/surface-run.json'; value = json.loads(path.read_text())
        value['reports'][0]['exit_code'] = 0; value.update(exit_code=0, status='pass', stop_reason=None)
        write(path, value)
        state = json.loads((self.stage / 'suite-state.json').read_text()); state['stop_reason'] = None
        write(self.stage / 'suite-state.json', state)
        terminal['exit_code'] = 0; (self.stage / 'results/exit-code').write_text('0')
        with self.assertRaises(ValueError): validate_result(self.stage, submitted, terminal, manifest)

    def test_metadata_cannot_change_queue_budget(self):
        submitted, terminal, manifest, children = self.fixture()
        path = self.stage / 'results/attempts' / children[1] / 'submission.json'
        value = json.loads(path.read_text()); value['queue_timeout_seconds'] = 1800; write(path, value)
        with self.assertRaises(ValueError): validate_result(self.stage, submitted, terminal, manifest)

    def test_duplicate_completed_identity_cannot_replace_missing_work(self):
        submitted, terminal, manifest, _ = self.fixture()
        path = self.stage / 'suite-state.json'; value = json.loads(path.read_text())
        value['completed'].append(value['completed'][0]); write(path, value)
        with self.assertRaises(ValueError): validate_result(self.stage, submitted, terminal, manifest)

    def test_conflicting_outputs_are_terminal_nonpass_and_identical_duplicates_are_allowed(self):
        submitted, terminal, manifest, children = self.fixture()
        name = 'results/generated/apps/borrower-web/e2e/dist/evidence.png'
        for index, contents in ((1, 'first'), (2, 'different')):
            child = self.stage / 'results/attempts' / children[index]
            (child / name).parent.mkdir(parents=True, exist_ok=True); (child / name).write_text(contents)
            metadata = json.loads((child / 'submission.json').read_text()); receipt(child, metadata, 0)
        conflicts = output_conflicts(self.stage, children, 'borrower-web')
        self.assertEqual(len(conflicts), 1)
        with self.assertRaisesRegex(ValueError, 'conflicting generated outputs'):
            validate_result(self.stage, submitted, terminal, manifest)
        path = self.stage / 'results/surface-run.json'; result = json.loads(path.read_text())
        frozen = json.loads((self.stage / 'results/surface-plan.json').read_text())
        result = summarize(frozen, result['reports'], keep_going=False, stop_reason='infrastructure'); write(path, result)
        state = json.loads((self.stage / 'suite-state.json').read_text()); state['stop_reason'] = 'infrastructure'
        write(self.stage / 'suite-state.json', state)
        write(self.stage / 'results/surface-output-error.json', {'version': 1, 'parent_attempt': submitted['attempt'],
              'source_digest': submitted['source_digest'], 'conflicts': conflicts})
        terminal, manifest = receipt(self.stage, submitted, 75)
        validate_result(self.stage, submitted, terminal, manifest)
        second = self.stage / 'results/attempts' / children[2]
        (second / name).write_text('first'); receipt(second, json.loads((second / 'submission.json').read_text()), 0)
        self.assertEqual(output_conflicts(self.stage, children, 'borrower-web'), [])
        with self.assertRaisesRegex(ValueError, 'conflict differs'):
            validate_result(self.stage, submitted, terminal, manifest)

    def test_nonfinite_queue_clock_is_rejected(self):
        submitted, terminal, manifest, _ = self.fixture()
        path = self.stage / 'queue.json'; value = json.loads(path.read_text()); value['waited'] = float('inf'); write(path, value)
        with self.assertRaises(ValueError): validate_result(self.stage, submitted, terminal, manifest)

if __name__ == '__main__': unittest.main()
