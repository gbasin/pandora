#!/usr/bin/env python3
"""Side effect 1: what the shim costs when it decides not to route.

Measured against a trivial real command, because the question is the shim's own
overhead, not pnpm's.  Reported as the delta over invoking that same real
command directly, so the cost of fork+exec itself is not charged to the shim.

    python3 bench.py --iterations 300
"""
import argparse
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness import Sandbox

TRIVIAL = '#!/bin/sh\nexit 0\n'


def timings(command, cwd, env, iterations, warmup=20):
    for _ in range(warmup):
        subprocess.run(command, cwd=str(cwd), env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        subprocess.run(command, cwd=str(cwd), env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def summary(samples):
    ordered = sorted(samples)
    return {'p50': statistics.median(ordered),
            'p95': ordered[int(0.95 * (len(ordered) - 1))],
            'p99': ordered[int(0.99 * (len(ordered) - 1))],
            'max': ordered[-1], 'mean': statistics.fmean(ordered)}


def row(name, samples, baseline=None):
    stats = summary(samples)
    if baseline is None:
        return '%-38s %8.2f %8.2f %8.2f %8.2f' % (
            name, stats['p50'], stats['p95'], stats['p99'], stats['max'])
    base = summary(baseline)
    return '%-38s %8.2f %8.2f %8.2f %8.2f   (+%.2f p50, +%.2f p95)' % (
        name, stats['p50'], stats['p95'], stats['p99'], stats['max'],
        stats['p50'] - base['p50'], stats['p95'] - base['p95'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iterations', type=int, default=250)
    args = parser.parse_args()

    box = Sandbox()
    try:
        box.real.write_text(TRIVIAL)
        box.real.chmod(0o755)
        box.enrol()                       # marker written; no daemon needed for these paths
        outside = box.root / 'outside'
        outside.mkdir()
        deep = box.repo / 'apps' / 'desk' / 'src' / 'components' / 'nested'
        deep.mkdir(parents=True)
        worktree = box.worktree('wt-bench')
        env = box.env()
        direct = dict(env, PATH=os.pathsep.join([str(box.realbin), '/usr/bin', '/bin']))

        print('%d iterations each, milliseconds\n' % args.iterations)
        print('%-38s %8s %8s %8s %8s' % ('case', 'p50', 'p95', 'p99', 'max'))
        baseline = timings(['pnpm', 'x'], outside, direct, args.iterations)
        print(row('baseline: real pnpm, no shim', baseline))

        cases = [
            ('sh shim, not a git repo', ['pnpm', 'x'], outside),
            ('sh shim, enrolled repo root, unclaimed', ['pnpm', 'lint:fast'], box.repo),
            ('sh shim, enrolled worktree, unclaimed', ['pnpm', 'lint:fast'], worktree),
            ('sh shim, 5 levels deep, unclaimed', ['pnpm', 'lint:fast'], deep),
            ('sh shim, PANDORA_OFF=1', ['pnpm', 'test:unit'], box.repo),
        ]
        for name, command, cwd in cases:
            case_env = dict(env, PANDORA_OFF='1') if 'OFF' in name else env
            print(row(name, timings(command, cwd, case_env, args.iterations), baseline))
        print(row('python shim, not a git repo',
                  timings(['pnpm-python', 'x'], outside, env, args.iterations), baseline))
        print(row('python shim, enrolled, unclaimed',
                  timings(['pnpm-python', 'lint:fast'], box.repo, env, args.iterations),
                  baseline))

        # The routed path is measured for context only; it is not on the budget,
        # because a claimed command is a test suite, not a `--version`.
        box.reconfigure({'mode': 'ok', 'stdout': [], 'exit_code': 0})
        box.start()
        print(row('sh shim, routed round trip (daemon)',
                  timings(['pnpm', 'test:unit'], box.repo, env, max(args.iterations // 5, 20)),
                  baseline))
        box.stop()

        print('\nrepo-root discovery, the same %d times' % args.iterations)
        print('%-38s %8s %8s %8s %8s' % ('case', 'p50', 'p95', 'p99', 'max'))
        print(row('git rev-parse --git-common-dir',
                  timings(['git', 'rev-parse', '--git-common-dir'], box.repo,
                          dict(env, PATH='/usr/bin:/bin'), args.iterations)))
        print(row('sh: walk up for .git (in-process)',
                  timings(['sh', '-c', 'd=$PWD; while :; do [ -e "$d/.git" ] && break; '
                           '[ "$d" = / ] && break; d=${d%/*}; [ -n "$d" ] || d=/; done'],
                          deep, dict(env, PATH='/usr/bin:/bin'), args.iterations)))
    finally:
        box.close()


if __name__ == '__main__':
    main()
