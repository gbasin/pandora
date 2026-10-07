#!/usr/bin/env python3
"""Aggregate a complete saved network comparison without probing either host."""
import argparse
import json
import math
from pathlib import Path

METHODS = ('rsync', 'cas_trusted', 'cas_rehash')
TOP_METRICS = ('manifest_request_bytes', 'control_request_bytes', 'control_response_bytes',
               'missing_files', 'missing_bytes', 'verification_seconds',
               'audit_transaction_seconds')
NESTED_METRICS = ('sender_self', 'sender_children', 'plan', 'finalize', 'stats', 'audit')


def number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0 and math.isfinite(value))
    except OverflowError:
        return False


def distribution(values):
    """Count each valid observation; never invent an observation for missing data."""
    values = sorted(value for value in values if number(value))

    def percentile(fraction):
        if not values:
            return None
        position = (len(values) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 6)

    return {'count': len(values), 'p50': percentile(.5), 'p95': percentile(.95),
            'min': min(values, default=None), 'max': max(values, default=None)}


def mapping(value):
    return value if isinstance(value, dict) else {}


def metric_paths(value, prefix=()):
    """Observe numeric metric leaves without flattening their producer namespaces."""
    paths = set()
    for key, child in mapping(value).items():
        if not isinstance(key, str):
            continue
        path = prefix + (key,)
        if isinstance(child, dict):
            paths.update(metric_paths(child, path))
        elif child is None or (isinstance(child, (int, float)) and not isinstance(child, bool)):
            paths.add(path)
    return paths


def lookup(value, path):
    for key in path:
        value = mapping(value).get(key)
    return value


def nested_distributions(rows, field, paths):
    result = {}
    for path in sorted(paths):
        target = result
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = distribution([lookup(row.get(field), path) for row in rows])
    return result


def validate(report):
    if not isinstance(report, dict):
        raise ValueError('expected a saved comparison object')
    rounds = report.get('rounds')
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise ValueError('rounds must be a positive integer')
    cases = report.get('cases')
    if (not isinstance(cases, list) or not cases
            or any(not isinstance(key, str) or not key for key in cases)
            or len(cases) != len(set(cases))):
        raise ValueError('cases must declare unique case names')
    methods = report.get('methods')
    if (not isinstance(methods, list) or len(methods) != len(METHODS)
            or any(not isinstance(method, str) for method in methods)
            or set(methods) != set(METHODS)):
        raise ValueError('methods must declare rsync and both CAS integrity policies')
    if (not isinstance(report.get('limitations', []), list)
            or any(not isinstance(value, str) for value in report.get('limitations', []))):
        raise ValueError('limitations must be a list of strings')
    rows = report.get('samples')
    if not isinstance(rows, list):
        raise ValueError('samples must be a list')
    expected = {(case, method, index) for case in cases for method in METHODS
                for index in range(rounds)}
    found = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('verified') is not True:
            raise ValueError('every measured sample must pass receiver audits')
        index = row.get('round')
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError('measured samples must exclude priming records')
        case, method = row.get('case'), row.get('method')
        if not isinstance(case, str) or not isinstance(method, str):
            raise ValueError('sample case and method must be strings')
        key = (case, method, index)
        if key not in expected or key in found:
            raise ValueError('unexpected or duplicate case/method/round sample')
        found.add(key)
    if found != expected:
        raise ValueError('every declared case/method/round must be present')
    return rows, sorted(cases)


def summarize(report):
    rows, cases = validate(report)
    groups = {}
    def observed(row):
        return {**{key: row.get(key) for key in TOP_METRICS},
                **{key: mapping(row.get(key)) for key in NESTED_METRICS}}

    observed_rows = [dict(row, observed_metrics=observed(row)) for row in rows]
    metric_fields = ('steps', 'observed_metrics')
    paths = {field: set().union(*(metric_paths(row.get(field)) for row in observed_rows))
             for field in metric_fields}
    for case in cases:
        groups[case] = {}
        for method in METHODS:
            selected = [row for row in observed_rows if row['case'] == case and row['method'] == method]
            groups[case][method] = {
                'count': len(selected),
                'wall_seconds': distribution([row.get('wall_seconds') for row in selected]),
                **{field: nested_distributions(selected, field, paths[field])
                   for field in metric_fields},
            }
    metadata = {key: report[key] for key in ('schema', 'scope', 'rounds',
                'priming_rounds_excluded', 'fixture', 'machine', 'rsync', 'transport',
                'methods', 'setup', 'sample_timeout_seconds', 'source_variants_immutable',
                'baseline_audit', 'cleanup')
                if key in report}
    return {**metadata, 'cases': groups, 'sample_count': len(rows),
            'limitations': [*report.get('limitations', []),
                'Only measured samples enter distributions; priming samples are excluded.',
                'Every numeric distribution has its own valid-observation count. Missing, invalid, or unsupported metrics remain unknown.',
                'Quantiles use linear interpolation; per-phase medians do not sum to a combined median.',
                'No live state, source trees, SSH connections, or jobs are inspected by this summarizer.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = summarize(json.loads(args.records.read_text()))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print('Summarized %d measured samples to %s' % (report['sample_count'], args.out))


if __name__ == '__main__':
    main()
