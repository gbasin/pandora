import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from execution_guard import Guard, reason


class ExecutionGuardTests(unittest.TestCase):
    def test_clock_and_disk_have_separate_boundaries(self):
        config = {'execution_seconds': 10, 'scheduler': {'disk_floor_mib': 2}}
        self.assertIsNone(reason(9, 2 * 1024**2, config))
        self.assertEqual(reason(10, 2 * 1024**2, config), 'deadline')
        self.assertEqual(reason(9, 2 * 1024**2 - 1, config), 'disk-floor')

    def test_deadline_interrupts_once_and_records_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp)
            notified = threading.Event()
            config = {'execution_seconds': 0, 'scheduler': {'disk_floor_mib': 0}}
            guard = Guard(attempt, config, interval=.001, interrupt=notified.set).start()
            try:
                self.assertTrue(notified.wait(2))
            finally:
                guard.close()
            self.assertTrue((attempt / 'deadline.request').is_file())
            self.assertEqual(json.loads((attempt / 'execution-stop.json').read_text())['reason'], 'deadline')

    def test_disk_fault_stops_without_claiming_a_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = Path(temp)
            notified = threading.Event()
            config = {'execution_seconds': 100, 'scheduler': {'disk_floor_mib': 2}}
            with patch('execution_guard.shutil.disk_usage', side_effect=OSError('disk unavailable')):
                guard = Guard(attempt, config, interval=.001, interrupt=notified.set).start()
                try:
                    self.assertTrue(notified.wait(2))
                finally:
                    guard.close()
            self.assertTrue((attempt / 'disk-stop.request').is_file())
            self.assertFalse((attempt / 'deadline.request').exists())

    def test_close_cancels_pending_observation(self):
        with tempfile.TemporaryDirectory() as temp:
            notified = threading.Event()
            guard = Guard(Path(temp), {'execution_seconds': 0, 'scheduler': {'disk_floor_mib': 0}},
                          interval=10, interrupt=notified.set).start()
            guard.close()
            self.assertFalse(notified.is_set())


if __name__ == '__main__':
    unittest.main()
