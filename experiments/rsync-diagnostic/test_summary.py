"""Preserve intervention pairing and distinguish dry-run from source audits."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('rsync_summary', Path(__file__).with_name('summary.py'))
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def report():
    samples = []
    walls = {'normal': [3, 5, 4], 'dry_basis': [2, 6, 3],
             'dry_no_basis': [1, 7, 2], 'empty': [.2, .3, .1], 'ssh_true': [.1, .2, .1]}
    for variant, times in walls.items():
        for index, wall in enumerate(times):
            samples.append({'variant': variant, 'round': index, 'wall_seconds': wall,
                            'verified': True, 'verified_full_source': variant == 'normal',
                            'verified_empty_stage': variant != 'normal',
                            'audit': {'verified': True},
                            'sender_self': {'user_seconds': 0},
                            'sender_children': {'user_seconds': .5},
                            'stats': {'literal_bytes': 0}})
    return {'rounds': 3, 'variants': list(summary.VARIANTS), 'samples': samples,
            'priming': [dict(samples[0], round=-1, wall_seconds=999)]}


class DiagnosticSummary(unittest.TestCase):
    def test_five_groups_preserve_pairing_signed_differences_and_audit_scope(self):
        result = summary.summarize(report())
        self.assertEqual(result['sample_count'], 15)
        self.assertEqual(set(result['groups']), set(summary.VARIANTS))
        self.assertEqual(result['groups']['normal']['audit_scope'], 'full_source')
        self.assertEqual(result['groups']['dry_basis']['audit_scope'], 'empty_stage')
        difference = result['paired_wall_differences_seconds']['normal_minus_dry_basis']
        self.assertEqual(difference['count'], 3)
        self.assertEqual(difference['p50'], 1)
        self.assertEqual(difference['min'], -1)
        self.assertEqual(result['groups']['normal']['wall_seconds']['p50'], 4)

    def test_missing_counters_are_unknown_and_real_zero_is_an_observation(self):
        result = summary.summarize(report())['groups']['normal']
        self.assertEqual(result['stats']['literal_bytes']['count'], 3)
        self.assertEqual(result['stats']['literal_bytes']['p50'], 0)
        self.assertEqual(result['stats']['sent_bytes']['count'], 0)
        self.assertIsNone(result['stats']['sent_bytes']['p50'])
        self.assertEqual(result['sender_self']['user_seconds']['p50'], 0)
        self.assertEqual(result['receiver']['child_logical_read_bytes']['count'], 0)

    def test_incomplete_duplicate_priming_and_unknown_variant_are_rejected(self):
        for kind in ('missing', 'duplicate', 'priming', 'unknown'):
            raw = report()
            if kind == 'missing':
                raw['samples'].pop()
            elif kind == 'duplicate':
                raw['samples'].append(copy.deepcopy(raw['samples'][0]))
            elif kind == 'priming':
                raw['samples'][0]['round'] = -1
            else:
                raw['samples'][0]['variant'] = 'unreviewed'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                summary.summarize(raw)

    def test_a_dry_run_cannot_claim_full_source_verification(self):
        for flag in ('verified_full_source', 'verified_empty_stage', 'verified'):
            raw = report()
            dry = next(row for row in raw['samples'] if row['variant'] == 'dry_basis')
            dry[flag] = not dry[flag]
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'audit flags'):
                summary.summarize(raw)

    def test_failed_nested_audit_cannot_be_hidden_by_top_level_flags(self):
        raw = report()
        raw['samples'][0]['audit']['verified'] = False
        with self.assertRaisesRegex(ValueError, 'audit flags'):
            summary.summarize(raw)

    def test_invalid_timings_are_rejected_and_invalid_optional_metrics_ignored(self):
        for value in (True, -1, float('nan'), float('inf'), 10 ** 400):
            raw = report()
            raw['samples'][0]['wall_seconds'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                summary.summarize(raw)
        raw = report()
        raw['samples'][0]['stats']['literal_bytes'] = False
        self.assertEqual(summary.summarize(raw)['groups']['normal']['stats']['literal_bytes']['count'], 2)

    def test_input_unchanged_and_priming_not_counted(self):
        raw = report()
        before = copy.deepcopy(raw)
        result = summary.summarize(raw)
        self.assertEqual(raw, before)
        self.assertEqual(result['groups']['normal']['wall_seconds']['count'], 3)
        json.dumps(result, allow_nan=False)

    def test_separate_edit_proof_is_audited_but_not_a_measured_sample(self):
        raw = report()
        raw['edit_probe'] = dict(raw['samples'][0], wall_seconds=999,
                                 audit={'verified': True})
        raw['debug_probe'] = dict(raw['samples'][0], wall_seconds=888)
        result = summary.summarize(raw)
        self.assertEqual(result['sample_count'], 15)
        self.assertEqual(result['groups']['normal']['wall_seconds']['max'], 5)
        for change in ({'verified_full_source': False}, {'verified_empty_stage': True},
                       {'audit': {'verified': False}}, {'variant': 'dry_basis'}):
            broken = copy.deepcopy(raw)
            broken['edit_probe'].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'edit probe'):
                summary.summarize(broken)


if __name__ == '__main__':
    unittest.main()
