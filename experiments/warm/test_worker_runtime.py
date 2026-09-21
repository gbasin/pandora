import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from worker_runtime import Lease, acquire
from resource_admission import InvocationStopped
from admission import QueueUnavailable


class WorkerRuntimeTests(unittest.TestCase):
    def test_fail_fast_before_enqueue_is_a_stopped_request_not_infrastructure(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / ('a' * 32); attempt.mkdir()
            config = json.loads(Path(__file__).with_name('worker-config.example.json').read_text())
            submitted = {'worker_config': config, 'parent_attempt': 'b' * 32}
            queue = Mock()
            queue.enqueue.side_effect = InvocationStopped('test-failure')
            queue.snapshot.return_value = {'invocations': [{'identity': 'b' * 32, 'waited': 1.0}]}
            with patch('worker_runtime.register', return_value=queue):
                with self.assertRaisesRegex(QueueUnavailable, 'test-failure'):
                    acquire(attempt, submitted, {'cpu_millis': 1000, 'memory_mib': 4096})
            queue.claim.assert_not_called()
            evidence = json.loads((attempt / 'queue.json').read_text())
            self.assertEqual(evidence['stopped'], 'test-failure')
            self.assertFalse(evidence['acquired'])

    def test_lease_close_settles_only_after_a_verified_matching_terminal_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / ('a' * 32)
            attempt.mkdir()
            queue, inner = Mock(), Mock()
            lease = Lease(attempt, queue, 'invocation', inner, {}, 0)

            (attempt / 'terminal.json').write_text(json.dumps({
                'attempt': 'b' * 32, 'cleanup_verified': True,
            }))
            lease.close()
            queue.settle.assert_not_called()
            inner.close.assert_called_once_with()

            inner.reset_mock()
            (attempt / 'terminal.json').write_text(json.dumps({
                'attempt': attempt.name, 'cleanup_verified': False,
            }))
            lease.close()
            queue.settle.assert_not_called()
            inner.close.assert_called_once_with()

            (attempt / 'terminal.json').write_text(json.dumps({
                'attempt': attempt.name, 'cleanup_verified': True,
            }))
            lease.close()
            queue.settle.assert_called_once_with(attempt.name)


if __name__ == '__main__':
    unittest.main()
