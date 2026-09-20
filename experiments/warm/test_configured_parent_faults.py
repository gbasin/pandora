"""Fault boundaries for the configured, multi-slot suite parent."""
import contextlib
import fcntl
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from resource_admission import Scheduler
from snapshot import digest
from suite_parent import execute_configured
from test_suite_parent_evidence import plan, report
from worker_bundle import NAMES


CONFIG = {
    'version': 1,
    'scheduler': {'version': 1, 'cpu_millis': 4000, 'memory_mib': 16000,
                  'disk_mib': 32000, 'disk_floor_mib': 100, 'max_running': 2,
                  'policy': 'fair'},
    'max_parallel': 2, 'execution_seconds': 1500, 'workspace_mib': 1000,
    'limits': {'main': {'cpu_millis': 1000, 'memory_mib': 1024},
               'db': {'cpu_millis': 500, 'memory_mib': 512},
               'pool': {'cpu_millis': 100, 'memory_mib': 128},
               'proxy': {'cpu_millis': 100, 'memory_mib': 128}},
}


def child_receipt(child, metadata, result, action, code):
    (child / 'results').mkdir(exist_ok=True)
    if result is not None:
        (child / ('results/suite-' + action + '.json')).write_text(json.dumps(result))
    (child / 'results/exit-code').write_text(str(code))
    (child / 'queue.json').write_text(json.dumps({'waited': 0, 'acquired': True}))
    (child / 'terminal.json').write_text(json.dumps({
        'attempt': child.name, 'workflow': 'suite', 'exit_code': code,
        'cleanup_verified': True,
    }))
    files = [*child.glob('results/*'), child / 'queue.json']
    (child / 'artifacts.json').write_text(json.dumps({
        str(path.relative_to(child)): digest(path) for path in files
    }))


