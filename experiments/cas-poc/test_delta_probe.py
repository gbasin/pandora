"""A small changed region should expose CAS whole-blob upload explicitly."""
import importlib.util
from pathlib import Path
import shutil
import sys
import unittest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location('delta_probe', HERE / 'delta_probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@unittest.skipUnless(shutil.which('rsync'), 'rsync unavailable')
class DeltaProbe(unittest.TestCase):
    def test_tiny_same_size_edit_requires_one_full_cas_blob(self):
        report = probe.run_probe(mib=1, files=103, rounds=1)
        self.assertTrue(report['source_variants_immutable'])
        self.assertEqual(report['fixture']['changed_bytes'], 16)
        self.assertEqual(len(report['samples']), 3)
        for row in report['samples']:
            self.assertTrue(row['verified'])
            if row['method'].startswith('cas_'):
                self.assertEqual(row['missing_files'], 1)
                self.assertEqual(row['missing_bytes'], report['fixture']['edited_file_bytes'])
                self.assertEqual(row['stats']['literal_bytes'], report['fixture']['edited_file_bytes'])


if __name__ == '__main__':
    unittest.main()
