import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from transport import validate_evidence


class EvidenceTests(unittest.TestCase):
    def prepare(self, root, status=1):
        (root / 'stdout.log').write_text('one failure\n')
        (root / 'artifacts.json').write_text(json.dumps({'stdout.log': hashlib.sha256(b'one failure\n').hexdigest()}))
        (root / 'terminal.json').write_text(json.dumps({'attempt': 'a' * 32, 'exit_code': status, 'cleanup_verified': True}))

    def test_failed_result_is_valid_but_tampering_is_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root)
            self.assertEqual(validate_evidence(root, 'a' * 32)['exit_code'], 1)
            (root / 'stdout.log').write_text('all passed\n')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                validate_evidence(root, 'a' * 32)

    def test_success_requires_test_evidence_and_matching_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root, 0)
            with self.assertRaisesRegex(ValueError, 'lacks test evidence'):
                validate_evidence(root, 'a' * 32)
            with self.assertRaisesRegex(ValueError, 'identity'):
                validate_evidence(root, 'b' * 32)

    def test_journey_report_must_match_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root, 0)
            terminal = json.loads((root / 'terminal.json').read_text())
            terminal['workflow'] = 'journey'
            (root / 'terminal.json').write_text(json.dumps(terminal))
            (root / 'results').mkdir()
            (root / 'results/exit-code').write_text('0')
            for state in ('fail', 'pass'):
                (root / 'results/journey.json').write_text(json.dumps({'journey': 'S0-01', 'status': state}))
                files = ['results/exit-code', 'results/journey.json']
                (root / 'artifacts.json').write_text(json.dumps({n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in files}))
                if state == 'fail':
                    with self.assertRaisesRegex(ValueError, 'Journey evidence'):
                        validate_evidence(root, 'a' * 32)
                else:
                    self.assertEqual(validate_evidence(root, 'a' * 32)['exit_code'], 0)

    def test_artifact_cannot_escape_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root)
            (root / 'artifacts.json').write_text(json.dumps({'../other': 'x'}))
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                validate_evidence(root, 'a' * 32)


class StreamTests(unittest.TestCase):
    def test_follow_preserves_stderr_and_reports_original_exit(self):
        import contextlib
        import io
        from unittest.mock import patch
        from transport import follow
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'submission.json').write_text(json.dumps({'attempt': 'a' * 32}))
            stdout, stderr = io.StringIO(), io.StringIO()
            state = {'stdout': 'test output\n', 'stderr': 'test error\n', 'offsets': [12, 11], 'cleanup_verified': True}
            with patch('transport.query', return_value=state), patch('transport.retrieve', return_value={'exit_code': 1}), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(follow('unused', root), 1)
            self.assertIn('test output', stdout.getvalue())
            self.assertNotIn('test error', stdout.getvalue())
            self.assertEqual(stderr.getvalue(), 'test error\n')

    def test_follow_uses_submitted_queue_timeout_after_recovery(self):
        import contextlib
        import io
        from unittest.mock import patch
        from transport import follow
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'submission.json').write_text(json.dumps({'attempt': 'a' * 32, 'queue_timeout_seconds': 1000}))
            state = {'offsets': [0, 0], 'cleanup_verified': True}
            # 2,446 seconds exceeded the former fixed deadline, but remains
            # within the original accepted request's 1,000 + 1,545 seconds.
            with patch('transport.time.monotonic', side_effect=[0, 0, 2446]), \
                 patch('transport.query', return_value=state), \
                 patch('transport.retrieve', return_value={'exit_code': 0}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(follow('unused', root), 0)

    def test_follow_rejects_invalid_submitted_queue_timeout(self):
        import contextlib
        import io
        from transport import follow
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'submission.json').write_text(json.dumps({'attempt': 'a' * 32, 'queue_timeout_seconds': True}))
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'queue timeout'):
                follow('unused', root)


if __name__ == '__main__':
    unittest.main()
