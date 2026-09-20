import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from artifact_limits import ArtifactDeliveryLimitExceeded
from transport import retrieve, validate_evidence


class EvidenceTests(unittest.TestCase):
    def prepare(self, root, status=1):
        (root / 'stdout.log').write_text('one failure\n')
        (root / 'artifacts.json').write_text(json.dumps({'stdout.log': hashlib.sha256(b'one failure\n').hexdigest()}))
        (root / 'terminal.json').write_text(json.dumps({'attempt': 'a' * 32, 'exit_code': status, 'cleanup_verified': True}))

    def prepare_success(self, root, workflow, report, submitted):
        self.prepare(root, 0)
        terminal = json.loads((root / 'terminal.json').read_text()) | {'workflow': workflow}
        (root / 'terminal.json').write_text(json.dumps(terminal))
        results = root / 'results'
        results.mkdir(exist_ok=True)
        (results / 'exit-code').write_text('0')
        report_path = results / ('journey.json' if workflow == 'journey' else 'surface.json')
        report_path.write_text(json.dumps(report))
        files = ['results/exit-code', str(report_path.relative_to(root))]
        if workflow == 'surface':
            (results / 'junit.xml').write_text('<testsuites/>')
            files.append('results/junit.xml')
        (root / 'artifacts.json').write_text(json.dumps({
            name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files
        }))
        return submitted

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

    def test_journey_report_must_match_submitted_id_and_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submitted = self.prepare_success(
                root, 'journey',
                {'journey': 'S0-02', 'status': 'pass', 'update': True, 'fault': 'dropped'},
                {'workflow': 'journey', 'selectors': ['S0-02', '--fault', 'dropped', '--update']},
            )
            self.assertEqual(validate_evidence(root, 'a' * 32, submitted)['exit_code'], 0)
            for report in (
                {'journey': 'S0-02', 'status': 'fail', 'update': True, 'fault': 'dropped'},
                {'journey': 'S0-01', 'status': 'pass', 'update': True, 'fault': 'dropped'},
                {'journey': 'S0-02', 'status': 'pass', 'update': False, 'fault': 'dropped'},
                {'journey': 'S0-02', 'status': 'pass', 'update': True, 'fault': None},
            ):
                self.prepare_success(root, 'journey', report, submitted)
                with self.assertRaisesRegex(ValueError, 'Journey evidence'):
                    validate_evidence(root, 'a' * 32, submitted)

    def test_surface_report_must_match_submitted_app_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            submitted = self.prepare_success(
                root, 'surface', {'app': 'desk'}, {'workflow': 'surface', 'surface_app': 'desk'},
            )
            self.assertEqual(validate_evidence(root, 'a' * 32, submitted)['exit_code'], 0)
            self.prepare_success(root, 'surface', {'app': 'borrower-web'}, submitted)
            with self.assertRaisesRegex(ValueError, 'Surface evidence'):
                validate_evidence(root, 'a' * 32, submitted)

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


class ArtifactRetrievalTests(unittest.TestCase):
    attempt = 'a' * 32

    def test_overlimit_keeps_terminal_unpromoted_and_retrying_same_attempt_with_higher_limit_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            output.mkdir()
            (output / 'submission.json').write_text('{}')
            payload = b'oversized\n'
            manifest = {'stdout.log': hashlib.sha256(payload).hexdigest()}
            terminal = {'attempt': self.attempt, 'exit_code': 1, 'cleanup_verified': True}
            calls = []
            queries = []

            def rsync(command, **kwargs):
                calls.append(command)
                stage = Path(command[-1])
                if len(calls) in (1, 2):
                    (stage / 'artifacts.json').write_text(json.dumps(manifest))
                    (stage / 'terminal.json').write_text(json.dumps(terminal))
                else:
                    (stage / 'stdout.log').write_bytes(payload)

            state = {'artifact_sizes': {'stdout.log': len(payload)},
                     'artifact_total_bytes': len(payload)}
            def query(host, attempt, action='status', offsets=None):
                queries.append((attempt, action))
                return state
            with patch('transport.subprocess.run', side_effect=rsync), \
                    patch('transport.query', side_effect=query):
                with self.assertRaises(ArtifactDeliveryLimitExceeded):
                    retrieve('unused', output, self.attempt, len(payload) - 1)
                self.assertFalse((output / 'terminal.json').exists())
                self.assertEqual(len(calls), 1, 'over-limit artifacts must not start bulk rsync')
                retrieved = retrieve('unused', output, self.attempt, len(payload))
            self.assertEqual(retrieved['attempt'], self.attempt)
            self.assertEqual(json.loads((output / 'terminal.json').read_text())['attempt'], self.attempt)
            self.assertEqual(len(calls), 3, 'retry must fetch the same attempt before one bulk rsync')
            self.assertEqual(queries, [(self.attempt, 'artifact-stats'), (self.attempt, 'artifact-stats')])


if __name__ == '__main__':
    unittest.main()
