import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from snapshot import digest
from suite_parent import execute, validate_result, stage_child
from test_suite_parent_evidence import plan, report
from worker_bundle import NAMES


def receipt(path, metadata, result, action, code):
    (path / 'results').mkdir(exist_ok=True)
    (path / ('results/suite-' + action + '.json')).write_text(json.dumps(result))
    (path / 'results/exit-code').write_text(str(code))
    (path / 'queue.json').write_text(json.dumps({'waited': 3, 'acquired': True}))
    (path / 'terminal.json').write_text(json.dumps({'attempt': path.name, 'workflow': 'suite',
                                                  'exit_code': code, 'cleanup_verified': True}))
    files = [*path.glob('results/*'), path / 'queue.json']
    (path / 'artifacts.json').write_text(json.dumps({str(x.relative_to(path)): digest(x) for x in files}))


class ParentTests(unittest.TestCase):
    def run_suite(self, root, keep_going=False, fail=0, budget=20, plan_fail=False):
        parent = root / 'runs' / ('f' * 32)
        parent.mkdir(parents=True)
        (parent / 'source').mkdir()
        (parent / 'source/input').write_text('frozen bytes')
        (parent / 'manifest.json').write_text('[]')
        for name in NAMES:
            (parent / name).write_bytes((Path(__file__).parent / name).read_bytes())
        (parent / 'runtime.Dockerfile').write_text('FROM unused')
        frozen = plan()
        submitted = {'attempt': parent.name, 'workflow': 'suite-run', 'source_digest': frozen['source_digest'],
                     'queue_timeout_seconds': budget, 'suite': {'action': 'run', 'shard_count': 3,
                     'selection': frozen['selection'], 'keep_going': keep_going}}
        seen = []
        def child_run(owner, child, started, waited):
            metadata = json.loads((child / 'submission.json').read_text())
            self.assertEqual(metadata['source_digest'], submitted['source_digest'])
            self.assertEqual((child / 'source/input').read_text(), 'frozen bytes')
            self.assertEqual(metadata['queue_timeout_seconds'], budget - 3 * len(seen))
            seen.append(child.name)
            task = metadata['suite']
            code = 70 if plan_fail and task['action'] == 'plan' else 1 if task.get('shard') == fail else 0
            result = frozen if task['action'] == 'plan' else report(frozen, task['shard'], code=code, status='fail' if code else 'pass')
            receipt(child, metadata, result, task['action'], code)
            return None
        with patch('suite_parent.run_child', side_effect=child_run), contextlib.redirect_stdout(io.StringIO()):
            status = execute(parent, submitted)
        artifacts = {str(x.relative_to(parent)): digest(x) for x in parent.rglob('*')
                     if x.is_file() and (x.is_relative_to(parent / 'results') or x.name in ('children.json', 'suite-state.json'))}
        terminal = {'exit_code': status}
        validate_result(parent, submitted, terminal, artifacts)
        return parent, submitted, terminal, artifacts, seen

    def test_failfast_reserves_all_ids_but_does_not_stage_tail(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, submitted, terminal, artifacts, seen = self.run_suite(Path(temp), fail=1)
            self.assertEqual(terminal['exit_code'], 1)
            self.assertEqual(len(seen), 2)
            result = json.loads((parent / 'results/suite-run.json').read_text())
            self.assertEqual(result['unrun_shards'], [2, 3])
            for identity in result['shard_attempts'][1:]:
                self.assertFalse((parent.parent / identity).exists())
            # Checksums alone cannot authorize another attempt's otherwise valid evidence.
            result['shard_attempts'][0] = 'a' * 32
            (parent / 'results/suite-run.json').write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, 'reserved identities'):
                validate_result(parent, submitted, terminal, artifacts)

    def test_keepgoing_charges_all_previous_queue_wait_and_preserves_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, _, terminal, _, seen = self.run_suite(Path(temp), keep_going=True, fail=1)
            self.assertEqual((terminal['exit_code'], len(seen)), (1, 4))
            self.assertEqual(json.loads((parent / 'suite-state.json').read_text())['queue_seconds'], 12)

    def test_cumulative_budget_stops_before_next_shard(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, _, terminal, _, seen = self.run_suite(Path(temp), budget=6)
            self.assertEqual((terminal['exit_code'], len(seen)), (75, 2))
            result = json.loads((parent / 'results/suite-run.json').read_text())
            self.assertEqual(result['stop_reason'], 'queue-timeout')
            self.assertEqual(result['unrun_shards'], [2, 3])

    def test_failed_planning_has_verified_receipt_and_never_starts_shards(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, submitted, terminal, artifacts, seen = self.run_suite(Path(temp), plan_fail=True)
            self.assertEqual((terminal['exit_code'], len(seen)), (70, 1))
            self.assertTrue((parent / 'results/suite-error.json').exists())
            (parent / 'results/suite-error.json').unlink()
            with self.assertRaises((OSError, ValueError)):
                validate_result(parent, submitted, terminal, artifacts)

    def test_queue_budget_cannot_be_reset_in_child_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, submitted, terminal, artifacts, seen = self.run_suite(Path(temp))
            metadata_path = parent / 'results/attempts' / seen[-1] / 'submission.json'
            metadata = json.loads(metadata_path.read_text())
            metadata['queue_timeout_seconds'] = submitted['queue_timeout_seconds']
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, 'reset'):
                validate_result(parent, submitted, terminal, artifacts)

    def test_no_missing_child_receipt_can_become_an_intentional_skip(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, submitted, terminal, artifacts, seen = self.run_suite(Path(temp))
            child = parent / 'results/attempts' / seen[-1]
            (child / 'terminal.json').unlink()
            with self.assertRaises((OSError, ValueError)):
                validate_result(parent, submitted, terminal, artifacts)


if __name__ == '__main__':
    unittest.main()
