import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import route


class RecoveryDelivery(unittest.TestCase):
    def test_failed_delivery_retries_completed_attempt_without_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp).resolve()
            state_root = repo / 'state'
            state = state_root / hashlib.sha256(str(repo).encode()).hexdigest()
            output = state / ('a' * 32)
            (output / 'results').mkdir(parents=True)
            files = {'results/exit-code': '0', 'results/junit.xml': '<testsuites/>'}
            for name, content in files.items():
                (output / name).write_text(content)
            (output / 'artifacts.json').write_text(json.dumps({
                name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}))
            (output / 'terminal.json').write_text(json.dumps({
                'attempt': 'a' * 32, 'exit_code': 0, 'cleanup_verified': True}))
            (output / 'submission.json').write_text(json.dumps({'source_digest': 'same'}))
            record = {'state': 'active', 'output': str(output), 'command': ['test:surface', 'borrower-web'],
                      'host': 'unused', 'attempt': 'a' * 32}
            route.write(state / 'active.json', record)
            env = {'PANDORA_STATE': str(state_root), 'PANDORA_HOST': 'unused'}
            with patch.dict(os.environ, env), patch('sys.argv', ['route.py', *record['command']]), \
                 patch('route.subprocess.check_output', return_value=str(repo)), \
                 patch('route.Path.cwd', return_value=repo), patch('route.current_digest', return_value='same'), \
                 patch('route.subprocess.Popen', side_effect=AssertionError('must not rerun or download')), \
                 patch('route.deliver', side_effect=[OSError('disk full'), None]) as delivery:
                self.assertEqual(route.main(), 75)
                self.assertEqual(json.loads((state / 'active.json').read_text())['state'], 'active')
                self.assertEqual(route.main(), 0)
                self.assertEqual(delivery.call_count, 2)
                self.assertEqual(json.loads((state / 'active.json').read_text())['state'], 'terminal')


if __name__ == '__main__':
    unittest.main()