class ConfiguredParentFaults(unittest.TestCase):
    def parent(self, root, *, budget=20, keep_going=False):
        parent = root / 'runs' / ('f' * 32)
        parent.mkdir(parents=True)
        (parent / 'source').mkdir()
        (parent / 'source/input').write_text('frozen bytes')
        (parent / 'manifest.json').write_text('[]')
        (parent / 'runtime.Dockerfile').write_text('FROM unused')
        for name in NAMES:
            (parent / name).write_bytes((Path(__file__).parent / name).read_bytes())
        frozen = plan()
        submitted = {
            'attempt': parent.name, 'workflow': 'suite-run',
            'source_digest': frozen['source_digest'], 'queue_timeout_seconds': budget,
            'worker_config': CONFIG,
            'suite': {'action': 'run', 'shard_count': 3,
                      'selection': frozen['selection'], 'keep_going': keep_going},
        }
        return parent, submitted, frozen

    @contextlib.contextmanager
    def scheduler(self, root, clock=lambda: 0):
        with patch('worker_runtime.scheduler', side_effect=lambda path, value:
                   Scheduler(path, value['scheduler'], boot_id='configured-parent-tests', clock=clock)):
            yield

    @staticmethod
    def task(child):
        return json.loads((child / 'submission.json').read_text())['suite']

    def test_ledger_queue_timeout_does_not_call_invalid_stop_reason(self):
        """A child observes the ledger's queue-timeout, which Scheduler.stop cannot accept."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); parent, submitted, frozen = self.parent(root, budget=1)
            now, locks, queued = [0], [], threading.Barrier(2)
            def clock(): return now[0]
            def work(owner, child, started, queue, invocation, execution, cancelled=None):
                task = self.task(child)
                if task['action'] == 'plan':
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), frozen, 'plan', 0)
                    return None
                # A real waiting request makes the real scheduler set the only
                # queue-timeout state it owns.  The parent must only observe it.
                lock = (child / 'attempt.lock').open('a'); fcntl.flock(lock, fcntl.LOCK_EX); locks.append(lock)
                queue.enqueue(child.name, invocation, {'cpu_millis': 1000, 'memory_mib': 1024, 'disk_mib': 1000, 'exclusive': []})
                queued.wait(1)
                now[0] = 2
                child_receipt(child, json.loads((child / 'submission.json').read_text()), None, 'shard', 75)
                return None
            try:
                with self.scheduler(root, clock), patch('suite_parent._configured_child', side_effect=work), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(execute_configured(parent, submitted), 75)
            finally:
                for lock in locks: lock.close()
            state = json.loads((parent / 'suite-state.json').read_text())
            self.assertEqual(state['stop_reason'], 'queue-timeout')
            ledger = Scheduler(root, CONFIG['scheduler'], boot_id='configured-parent-tests', clock=clock).snapshot()
            self.assertEqual(ledger['invocations'][0]['stopped'], 'queue-timeout')

    def test_failfast_stops_dispatch_but_reaps_running_sibling(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); parent, submitted, frozen = self.parent(root)
            release = threading.Event()
            def work(owner, child, started, queue, invocation, execution, cancelled=None):
                task = self.task(child)
                if task['action'] == 'plan':
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), frozen, 'plan', 0)
                elif task['shard'] == 1:
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), report(frozen, 1, code=1, status='fail'), 'shard', 1)
                else:
                    release.wait(2)
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), report(frozen, 2), 'shard', 0)
                return None
            outcome = []
            def invoke():
                with self.scheduler(root), patch('suite_parent._configured_child', side_effect=work), contextlib.redirect_stdout(io.StringIO()):
                    outcome.append(execute_configured(parent, submitted))
            runner = threading.Thread(target=invoke); runner.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state_path = parent / 'suite-state.json'
                if state_path.exists() and json.loads(state_path.read_text())['stop_reason'] == 'test-failure': break
                time.sleep(.01)
            self.assertEqual(json.loads((parent / 'suite-state.json').read_text())['stop_reason'], 'test-failure')
            self.assertFalse((parent.parent / json.loads((parent / 'children.json').read_text())['children'][3]).exists())
            release.set(); runner.join(2)
            self.assertFalse(runner.is_alive())
            self.assertEqual(outcome, [1])
            self.assertEqual(len(json.loads((parent / 'suite-state.json').read_text())['completed']), 3)

    def test_keyboard_interrupt_sets_cancellation_and_reaps_before_propagating(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); parent, submitted, frozen = self.parent(root)
            entered, reaped, events = threading.Event(), [], []
            def work(owner, child, started, queue, invocation, execution, cancelled=None):
                task = self.task(child)
                if task['action'] == 'plan':
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), frozen, 'plan', 0)
                    return None
                events.append(cancelled); entered.set()
                while not cancelled.is_set(): time.sleep(.005)
                reaped.append(task['shard'])
                return None
            def interrupt(*args, **kwargs):
                entered.wait(1)
                raise KeyboardInterrupt
            with self.scheduler(root), patch('suite_parent._configured_child', side_effect=work), patch('suite_parent.wait', side_effect=interrupt), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt): execute_configured(parent, submitted)
            self.assertTrue(events and all(event.is_set() for event in events))
            self.assertEqual(sorted(reaped), [1, 2])
            self.assertEqual(json.loads((parent / 'suite-state.json').read_text())['stop_reason'], 'cancelled')

    def test_retain_failure_cancels_and_reaps_children_then_preserves_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); parent, submitted, frozen = self.parent(root)
            reaped = []
            def work(owner, child, started, queue, invocation, execution, cancelled=None):
                task = self.task(child)
                if task['action'] == 'plan':
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), frozen, 'plan', 0)
                    return None
                if task['shard'] == 1:
                    child_receipt(child, json.loads((child / 'submission.json').read_text()), report(frozen, 1), 'shard', 0)
                    return None
                while not cancelled.is_set(): time.sleep(.005)
                reaped.append(task['shard'])
                return None
            from suite_parent import retain as real_retain
            def retain_failure(owner, child):
                if self.task(child).get('shard') == 1: raise RuntimeError('retain broke')
                return real_retain(owner, child)
            with self.scheduler(root), patch('suite_parent._configured_child', side_effect=work), patch('suite_parent.retain', side_effect=retain_failure), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'retain broke'): execute_configured(parent, submitted)
            self.assertEqual(reaped, [2])
            self.assertEqual(json.loads((parent / 'suite-state.json').read_text())['stop_reason'], 'infrastructure')

    def test_infrastructure_with_later_running_success_aggregates_infrastructure(self):
        self._mixed_outcome(first_code=70, expected=75)

    def test_infrastructure_overrides_earlier_test_failure(self):
        self._mixed_outcome(first_code=1, second_code=70, expected=75)

    def _mixed_outcome(self, *, first_code, expected, second_code=0):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); parent, submitted, frozen = self.parent(root)
            release = threading.Event()
            def work(owner, child, started, queue, invocation, execution, cancelled=None):
                task = self.task(child)
                metadata = json.loads((child / 'submission.json').read_text())
                if task['action'] == 'plan': child_receipt(child, metadata, frozen, 'plan', 0)
                elif task['shard'] == 1:
                    child_receipt(child, metadata, report(frozen, 1, code=first_code, status='fail', infra=int(first_code != 1)), 'shard', first_code)
                    release.set()
                else:
                    release.wait(1)
                    child_receipt(child, metadata, report(frozen, 2, code=second_code,
                                  status='fail' if second_code else 'pass', infra=int(second_code not in (0, 1))),
                                  'shard', second_code)
                return None
            with self.scheduler(root), patch('suite_parent._configured_child', side_effect=work), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(execute_configured(parent, submitted), expected)
            result = json.loads((parent / 'results/suite-run.json').read_text())
            self.assertEqual((result['stop_reason'], result['completed_shards'], result['unrun_shards']), ('infrastructure', [1, 2], [3]))


if __name__ == '__main__':
    unittest.main()
