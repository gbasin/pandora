"""The memory-limit investigation.

The Firecracker spike found that an Incus instance with `limits.memory.enforce=hard`
(cgroup `memory.max`) LIVELOCKED in reclaim — 349,230 `max` events in 120 s with
`oom_kill 0` — instead of being OOM-killed. This reproduces it, instruments why,
and measures the arrangements that make an over-limit run die instead.

  repro <variant> <cap_mib> [seconds]  one arrangement, no watchdog, full trace
  watchdog <cap_mib>                   the driver's watchdog, time to verdict
  neighbour                            a thrashing run beside a real journey

Variants:
  bare        what Incus writes by itself: memory.max, nothing else
  oomgroup    + memory.oom.group=1
  high        + memory.high at 0.9 * max
  full        + both (what `IncusDriver.harden` writes)
"""
import json
import os
from pathlib import Path
import sys
import threading
import time

from incus_driver import IncusDriver, run
from interface import Limits
from poc import EICHLER, JOURNEY, ROOT, SOURCE, emit, host_pressure, one_run

# Four shapes of over-limit run. They do not behave alike, which turns out to
# be the whole answer to the spike's finding.
HOGS = {
    # The spike's exact command. `Buffer.alloc(n)` for a large n is calloc of a
    # fresh mmap, so the pages are never written and never become resident:
    # this grows address space, not charge.
    'spike': ['node', '-e', 'const a=[];for(;;){a.push(Buffer.alloc(64*1024*1024));}'],
    # The same loop with the pages actually touched: real anonymous demand.
    'anon': ['node', '-e', 'const a=[];for(;;){a.push(Buffer.alloc(64*1024*1024).fill(1));}'],
    # What an over-limit *real* run looks like: a working set of file pages
    # several times the cap, read round and round. Reclaim always succeeds, so
    # the charge never fails, so the kernel never OOMs.
    'file': ['bash', '-c', 'while :; do cat $(find /work/node_modules -type f -size +8k | head -20000) > /dev/null 2>&1; done'],
    # Both at once, which is what a build under a too-small ceiling really is.
    'mixed': ['bash', '-c', 'node -e "const a=[];for(;;){a.push(Buffer.alloc(16*1024*1024).fill(1));}" & '
                            'while :; do cat $(find /work/node_modules -type f -size +8k | head -20000) > /dev/null 2>&1; done'],
}
HOG = HOGS['anon']


def arrangement(driver, name, variant, cap_mib):
    """Write one cgroup arrangement and report what actually landed."""
    path = driver.cgroup(name)
    wrote = {}

    def put(leaf, value):
        rc, _, err = run(['sudo', 'tee', os.path.join(path, leaf)], stdin=str(value).encode(), check=False)
        wrote[leaf] = str(value) if rc == 0 else 'ERR:' + err.strip()[:60]

    if variant != 'raw':
        put('memory.swap.max', 0)
    if variant in ('oomgroup', 'full'):
        put('memory.oom.group', 1)
    if variant in ('high', 'full'):
        put('memory.high', int(cap_mib * 0.9) * 1024 * 1024)
    return path, wrote


def memory_stat(path, keys):
    rc, out, _ = run(['sudo', 'cat', os.path.join(path, 'memory.stat')], check=False)
    stat = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in keys:
            stat[parts[0]] = int(parts[1])
    return stat


STAT_KEYS = {'anon', 'file', 'slab', 'pgscan', 'pgsteal', 'pgmajfault',
             'workingset_refault_anon', 'workingset_refault_file', 'shmem'}


def trace(driver, instance, path, seconds, interval=0.5):
    """Sample the cgroup until the hog dies or the window closes."""
    rows, t0 = [], time.monotonic()
    while time.monotonic() - t0 < seconds:
        use = driver.usage(instance)
        stat = memory_stat(path, STAT_KEYS)
        rows.append({'t': round(time.monotonic() - t0, 1),
                     'current_mib': use.memory_current // 1048576,
                     'peak_mib': use.memory_peak // 1048576,
                     'events': use.events,
                     'psi_some10': use.pressure.get('memory_some_avg10', 0.0),
                     'psi_full10': use.pressure.get('memory_full_avg10', 0.0),
                     'anon_mib': stat.get('anon', 0) // 1048576,
                     'file_mib': stat.get('file', 0) // 1048576,
                     'pgscan': stat.get('pgscan', 0), 'pgsteal': stat.get('pgsteal', 0),
                     'refault_file': stat.get('workingset_refault_file', 0),
                     'pids': use.processes, 'host': host_pressure()})
        rc, out, _ = driver.incus('exec', instance.name, '--', 'bash', '-c',
                                  'cat /pandora/rc 2>/dev/null', check=False, timeout=60)
        if out.strip().isdigit():
            rows[-1]['rc'] = int(out.strip())
            break
        time.sleep(interval)
    return rows


