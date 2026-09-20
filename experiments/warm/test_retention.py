import json
from pathlib import Path
import tempfile
import unittest
from retention import candidates, local, PROFILE


class Retention(unittest.TestCase):
    def test_only_acknowledged_profile_runs_are_collectible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = []
            for index in range(15):
                path = root / f'{index:032x}'
                path.mkdir()
                (path / 'submission.json').write_text(json.dumps({'profile': PROFILE}))
                (path / 'terminal.json').write_text(json.dumps({'cleanup_verified': True}))
                (path / 'completed.json').write_text('{}')
                paths.append(path)
            (paths[0] / 'completed.json').unlink()  # unresolved delivery
            (paths[1] / 'submission.json').write_text('{}')  # earlier experiment
            (paths[2] / 'terminal.json').write_text('{"cleanup_verified":false}')
            victims = candidates(root, 'completed.json', keep=1, protected=[paths[3]])
            self.assertFalse(set(paths[:4]) & set(victims))
            self.assertEqual(len(victims), 10)
            local(root, paths[3])
            self.assertTrue(all(path.exists() for path in paths[:4]))
            self.assertEqual(sum(path.exists() for path in paths), 14)


if __name__ == '__main__':
    unittest.main()
