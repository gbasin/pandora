#!/usr/bin/env python3
"""Summarize the five saved rsync diagnostics without contacting either host."""
import argparse
import json
import math
from pathlib import Path

VARIANTS = ('normal', 'dry_basis', 'dry_no_basis', 'empty', 'ssh_true')
CPU_FIELDS = ('user_seconds', 'system_seconds', 'filesystem_input_blocks', 'filesystem_output_blocks')
STAT_FIELDS = ('sent_bytes', 'received_bytes', 'literal_bytes', 'matched_bytes',
               'transferred_file_bytes', 'regular_files_transferred', 'file_list_bytes',
               'file_list_generation_seconds', 'file_list_transfer_seconds')


def numeric(value, *, signed=False):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and (signed or value >= 0))
    except OverflowError:
        return False


def distribution(values, *, signed=False):
    values = sorted(value for value in values if numeric(value, signed=signed))
    def percentile(fraction):
        if not values:
            return None
        index = (len(values) - 1) * fraction
        lo, hi = math.floor(index), math.ceil(index)
        return round(values[lo] + (values[hi] - values[lo]) * (index - lo), 6)
    return {'count': len(values), 'p50': percentile(.5), 'p95': percentile(.95),
            'min': min(values, default=None), 'max': max(values, default=None)}


def at(row, *path):
    for key in path:
        row = row.get(key) if isinstance(row, dict) else None
    return row


def summarize(report):
    if not isinstance(report, dict):
        raise ValueError('expected a saved diagnostic report')
    rounds, variants = report.get('rounds'), report.get('variants')
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise ValueError('rounds must be a positive integer')
    if (not isinstance(variants, list) or len(variants) != len(VARIANTS)
            or any(not isinstance(value, str) for value in variants)
            or set(variants) != set(VARIANTS)):
        raise ValueError('report must declare exactly the five diagnostic variants')
    rows = report.get('samples')
    if not isinstance(rows, list):
        raise ValueError('samples must be a list')
    samples = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('sample must be an object')
        variant, index = row.get('variant'), row.get('round')
        if (not isinstance(variant, str) or variant not in VARIANTS
                or isinstance(index, bool) or not isinstance(index, int)
                or index < 0 or index >= rounds):
            raise ValueError('invalid variant or measured round; priming must remain separate')
        full = variant == 'normal'
        if (row.get('verified') is not True or at(row, 'audit', 'verified') is not True
                or row.get('verified_full_source') is not full
                or row.get('verified_empty_stage') is not (not full)):
            raise ValueError('sample audit flags must match its full-source or empty-stage scope')
        if not numeric(row.get('wall_seconds')):
            raise ValueError('sample wall_seconds must be a recorded nonnegative finite duration')
        key = (variant, index)
        if key in samples:
            raise ValueError('duplicate variant/round')
        samples[key] = row
    if set(samples) != {(variant, index) for variant in VARIANTS for index in range(rounds)}:
        raise ValueError('every variant/round must have one audited sample')
    if 'edit_probe' in report:
        proof = report['edit_probe']
        if (not isinstance(proof, dict) or proof.get('variant') != 'normal'
                or proof.get('verified') is not True
                or proof.get('verified_full_source') is not True
                or proof.get('verified_empty_stage') is not False
                or at(proof, 'audit', 'verified') is not True):
            raise ValueError('the separate edit probe must pass its full-source audit')
    groups = {}
    for variant in VARIANTS:
        selected = [samples[(variant, index)] for index in range(rounds)]
        def measured(*path):
            return distribution([at(row, *path) for row in selected])
        groups[variant] = {
            'audit_scope': 'full_source' if variant == 'normal' else 'empty_stage',
            'wall_seconds': measured('wall_seconds'),
            'sender_self': {key: measured('sender_self', key) for key in CPU_FIELDS},
            'sender_children': {key: measured('sender_children', key) for key in CPU_FIELDS},
            'receiver': {
                'wall_seconds': measured('receiver', 'wall_seconds'),
                'self': {key: measured('receiver', 'self', key) for key in CPU_FIELDS},
                'children': {key: measured('receiver', 'children', key) for key in CPU_FIELDS},
                'child_logical_read_bytes': measured('receiver', 'child_logical_read_bytes'),
                'child_logical_write_bytes': measured('receiver', 'child_logical_write_bytes')},
            'stats': {key: measured('stats', key) for key in STAT_FIELDS}}
    paired = {}
    for first, second in (('normal', 'dry_basis'), ('dry_basis', 'dry_no_basis')):
        differences = [samples[(first, index)]['wall_seconds'] - samples[(second, index)]['wall_seconds']
                       for index in range(rounds)]
        paired[first + '_minus_' + second] = distribution(differences, signed=True)
    return {'rounds': rounds, 'sample_count': len(rows), 'groups': groups,
            'paired_wall_differences_seconds': paired,
            'limitations': [
                'Normal samples audit the full source; dry-run, empty, and SSH-only samples audit empty stages.',
                'Paired differences compare interventions in the same round; they are not an additive CPU, checksum, or network decomposition.',
                'Setup, planning, finalization, output audits, source capture, and untimed tracing are outside these wall timers.',
                'CPU subsets exclude the persistent SSH master and sshd. Filesystem blocks remain native units; unavailable logical child bytes remain unknown.',
                'Each distribution counts only its own observations. Unsupported counters are unknown, not zero.',
                'Priming, edit-proof, and checksum-tracing probes never enter this summary. Small samples do not establish stable tails.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = summarize(json.loads(args.records.read_text()))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    args.out.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    for name, group in result['groups'].items():
        print('%s median=%.3fs n=%d audit=%s' % (
            name, group['wall_seconds']['p50'], group['wall_seconds']['count'], group['audit_scope']))


if __name__ == '__main__':
    main()
