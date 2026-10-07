"""Small-fixture checks of comparison fairness and complete receiver audits."""
import importlib.util
from pathlib import Path
import shutil
import sys
import unittest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location('cas_comparison', HERE / 'benchmark.py')
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@unittest.skipUnless(shutil.which('rsync'), 'rsync unavailable')
class Comparison(unittest.TestCase):
    def test_all_policies_use_identical_cases_and_audit_execution_copies(self):
        report = benchmark.run_benchmark(mib=1, files=103, rounds=1)
        self.assertEqual(len(report['samples']), len(benchmark.CASES) * len(benchmark.METHODS))
        self.assertEqual(len(report['priming']), len(report['samples']))
        self.assertTrue(report['source_variants_immutable'])
        rows = {(row['case'], row['method']): row for row in report['samples']}
        for case in benchmark.CASES:
            for method in benchmark.METHODS:
                row = rows[case, method]
                self.assertTrue(row['verified'])
                self.assertGreaterEqual(row['verified_transfer_and_copy_seconds'],
                                        row['transfer_and_copy_seconds'])
        for method in ('cas_trusted', 'cas_rehash'):
            self.assertEqual(rows['cold', method]['missing_files'], 103)
            self.assertEqual(rows['warm_unchanged', method]['missing_files'], 0)
            self.assertEqual(rows['warm_small_delta', method]['missing_files'], 1)
            self.assertEqual(rows['warm_large_delta', method]['missing_files'], 1)
            self.assertEqual(rows['mode_change', method]['missing_files'], 1)
            self.assertEqual(rows['divergent', method]['missing_files'], 90)
            self.assertEqual(rows['retained_history', method]['missing_files'], 0)
            self.assertGreater(rows['retained_history', method]['initial_store']['blob_bytes'],
                               rows['divergent', method]['initial_store']['blob_bytes'])
        self.assertNotIn('verify_cached', rows['warm_unchanged', 'cas_trusted']['steps'])
        self.assertIn('verify_cached', rows['warm_unchanged', 'cas_rehash']['steps'])


if __name__ == '__main__':
    unittest.main()
