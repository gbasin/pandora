import contextlib
import fcntl
import io
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from control import main


def hold_attempt_lock(path, ready, release):
    handle = Path(path).open('a')
    fcntl.flock(handle, fcntl.LOCK_EX)
    ready.set()
    release.wait(10)
    handle.close()


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

    def test_status_reports_acknowledged_infrastructure_outcome_distinctly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); attempt = 'a' * 32
            run = root / 'pandora-warm/runs' / attempt; run.mkdir(parents=True)
            (run / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'cleanup_verified': False}))
            receipt = {'attempt': attempt, 'state': 'infrastructure-failed', 'cleanup_verified': True}
            (run / 'operator-result.json').write_text(json.dumps(receipt))
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(sys, 'argv', ['control.py', attempt, 'status']):
                captured = io.StringIO()
                with contextlib.redirect_stdout(captured):
                    main()
            result = json.loads(captured.getvalue())
            self.assertEqual(result['state'], 'infrastructure-failed')
            self.assertEqual(result['operator_result'], receipt)
            self.assertNotIn('exit_code', result)

    def test_abandon_unregistered_never_marks_a_worker_owned_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); attempt = 'a' * 32
            run = root / 'pandora-warm/runs' / attempt; run.mkdir(parents=True)
            ready, release = multiprocessing.Event(), multiprocessing.Event()
            owner = multiprocessing.Process(target=hold_attempt_lock, args=(run / 'attempt.lock', ready, release))
            owner.start(); self.addCleanup(lambda: (release.set(), owner.join(10)))
            self.assertTrue(ready.wait(5))
            try:
                with patch.object(Path, 'home', return_value=root), \
                        patch.object(sys, 'argv', ['control.py', attempt, 'abandon-unregistered']), \
                        contextlib.redirect_stdout(io.StringIO()) as captured:
                    main()
                self.assertEqual(json.loads(captured.getvalue())['state'], 'worker-owned')
                self.assertFalse((run / 'cancel.request').exists())
            finally:
                release.set(); owner.join(10)
            with patch.object(Path, 'home', return_value=root), \
                    patch.object(sys, 'argv', ['control.py', attempt, 'abandon-unregistered']), \
                    contextlib.redirect_stdout(io.StringIO()) as captured:
                main()
            self.assertEqual(json.loads(captured.getvalue())['state'], 'abandoned-unregistered')
            self.assertTrue((run / 'cancel.request').exists())


if __name__ == '__main__':
    unittest.main()
