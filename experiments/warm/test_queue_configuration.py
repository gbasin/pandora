from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import warm


class QueueTimeoutArgumentTests(unittest.TestCase):
    def test_warm_timeout_argument_requires_a_bounded_positive_integer(self):
        self.assertEqual(warm.queue_timeout_seconds(900), 900)
        self.assertEqual(warm.queue_timeout_seconds(86400), 86400)
        for value in (0, -1, 86401, True, False, 1.5, '900'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Queue timeout'):
                warm.queue_timeout_seconds(value)

    def test_docker_spec_timeout_overrides_warm_cli_timeout(self):
        spec = {'config': {'queue_timeout_seconds': 120}}
        self.assertEqual(warm.effective_queue_timeout(300, spec), 120)
        self.assertEqual(warm.effective_queue_timeout(300, None), 300)

    def test_warm_cli_accepts_integer_timeout_before_capture(self):
        class Captured(Exception):
            pass
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'output'
            argv = ['warm.py', '--host', 'unused', '--repo', temp, '--output', str(output),
                    '--attempt', 'a' * 32, '--queue-timeout-seconds', '300']
            with patch.object(sys, 'argv', argv), patch.object(warm, 'repository_key', return_value='k'), \
                 patch.object(warm, 'freeze', side_effect=Captured):
                with self.assertRaises(Captured):
                    warm.main()


if __name__ == '__main__':
    unittest.main()


class JourneyUpdateArgumentTests(unittest.TestCase):
    def test_private_update_flag_records_explicit_selector(self):
        class Captured(Exception):
            pass
        captured = []
        def capture(path, metadata):
            captured.append(metadata)
            raise Captured
        with tempfile.TemporaryDirectory() as temp:
            argv = ['warm.py', '--host', 'unused', '--repo', temp,
                    '--output', str(Path(temp) / 'output'), '--workflow', 'journey',
                    '--journey-update', 'S0-01']
            with patch.object(sys, 'argv', argv), patch.object(warm, 'repository_key', return_value='k'), \
                 patch.object(warm, 'freeze', return_value=([], [])), \
                 patch.object(warm, 'write_metadata', side_effect=capture):
                with self.assertRaises(Captured):
                    warm.main()
            self.assertEqual(captured[0]['selectors'], ['S0-01', '--update'])

class SelectorTransportTests(unittest.TestCase):
    def test_json_preserves_flags_patterns_and_app_in_submission(self):
        import json
        class Captured(Exception): pass
        for workflow, app, selectors in [('surface', 'desk', ['smoke.spec.ts', '--grep', 'one | two']),
                                          ('journey', 'borrower-web', ['S2-03', '--fault', 'dropped', '--update'])]:
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as temp:
                captured = []
                def capture(path, metadata):
                    captured.append(metadata)
                    raise Captured
                argv = ['warm.py', '--host', 'unused', '--repo', temp,
                        '--output', str(Path(temp) / 'output'), '--workflow', workflow,
                        '--surface-app', app, '--selectors-json=' + json.dumps(selectors)]
                with patch.object(sys, 'argv', argv), patch.object(warm, 'repository_key', return_value='k'), \
                     patch.object(warm, 'freeze', return_value=([], [])), \
                     patch.object(warm, 'write_metadata', side_effect=capture):
                    with self.assertRaises(Captured): warm.main()
                self.assertEqual(captured[0]['selectors'], selectors)
                if workflow == 'surface': self.assertEqual(captured[0]['surface_app'], app)
