import contextlib
import io
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import warm


class SnapshotFeedbackTests(unittest.TestCase):
    def test_slow_capture_emits_feedback_only_after_the_interval(self):
        entered = threading.Event()
        release = threading.Event()

        def freeze(_repo, _destination):
            entered.set()
            self.assertTrue(release.wait(1))
            return [], []

        output = io.StringIO()
        with patch('warm.freeze', side_effect=freeze), contextlib.redirect_stdout(output):
            caller = threading.Thread(target=lambda: warm.capture_source(Path('.'), Path('source'), .01))
            caller.start()
            self.assertTrue(entered.wait(1))
            time.sleep(.03)
            self.assertIn('still freezing local source', output.getvalue())
            release.set()
            caller.join(1)
        self.assertFalse(caller.is_alive())

    def test_capture_exception_stops_and_joins_the_feedback_thread(self):
        def freeze(_repo, _destination):
            time.sleep(.025)
            raise RuntimeError('capture failed')

        output = io.StringIO()
        with patch('warm.freeze', side_effect=freeze), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                warm.capture_source(Path('.'), Path('source'), .01)
            observed = output.getvalue()
            time.sleep(.03)
            self.assertEqual(output.getvalue(), observed)
        self.assertFalse(any(thread.name == 'pandora-capture-feedback'
                             for thread in threading.enumerate()))


if __name__ == '__main__':
    unittest.main()
