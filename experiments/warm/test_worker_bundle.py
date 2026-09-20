import json
from pathlib import Path
import tempfile
import unittest
from worker_bundle import bundle, prepare, verified


class BundleTests(unittest.TestCase):
    def test_miss_hit_corruption_repair_and_attempt_isolation(self):
        scripts = Path(__file__).parent
        identity, payload = bundle(scripts)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(prepare(root, identity, 'a' * 32, None), {'missing': True})
            self.assertFalse((root / 'runs').exists())
            self.assertFalse(prepare(root, identity, 'a' * 32, None, payload)['missing'])
            cache = root / 'worker-bundles' / identity
            self.assertTrue(verified(cache, identity))
            self.assertFalse(prepare(root, identity, 'b' * 32, None)['missing'])
            (root / 'runs' / ('a' * 32) / 'worker.py').write_text('changed')
            self.assertTrue(verified(cache, identity))
            (cache / 'worker.py').write_text('corrupt')
            self.assertTrue(prepare(root, identity, 'c' * 32, None)['missing'])
            self.assertFalse(prepare(root, identity, 'c' * 32, None, payload)['missing'])
            with self.assertRaises(FileExistsError):
                prepare(root, identity, 'c' * 32, None)
            self.assertEqual((root / 'runs' / ('b' * 32) / 'worker.py').read_bytes(),
                             (scripts / 'worker.py').read_bytes())

    def test_payload_must_match_content_identity(self):
        identity, payload = bundle(Path(__file__).parent)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, 'checksum'):
                prepare(Path(temp), identity, 'a' * 32, None, payload + ' ')


if __name__ == '__main__':
    unittest.main()
