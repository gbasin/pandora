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

    def test_artifact_cannot_escape_staging(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.prepare(root)
            (root / 'artifacts.json').write_text(json.dumps({'../other': 'x'}))
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                validate_evidence(root, 'a' * 32)


if __name__ == '__main__':
    unittest.main()
