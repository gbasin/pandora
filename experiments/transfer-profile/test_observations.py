"""Offline cohort and missing-observation regressions over synthetic records."""
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('observations', Path(__file__).with_name('observations.py'))
observations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observations)


def row(run_id, *, state='passed', cache='absent', ship=10, rsync=8, submit=1):
    return {'id': run_id, 'repo': 'alpha', 'state': state, 'started': 100, 'updated': 101,
            'pre_accept': {'ship': ship, 'submit': submit},
            'transfer': {'cache': cache, 'rsync_exit': 0,
                         'steps': {'probe': .2, 'rsync': rsync, 'publish': .1, 'cleanup': .1}}}


def cohorts(rows):
    return observations.summarize({'observed_at': 200, 'records': rows})['repositories']['alpha']


class TransferObservations(unittest.TestCase):
    def test_completion_uses_transfer_and_submission_evidence_not_later_job_state(self):
        failed = row('failed', state='command_failed')
        running = row('running', state='running')
        stopped_upload = row('stopped-upload')
        stopped_upload['pre_accept'].pop('submit')
        failed_upload = row('failed-upload')
        failed_upload['transfer']['rsync_exit'] = 23
        hits = row('hit', state='command_failed', cache='present')
        details = cohorts([failed, running, stopped_upload, failed_upload, hits])
        self.assertEqual(details['completed_misses']['record_ids'], ['failed', 'running'])
        self.assertEqual(details['cache_hits']['record_ids'], ['hit'])
        self.assertEqual(details['incomplete_or_unknown']['record_ids'], ['stopped-upload', 'failed-upload'])

    def test_missing_counters_stay_unknown_and_cannot_be_low_wire_examples(self):
        complete = row('missing-stats')
        complete['transfer']['source_bytes'] = 1000000
        details = cohorts([complete])['completed_misses']
        for field in observations.BYTE_FIELDS:
            self.assertEqual(details['byte_distributions'][field]['count'], 0)
            self.assertIsNone(details['byte_distributions'][field]['p50'])
            self.assertIsNone(details['byte_distributions'][field]['sum'])
        self.assertEqual(details['base_count_counts'], {})
        self.assertEqual(details['base_count_unknown'], 1)
        self.assertEqual(details['low_wire_examples'], [])

    def test_fraction_uses_paired_positive_ship_rows_and_preserves_real_zero(self):
        first = row('first', ship=10, rsync=8)
        second = row('second', ship=30, rsync=6)
        zero_ship = row('zero-ship', ship=0, rsync=100)
        missing_ship = row('missing-ship', rsync=200)
        missing_ship['pre_accept'].pop('ship')
        zero_rsync = row('zero-rsync', ship=10, rsync=0)
        details = cohorts([first, second, zero_ship, missing_ship, zero_rsync])['completed_misses']
        ratio = details['rsync_fraction_of_ship']
        self.assertEqual(ratio['paired_count'], 3)
        self.assertEqual(ratio['sum_rsync_seconds'], 14)
        self.assertEqual(ratio['sum_ship_seconds'], 50)
        self.assertAlmostEqual(ratio['fraction'], .28)
        self.assertEqual(details['step_seconds']['rsync']['count'], 5)

    def test_invalid_values_are_excluded_without_mutating_input(self):
        malformed = row('bad', ship=True, rsync=float('nan'), submit='1')
        malformed['transfer'].update(base_count=True, files=-1, source_bytes=float('inf'))
        malformed['transfer']['rsync'] = {'sent_bytes': -1, 'received_bytes': False,
                                        'literal_bytes': 1.5, 'matched_bytes': '100'}
        malformed['freeze_steps'] = {'entries': float('inf'), 'names': .5}
        report = {'records': [malformed, None, {'repo': 123}], 'observed_at': True}
        original = json.dumps(report, sort_keys=True)
        result = observations.summarize(report)
        self.assertEqual(json.dumps(report, sort_keys=True), original)
        self.assertIsNone(result['observed_at'])
        self.assertEqual(result['skipped_malformed_records'], 2)
        details = result['repositories']['alpha']['incomplete_or_unknown']
        self.assertEqual(details['pre_accept_seconds']['ship']['count'], 0)
        self.assertNotIn('rsync', details['step_seconds'])
        self.assertEqual(details['base_count_unknown'], 1)
        self.assertEqual(details['freeze_step_seconds']['names']['count'], 1)
        self.assertNotIn('entries', details['freeze_step_seconds'])
        json.dumps(result, allow_nan=False)

    def test_repositories_are_separate_and_low_wire_examples_require_paired_bytes(self):
        low = row('low')
        low['transfer'].update(source_bytes=1000000, base_count=4)
        low['transfer']['rsync'] = {'sent_bytes': 100, 'received_bytes': 20}
        high = row('high')
        high['transfer'].update(source_bytes=1000000, base_count=2)
        high['transfer']['rsync'] = {'sent_bytes': 20000, 'received_bytes': 20}
        unknown = row('unknown')
        unknown['repo'] = 'beta'
        unknown['transfer'].update(source_bytes=1000000)
        unknown['transfer']['rsync'] = {'sent_bytes': 0}
        result = observations.summarize({'records': [low, high, unknown]})
        alpha = result['repositories']['alpha']['completed_misses']
        self.assertEqual(alpha['base_count_counts'], {'2': 1, '4': 1})
        self.assertEqual(alpha['low_wire_count'], 1)
        sample = alpha['low_wire_examples'][0]
        self.assertEqual(sample['id'], 'low')
        self.assertEqual(sample['wire_bytes'], 120)
        self.assertIsNone(sample['literal_bytes'])
        self.assertEqual(result['repositories']['beta']['completed_misses']['low_wire_count'], 0)

    def test_overflowing_derived_ratios_remain_unknown_and_json_serializable(self):
        extreme = row('extreme', ship=1e-6, rsync=1e308)
        extreme['transfer']['source_bytes'] = 1
        extreme['transfer']['rsync'] = {'sent_bytes': 10**308, 'received_bytes': 10**308}
        result = observations.summarize({'records': [extreme]})
        details = result['repositories']['alpha']['completed_misses']
        self.assertIsNone(details['rsync_fraction_of_ship']['fraction'])
        self.assertEqual(details['low_wire_count'], 0)
        json.dumps(result, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
