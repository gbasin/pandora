"""Concurrency and CPU-soft measurements on the real box.

  conc <N> [cpus_hint]      N concurrent S0-01 clones under learned reservations
  mixed [priority]          2 journeys + one CPU-heavy job, with/without weight

Every number here is measured, never modelled. The host sampler runs for the
whole window so the PSI columns describe the episode rather than its end.
"""
import json
import os
import statistics
import sys
import threading
import time

from admission import Admission, Store, classify
from incus_driver import IncusDriver
from interface import Limits
from poc import EICHLER, JOURNEY, ROOT, SOURCE, emit, host_pressure, one_run

HEAVY = ['pnpm', 'exec', 'turbo', 'run', 'typecheck', '--force', '--concurrency=4']


class Sampler(threading.Thread):
    """Host pressure for the length of a window, not just at its end."""

    def __init__(self, interval=1.0):
        super().__init__(daemon=True)
        self.interval, self.rows, self.stopped = interval, [], threading.Event()

    def run(self):
        while not self.stopped.wait(self.interval):
            self.rows.append(host_pressure())

    def summary(self):
        self.stopped.set()
        if not self.rows:
            return {}
        out = {}
        for key in ('memory_some_avg10', 'memory_full_avg10', 'cpu_some_avg10',
                    'io_some_avg10', 'io_full_avg10', 'loadavg'):
            values = [row[key] for row in self.rows if key in row]
            if values:
                out[key + '_max'] = round(max(values), 2)
                out[key + '_mean'] = round(statistics.fmean(values), 2)
        out['mem_available_mib_min'] = min(row['mem_available_mib'] for row in self.rows)
        out['samples'] = len(self.rows)
        return out


def concurrent(driver, count, cpus_hint=None, reservation=3800, ceiling=5120, tag='', force=False):
    golden = driver.prepare(EICHLER, source=SOURCE)
    # 15 GiB host, 1 GiB left outside the runs: the budget admission holds.
    budget = 14336
    if force:
        # Deliberate oversubscription: admission would refuse this, and the
        # point of running it anyway is to measure what it is protecting.
        budget = max(budget, count * reservation + 1024)
    admission = Admission(budget_mib=budget, max_running=count)
    store = admission.store
    for _ in range(3):
        store.record('eichler', 'journey', reservation, 'ok')
    # The policy's own classifier, applied to the observed peaks: `medium`
    # (4096 MiB) would make the ceiling bind before the reservation does.
    store.set_class('eichler', 'journey', classify(store.peaks('eichler', 'journey')))
    hint = cpus_hint or max(1, (os.cpu_count() or 1) // count)
    decisions, results, lock = [], {}, threading.Lock()

    for index in range(count):
        decisions.append(admission.admit('c%d' % index, 'eichler', 'journey'))
    admitted = [d for d in decisions if d['admitted']]

    def lane(index, decision):
        limits = Limits(memory_mib=decision['reservation_mib'],
                        ceiling_mib=decision['ceiling_mib'],
                        cpus_hint=hint, wall_seconds=2400)
        record = one_run(driver, golden, '%sn%d-%d' % (tag, count, index), limits)
        with lock:
            results[index] = record

    sampler = Sampler()
    sampler.start()
    t0 = time.monotonic()
    threads = [threading.Thread(target=lane, args=(i, d)) for i, d in enumerate(admitted)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.monotonic() - t0
    host = sampler.summary()

    rows = [results[i] for i in sorted(results)]
    passed = sum(1 for row in rows if row['outcome'] == 'ok' and '45/45' in row.get('verdict', ''))
    emit({'event': 'concurrency', 'n': count, 'admitted': len(admitted), 'forced': force,
          'budget_mib': budget,
          'refused': [d for d in decisions if not d['admitted']],
          'cpus_hint': hint, 'reservation_mib': admitted[0]['reservation_mib'] if admitted else 0,
          'ceiling_mib': admitted[0]['ceiling_mib'] if admitted else 0,
          'size_class': admitted[0]['size_class'] if admitted else '', 'wall_s': round(wall, 1), 'passed': passed,
          'exec_s': [row['exec_s'] for row in rows],
          'exec_s_median': round(statistics.median([row['exec_s'] for row in rows]), 1) if rows else 0,
          'clone_s': [row['clone_s'] for row in rows],
          'start_s': [row['start_s'] for row in rows],
          'inject_s': [row['inject_s'] for row in rows],
          'destroy_s': [row.get('destroy_s') for row in rows],
          'peak_mib': [row['peak_mib'] for row in rows],
          'peak_mib_sum': sum(row['peak_mib'] for row in rows),
          'outcomes': [row['outcome'] for row in rows],
          'host': host})


def mixed(driver, priority=None):
    """Two journeys beside a CPU-heavy job. CPU is soft, so nothing is capped."""
    golden = driver.prepare(EICHLER, source=SOURCE)
    hint = os.cpu_count() or 1
    results, lock = {}, threading.Lock()
    tag = 'p%s' % (priority if priority is not None else 'none')

    def journey(index):
        record = one_run(driver, golden, 'mix%s-j%d' % (tag, index),
                         Limits(memory_mib=3800, ceiling_mib=5120, cpus_hint=hint,
                                cpu_weight=100, wall_seconds=2400))
        with lock:
            results['journey%d' % index] = record

    def heavy():
        record = one_run(driver, golden, 'mix%s-heavy' % tag,
                         Limits(memory_mib=3800, ceiling_mib=5120, cpus_hint=4,
                                cpu_weight=priority if priority is not None else 100,
                                wall_seconds=2400),
                         argv=HEAVY, collect=False)
        with lock:
            results['heavy'] = record

    sampler = Sampler()
    sampler.start()
    t0 = time.monotonic()
    threads = [threading.Thread(target=journey, args=(0,)),
               threading.Thread(target=journey, args=(1,)),
               threading.Thread(target=heavy)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    emit({'event': 'mixed', 'priority': priority, 'cpus_hint': hint,
          'wall_s': round(time.monotonic() - t0, 1),
          'journey_exec_s': [results['journey0']['exec_s'], results['journey1']['exec_s']],
          'journey_outcomes': [results['journey0']['outcome'], results['journey1']['outcome']],
          'journey_verdicts': [results['journey0'].get('verdict', '')[:60],
                               results['journey1'].get('verdict', '')[:60]],
          'heavy_exec_s': results['heavy']['exec_s'],
          'heavy_outcome': results['heavy']['outcome'],
          'heavy_exit': results['heavy']['exit_code'],
          'peak_mib': {k: v['peak_mib'] for k, v in results.items()},
          'cpu_usec': {k: v['cpu_usec'] for k, v in results.items()},
          'host': sampler.summary()})


if __name__ == '__main__':
    driver = IncusDriver(root=ROOT)
    command = sys.argv[1]
    if command == 'conc':
        concurrent(driver, int(sys.argv[2]),
                   int(sys.argv[3]) if len(sys.argv) > 3 else None,
                   tag=sys.argv[4] if len(sys.argv) > 4 else '',
                   force='--force' in sys.argv)
    elif command == 'mixed':
        mixed(driver, int(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        raise SystemExit('unknown command ' + command)
