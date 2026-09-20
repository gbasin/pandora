import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from service_cleanup import cleanup


class CleanupTests(unittest.TestCase):
    def test_partial_creation_is_removed_by_reserved_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('a' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, '', '')
            with patch('service_cleanup.subprocess.run', side_effect=run):
                self.assertTrue(cleanup(attempt))
            removed = [a[-1] for a in calls if a[2:3] == ['rm']]
            self.assertEqual(len(removed), 4)
            self.assertFalse((attempt / 'service-cleanup.pending').exists())
            self.assertTrue(json.loads((attempt / 'service-cleanup.json').read_text())['verified'])

    def test_unknown_docker_state_keeps_cleanup_pending(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp) / ('b' * 32)
            attempt.mkdir()
            (attempt / 'service-cleanup.pending').touch()
            with patch('service_cleanup.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'daemon unavailable')):
                self.assertFalse(cleanup(attempt))
            self.assertTrue((attempt / 'service-cleanup.pending').exists())
