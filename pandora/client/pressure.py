"""Host pressure, and the local admission pause it drives.

`assertHealthy` was Pueue's blunt answer to a real question: after something has
gone wrong on this machine, do not let an agent pile the next job on top. The
local lane dropped it, because the failure it actually guarded -- a task marked
dead while its children ran -- cannot happen to a supervisor that holds the
process group. What it did *not* replace was the other half: a Mac that is
already thrashing does not get faster because a thirteenth job was admitted.

So the queue gains one more gate, and it is about the machine rather than about
any run. Memory admission asks "does the reservation fit in the budget"; this
asks "is the host in a state where starting anything is a mistake". Both are
pre-accept, so a job held here has provably not run, and a job that waits too
long exits 70 rather than being started anyway -- the whole point is that the
alternative to waiting is not running.

The probes are deliberately shallow and platform-honest:

* **macOS** -- `memory_pressure -Q` for the kernel's own free percentage (3 ms),
  `sysctl vm.swapusage` for swap in use, `vm_stat` as the arithmetic fallback
  when `memory_pressure` is absent. Raw `Pages free` is not a signal on macOS:
  on a healthy 16 GiB Mac it sits near 0.4 %, because the OS uses what it has.
* **Linux** -- `/proc/pressure/memory`, which is the measurement the whole idea
  is named after, plus `/proc/meminfo` for swap.

Swap *growth* rather than swap *level* is the load-bearing signal on both. A Mac
with 4 GiB of swap sitting still is a Mac that swapped yesterday; a Mac adding
300 MiB a minute is one that is going down now.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULTS = {
    'enabled': True,
    'sample_seconds': 3.0,
    # Growth, measured between consecutive samples and extrapolated to a minute.
    'swap_growth_mib_per_minute': 256,
    # Linux PSI: the fraction of the last 10 s in which *every* task stalled on
    # memory. Anything sustained above ~20 is a machine that is not working.
    'psi_full_avg10': 20.0,
    # macOS: the kernel's own free percentage, or the vm_stat approximation.
    'free_percent': 5.0,
    # Load average per core. Generous on purpose: a compile is allowed to be
    # busy, and this is the backstop signal rather than the interesting one.
    'load_per_cpu': 8.0,
    # How long one job may wait for the machine to recover before it is told no.
    'max_wait_seconds': 300.0,
}

NUMBER = re.compile(r'[-+]?\d+(?:\.\d+)?')


def _run(argv, timeout=5.0):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _first_number(text, default=None):
    match = NUMBER.search(text or '')
    return float(match.group()) if match else default


def darwin_reading():
    """What this Mac will say about itself, in about 6 ms."""
    reading = {'platform': 'darwin', 'swap_used_mib': None, 'free_percent': None,
               'psi_full_avg10': None}
    swap = _run(['sysctl', '-n', 'vm.swapusage'])
    if swap:
        match = re.search(r'used\s*=\s*([\d.]+)([MG])', swap)
        if match:
            reading['swap_used_mib'] = float(match.group(1)) * (1024 if match.group(2) == 'G' else 1)
    quick = _run(['memory_pressure', '-Q'])
    if quick and 'free percentage' in quick:
        for line in quick.splitlines():
            if 'free percentage' in line:
                reading['free_percent'] = _first_number(line.split(':')[-1])
    if reading['free_percent'] is None:
        stats = _run(['vm_stat'])
        if stats:
            pages = {}
            for line in stats.splitlines():
                key, _, rest = line.partition(':')
                value = _first_number(rest)
                if value is not None:
                    pages[key.strip().lower()] = value
            total = sum(value for key, value in pages.items() if key.startswith('pages '))
            # Reclaimable, not free: inactive and speculative pages are what the
            # OS hands back the moment anything asks.
            spare = sum(pages.get('pages ' + name, 0.0)
                        for name in ('free', 'inactive', 'speculative', 'purgeable'))
            if total > 0:
                reading['free_percent'] = 100.0 * spare / total
    return reading


def linux_reading():
    reading = {'platform': 'linux', 'swap_used_mib': None, 'free_percent': None,
               'psi_full_avg10': None}
    try:
        for line in Path('/proc/pressure/memory').read_text().splitlines():
            if line.startswith('full'):
                match = re.search(r'avg10=([\d.]+)', line)
                if match:
                    reading['psi_full_avg10'] = float(match.group(1))
    except OSError:
        pass
    try:
        info = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, rest = line.partition(':')
            info[key.strip()] = _first_number(rest, 0.0)
        if info.get('SwapTotal'):
            reading['swap_used_mib'] = (info['SwapTotal'] - info.get('SwapFree', 0)) / 1024.0
        if info.get('MemTotal'):
            spare = info.get('MemAvailable', info.get('MemFree', 0))
            reading['free_percent'] = 100.0 * spare / info['MemTotal']
    except OSError:
        pass
    return reading


def read_host():
    reading = darwin_reading() if sys.platform == 'darwin' else linux_reading()
    try:
        reading['load_per_cpu'] = os.getloadavg()[0] / float(os.cpu_count() or 1)
    except (OSError, AttributeError):                  # pragma: no cover - exotic platform
        reading['load_per_cpu'] = None
    return reading


class Paused(Exception):
    """The machine stayed bad for the whole of this job's wait.

    Not a queue timeout: the memory was there, the exclusivity rules were clear,
    and the machine was still in no state to be given more work. Exit 70, never
    a run anyway.
    """


class Gate:
    """Is the local lane open? Sampled lazily, counted for `pandora stats`.

    Lazily, because a background thread sampling a healthy Mac every three
    seconds forever is exactly the kind of cost this project keeps refusing
    elsewhere. Nothing asks whether the lane is open unless something wants in,
    or a person typed `pandora ps`.
    """

    def __init__(self, config=None, *, reader=read_host, clock=time.monotonic, store=None):
        self.config = dict(DEFAULTS)
        self.config.update({key: value for key, value in (config or {}).items()
                            if key in DEFAULTS})
        self.reader = reader
        self.clock = clock
        self.store = Path(store) if store else None
        self.at = None
        self.reading = None
        self.previous = None
        self.growth = None
        self.evidence = None
        self.since = None
        self.counters = {'episodes': 0, 'paused_seconds': 0.0, 'jobs_delayed': 0,
                         'jobs_refused': 0, 'last_evidence': None}
        self._load()

    # -- sampling ----------------------------------------------------------

    def sample(self, force=False):
        """One reading, at most every `sample_seconds`. Returns the evidence."""
        now = self.clock()
        if not self.config['enabled']:
            self.evidence = None
            return None
        if not force and self.at is not None and now - self.at < self.config['sample_seconds']:
            return self.evidence
        reading = self.reader() or {}
        if self.previous is not None and reading.get('swap_used_mib') is not None:
            span = now - self.previous[0]
            before = self.previous[1]
            if span >= 1.0 and before is not None:
                self.growth = (reading['swap_used_mib'] - before) * 60.0 / span
        self.previous = (now, reading.get('swap_used_mib'))
        self.at, self.reading = now, reading
        self._verdict(reading, now)
        return self.evidence

    def _verdict(self, reading, now):
        was = self.evidence
        self.evidence = self.judge(reading, self.growth)
        if self.evidence and not was:
            self.since = now
            self.counters['episodes'] += 1
            self.counters['last_evidence'] = self.evidence
            self._save()
        elif was and not self.evidence:
            self.counters['paused_seconds'] = round(
                self.counters['paused_seconds'] + (now - (self.since or now)), 1)
            self.since = None
            self._save()

    def judge(self, reading, growth):
        """The evidence sentence, or None. Order is worst-first, not truest-first."""
        psi = reading.get('psi_full_avg10')
        if psi is not None and psi >= self.config['psi_full_avg10']:
            return ('memory stall PSI full avg10 %.1f, limit %.1f'
                    % (psi, self.config['psi_full_avg10']))
        limit = self.config['swap_growth_mib_per_minute']
        if growth is not None and limit and growth >= limit:
            return 'swap growing %.0f MiB/min, limit %d' % (growth, limit)
        free = reading.get('free_percent')
        if free is not None and free <= self.config['free_percent']:
            return ('%.1f%% of memory free, limit %.1f%%' % (free, self.config['free_percent']))
        load = reading.get('load_per_cpu')
        if load is not None and self.config['load_per_cpu'] and load >= self.config['load_per_cpu']:
            return ('load average %.1f per core, limit %.1f'
                    % (load, self.config['load_per_cpu']))
        return None

    # -- what the queue and the CLI ask ------------------------------------

    def closed(self):
        """The evidence if the lane is shut, else None. This is the whole API."""
        return self.sample()

    def delayed(self):
        self.counters['jobs_delayed'] += 1
        self._save()

    def refused(self):
        self.counters['jobs_refused'] += 1
        self._save()

    def state(self):
        seconds = self.counters['paused_seconds']
        if self.evidence and self.since is not None:
            seconds = round(seconds + (self.clock() - self.since), 1)
        return {'enabled': bool(self.config['enabled']), 'paused': bool(self.evidence),
                'evidence': self.evidence, 'since': self.since,
                'max_wait_seconds': self.config['max_wait_seconds'],
                'paused_seconds': seconds, **{key: value for key, value in self.counters.items()
                                              if key != 'paused_seconds'}}

    # -- counters that survive a restart -----------------------------------

    def _load(self):
        if self.store is None or not self.store.is_file():
            return
        try:
            saved = json.loads(self.store.read_text())
        except (OSError, ValueError):
            return
        for key in self.counters:
            if key in saved:
                self.counters[key] = saved[key]

    def _save(self):
        if self.store is None:
            return
        try:
            self.store.write_text(json.dumps(self.counters, sort_keys=True) + '\n')
        except OSError:
            pass                       # counters are diagnostics; never fail a run for them
