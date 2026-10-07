#!/usr/bin/env python3
"""Summarize measured CAS comparison rounds, excluding the priming records."""
import argparse
import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'timing_helpers', Path(__file__).parents[1] / 'transfer-profile' / 'saved_timings.py')
helpers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helpers)


def distribution(values):
    values = [value for value in values if helpers.number(value)]
    return {'count': len(values), 'p50': helpers.percentile(values, .5),
            'p95': helpers.percentile(values, .95), 'min': min(values, default=None),
            'max': max(values, default=None)}


def summarize(report):
    rows = report['samples']
    if any(row['round'] < 0 or row.get('verified') is not True for row in rows):
        raise ValueError('measured samples must exclude priming and pass receiver audits')
    groups = {}
    expected_cases = set(report['fixture']['case_manifest_bytes'])
    expected_methods = {'rsync', 'cas_trusted', 'cas_rehash'}
    if {row['case'] for row in rows} != expected_cases or {row['method'] for row in rows} != expected_methods:
        raise ValueError('comparison cases and policies must all be present')
    for case in sorted(expected_cases):
        groups[case] = {}
        for method in sorted({row['method'] for row in rows}):
            selected = [row for row in rows if row['case'] == case and row['method'] == method]
            if (len(selected) != report['rounds']
                    or {row['round'] for row in selected} != set(range(report['rounds']))):
                raise ValueError('each case/method must contain the declared round count')
            groups[case][method] = {
                'seconds': {key: distribution([row.get(key) for row in selected])
                            for key in ('wall_seconds', 'verification_seconds',
                                        'execution_copy_seconds', 'execution_audit_seconds',
                                        'transfer_and_copy_seconds', 'verified_transfer_and_copy_seconds')},
                'step_seconds': {key: distribution([row.get('steps', {}).get(key) for row in selected])
                                 for key in sorted({key for row in selected for key in row.get('steps', {})})},
                'missing_files': distribution([row.get('missing_files') for row in selected]),
                'missing_bytes': distribution([row.get('missing_bytes') for row in selected]),
                'stats': {key: distribution([row.get('stats', {}).get(key) for row in selected])
                          for key in ('sent_bytes', 'received_bytes', 'literal_bytes', 'matched_bytes')},
                'initial_blob_bytes': distribution([row['initial_store']['blob_bytes'] for row in selected]),
                'initial_blob_count': distribution([row['initial_store']['blob_count'] for row in selected]),
            }
    return {'schema': 1, 'scope': report['scope'], 'rounds': report['rounds'],
            'priming_rounds_excluded': report['priming_rounds_excluded'],
            'fixture': report['fixture'], 'machine': report['machine'], 'rsync': report['rsync'],
            'cases': groups, 'limitations': report['limitations']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    report = summarize(json.loads(args.records.read_text()))
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    for case, methods in report['cases'].items():
        print(case, ' '.join('%s=%.3fs' % (method, details['seconds']['wall_seconds']['p50'])
                            for method, details in methods.items()))


if __name__ == '__main__':
    main()
