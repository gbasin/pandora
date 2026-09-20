import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import route
import transport
from transport import validate_operator_result
import operator_recovery as writer


def submission(attempt):
    return {'attempt': attempt, 'source_digest': 'd' * 64, 'workflow': 'surface'}


def receipt(output, attempt, **changes):
    raw = (output / 'submission.json').read_bytes()
    value = {'attempt': attempt, 'state': 'infrastructure-failed', 'reason': 'worker-lost',
             'cleanup_verified': True, 'acknowledged_at': 1.0, 'submission_sha256': hashlib.sha256(raw).hexdigest(),
             'source_digest': 'd' * 64, 'workflow': 'surface'}
    value.update(changes)
    (output / 'operator-result.json').write_text(json.dumps(value))
    return value


class OperatorRecoveryTests(unittest.TestCase):
    def test_writer_receipt_drives_control_and_route_for_invalid_terminal_evidence(self):
        """An actual operator receipt closes the same active request without a test result."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); attempt = 'a' * 32
            remote = root / 'pandora-warm'; output = remote / 'runs' / attempt
            output.mkdir(parents=True)
            (output / 'attempt.lock').touch()
            (output / 'submission.json').write_text(json.dumps(submission(attempt)))
            (output / 'admission-cleanup.json').write_text(json.dumps({'attempt': attempt, 'cleanup_verified': True}))
            # This has a cleanup claim but is not evidence: it has no manifest.
            (output / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'cleanup_verified': True, 'exit_code': 0}))
            acknowledged = writer.acknowledge_missing_result(
                remote, attempt, 'worker-lost', now=lambda: 1.0, list_resources=lambda args: [])
            self.assertEqual(validate_operator_result(output, attempt), acknowledged)
            with patch('transport.query', return_value={'state': 'infrastructure-failed',
                                                        'operator_result': acknowledged, 'registered': False}), \
                    patch('transport.retrieve_operator_result', return_value=acknowledged) as retrieve:
                self.assertEqual(transport.follow('unused', output), 70)
            retrieve.assert_called_once_with('unused', output, attempt)
            import contextlib
            import io
            from control import main as control_main
            with patch.object(Path, 'home', return_value=root), \
                    patch('sys.argv', ['control.py', attempt, 'status']), contextlib.redirect_stdout(io.StringIO()) as captured:
                control_main()
            self.assertEqual(json.loads(captured.getvalue())['state'], 'infrastructure-failed')

            repo = root / 'repo'; repo.mkdir(); state_root = root / 'state'
            state = state_root / hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
            state.mkdir(parents=True)
            record = {'state': 'active', 'output': str(output), 'command': ['test:surface', 'borrower-web'],
                      'host': 'unused', 'attempt': attempt}
            route.write(state / 'active.json', record)
            child = type('Child', (), {'wait': lambda self: 70})()
            env = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            with patch.dict(os.environ, env), patch('sys.argv', ['route.py', *record['command']]), \
                 patch('route.subprocess.check_output', return_value=str(repo)), \
                 patch('route.Path.cwd', return_value=repo), patch('route.subprocess.Popen', return_value=child):
                self.assertEqual(route.main(), 70)
            self.assertEqual(json.loads((state / 'active.json').read_text())['state'], 'infrastructurefailure')

    def test_verified_acknowledgement_completes_as_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve(); state_root = repo / 'state'; attempt = 'a' * 32
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest(); output = state / attempt
            output.mkdir(parents=True)
            (output / 'submission.json').write_text(json.dumps(submission(attempt)))
            acknowledged = receipt(output, attempt)
            record = {'state': 'active', 'output': str(output), 'command': ['test:surface', 'borrower-web'],
                      'host': 'unused', 'attempt': attempt}
            route.write(state / 'active.json', record)
            env = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            child = type('Child', (), {'wait': lambda self: 70})()
            with patch.dict(os.environ, env), patch('sys.argv', ['route.py', *record['command']]), \
                 patch('route.subprocess.check_output', return_value=str(repo)), \
                 patch('route.Path.cwd', return_value=repo), patch('route.subprocess.Popen', return_value=child):
                self.assertEqual(route.main(), 70)
            active = json.loads((state / 'active.json').read_text())
            self.assertEqual(active['state'], 'infrastructurefailure')
            self.assertEqual(active['operator_result'], acknowledged)
            self.assertEqual(json.loads((output / 'completed.json').read_text())['outcome'], 'infrastructure-failed')

    def test_mismatched_acknowledgement_does_not_clear_active_request(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp); attempt = 'a' * 32
            (output / 'submission.json').write_text(json.dumps(submission(attempt)))
            receipt(output, attempt, submission_sha256='0' * 64)
            with self.assertRaisesRegex(ValueError, 'submission digest mismatch'):
                validate_operator_result(output, attempt)

    def test_acknowledgement_rejects_unknown_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp); attempt = 'a' * 32
            (output / 'submission.json').write_text(json.dumps(submission(attempt)))
            receipt(output, attempt, cleanup_verified=False)
            with self.assertRaisesRegex(ValueError, 'identity or cleanup'):
                validate_operator_result(output, attempt)

    def test_retained_terminal_must_match_acknowledgement_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp); attempt = 'a' * 32
            (output / 'submission.json').write_text(json.dumps(submission(attempt)))
            (output / 'terminal.json').write_text('{"broken":true}')
            receipt(output, attempt, terminal_sha256='0' * 64)
            with self.assertRaisesRegex(ValueError, 'terminal digest mismatch'):
                validate_operator_result(output, attempt)


if __name__ == '__main__':
    unittest.main()
