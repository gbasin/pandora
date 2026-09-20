import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from worker_runtime import Lease


class WorkerRuntimeTests(unittest.TestCase):
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
