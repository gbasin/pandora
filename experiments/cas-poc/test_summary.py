"""Reject incomplete, duplicated, priming, or unaudited comparison samples."""
import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('cas_summary', Path(__file__).with_name('summary.py'))
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def report():
    return {'rounds': 2, 'priming_rounds_excluded': 1, 'scope': 'scratch',
            'fixture': {'case_manifest_bytes': {'warm': 42}}, 'machine': {}, 'rsync': {},
            'limitations': [], 'samples': [
                {'case': 'warm', 'method': method, 'round': index, 'verified': True,
                 'wall_seconds': index + 1, 'initial_store': {'blob_bytes': 12, 'blob_count': 1}}
                for method in ('rsync', 'cas_trusted', 'cas_rehash') for index in range(2)]}


class Summaries(unittest.TestCase):
    def test_groups_rounds_and_preserves_unknown_counters(self):
        value = report()
        original = copy.deepcopy(value)
        result = summary.summarize(value)
        self.assertEqual(value, original)
        for method in result['cases']['warm'].values():
            self.assertEqual(method['seconds']['wall_seconds']['p50'], 1.5)
            self.assertEqual(method['stats']['sent_bytes']['count'], 0)
            self.assertIsNone(method['stats']['sent_bytes']['p50'])

    def test_missing_or_duplicate_rounds_are_rejected(self):
        for changed in ('missing', 'duplicate', 'missing-policy', 'missing-case'):
            with self.subTest(changed=changed):
                value = report()
                if changed == 'missing':
                    value['samples'].pop()
                elif changed == 'duplicate':
                    value['samples'][-1]['round'] = 0
                elif changed == 'missing-policy':
                    value['samples'] = value['samples'][:4]
                else:
                    value['fixture']['case_manifest_bytes']['cold'] = 43
                with self.assertRaises(ValueError):
                    summary.summarize(value)

    def test_priming_or_failed_audits_cannot_enter_summary(self):
        for field, invalid in (('round', -1), ('verified', False)):
            with self.subTest(field=field):
                value = report()
                value['samples'][0][field] = invalid
                with self.assertRaises(ValueError):
                    summary.summarize(value)


if __name__ == '__main__':
    unittest.main()
