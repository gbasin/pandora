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
