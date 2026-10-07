#!/usr/bin/env python3
"""Read saved metadata/results; write only aggregate timing diagnostics."""
import argparse
import collections
import datetime
import json
import math
import stat
from pathlib import Path


def utc(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return round(values[lower] + (values[upper] - values[lower]) * (position - lower), 6)


def number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and value >= 0 and math.isfinite(value))
    except OverflowError:
        return False


def timestamp(value):
    if not number(value):
        return False
    try:
        utc(value)
        return True
    except (OverflowError, ValueError, OSError):
        return False


def cutoff(value):
    try:
        parsed = datetime.datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timezone required')
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError) as error:
        raise argparse.ArgumentTypeError('use an ISO 8601 timestamp with an explicit timezone') from error


def describe(rows):
    phases = {}
    for phase in ('freeze', 'ship', 'submit'):
        values = [r['pre_accept'][phase] for r in rows if number(r['pre_accept'].get(phase))]
        phases[phase] = {'count': len(values), 'p50': percentile(values, .5),
                         'p95': percentile(values, .95), 'max': max(values, default=None)}
    paired = [r for r in rows if all(number(r['pre_accept'].get(p)) for p in ('freeze', 'ship'))]
    phases['paired_count'] = len(paired)
    phases['ship_larger_than_freeze_count'] = sum(r['pre_accept']['ship'] > r['pre_accept']['freeze'] for r in paired)
    phases['sum_freeze_seconds'] = round(sum(r['pre_accept']['freeze'] for r in paired), 6)
    phases['sum_ship_seconds'] = round(sum(r['pre_accept']['ship'] for r in paired), 6)
    phases['run_count'] = len(rows)
    phases['freeze_steps_count'] = sum(bool(r['freeze_steps']) for r in rows)
    phases['time_window'] = [utc(min(r['started'] for r in rows)), utc(max(r['started'] for r in rows))] if rows else None
    return phases


def sampled(row):
    sums = collections.defaultdict(float)
    for key, value in row['freeze_steps'].items():
        if number(value):
            sums[key.split('.')[-1]] += value
    return {**{k: row[k] for k in ('id', 'repo', 'job', 'pre_accept')},
            'started': utc(row['started']),
            'phase_seconds_both_passes': {k: round(v, 6) for k, v in sums.items()},
            'dominant_measured_phase': max(sums, key=sums.get) if sums else None}


def audit(state, since, *, repos=None):
    """Inspect regular saved records. No log or source contents are read."""
    since_at = cutoff(since)
    rows, result_count = [], 0
    counters = collections.Counter()
    interesting_keys = collections.Counter()

    def read(path):
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                counters['symlink_records_skipped'] += 1
                return None
            if not stat.S_ISREG(mode):
                counters['nonregular_records_skipped'] += 1
                return None
            value = json.loads(path.read_text())
        except OSError:
            counters['unreadable_records'] += 1
            return None
        except (ValueError, UnicodeError):
            counters['malformed_records'] += 1
            return None
        if not isinstance(value, dict):
            counters['malformed_records'] += 1
            return None
        return value

    def timing_map(meta, field):
        values = meta.get(field, {})
        if not isinstance(values, dict):
            counters['malformed_timing_maps'] += 1
            return {}
        valid = {}
        for key, value in values.items():
            if number(value):
                valid[key] = value
            else:
                counters['invalid_timing_values'] += 1
        return valid

    def inventory(value, prefix=''):
        if isinstance(value, dict):
            for key, child in value.items():
                name = prefix + key
                if (any(term in key.lower() for term in ('source', 'reused', 'rsync', 'received', 'transfer_bytes'))
                        or key.lower() == 'sent' or key.lower().startswith('sent_')):
                    interesting_keys[name] += 1
                inventory(child, name + '.')
        elif isinstance(value, list):
            for child in value:
                inventory(child, prefix + '[].')

    runs = Path(state) / 'runs'
    if runs.is_symlink():
        raise ValueError('runs directory must not be a symlink')
    for directory in sorted(runs.iterdir()):
        if directory.is_symlink():
            counters['symlink_runs_skipped'] += 1
            continue
        if not directory.is_dir():
            continue
        meta = read(directory / 'meta.json')
        if meta is not None:
            counters['readable_metadata'] += 1
            inventory(meta, 'meta.')
            pre_accept = timing_map(meta, 'pre_accept')
            freeze_steps = timing_map(meta, 'freeze_steps')
            repo = meta.get('repo')
            if not isinstance(repo, str) or not repo:
                counters['unidentified_repository'] += 1
            elif not timestamp(meta.get('started')):
                counters['invalid_started_values'] += 1
            else:
                rows.append({'id': directory.name, 'repo': repo,
                             'job': meta.get('job') if isinstance(meta.get('job'), str) else None,
                             'started': meta['started'], 'pre_accept': pre_accept,
                             'freeze_steps': freeze_steps})
        result = directory / 'result.json'
        if result.exists() or result.is_symlink():
            saved = read(result)
            if saved is not None:
                inventory(saved, 'result.')
                result_count += 1
    report = {'generated_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'since': since, 'metadata_count': len(rows), 'result_count': result_count,
              'record_counters': dict(counters), 'interesting_key_counts': dict(interesting_keys),
              'repositories': {}}
    for repo in sorted(set(repos) if repos else {r['repo'] for r in rows}):
        selected = [r for r in rows if r['repo'] == repo]
        recent = [r for r in selected if r['started'] >= since_at]
        instrumented = [r for r in selected if r['freeze_steps']]
        recent_samples = [sampled(r) for r in recent if r['freeze_steps']]
        report['repositories'][repo] = {'all_retained': describe(selected), 'since_cutoff': describe(recent),
                                        'instrumented': describe(instrumented),
                                        'slow_capture_samples': [sampled(r) for r in selected if r['pre_accept'].get('freeze', 0) >= 60],
                                        'recent_dominant_phase_counts': dict(collections.Counter(s['dominant_measured_phase'] for s in recent_samples)),
                                        'recent_slowest_capture_samples': sorted(recent_samples, key=lambda s: s['pre_accept'].get('freeze', 0), reverse=True)[:3],
                                        'recent_slowest_ship_samples': sorted(recent_samples, key=lambda s: s['pre_accept'].get('ship', 0), reverse=True)[:1]}
    report['limitations'] = ['Read metadata/result JSON only; no live logs, source files, or jobs.',
                             'Retained runs are a censored sample; timing fields are absent for many local/refused/older runs.',
                             'Quantiles use linear interpolation. Aggregate durations can include failed/retried work.',
                             'Ship includes cache probing, SSH and rsync setup/transfer; elapsed ship is not wire-byte throughput.',
                             'No retained transfer-byte/source-reuse fields found unless listed in interesting_key_counts.']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--since', required=True, help='ISO 8601 timestamp with timezone')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--repo', action='append', help='include this repository; repeat to select several')
    args = parser.parse_args()
    try:
        report = audit(args.state, args.since, repos=args.repo)
    except (argparse.ArgumentTypeError, ValueError, OSError) as error:
        parser.error(str(error))
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'repositories'}, indent=2))
    for repo, details in report['repositories'].items():
        print(repo, json.dumps({k: v for k, v in details.items() if not k.endswith('samples')}, indent=2))


if __name__ == '__main__':
    main()
