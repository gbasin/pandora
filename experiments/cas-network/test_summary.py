"""The offline summary requires audited comparisons and preserves observation counts."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('network_summary', Path(__file__).with_name('summary.py'))
summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(summary)


def report(rounds=2):
    cases = ['cold', 'warm_unchanged']
    rows = []
    for case in cases:
        for method in summary.METHODS:
            for index in range(rounds):
                rows.append({'case': case, 'method': method, 'round': index,
                             'verified': True, 'wall_seconds': 2 + index,
                             'steps': {'plan': .2, 'finalize': .3},
                             'manifest_request_bytes': 100, 'control_request_bytes': 120,
                             'sender_children': {'user_seconds': .1,
                                                 'filesystem_input_blocks': 0},
                             'plan': {'helper_metrics': {'user_seconds': .01}},
                             'finalize': {'receiver': {'children': {'system_seconds': .02},
                                                       'child_logical_read_bytes': None}},
                             'stats': {'sent_bytes': 200, 'matched_bytes': None},
                             'audit': {'verified': True, 'execution_copy_seconds': .4}})
    return {'schema': 1, 'scope': 'scratch SSH comparison', 'fixture': {}, 'rounds': rounds,
            'cases': cases, 'methods': list(summary.METHODS), 'samples': rows,
            'priming': [dict(rows[0], round=-1, wall_seconds=900)],
            'priming_rounds_excluded': 1, 'limitations': ['No production adoption.']}


class NetworkSummary(unittest.TestCase):
    def test_complete_matrix_has_own_denominators_and_excludes_priming(self):
        result = summary.summarize(report())
        self.assertEqual(result['sample_count'], 12)
        details = result['cases']['cold']['rsync']
        self.assertEqual(details['count'], 2)
        self.assertEqual(details['wall_seconds']['p50'], 2.5)
        self.assertEqual(details['wall_seconds']['p95'], 2.95)
        observed = details['observed_metrics']
        self.assertEqual(observed['sender_children']['filesystem_input_blocks']['p50'], 0)
        self.assertEqual(observed['sender_children']['filesystem_input_blocks']['count'], 2)
        self.assertEqual(observed['finalize']['receiver']['children']['system_seconds']['count'], 2)
        self.assertEqual(observed['finalize']['receiver']['child_logical_read_bytes']['count'], 0)
        self.assertNotIn('verified', observed['audit'])
        self.assertEqual(observed['audit']['execution_copy_seconds']['p50'], .4)

    def test_missing_and_invalid_data_stay_unknown_in_each_distribution(self):
        raw = report()
        first, second = raw['samples'][:2]
        first['sender_children'].pop('user_seconds')
        second['sender_children']['user_seconds'] = .5
        first['stats'] = {'sent_bytes': False, 'matched_bytes': -1}
        second['stats'] = {'sent_bytes': float('inf'), 'matched_bytes': '100'}
        first['control_request_bytes'] = float('nan')
        second.pop('control_request_bytes')
        first['wall_seconds'] = True
        second['wall_seconds'] = -2
        result = summary.summarize(raw)
        details = result['cases']['cold']['rsync']
        self.assertEqual(details['wall_seconds']['count'], 0)
        self.assertIsNone(details['wall_seconds']['p50'])
        metrics = details['observed_metrics']
        self.assertEqual(metrics['sender_children']['user_seconds']['count'], 1)
        self.assertEqual(metrics['sender_children']['user_seconds']['p50'], .5)
        self.assertEqual(metrics['stats']['sent_bytes']['count'], 0)
        self.assertIsNone(metrics['stats']['sent_bytes']['max'])
        self.assertEqual(metrics['control_request_bytes']['count'], 0)
        json.dumps(result, allow_nan=False)

    def test_missing_duplicate_extra_or_priming_sample_is_rejected(self):
        for kind in ('missing', 'duplicate', 'extra-case', 'extra-method', 'extra-round', 'priming'):
            raw = report()
            if kind == 'missing':
                raw['samples'].pop()
            elif kind == 'duplicate':
                raw['samples'].append(copy.deepcopy(raw['samples'][0]))
            else:
                key, value = {'extra-case': ('case', 'undeclared'),
                              'extra-method': ('method', 'undeclared'),
                              'extra-round': ('round', 99), 'priming': ('round', -1)}[kind]
                raw['samples'][0][key] = value
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                summary.summarize(raw)

    def test_failed_receiver_audit_is_rejected(self):
        for verified in (False, None, 1, 'true'):
            raw = report()
            raw['samples'][0]['verified'] = verified
            with self.subTest(verified=verified), self.assertRaises(ValueError):
                summary.summarize(raw)

    def test_bad_matrix_declarations_have_useful_errors(self):
        mutations = [('rounds', True), ('rounds', 0), ('rounds', 1.5),
                     ('cases', []), ('cases', ['cold', 'cold']), ('cases', [{}]),
                     ('methods', ['rsync']), ('methods', ['rsync', 'rsync', 'cas_rehash']),
                     ('samples', None), ('limitations', 'not a list')]
        for key, value in mutations:
            raw = report()
            raw[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                summary.summarize(raw)

    def test_malformed_sample_keys_are_rejected(self):
        for mutation in (None, {'round': True}, {'case': []}, {'method': {}}, {'round': '0'}):
            raw = report()
            if mutation is None:
                raw['samples'][0] = None
            else:
                raw['samples'][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                summary.summarize(raw)

    def test_input_is_not_mutated_and_receiver_metrics_do_not_mix_with_sender(self):
        raw = report()
        before = copy.deepcopy(raw)
        metrics = summary.summarize(raw)['cases']['cold']['rsync']['observed_metrics']
        self.assertEqual(raw, before)
        self.assertEqual(metrics['sender_children']['user_seconds']['p50'], .1)
        self.assertEqual(metrics['plan']['helper_metrics']['user_seconds']['p50'], .01)

    def test_extreme_invalid_numbers_cannot_poison_output(self):
        raw = report()
        raw['samples'][0]['wall_seconds'] = 10 ** 400
        raw['samples'][0]['sender_children']['user_seconds'] = float('inf')
        result = summary.summarize(raw)
        self.assertEqual(result['cases']['cold']['rsync']['wall_seconds']['count'], 1)
        json.dumps(result, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
