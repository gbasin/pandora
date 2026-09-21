"""What would gate a worker image rebuild.

Every check prints one line and either passes or fails with a legible reason.
The exit code is the number of failed checks, capped at 125. Nothing here is
optional: if any of it is false, the image cannot run a Pandora run.

  python3 canary.py [--keep]
"""
import json
import os
import sys
import time

from incus_driver import IncusDriver, run
from interface import DestroyIncomplete, Limits
from poc import EICHLER, JOURNEY, ROOT, SOURCE

FAILURES = []
STARTED = time.monotonic()


def check(name, condition, detail=''):
    mark = 'ok  ' if condition else 'FAIL'
    print('%s %-34s %6.1fs %s' % (mark, name, time.monotonic() - STARTED, detail), flush=True)
    if not condition:
        FAILURES.append('%s: %s' % (name, detail or 'false'))
    return condition


def main(keep=False):
    driver = IncusDriver(root=ROOT)

    rc, out, _ = run(['incus', '--version'], check=False)
    check('incus present', rc == 0, out.strip())
    rc, out, _ = driver.incus('project', 'list', '--format', 'csv', check=False)
    check('project %s exists' % driver.project, driver.project in out, '')
    rc, out, _ = run(['sudo', 'incus', 'storage', 'list', '--format', 'csv'], check=False)
    check('pool %s exists' % driver.pool, driver.pool in out, '')

    golden = driver.prepare(EICHLER, source=SOURCE)
    check('golden %s ready' % golden.name, bool(golden.name),
          'reused' if golden.reused else 'built in %.1fs' % golden.built_seconds)

    limits = Limits(memory_mib=3800, ceiling_mib=5120, cpus_hint=os.cpu_count() or 1,
                    wall_seconds=1800)
    instance = driver.clone(golden, 'canary', limits=limits)
    check('clone under 2s', instance.clone_seconds < 2.0, '%.2fs' % instance.clone_seconds)
    check('start under 5s', instance.start_seconds < 5.0, '%.2fs' % instance.start_seconds)
    wrote = driver.harden(instance, limits)
    check('cgroup arrangement written',
          all(not str(v).startswith('ERR') for v in wrote.values()), json.dumps(wrote))

    seconds = driver.inject(instance.name, SOURCE, '/work', method='device-rsync')
    check('source injected', seconds < 30, '%.2fs' % seconds)

    _, docker, _ = driver.sh(instance.name,
                             'systemctl start docker && for i in $(seq 100); do '
                             'docker info >/dev/null 2>&1 && break; sleep 0.2; done; '
                             'docker info --format "{{.Driver}} {{.CgroupVersion}}"',
                             check=False, timeout=300)
    check('nested dockerd up', 'overlay' in docker, docker.strip())

    _, stack, _ = driver.sh(instance.name,
                            'cd /work && docker compose -f tools/stack/compose.yml up -d --wait 2>&1 | tail -2; '
                            'docker ps --format "{{.Names}}" | wc -l', check=False, timeout=600)
    containers = stack.strip().splitlines()[-1] if stack.strip() else '0'
    check('compose stack up', containers.isdigit() and int(containers) > 0,
          '%s containers' % containers)
    _, ports, _ = driver.sh(instance.name, 'ss -ltn | tail -n +2 | wc -l', check=False)
    check('fixed ports bound inside the run', ports.strip().isdigit() and int(ports.strip()) > 0,
          '%s listeners' % ports.strip())
    driver.sh(instance.name, 'cd /work && docker compose -f tools/stack/compose.yml down -v 2>&1 | tail -1',
              check=False, timeout=600)
    _, left, _ = driver.sh(instance.name, 'docker ps -q | wc -l', check=False)
    check('compose stack down', left.strip() == '0', '%s containers left' % left.strip())

    result = driver.execute(instance, JOURNEY, env={'JOURNEY_REPLAY': 'cover'},
                            cwd='/work', limits=limits)
    log = (ROOT / 'logs' / 'canary.log')
    check('journey S0-01 passes', result.outcome == 'ok' and result.exit_code == 0,
          'outcome=%s exit=%s in %.1fs' % (result.outcome, result.exit_code, result.seconds))
    check('run stayed under its ceiling',
          result.usage.memory_peak < limits.ceiling_mib * 1048576,
          'peak %d MiB of %d' % (result.usage.memory_peak // 1048576, limits.ceiling_mib))
    check('soft limit was crossed without killing the run',
          result.usage.memory_peak > limits.memory_mib * 1048576 * 0.5,
          'peak %d MiB vs reservation %d' % (result.usage.memory_peak // 1048576, limits.memory_mib))

    receipt = None
    try:
        receipt = driver.destroy(instance)
    except DestroyIncomplete as error:
        check('destroy receipt clean', False, json.dumps(error.receipt))
    if receipt:
        check('destroy receipt clean', receipt.clean,
              'in %.2fs, leftovers=%s' % (receipt.seconds, list(receipt.leftovers)))

    # Hard memory: an over-ceiling run must end with outcome `oom` and evidence.
    hard = Limits(memory_mib=512, ceiling_mib=512, cpus_hint=1, wall_seconds=180)
    driver.incus('delete', '-f', 'run-canary-oom', check=False)
    bad = driver.clone(golden, 'canary-oom', limits=hard)
    driver.harden(bad, hard)
    t0 = time.monotonic()
    hog = ['bash', '-c', 'while :; do cat $(find /work/node_modules -type f -size +8k '
                         '| head -20000) > /dev/null 2>&1; done']
    verdict = driver.execute(bad, hog, cwd='/work', limits=hard)
    elapsed = time.monotonic() - t0
    check('over-ceiling run is killed as oom', verdict.outcome == 'oom',
          'outcome=%s reason=%s' % (verdict.outcome, verdict.evidence.get('reason')))
    check('oom verdict within 60s', elapsed < 60, '%.1fs' % elapsed)
    check('oom verdict carries evidence',
          bool(verdict.evidence.get('reason')) and bool(verdict.evidence.get('events')),
          json.dumps({k: v for k, v in verdict.evidence.items() if k != 'samples'})[:200])
    try:
        bad_receipt = driver.destroy(bad)
        check('oom run destroyed cleanly', bad_receipt.clean, '%.2fs' % bad_receipt.seconds)
    except DestroyIncomplete as error:
        check('oom run destroyed cleanly', False, json.dumps(error.receipt))

    if not keep:
        for name in ('run-canary', 'run-canary-oom'):
            driver.incus('delete', '-f', name, check=False)

    print('\n%d checks failed' % len(FAILURES))
    for failure in FAILURES:
        print('  - ' + failure)
    return min(125, len(FAILURES))


if __name__ == '__main__':
    sys.exit(main('--keep' in sys.argv))
