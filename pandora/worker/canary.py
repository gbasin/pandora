"""What has to be true before this worker may be called `ready`.

The POC's canary proved the *driver*. This one proves the *worker*: the same
memory checks, plus the nested docker and compose stack a repository's own
services need, plus a real journey in a clone of the journeys golden and a real
surface command in a clone of the surfaces golden, plus the disk quota, the
receipts and the headroom.

Budget: under four minutes for the whole thing, which is why the surfaces check
runs `--list` rather than a browser by default and why the memory hog is given a
512 MiB ceiling it reaches in about six seconds. A gate nobody can afford to run
is a gate nobody runs.

Every check prints one row into the verdict: name, ok, detail, seconds. The
verdict's `ok` is false if any of them is false, and `pandora worker provision`
refuses to write `ready` when it is.
"""
import json
import time
from pathlib import Path

from ..engine.runner import Paths, toolchain_of
from ..executor.incus import IncusDriver
from ..executor.interface import DestroyIncomplete, Limits
from ..executor.memtest import hog

BUDGET_SECONDS = 240


class Checks:
    def __init__(self):
        self.started = time.monotonic()
        self.rows = []

    def add(self, name, ok, detail=''):
        self.rows.append({'check': name, 'ok': bool(ok), 'detail': str(detail)[:400],
                          'at': round(time.monotonic() - self.started, 1)})
        return bool(ok)

    @property
    def failures(self):
        return [row for row in self.rows if not row['ok']]

    @property
    def seconds(self):
        return round(time.monotonic() - self.started, 1)


def load_toolchain(path):
    return toolchain_of(json.loads(Path(path).read_text()))