def repro(driver, variant='bare', cap_mib=512, seconds=120, hog='anon'):
    golden = driver.prepare(EICHLER, source=SOURCE)
    run_id = 'mem-%s-%s' % (hog, variant)
    limits = Limits(memory_mib=cap_mib, ceiling_mib=cap_mib, cpus_hint=1, wall_seconds=seconds + 60)
    driver.incus('delete', '-f', 'run-' + run_id, check=False)
    instance = driver.clone(golden, run_id, limits=limits)
    path, wrote = arrangement(driver, instance.name, variant, cap_mib)
    # Start the hog detached, then watch without interfering.
    driver.start(instance.name, HOGS[hog], cwd='/work', docker=False)
    rows = trace(driver, instance, path, seconds)
    last = rows[-1]
    rc, out, _ = driver.incus('exec', instance.name, '--', 'bash', '-c',
                              'tail -c 400 /pandora/log 2>/dev/null; echo; cat /pandora/rc 2>/dev/null',
                              check=False, timeout=60)
    _, dmesg, _ = run(['sudo', 'dmesg', '-T', '--level=err,warn,info', '--since', '-3min'], check=False)
    killed = [l for l in dmesg.splitlines() if 'oom' in l.lower() or 'Killed process' in l]
    verdict = {'event': 'memrepro', 'hog': hog, 'variant': variant, 'cap_mib': cap_mib,
               'wrote': wrote, 'window_s': seconds,
               'died': 'rc' in last, 'rc': last.get('rc'),
               'max_events': last['events'].get('max', 0),
               'oom': last['events'].get('oom', 0),
               'oom_kill': last['events'].get('oom_kill', 0),
               'oom_group_kill': last['events'].get('oom_group_kill', 0),
               'high_events': last['events'].get('high', 0),
               'psi_full10': last['psi_full10'], 'psi_some10': last['psi_some10'],
               'anon_mib': last['anon_mib'], 'file_mib': last['file_mib'],
               'pgscan': last['pgscan'], 'pgsteal': last['pgsteal'],
               'refault_file': last['refault_file'],
               'host_mem_avail_mib': last['host']['mem_available_mib'],
               'host_psi_full10': last['host']['memory_full_avg10'],
               'tail': out.strip()[-300:], 'dmesg_oom': killed[-3:]}
    emit(verdict)
    (ROOT / 'logs' / ('memtrace-%s-%s.json' % (hog, variant))).write_text(json.dumps(rows, indent=1))
    driver.incus('delete', '-f', instance.name, check=False)
    return verdict


def watchdog(driver, cap_mib=512, hog='file'):
    """The same hog, with the driver supervising. Time from start to verdict."""
    golden = driver.prepare(EICHLER, source=SOURCE)
    run_id = 'mem-watchdog-' + hog
    driver.incus('delete', '-f', 'run-' + run_id, check=False)
    limits = Limits(memory_mib=cap_mib, ceiling_mib=cap_mib, cpus_hint=1, wall_seconds=300)
    instance = driver.clone(golden, run_id, limits=limits)
    wrote = driver.harden(instance, limits)
    t0 = time.monotonic()
    result = driver.execute(instance, HOGS[hog], cwd='/work', limits=limits)
    verdict = {'event': 'memwatchdog', 'hog': hog, 'cap_mib': cap_mib, 'wrote': wrote,
               'outcome': result.outcome, 'exit_code': result.exit_code,
               'seconds_to_verdict': round(time.monotonic() - t0, 1),
               'evidence': {k: v for k, v in result.evidence.items() if k != 'samples'},
               'last_samples': result.evidence.get('samples', [])[-6:]}
    emit(verdict)
    driver.incus('delete', '-f', instance.name, check=False)
    return verdict


def neighbour(driver, cap_mib=512, hog='file'):
    """A run thrashing at its ceiling must not cost the run beside it."""
    golden = driver.prepare(EICHLER, source=SOURCE)
    driver.incus('delete', '-f', 'run-mem-bad', check=False)
    bad_limits = Limits(memory_mib=cap_mib, ceiling_mib=cap_mib, cpus_hint=1, wall_seconds=300)
    bad = driver.clone(golden, 'mem-bad', limits=bad_limits)
    driver.harden(bad, bad_limits)
    results = {}

    def lane(key, work):
        try:
            results[key] = work()
        except Exception as error:                     # noqa: BLE001 - recorded, not raised
            results[key] = {'outcome': 'driver-error', 'error': repr(error)[:300],
                            'seconds': 0.0, 'evidence': {}, 'exec_s': 0.0,
                            'peak_mib': 0, 'verdict': ''}

    def hog():
        lane('bad', lambda: driver.execute(bad, HOGS[hog], cwd='/work', limits=bad_limits).__dict__)

    def good():
        lane('good', lambda: one_run(driver, golden, 'mem-good',
                                     Limits(memory_mib=4096, ceiling_mib=5120, cpus_hint=4)))

    threads = [threading.Thread(target=hog), threading.Thread(target=good)]
    t0 = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    emit({'event': 'memneighbour', 'hog': hog, 'cap_mib': cap_mib,
          'bad_outcome': results['bad']['outcome'],
          'bad_seconds': round(results['bad']['seconds'], 1),
          'bad_evidence': {k: v for k, v in results['bad']['evidence'].items() if k != 'samples'},
          'good_outcome': results['good']['outcome'],
          'good_verdict': results['good']['verdict'],
          'good_exec_s': results['good']['exec_s'],
          'good_peak_mib': results['good']['peak_mib'],
          'wall_s': round(time.monotonic() - t0, 1)})
    driver.incus('delete', '-f', bad.name, check=False)


if __name__ == '__main__':
    driver = IncusDriver(root=ROOT)
    command = sys.argv[1]
    if command == 'repro':
        repro(driver, sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 512,
              int(sys.argv[4]) if len(sys.argv) > 4 else 120,
              sys.argv[5] if len(sys.argv) > 5 else 'anon')
    elif command == 'watchdog':
        watchdog(driver, int(sys.argv[2]) if len(sys.argv) > 2 else 512,
                 sys.argv[3] if len(sys.argv) > 3 else 'file')
    elif command == 'neighbour':
        neighbour(driver, int(sys.argv[2]) if len(sys.argv) > 2 else 512,
                  sys.argv[3] if len(sys.argv) > 3 else 'file')
    else:
        raise SystemExit('unknown command ' + command)
