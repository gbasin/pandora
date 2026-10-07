#!/usr/bin/env python3
"""Summarize an immutable saved transfer-record JSON file, entirely offline."""
import argparse
import collections
import importlib.util
import json
import math
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location('saved_timings', Path(__file__).with_name('saved_timings.py'))
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)
number, percentile, utc = _HELPERS.number, _HELPERS.percentile, _HELPERS.utc

BYTE_FIELDS = ('sent_bytes', 'received_bytes', 'literal_bytes', 'matched_bytes',
               'transferred_file_bytes')
COHORTS = ('cache_hits', 'completed_misses', 'incomplete_or_unknown')


def mapping(value):
    return value if isinstance(value, dict) else {}


def count(value):
    return isinstance(value, int) and number(value)


def total(values):
    try:
        return round(math.fsum(values), 6)
    except OverflowError:
        return None


def distribution(values):
    return {'count': len(values), 'p50': percentile(values, .5),
            'p95': percentile(values, .95), 'min': min(values, default=None),
            'max': max(values, default=None), 'sum': total(values) if values else None}


def ratio(numerator, denominator):
    if numerator is None or not denominator:
        return None
    try:
        value = numerator / denominator
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def iso(value):
    if not number(value):
        return None
    try:
        return utc(value)
    except (ValueError, OverflowError, OSError):
        return None


def sanitized(row):
    transfer = mapping(row.get('transfer'))
    counters = mapping(transfer.get('rsync'))
    return {
        'id': row.get('id') if isinstance(row.get('id'), str) else None,
        'repo': row['repo'],
        'state': row.get('state') if isinstance(row.get('state'), str) else None,
        'started': iso(row.get('started')), 'updated': iso(row.get('updated')),
        'pre_accept': {key: value for key, value in mapping(row.get('pre_accept')).items()
                       if isinstance(key, str) and number(value)},
        'freeze_steps': {key: value for key, value in mapping(row.get('freeze_steps')).items()
                         if isinstance(key, str) and number(value)},
        'steps': {key: value for key, value in mapping(transfer.get('steps')).items()
                  if isinstance(key, str) and number(value)},
        'cache': transfer.get('cache') if transfer.get('cache') in ('present', 'absent') else None,
        'rsync_exit': transfer.get('rsync_exit') if count(transfer.get('rsync_exit')) else None,
        'base_count': transfer.get('base_count') if count(transfer.get('base_count')) else None,
        'files': transfer.get('files') if count(transfer.get('files')) else None,
        'source_bytes': transfer.get('source_bytes') if count(transfer.get('source_bytes')) else None,
        'rsync': {key: value for key, value in counters.items()
                  if key in (*BYTE_FIELDS, 'regular_files_transferred') and count(value)},
    }


def cohort(row):
    if row['cache'] == 'present':
        return 'cache_hits'
    if (row['cache'] == 'absent' and row['rsync_exit'] == 0
            and 'submit' in row['pre_accept']
            and all(key in row['steps'] for key in ('rsync', 'publish', 'cleanup'))):
        return 'completed_misses'
    return 'incomplete_or_unknown'