def run(root, *, journey=None, surfaces=None, source=None, hog_kind='file',
        floor_gib=4, quota_gib=1, keep=False, journey_argv=None, surfaces_argv=None,
        driver=None):
    """Return the verdict dictionary. Never raises for a failed check."""
    paths = Paths(root).ensure()
    driver = driver or IncusDriver(root=paths.root)
    checks = Checks()
    instances = []

    def clone(toolchain, tag, limits, src):
        golden = driver.prepare(toolchain, source=src, log=lambda text: None)
        checks.add('golden %s ready' % golden.name, bool(golden.name),
                   'reused' if golden.reused else 'built in %.1fs' % golden.built_seconds)
        instance = driver.clone(golden, tag, limits=limits)
        instances.append(instance)
        checks.add('clone %s under 2s' % tag, instance.clone_seconds < 2.0,
                   '%.2fs clone, %.2fs start' % (instance.clone_seconds, instance.start_seconds))
        driver.harden(instance, limits)
        return golden, instance

    # --- the machine itself -------------------------------------------------
    code, out, _ = driver.incus('project', 'list', '--format', 'csv', check=False)
    checks.add('project %s exists' % driver.project, driver.project in out)
    room = driver.capacity(floor_gib=floor_gib)
    checks.add('pool headroom above the floor', room.get('ok'),
               '%.2f GiB free of %.1f, floor %d GiB'
               % (room.get('free_gib', 0), room.get('total_bytes', 0) / (1 << 30), floor_gib))

    # --- the journeys golden: docker, compose, a real journey ---------------
    if journey:
        toolchain = load_toolchain(journey)
        limits = Limits(memory_mib=3800, ceiling_mib=5120, cpus_hint=2,
                        wall_seconds=600, disk_gib=quota_gib * 8)
        try:
            _, instance = clone(toolchain, 'canary-journey', limits, source)
            _, docker, _ = driver.sh(
                instance.name,
                'systemctl start docker && for i in $(seq 150); do '
                'docker info >/dev/null 2>&1 && break; sleep 0.2; done; '
                'docker info --format "{{.Driver}} {{.CgroupVersion}}"',
                check=False, timeout=300)
            checks.add('nested dockerd up', 'overlay' in docker, docker.strip())
            _, stack, _ = driver.sh(
                instance.name,
                'cd /work && docker compose -f tools/stack/compose.yml up -d --wait 2>&1 | tail -2; '
                'docker ps --format "{{.Names}}" | wc -l', check=False, timeout=420)
            count = stack.strip().splitlines()[-1] if stack.strip() else '0'
            checks.add('compose stack up', count.isdigit() and int(count) > 0,
                       '%s containers' % count)
            driver.sh(instance.name,
                      'cd /work && docker compose -f tools/stack/compose.yml down -v 2>&1 | tail -1',
                      check=False, timeout=420)
            _, left, _ = driver.sh(instance.name, 'docker ps -q | wc -l', check=False)
            checks.add('compose stack down', left.strip() == '0',
                       '%s containers left' % left.strip())
            argv = journey_argv or ['node', 'tools/validation/journey-runner.mjs', 'run', 'S0-01']
            result = driver.execute(instance, argv, env={'JOURNEY_REPLAY': 'cover'},
                                    cwd='/work', limits=limits)
            checks.add('journey S0-01 passes', result.outcome == 'ok' and result.exit_code == 0,
                       'outcome=%s exit=%s in %.1fs'
                       % (result.outcome, result.exit_code, result.seconds))
            peak = result.usage.memory_peak
            checks.add('run crossed its soft limit without being killed',
                       result.outcome == 'ok' and peak > limits.memory_mib * 1048576 * 0.5,
                       'peak %d MiB, reservation %d, ceiling %d'
                       % (peak // 1048576, limits.memory_mib, limits.ceiling_mib))
            checks.add('run stayed under its ceiling', peak < limits.ceiling_mib * 1048576,
                       'peak %d MiB of %d' % (peak // 1048576, limits.ceiling_mib))
            checks.add(*receipt_of(driver, instance, instances))
        except Exception as error:                                   # noqa: BLE001
            checks.add('journeys golden usable', False, '%s: %s' % (type(error).__name__, error))

    # --- the surfaces golden ------------------------------------------------
    if surfaces:
        toolchain = load_toolchain(surfaces)
        limits = Limits(memory_mib=3800, ceiling_mib=6144, cpus_hint=2,
                        wall_seconds=300, disk_gib=quota_gib * 8)
        try:
            _, instance = clone(toolchain, 'canary-surfaces', limits, source)
            argv = surfaces_argv or ['node', 'tools/validation/surface-runner.mjs', '--list']
            result = driver.execute(instance, argv, env={}, cwd='/work', limits=limits)
            checks.add('surfaces golden answers', result.exit_code == 0,
                       'outcome=%s exit=%s in %.1fs'
                       % (result.outcome, result.exit_code, result.seconds))
            checks.add(*receipt_of(driver, instance, instances))
        except Exception as error:                                   # noqa: BLE001
            checks.add('surfaces golden usable', False, '%s: %s' % (type(error).__name__, error))

    # --- the disk quota -----------------------------------------------------
    # A quota that is set and not enforced is worse than no quota: the pool
    # fills anyway and the operator believes it cannot.
    if journey or surfaces:
        toolchain = load_toolchain(journey or surfaces)
        limits = Limits(memory_mib=512, ceiling_mib=1024, cpus_hint=1,
                        wall_seconds=120, disk_gib=quota_gib)
        try:
            _, instance = clone(toolchain, 'canary-quota', limits, None)
            _, out, _ = driver.sh(
                instance.name,
                'dd if=/dev/zero of=/work/.pandora-quota-probe bs=1M count=%d 2>&1 | tail -1; '
                'rm -f /work/.pandora-quota-probe' % (quota_gib * 1024 + 512),
                check=False, timeout=600)
            refused = any(word in out.lower() for word in
                          ('no space', 'quota exceeded', 'disk quota', 'error writing'))
            checks.add('disk quota refuses an over-limit write', refused,
                       '%dGiB quota, wrote %dMiB: %s'
                       % (quota_gib, quota_gib * 1024 + 512, out.strip()[:160]))
            checks.add(*receipt_of(driver, instance, instances))
        except Exception as error:                                   # noqa: BLE001
            checks.add('disk quota enforced', False, '%s: %s' % (type(error).__name__, error))

    # --- the memory watchdog ------------------------------------------------
    if journey or surfaces:
        toolchain = load_toolchain(journey or surfaces)
        limits = Limits(memory_mib=512, ceiling_mib=512, cpus_hint=1, wall_seconds=120)
        try:
            _, instance = clone(toolchain, 'canary-oom', limits, None)
            mark = time.monotonic()
            result = driver.execute(instance, hog(hog_kind), env={}, cwd='/work', limits=limits)
            elapsed = time.monotonic() - mark
            evidence = {key: value for key, value in result.evidence.items() if key != 'samples'}
            checks.add('file-cache thrash is killed as oom', result.outcome == 'oom',
                       json.dumps(evidence)[:300])
            checks.add('oom verdict inside 60s', elapsed < 60, '%.1fs' % elapsed)
            checks.add('oom verdict carries evidence',
                       bool(evidence.get('reason')) and bool(evidence.get('events')),
                       evidence.get('reason', ''))
            checks.add(*receipt_of(driver, instance, instances))
        except Exception as error:                                   # noqa: BLE001
            checks.add('memory watchdog works', False, '%s: %s' % (type(error).__name__, error))

    if not keep:
        for instance in instances:
            driver.incus('delete', '-f', instance.name, check=False)

    checks.add('canary inside its %ds budget' % BUDGET_SECONDS,
               checks.seconds < BUDGET_SECONDS, '%.1fs' % checks.seconds)
    failures = checks.failures
    return {'ok': not failures, 'checks': checks.rows, 'failures': len(failures),
            'seconds': checks.seconds,
            'reason': '; '.join('%s: %s' % (row['check'], row['detail'] or 'false')
                                for row in failures) or None}


def receipt_of(driver, instance, instances):
    """(name, ok, detail) for destroying one canary instance."""
    try:
        got = driver.destroy(instance)
        instances.remove(instance)
        return ('%s destroy receipt clean' % instance.name, got.clean,
                '%.2fs, leftovers=%s' % (got.seconds, list(got.leftovers)))
    except DestroyIncomplete as error:
        return ('%s destroy receipt clean' % instance.name, False,
                json.dumps(error.receipt, default=str)[:300])
    except Exception as error:                                       # noqa: BLE001
        return ('%s destroy receipt clean' % instance.name, False, str(error)[:200])
