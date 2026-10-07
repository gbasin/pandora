"""Small real-rsync checks; the representative benchmark belongs on the worker."""
import importlib.util
from pathlib import Path
import shutil
import unittest

SPEC = importlib.util.spec_from_file_location('transfer_benchmark', Path(__file__).with_name('benchmark.py'))
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class TransferProfile(unittest.TestCase):
    @unittest.skipUnless(shutil.which('rsync'), 'rsync is required')
    def test_small_real_transfer_preserves_bytes_modes_symlinks_and_source_bases(self):
        result = benchmark.run_benchmark(mib=1, files=120, rounds=1)
        self.assertEqual(result['fixture']['regular_bytes'], 1 << 20)
        self.assertEqual(result['fixture']['manifest_paths'], 121)
        self.assertEqual(len(result['samples']), 6)
        self.assertEqual(len(result['priming']), 6)
        self.assertTrue(result['source_variants_immutable'])
        self.assertTrue(result['sanity_probes']['baseline_immutable'])
        samples = {sample['case']: sample for sample in result['samples']}
        self.assertTrue(all(sample['verified'] for sample in samples.values()))
        self.assertEqual(samples['warm_unchanged']['shared_with_bases']['regular_files'], 120)
        self.assertEqual(samples['four_bases']['shared_with_bases']['regular_files'], 119)
        self.assertEqual(samples['warm_delta']['shared_with_bases']['regular_files'], 119)
        self.assertEqual(samples['cold']['shared_with_bases']['regular_files'], 0)
        edited = result['sanity_probes']['same_size_edit_original_mtime']
        self.assertEqual(edited['shared_with_bases']['regular_files'], 119)
        changed_mode = result['sanity_probes']['same_bytes_changed_mode']
        self.assertEqual(changed_mode['shared_with_bases']['regular_files'], 119)

    def test_stats_parse_separators_and_older_regular_file_label(self):
        text = ('Total transferred file size: 1,234 bytes\nLiteral data: 1 000 bytes\n'
                'Matched data: 234 bytes\nFile list size: 88\nTotal bytes sent: 1,111\n'
                'Total bytes received: 222\nNumber of files transferred: 3\n')
        stats = benchmark.parse_stats(text)
        self.assertEqual(stats['transferred_file_bytes'], 1234)
        self.assertEqual(stats['literal_bytes'], 1000)
        self.assertEqual(stats['regular_files_transferred'], 3)
        self.assertEqual(benchmark.parse_stats('unsupported')['literal_bytes'], None)


if __name__ == '__main__':
    unittest.main()
