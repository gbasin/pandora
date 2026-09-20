import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

from deadline_stop import stop


class DeadlineStopTests(unittest.TestCase):
    def attempt(self, root):
        path = root / ('a' * 32)
        path.mkdir()
        return path

    def test_marks_and_signals_only_the_registered_process_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(Path(temp))
            (attempt / 'worker.json').write_text(json.dumps({'pid': 42, 'start_ticks': '123'}))
            with patch('deadline_stop.start_ticks', return_value='123'), patch('deadline_stop.os.kill') as kill:
                self.assertTrue(stop(attempt))
            self.assertTrue((attempt / 'deadline.request').is_file())
            kill.assert_called_once_with(42, signal.SIGTERM)

    def test_marks_but_never_signals_a_reused_or_missing_process(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(Path(temp))
            (attempt / 'worker.json').write_text(json.dumps({'pid': 42, 'start_ticks': '123'}))
            with patch('deadline_stop.start_ticks', return_value='456'), patch('deadline_stop.os.kill') as kill:
                self.assertFalse(stop(attempt))
            self.assertTrue((attempt / 'deadline.request').is_file())
            kill.assert_not_called()

    def test_missing_registration_does_not_signal_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(Path(temp))
            with patch('deadline_stop.os.kill') as kill:
                self.assertFalse(stop(attempt))
            self.assertTrue((attempt / 'deadline.request').is_file())
            kill.assert_not_called()

    def test_invalid_process_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            attempt = self.attempt(Path(temp))
            for registration in ({'pid': 0, 'start_ticks': '123'}, {'pid': 42, 'start_ticks': '-1'}):
                (attempt / 'worker.json').write_text(json.dumps(registration))
                with self.assertRaisesRegex(ValueError, 'registration'):
                    stop(attempt)


if __name__ == '__main__':
    unittest.main()
