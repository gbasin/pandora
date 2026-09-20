import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import builder_owner as owners

BUILDER = 'pandora-surface-deps-v3'
MARKER = 'dependency-cleanup.pending'


class BuilderOwners(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.a = self.root / 'runs' / ('a' * 32)
        self.b = self.root / 'runs' / ('b' * 32)
        for attempt in (self.a, self.b):
            attempt.mkdir(parents=True)
            handle = (attempt / 'attempt.lock').open('a')
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.addCleanup(handle.close)
        self.runner = patch('builder_owner.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.run = self.runner.start()
        self.addCleanup(self.runner.stop)

    def test_live_owner_excludes_build_and_cleanup_but_reuses_after_verified_stop(self):
        lease = owners.acquire(self.a, BUILDER, MARKER)
        try:
            with self.assertRaisesRegex(RuntimeError, 'owned by another'):
                owners.acquire(self.b, BUILDER, MARKER)
            self.assertFalse(owners.cleanup(self.a, BUILDER, MARKER))
            self.assertTrue(owners.cleanup(self.b, BUILDER, MARKER))
            self.assertFalse(any(c.args[0][2:4] == ['buildx', 'stop'] for c in self.run.call_args_list))
        finally:
            self.assertTrue(lease.close())
        self.assertFalse((self.a / MARKER).exists())
        successor = owners.acquire(self.b, BUILDER, MARKER)
        try:
            self.run.reset_mock()
            self.assertTrue(owners.cleanup(self.a, BUILDER, MARKER))
            self.run.assert_not_called()
        finally:
            successor.close()

    def test_dead_owner_record_cannot_be_stolen_and_cleanup_does_not_invent_terminal(self):
        lease = owners.acquire(self.a, BUILDER, MARKER)
        lease.handle.close()  # abrupt owner exit leaves the durable record
        with self.assertRaisesRegex(RuntimeError, 'unresolved'):
            owners.acquire(self.b, BUILDER, MARKER)
        self.assertTrue(owners.cleanup(self.a, BUILDER, MARKER))
        self.assertFalse((self.a / 'terminal.json').exists())
        successor = owners.acquire(self.b, BUILDER, MARKER)
        self.assertTrue(successor.close())

    def test_unknown_stop_retains_owner_and_pending(self):
        lease = owners.acquire(self.a, BUILDER, MARKER)
        self.run.return_value = subprocess.CompletedProcess([], 1, '', 'daemon unavailable')
        self.assertFalse(lease.close())
        self.assertTrue((self.a / MARKER).exists())
        self.assertEqual(owners.owner(owners.paths(self.a, BUILDER)[1]), self.a.name)

    def test_late_old_marker_never_stops_successor(self):
        lease = owners.acquire(self.b, BUILDER, MARKER)
        (self.a / MARKER).touch()
        self.run.reset_mock()
        self.assertFalse(owners.cleanup(self.a, BUILDER, MARKER))
        self.run.assert_not_called()
        lease.close()

    def test_crash_between_owner_record_and_marker_is_recoverable(self):
        record = owners.paths(self.a, BUILDER)[1]
        record.write_text(json.dumps({'attempt': self.a.name}))
        self.assertTrue(owners.cleanup(self.a, BUILDER, MARKER))
        self.assertFalse(record.exists())

    def test_unowned_pending_marker_requires_verified_stopped_builder(self):
        (self.a / MARKER).touch()
        self.run.return_value = subprocess.CompletedProcess([], 1, '', 'daemon unavailable')
        self.assertFalse(owners.cleanup(self.a, BUILDER, MARKER))
        self.assertTrue((self.a / MARKER).exists())
        self.run.return_value = subprocess.CompletedProcess([], 0, '', '')
        self.assertTrue(owners.cleanup(self.a, BUILDER, MARKER))
        self.assertFalse(any(c.args[0][2:4] == ['buildx', 'stop'] for c in self.run.call_args_list))

    def test_unowned_running_builder_cannot_be_claimed(self):
        self.run.return_value = subprocess.CompletedProcess([], 0, 'buildx_buildkit_live', '')
        with self.assertRaisesRegex(RuntimeError, 'unowned activity'):
            owners.acquire(self.a, BUILDER, MARKER)
        self.assertFalse((self.a / MARKER).exists())

    def test_interruption_after_owner_removal_keeps_recoverable_pending_barrier(self):
        lease = owners.acquire(self.a, BUILDER, MARKER)
        original = Path.unlink
        def fail_marker(path, *args, **kwargs):
            if path == self.a / MARKER:
                raise OSError('interrupted marker removal')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_marker):
            with self.assertRaises(OSError):
                lease.close()
        self.assertIsNone(owners.owner(owners.paths(self.a, BUILDER)[1]))
        self.assertTrue((self.a / MARKER).exists())
        self.assertTrue(owners.cleanup(self.a, BUILDER, MARKER))
        self.assertFalse((self.a / MARKER).exists())
        successor = owners.acquire(self.b, BUILDER, MARKER)
        self.assertTrue(successor.close())

    def test_corrupt_owner_is_not_cleanup_authority(self):
        owners.paths(self.a, BUILDER)[1].write_text('{}')
        with self.assertRaises(ValueError):
            owners.cleanup(self.a, BUILDER, MARKER)
        self.run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
