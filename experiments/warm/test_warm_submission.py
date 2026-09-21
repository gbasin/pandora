import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import warm


class WarmSubmissionTests(unittest.TestCase):
    def test_follow_observations_do_not_mutate_uploaded_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repo = root / 'repo'; repo.mkdir(); output = root / 'output'
            responses = [
                subprocess.CompletedProcess([], 0, stdout=json.dumps({
                    'missing': False, 'home': '/srv/pandora', 'cached': ''}), stderr=''),
                subprocess.CompletedProcess([], 0, stdout='sent source', stderr=''),
                subprocess.CompletedProcess([], 0, stdout='', stderr=''),
            ]
            with patch.object(sys, 'argv', ['warm.py', '--host', 'worker', '--repo', str(repo),
                                            '--output', str(output), '--attempt', 'a' * 32]), \
                    patch('warm.freeze', return_value=([], [])), \
                    patch('warm.repository_key', return_value='repo-key'), \
                    patch('warm.bundle', return_value=('b' * 64, 'payload')), \
                    patch('warm.run', side_effect=responses) as run, \
                    patch('warm.subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                    patch('warm.follow', return_value=70):
                self.assertEqual(warm.main(), 70)
            metadata_transfer = run.call_args_list[-1].args
            self.assertEqual(metadata_transfer[:3], ('rsync', '-rlpc', '--delay-updates'))
            self.assertEqual({Path(item).name for item in metadata_transfer if item.endswith('.json')},
                             {'manifest.json', 'submission.json'})
            submitted = json.loads((output / 'submission.json').read_text())
            self.assertNotIn('total_seconds', submitted)
            self.assertNotIn('exit_code', submitted)
            client = json.loads((output / 'client-result.json').read_text())
            self.assertEqual((client['attempt'], client['exit_code']), ('a' * 32, 70))


if __name__ == '__main__':
    unittest.main()
