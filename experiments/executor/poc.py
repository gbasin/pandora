"""The measured runs. Everything here is `python3 poc.py <command>` on the worker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from incus_driver import IncusDriver, run
from interface import Limits, Toolchain, DestroyIncomplete, MemoryExceeded

ROOT = Path(os.environ.get('PANDORA_ROOT', Path.home() / 'incus-exec'))
SOURCE = ROOT / 'acme'

ACME = Toolchain(
    base_image='images:ubuntu/26.04',
    packages=('docker.io', 'docker-compose-v2', 'docker-buildx', 'ca-certificates',
              'curl', 'xz-utils', 'git', 'python3', 'jq', 'rsync'),
    node_version='24.9.0',
    pnpm_version='12.3.4',
    service_images=('postgres:16', 'edoburu/pgbouncer:latest', 'ghcr.io/neondatabase/wsproxy:latest'),
    install_command='pnpm install --frozen-lockfile 2>&1 | tail -2',
    source_id='acme-journey-runner-proxy',
)

JOURNEY = ['node', 'tools/validation/journey-runner.mjs', 'run', 'S0-01']


def host_pressure():
    out = {}
    for resource in ('memory', 'cpu', 'io'):
        try:
            for line in Path('/proc/pressure/' + resource).read_text().splitlines():
                parts = line.split()
                for item in parts[1:]:
                    key, _, value = item.partition('=')
                    if key in ('avg10', 'avg60'):
                        out['%s_%s_%s' % (resource, parts[0], key)] = float(value)
        except OSError:
            pass
    out['loadavg'] = os.getloadavg()[0]
    free = Path('/proc/meminfo').read_text().splitlines()
    out['mem_available_mib'] = next(int(l.split()[1]) // 1024 for l in free if l.startswith('MemAvailable:'))
    return out


def emit(record):
    print(json.dumps(record, sort_keys=True), flush=True)
    with (ROOT / 'logs' / 'poc.jsonl').open('a') as handle:
        handle.write(json.dumps(record, sort_keys=True) + '\n')


def golden(driver):
    result = driver.prepare(ACME, source=SOURCE)
    emit({'event': 'golden', **result.__dict__, 'disk_mib': result.disk_bytes // 1048576})
    return result


def measure_injection(driver, golden_ref):
    """Three ways to put a 375 MiB / 4,852-file tree into a clone, timed."""
    rows = []
    for method in ('tar', 'push', 'device', 'device-rsync'):
        name = 'inject-' + method.replace('-', '')
        driver.incus('delete', '-f', name, check=False)
        t0 = time.monotonic()
        driver.incus('copy', '%s/warm' % golden_ref.name, name, timeout=900)
        driver.incus('start', name, timeout=300)
        driver.wait_ready(name)
        ready = time.monotonic() - t0
        before = driver.qgroup(name)[1]
        try:
            seconds = driver.inject(name, SOURCE, '/work' if method == 'device-rsync' else '/src', method=method)
            probe = '/work' if method == 'device-rsync' else '/src'
            _, files, _ = driver.sh(name, 'find %s -type f -not -path "*/node_modules/*" | wc -l' % probe, check=False)
            _, bytes_, _ = driver.sh(name, 'du -sm --exclude node_modules %s | cut -f1' % probe, check=False)
            run(['sudo', 'btrfs', 'quota', 'rescan', '-w',
                 '/var/lib/incus/storage-pools/' + driver.pool], check=False, timeout=300)
            row = {'method': method, 'seconds': round(seconds, 2),
                   'files': files.strip(), 'mib': bytes_.strip(),
                   'exclusive_delta_mib': (driver.qgroup(name)[1] - before) // 1048576,
                   'clone_ready_s': round(ready, 2)}
        except Exception as error:                     # noqa: BLE001 - recorded, not raised
            row = {'method': method, 'error': str(error)[:300]}
        rows.append(row)
        emit({'event': 'inject', **row})
        driver.incus('delete', '-f', name, check=False)
    return rows


def one_run(driver, golden_ref, run_id, limits, argv=JOURNEY, collect=True, harden=True):
    marks = {}
    t0 = time.monotonic()
    instance = driver.clone(golden_ref, run_id, limits=limits)
    marks['clone_s'] = round(instance.clone_seconds, 2)
    marks['start_s'] = round(instance.start_seconds, 2)
    # The golden already carries the source at its fingerprint; a run rsyncs
    # its own tree over the top so the golden stays reusable across branches.
    marks['inject_s'] = round(driver.inject(instance.name, SOURCE, '/work', method='device-rsync'), 2)
    written = driver.harden(instance, limits) if harden else {}
    log = (ROOT / 'logs' / (run_id + '.log')).open('w')
    result = driver.execute(instance, argv, env={'JOURNEY_REPLAY': 'cover'},
                            cwd='/work', limits=limits,
                            on_log=lambda chunk: (log.write(chunk), log.flush()))
    log.close()
    marks['exec_s'] = round(result.seconds, 2)
    marks['verdict'] = ''.join(line for line in (ROOT / 'logs' / (run_id + '.log')).read_text().splitlines(True)
                               if 'S0-01:' in line or 'stage-routes' in line).strip()[:200]
    files = {}
    if collect:
        files = driver.collect(instance, ['/work/tools/validation'], ROOT / 'out' / run_id)
    receipt = None
    try:
        receipt = driver.destroy(instance)
        marks['destroy_s'] = round(receipt.seconds, 2)
    except DestroyIncomplete as error:
        marks['destroy_error'] = error.receipt
    marks['wall_s'] = round(time.monotonic() - t0, 2)
    record = {'event': 'run', 'run_id': run_id, 'outcome': result.outcome,
              'exit_code': result.exit_code, 'peak_mib': result.usage.memory_peak // 1048576,
              'cpu_usec': result.usage.cpu_usec, 'cgroup': written,
              'host': host_pressure(), 'receipt_clean': bool(receipt and receipt.clean),
              'evidence': {k: v for k, v in result.evidence.items() if k != 'samples'},
              **marks}
    emit(record)
    return record


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'golden'
    (ROOT / 'logs').mkdir(parents=True, exist_ok=True)
    driver = IncusDriver(root=ROOT)
    if command == 'golden':
        golden(driver)
    elif command == 'inject':
        measure_injection(driver, driver.prepare(ACME, source=SOURCE))
    elif command == 'run':
        limits = Limits(memory_mib=int(sys.argv[3]) if len(sys.argv) > 3 else 3072,
                        ceiling_mib=int(sys.argv[4]) if len(sys.argv) > 4 else 5120,
                        cpus_hint=int(sys.argv[5]) if len(sys.argv) > 5 else os.cpu_count())
        one_run(driver, driver.prepare(ACME, source=SOURCE), sys.argv[2], limits)
    else:
        raise SystemExit('unknown command ' + command)


if __name__ == '__main__':
    main()
