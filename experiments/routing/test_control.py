import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from control import main


class ControlTests(unittest.TestCase):
    def test_artifact_stats_returns_regular_declared_file_sizes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = 'a' * 32
            run = root / 'pandora-warm/runs' / attempt
            (run / 'results').mkdir(parents=True)
            (run / 'results/junit.xml').write_bytes(b'<testsuites/>')
            (run / 'artifacts.json').write_text(json.dumps({'results/junit.xml': 'digest'}))
            (run / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'cleanup_verified': True}))
            with patch.object(Path, 'home', return_value=root):
                captured = io.StringIO()
                with patch.object(sys, 'argv', ['control.py', attempt, 'artifact-stats']), contextlib.redirect_stdout(captured):
                    main()
            self.assertEqual(json.loads(captured.getvalue()), {
                'artifact_sizes': {'results/junit.xml': 13}, 'artifact_total_bytes': 13,
            })

    def test_artifact_stats_rejects_symlink_and_unsafe_declared_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = 'a' * 32
            run = root / 'pandora-warm/runs' / attempt
            run.mkdir(parents=True)
            (run / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'cleanup_verified': True}))
            (run / 'artifacts.json').write_text(json.dumps({'../outside': 'digest'}))
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(sys, 'argv', ['control.py', attempt, 'artifact-stats']):
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    main()
            (run / 'artifacts.json').write_text(json.dumps({'link': 'digest'}))
            (run / 'link').symlink_to(root)
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(sys, 'argv', ['control.py', attempt, 'artifact-stats']):
                with self.assertRaisesRegex(ValueError, 'not a regular'):
                    main()

    def test_artifact_stats_rejects_unverified_terminal_and_symlink_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = 'a' * 32
            run = root / 'pandora-warm/runs' / attempt
            run.mkdir(parents=True)
            (run / 'artifacts.json').write_text(json.dumps({'results/junit.xml': 'digest'}))
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(sys, 'argv', ['control.py', attempt, 'artifact-stats']):
                with self.assertRaisesRegex(ValueError, 'verified terminal'):
                    main()
            (run / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'cleanup_verified': True}))
            outside = root / 'outside'
            outside.mkdir()
            (outside / 'junit.xml').write_text('outside')
            (run / 'results').symlink_to(outside, target_is_directory=True)
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(sys, 'argv', ['control.py', attempt, 'artifact-stats']):
                with self.assertRaisesRegex(ValueError, 'contains a symlink'):
                    main()

    def test_terminal_logs_are_drained_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            attempt = 'a' * 32
            run = root / 'pandora-warm/runs' / attempt
            run.mkdir(parents=True)
            (run / 'stdout.log').write_text('x' * 70000)
            (run / 'stderr.log').write_text('error\n')
            (run / 'terminal.json').write_text(json.dumps({'cleanup_verified': True, 'exit_code': 1}))
            with patch.object(Path, 'home', return_value=root):
                first = io.StringIO()
                with patch.object(sys, 'argv', ['control.py', attempt, 'status', '0', '0']), contextlib.redirect_stdout(first):
                    main()
                data = json.loads(first.getvalue())
                self.assertTrue(data['more_logs'])
                self.assertEqual(len(data['stdout']), 65536)
                self.assertEqual(data['stderr'], 'error\n')
                second = io.StringIO()
                with patch.object(sys, 'argv', ['control.py', attempt, 'status', *map(str, data['offsets'])]), contextlib.redirect_stdout(second):
                    main()
                data = json.loads(second.getvalue())
                self.assertFalse(data['more_logs'])
                self.assertEqual(len(data['stdout']), 4464)
                self.assertEqual(data['stderr'], '')


if __name__ == '__main__':
    unittest.main()
