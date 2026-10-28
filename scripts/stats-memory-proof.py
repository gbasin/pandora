#!/usr/bin/env python3
"""Compare real CLI -> socket -> daemon stats memory on two source revisions.

Synthetic state only. Never connects to the installed daemon or a worker.
Run: python3 scripts/stats-memory-proof.py --baseline origin/main --output /tmp/proof.json
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
MIB = 1024 * 1024
BASELINE = 'c8d4d32c99877fe725e76ed356ea7a52cd97dbe1'


def serve(source, state):
    sys.path.insert(0, str(source))
    from pandora.client.daemon import Daemon

    tracemalloc.start()
    daemon = Daemon(state=state, config_path=str(state.parent / 'config.toml'))
    daemon.start()
    thread = threading.Thread(target=daemon.serve, daemon=True)
    thread.start()
    try:
        print(json.dumps({'ready': True, 'pid': os.getpid()}), flush=True)
        for line in sys.stdin:
            if line.strip() == 'stop':
                break
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            print(json.dumps({'traced_bytes': current, 'peak_traced_bytes': peak,
                              'max_rss_bytes': rss if sys.platform == 'darwin' else rss * 1024,
                              'cached_results': len(daemon.index.results)}), flush=True)
            tracemalloc.reset_peak()
    finally:
        daemon.stop()
        thread.join(5)


def write_rows(state, start, end, epoch, kib):
    """Exercise every stats input, with large nested data it must not retain."""
    for n in range(start, end):
        folder = state / 'runs' / ('r%05d' % n)
        folder.mkdir(parents=True)
        local = n % 3 == 0
        # Two cohorts, far from the one-hour cutoff so before/after wall time
        # cannot change membership. A window excludes the entire first cohort.
        stamp = epoch - (7200 if start == 0 else 300) + n / 1000
        meta = {'id': folder.name, 'job': 'job%d' % (n % 3),
                'state': 'fell_back' if n % 19 == 0 else 'passed',
                'lane': 'local' if local else 'remote', 'started': stamp,
                'queue_ms': n * 10, 'reason': 'fallback:worker-down' if local else '',
                'attempts': [{'cause': 'meta-retry'}] if n % 7 == 0 else [],
                'placement': {'override': 'local', 'overridden': True} if local else {}}
        # Different allocations after JSON decoding, like the duplicated suite
        # evidence in real results. This is not an RSS test of shared fixtures.
        detail = {'observations': [{'id': '%d-%d' % (n, i), 'outcome': 'passed',
                                    'detail': 'x' * 950} for i in range(kib // 3)]}
        result = {'outcome': ['passed', 'command_failed', 'oom'][n % 3],
                  'input_id': 'input-%d' % n, 'lane': meta['lane'], 'wall_seconds': n + 1,
                  'durations': {'queue': n / 10, **({} if local else {'execute': n + 2})},
                  'drifted': n % 5 == 0, 'drift': 'fail' if n % 2 else 'warn',
                  'hint_rule': 'oom' if n % 3 == 2 else None,
                  'evidence': {'cause': 'queue-timeout' if n % 11 == 0 else None, **detail},
                  'verdict': detail, 'test_evidence': detail,
                  'attempts': [{'cause': 'worker-lost', 'evidence': detail}]
                  if n % 13 == 0 else [],
                  'flaky': {'order': ['failed', 'passed'], 'shards': [detail, detail]}
                  if n % 17 == 0 else {}}
        (folder / 'meta.json').write_text(json.dumps(meta))
        (folder / 'result.json').write_text(json.dumps(result))
        os.utime(folder, (stamp, stamp))


def report_identity(report):
    # Machine probes and clock-derived values vary between processes. Every
    # historical aggregate, its window, and the retention policy must match.
    result = {k: v for k, v in report.items()
              if k not in ('local', 'pause', 'worker', 'client', 'since')}
    if result.get('retention'):
        result['retention'] = {k: v for k, v in result['retention'].items() if k != 'oldest'}
    return result


def cli(source, state, *args):
    env = dict(os.environ, PYTHONPATH=str(source))
    completed = subprocess.run(
        [sys.executable, '-m', 'pandora.cli', '--state', str(state), '--config',
         str(state.parent / 'config.toml'), *args], cwd=source, env=env,
        capture_output=True, text=True, timeout=90, check=True)
    if 'no report from the daemon' in completed.stderr:
        raise AssertionError('CLI silently fell back to disk: ' + completed.stderr)
    return json.loads(completed.stdout)


def measure(process):
    process.stdin.write('measure\n')
    process.stdin.flush()
    line = process.stdout.readline()
    if not line:
        raise RuntimeError('scratch daemon exited before measurement')
    return json.loads(line)


def exercise(source, home, *, small, large, kib, epoch):
    state = home / 'state'
    state.mkdir(parents=True)
    (home / 'config.toml').write_text(
        '[client]\nstate = %s\nkeep_runs_days = 0\nname = "memory-proof"\n'
        '[worker]\nhost = ""\n[notify]\nenabled = false\n'
        '[local]\nbudget_mib = 1024\n[local.pause]\nenabled = false\n'
        % json.dumps(str(state)))
    with (home / 'daemon.log').open('w') as log:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), '--serve', '--source', str(source),
             '--state', str(state)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=log, text=True)
        try:
            if not json.loads(process.stdout.readline()).get('ready'):
                raise AssertionError('scratch daemon did not start')
            samples = {'empty': measure(process)}
            reports = {}
            for name, start, end in [('small', 0, small), ('large', small, large)]:
                write_rows(state, start, end, epoch, kib)
                begin = time.monotonic()
                reports[name] = report_identity(cli(source, state, 'stats', '--json'))
                samples[name] = dict(measure(process), seconds=time.monotonic() - begin)
                for repeat in range(3):
                    answer = report_identity(cli(source, state, 'stats', '--json'))
                    assert answer == reports[name], 'warm report changed'
                samples[name + '_warm'] = measure(process)
            reports['window'] = report_identity(cli(source, state, 'stats', '--since', '1h', '--json'))
            assert 0 < reports['window']['runs'] < reports['large']['runs'], 'window excluded no rows'
            # A result rewritten by resolve must invalidate the summary.
            path = state / 'runs' / 'r00001' / 'result.json'
            result = json.loads(path.read_text())
            result['outcome'] = 'passed'
            result['hint_rule'] = 'resolved'
            path.write_text(json.dumps(result))
            reports['changed'] = report_identity(cli(source, state, 'stats', '--json'))
            assert reports['changed'] != reports['large'], 'rewritten result was ignored'
            path.unlink()
            reports['missing'] = report_identity(cli(source, state, 'stats', '--json'))
            assert reports['missing'] != reports['changed'], 'deleted result was retained'
            samples['final'] = measure(process)
        finally:
            if process.poll() is None:
                try:
                    process.stdin.write('stop\n')
                    process.stdin.flush()
                    process.wait(timeout=15)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait()
            process.stdin.close()
            process.stdout.close()
        if process.returncode:
            raise AssertionError('scratch daemon failed: ' + (home / 'daemon.log').read_text())
    # The real CLI's daemon-unavailable path must produce the same history.
    completed = subprocess.run(
        [sys.executable, '-m', 'pandora.cli', '--state', str(state), '--config',
         str(home / 'config.toml'), 'stats', '--json'], cwd=source,
        env=dict(os.environ, PYTHONPATH=str(source)), capture_output=True,
        text=True, timeout=90, check=True)
    reports['offline'] = report_identity(json.loads(completed.stdout))
    assert reports['offline'] == reports['missing'], 'offline report disagrees'
    return {'samples': samples, 'reports': reports,
            'fixture_bytes': sum(p.stat().st_size for p in (state / 'runs').glob('*/result.json'))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default=BASELINE)
    parser.add_argument('--output', type=Path, default=Path('/tmp/pandora-stats-memory.json'))
    parser.add_argument('--small', type=int, default=64)
    parser.add_argument('--large', type=int, default=256)
    parser.add_argument('--payload-kib', type=int, default=1024)
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--source', type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument('--state', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.source, args.state)
        return
    baseline_sha = subprocess.check_output(['git', 'rev-parse', args.baseline], cwd=ROOT,
                                           text=True).strip()
    with tempfile.TemporaryDirectory(prefix='pandora-memory-', dir='/tmp') as temporary:
        home = Path(temporary)
        baseline = home / 'baseline'
        baseline.mkdir()
        archive = subprocess.check_output(['git', 'archive', baseline_sha], cwd=ROOT)
        subprocess.run(['tar', '-xf', '-', '-C', str(baseline)], input=archive, check=True)
        epoch = time.time()
        options = dict(small=args.small, large=args.large, kib=args.payload_kib, epoch=epoch)
        before = exercise(baseline, home / 'before', **options)
        after = exercise(ROOT, home / 'after', **options)
    equal = before['reports'] == after['reports']
    def growth(run):
        return run['samples']['large_warm']['traced_bytes'] - run['samples']['empty']['traced_bytes']
    old, new = growth(before), growth(after)
    checks = {'all_reports_identical': equal,
              'baseline_retains_large_evidence': old > 64 * MIB,
              'candidate_retained_growth_under_16_mib': new < 16 * MIB,
              'candidate_peak_growth_under_16_mib':
              after['samples']['large']['peak_traced_bytes']
              - after['samples']['empty']['traced_bytes'] < 16 * MIB,
              'candidate_growth_under_5_percent_of_baseline': new < old * .05}
    evidence = {'baseline_commit': baseline_sha,
                'candidate_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
                                                            cwd=ROOT, text=True).strip(),
                'candidate_diff_sha256': hashlib.sha256(subprocess.check_output(
                    ['git', 'diff', 'HEAD'], cwd=ROOT)).hexdigest(),
                'python': sys.version, 'platform': platform.platform(),
                'workload': options, 'checks': checks, 'before': before, 'after': after}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'checks': checks, 'baseline_growth_mib': old / MIB,
                      'candidate_growth_mib': new / MIB, 'evidence': str(args.output)}, indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