def describe(rows):
    timings = {key: distribution([row['pre_accept'][key] for row in rows
                                 if key in row['pre_accept']])
               for key in ('freeze', 'ship', 'submit')}
    steps = {key: distribution([row['steps'][key] for row in rows if key in row['steps']])
             for key in sorted({key for row in rows for key in row['steps']})}
    freeze_steps = {key: distribution([row['freeze_steps'][key] for row in rows
                                      if key in row['freeze_steps']])
                    for key in sorted({key for row in rows for key in row['freeze_steps']})}
    paired = [row for row in rows if 'rsync' in row['steps']
              and row['pre_accept'].get('ship', 0) > 0]
    sum_rsync = total([row['steps']['rsync'] for row in paired]) if paired else None
    sum_ship = total([row['pre_accept']['ship'] for row in paired]) if paired else None
    fraction = ratio(sum_rsync, sum_ship)
    wire = []
    for row in rows:
        counters = row['rsync']
        if (row['source_bytes'] is not None and row['source_bytes'] > 0
                and all(key in counters for key in ('sent_bytes', 'received_bytes'))):
            wire_bytes = counters['sent_bytes'] + counters['received_bytes']
            wire_fraction = ratio(wire_bytes, row['source_bytes'])
            if wire_fraction is not None and wire_fraction <= .01:
                wire.append({'id': row['id'], 'state': row['state'], 'started': row['started'],
                             'source_bytes': row['source_bytes'], 'wire_bytes': wire_bytes,
                             'wire_fraction_of_source': wire_fraction,
                             'ship_seconds': row['pre_accept'].get('ship'),
                             'rsync_seconds': row['steps'].get('rsync'),
                             'literal_bytes': counters.get('literal_bytes'),
                             'matched_bytes': counters.get('matched_bytes'),
                             'base_count': row['base_count']})
    bases = collections.Counter(str(row['base_count']) for row in rows if row['base_count'] is not None)
    return {
        'count': len(rows), 'state_counts': dict(collections.Counter(row['state'] or '(unknown)' for row in rows)),
        'pre_accept_seconds': timings, 'step_seconds': steps, 'freeze_step_seconds': freeze_steps,
        'byte_distributions': {
            **{key: distribution([row['rsync'][key] for row in rows if key in row['rsync']])
               for key in BYTE_FIELDS},
            'source_bytes': distribution([row['source_bytes'] for row in rows if row['source_bytes'] is not None]),
        },
        'file_distributions': {
            'manifest_files': distribution([row['files'] for row in rows if row['files'] is not None]),
            'regular_files_transferred': distribution([row['rsync']['regular_files_transferred']
                                                       for row in rows if 'regular_files_transferred' in row['rsync']]),
        },
        'base_count_counts': dict(sorted(bases.items(), key=lambda pair: int(pair[0]))),
        'base_count_unknown': sum(row['base_count'] is None for row in rows),
        'rsync_fraction_of_ship': {'paired_count': len(paired), 'sum_rsync_seconds': sum_rsync,
                                   'sum_ship_seconds': sum_ship, 'fraction': fraction,
                                   'rsync_longer_than_ship_count': sum(row['steps']['rsync'] > row['pre_accept']['ship'] for row in paired)},
        'low_wire_count': len(wire),
        'low_wire_examples': sorted(wire, key=lambda row: row['wire_fraction_of_source'])[:5],
        'record_ids': [row['id'] for row in rows],
    }


def summarize(report):
    """Return aggregate observations without mutating or rereading the input."""
    if not isinstance(report, dict) or not isinstance(report.get('records'), list):
        raise ValueError('expected an object containing a records list')
    rows = []
    skipped = 0
    for row in report['records']:
        if not isinstance(row, dict) or not isinstance(row.get('repo'), str) or not row['repo']:
            skipped += 1
            continue
        rows.append(sanitized(row))
    return {
        'observed_at': iso(report.get('observed_at')), 'record_count': len(rows),
        'skipped_malformed_records': skipped,
        'repositories': {
            repo: {name: describe([row for row in rows if row['repo'] == repo and cohort(row) == name])
                   for name in COHORTS}
            for repo in sorted({row['repo'] for row in rows})
        },
        'limitations': [
            'Offline summary of the supplied retained-record snapshot; not a random or complete workload sample.',
            'Cohorts use transfer observations, independently of a later passed, failed, or running job state.',
            'Completed misses require cache absent, observed rsync exit 0, rsync/publication/cleanup timers, and valid submit elapsed time proving progress to engine submission. Timers alone cannot prove callback success; cleanup can fail harmlessly.',
            'Missing or invalid values remain unknown. Each distribution has its own observation count.',
            'Quantiles use linear interpolation; small samples do not establish stable latency percentiles.',
            'Rsync fraction is sum(rsync)/sum(ship) over paired valid rows with positive ship, not a ratio of unmatched aggregates.',
            'Rsync elapsed time mixes metadata/checksum reads, protocol work, and transport; it does not isolate checksum CPU.',
            'Sent plus received bytes are rsync protocol bytes, not complete network or SSH traffic.',
            'Low-wire examples require observed sent/received bytes totaling at most 1% of observed source bytes; literal/matched counters may be unknown.',
            'No source contents, logs, live state, SSH calls, or jobs are read or run.',
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = summarize(json.loads(args.records.read_text()))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print('Summarized %d records to %s' % (result['record_count'], args.out))


if __name__ == '__main__':
    main()
