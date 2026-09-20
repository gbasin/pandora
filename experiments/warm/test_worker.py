from pathlib import Path
import tempfile
import json
import unittest

from worker import interruption_status, mark_deadline_report
from suite_evidence import validate_shard
from test_suite_parent_evidence import plan, report


class WorkerTests(unittest.TestCase):
    def test_deadline_marker_distinguishes_deadline_from_manual_cancellation(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp)
            self.assertEqual(interruption_status(attempt), 130)
            (attempt / 'deadline.request').touch()
            self.assertEqual(interruption_status(attempt), 124)

    def test_deadline_aligns_an_existing_partial_shard_report_before_hashing(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp)
            results = attempt / 'results'
            results.mkdir()
            frozen = plan()
            partial = report(frozen, 1, code=0, status='pass')
            path = results / 'suite-shard.json'
            path.write_text(json.dumps(partial))
            self.assertTrue(mark_deadline_report(attempt))
            actual = json.loads(path.read_text())
            self.assertEqual(actual['exit_code'], 124)
            self.assertEqual(actual['errors']['infrastructureFailures'], 1)
            self.assertTrue(actual['detail'].endswith('Attempt deadline reached'))
            self.assertEqual(set(actual), set(partial))
            validate_shard(frozen, actual)

    def test_deadline_does_not_invent_a_missing_shard_report(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(mark_deadline_report(Path(temp)))


if __name__ == '__main__':
    unittest.main()
