import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from snapshot import digest
from suite_parent import _configured_child, execute, validate_result, stage_child
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
    def test_configured_child_marks_deadline_and_cancellation_before_reaping(self):
        class Queue:
            def snapshot(self): return {'invocations': [{'identity': 'f' * 32, 'waited': 0}]}
            def stop(self, invocation, reason): self.reason = reason
        class Process:
            def __init__(self): self.ended = False
            def poll(self): return 0 if self.ended else None
            def terminate(self): self.ended = True
            def wait(self, timeout=None): self.ended = True
            def kill(self): self.ended = True
        with tempfile.TemporaryDirectory() as temp:
            child = Path(temp); queue = Queue()
            with patch('suite_parent.subprocess.Popen', return_value=Process()), \
                 patch('suite_parent.time.monotonic', return_value=1):
                self.assertEqual(_configured_child(child, child, 0, queue, 'f' * 32, 0), 'deadline')
            self.assertTrue((child / 'deadline.request').exists())
            from threading import Event
            cancelled = Event(); cancelled.set()
            with patch('suite_parent.subprocess.Popen', return_value=Process()):
                self.assertEqual(_configured_child(child, child, 0, queue, 'f' * 32, 20, cancelled), 'cancelled')
            self.assertTrue((child / 'cancel.request').exists())

    def test_configured_parent_registers_once_and_keeps_parallel_receipts(self):
        for plan_failure in (False, True):
          with self.subTest(plan_failure=plan_failure), tempfile.TemporaryDirectory() as temp:
            root = Path(temp); parent = root / 'runs' / ('f' * 32); parent.mkdir(parents=True)
            (parent / 'source').mkdir(); (parent / 'source/input').write_text('frozen bytes')
            (parent / 'manifest.json').write_text('[]'); (parent / 'runtime.Dockerfile').write_text('FROM unused')
            for name in NAMES: (parent / name).write_bytes((Path(__file__).parent / name).read_bytes())
            frozen = plan()
            config = {'version': 1, 'scheduler': {'version': 1, 'cpu_millis': 4000, 'memory_mib': 16000,
                      'disk_mib': 32000, 'disk_floor_mib': 100, 'max_running': 2, 'policy': 'fair'},
                      'max_parallel': 2, 'execution_seconds': 1500, 'workspace_mib': 1000,
                      'limits': {'main': {'cpu_millis': 1000, 'memory_mib': 1024},
                                 'db': {'cpu_millis': 500, 'memory_mib': 512},
                                 'pool': {'cpu_millis': 100, 'memory_mib': 128},
                                 'proxy': {'cpu_millis': 100, 'memory_mib': 128}}}
            submitted = {'attempt': parent.name, 'workflow': 'suite-run', 'source_digest': frozen['source_digest'],
                         'queue_timeout_seconds': 20, 'worker_config': config,
                         'suite': {'action': 'run', 'shard_count': 3, 'selection': frozen['selection'], 'keep_going': False}}
            def child_run(owner, child, started, queue, invocation, execution, cancelled=None):
                task = json.loads((child / 'submission.json').read_text())['suite']
                if task.get('shard') == 1: time.sleep(.05)
                code = 70 if plan_failure and task['action'] == 'plan' else 1 if task.get('shard') == 2 else 0
                result = frozen if task['action'] == 'plan' else report(frozen, task['shard'], code=code, status='fail' if code else 'pass')
                receipt(child, json.loads((child / 'submission.json').read_text()), result, task['action'], code)
                return None
            from resource_admission import Scheduler
            with patch('suite_parent._configured_child', side_effect=child_run), \
                 patch('worker_runtime.scheduler', side_effect=lambda path, value: Scheduler(path, value['scheduler'], boot_id='test-boot')), \
                 contextlib.redirect_stdout(io.StringIO()):
                status = execute(parent, submitted)
            state = json.loads((parent / 'suite-state.json').read_text())
            self.assertEqual((status, state['stop_reason']), ((70, 'planning-failed') if plan_failure else (1, 'test-failure')))
            self.assertEqual(len(state['dispatched']), 1 if plan_failure else 3)  # plan plus two-slot window, never shard 3
            self.assertEqual(sorted(state['completed']), sorted(state['dispatched']))
            attempts = [parent / 'results/attempts' / identity / 'submission.json' for identity in state['completed']]
            self.assertTrue(all(json.loads(path.read_text())['queue_timeout_seconds'] == 20 for path in attempts))
            snapshot = Scheduler(root, config['scheduler'], boot_id='test-boot').snapshot()
            self.assertEqual([row['identity'] for row in snapshot['invocations']], [parent.name])
            self.assertEqual(len(snapshot['requests']), 0)
            artifacts = {str(x.relative_to(parent)): digest(x) for x in parent.rglob('*') if x.is_file() and
                         (x.is_relative_to(parent / 'results') or x.name in ('children.json', 'suite-state.json', 'queue.json'))}
            validate_result(parent, submitted, {'exit_code': status}, artifacts)
            if plan_failure:
                self.assertTrue((parent / 'queue.json').exists())
                queue = json.loads((parent / 'queue.json').read_text()); queue['waited'] = float('nan')
                (parent / 'queue.json').write_text(json.dumps(queue))
                with self.assertRaises(ValueError): validate_result(parent, submitted, {'exit_code': status}, artifacts)
            else:
                submission_path = parent / 'results/attempts' / state['completed'][0] / 'submission.json'
                forged = json.loads(submission_path.read_text()); forged['source_digest'] = '0' * 64
                submission_path.write_text(json.dumps(forged))
                with self.assertRaises(ValueError): validate_result(parent, submitted, {'exit_code': status}, artifacts)

    def run_suite(self, root, keep_going=False, fail=0, budget=20, plan_fail=False, plan_deadline=False, shard_deadline=False):
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
            code = (124 if (plan_deadline and task['action'] == 'plan') or
                    (shard_deadline and task.get('shard') == 1) else
                    70 if plan_fail and task['action'] == 'plan' else 1 if task.get('shard') == fail else 0)
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

    def test_deadline_in_planning_is_a_deadline_not_a_planning_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, submitted, terminal, artifacts, seen = self.run_suite(Path(temp), plan_deadline=True)
            self.assertEqual((terminal['exit_code'], len(seen)), (75, 1))
            self.assertEqual(json.loads((parent / 'suite-state.json').read_text())['stop_reason'], 'deadline')
            self.assertEqual(json.loads((parent / 'results/suite-error.json').read_text())['reason'], 'deadline')
            for forged in (1, 70):
                with self.assertRaisesRegex(ValueError, 'exit 75'):
                    validate_result(parent, submitted, {'exit_code': forged}, artifacts)

    def test_deadline_child_report_stops_the_suite_as_a_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, _, terminal, _, seen = self.run_suite(Path(temp), shard_deadline=True)
            self.assertEqual((terminal['exit_code'], len(seen)), (75, 2))
            self.assertEqual(json.loads((parent / 'results/suite-run.json').read_text())['stop_reason'], 'deadline')

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
