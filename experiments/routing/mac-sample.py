#!/usr/bin/env python3
"""Take one bounded, read-only macOS host-health sample as JSON.

This is a host-level observation for the Pandora CLI evaluation.  It neither
starts nor follows an agent process, so it cannot attribute any measurement to
one agent or establish that the interactive UI remained responsive.
"""
import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import time


COMMAND_TIMEOUT_SECONDS = 1


def command_sample(command):
    """Run one fixed read-only command and retain both its result and duration."""
    started = time.monotonic_ns()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        result = {
            'exit_code': completed.returncode,
            'stdout': completed.stdout.strip(),
            'stderr': completed.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        result = {'timeout': True}
    except OSError as error:
        result = {'error': type(error).__name__, 'message': str(error)}
    result['duration_ms'] = round((time.monotonic_ns() - started) / 1_000_000, 3)
    return result


def memory_free_percent(output):
    """Return the percentage reported by `memory_pressure -Q`, if present."""
    match = re.search(r'System-wide memory free percentage:\s*(\d+)%', output)
    return int(match.group(1)) if match else None


def vm_stat_pages(output):
    """Extract current page counters from the human-readable `vm_stat` report."""
    pages = {}
    for label, amount in re.findall(r'^Pages ([^:]+):\s*([\d,]+)\.', output, re.MULTILINE):
        pages[label.replace(' ', '_')] = int(amount.replace(',', ''))
    return pages


def vm_stat_page_size_bytes(output):
    """Return the `vm_stat` page size, if the report includes one."""
    match = re.search(r'page size of (\d+) bytes', output)
    return int(match.group(1)) if match else None


def vm_stat_counters(output):
    """Extract cumulative VM counters useful for comparing sequential samples."""
    counters = {}
    for label, amount in re.findall(
        r'^(Swapins|Swapouts|Compressions|Decompressions):\s*([\d,]+)\.',
        output,
        re.MULTILINE,
    ):
        counters[label.lower()] = int(amount.replace(',', ''))
    return counters


def shell_spawn_sample(count):
    """Measure an empty non-interactive zsh spawn without evaluating user startup files."""
    durations = []
    failures = []
    for _ in range(count):
        result = command_sample(['/bin/zsh', '-fc', ':'])
        if result.get('exit_code') == 0:
            durations.append(result['duration_ms'])
        else:
            failures.append(result)
    values = {'samples': durations, 'failures': failures}
    if durations:
        values.update({
            'min_ms': min(durations),
            'median_ms': round(statistics.median(durations), 3),
            'max_ms': max(durations),
        })
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--shell-samples', type=int, default=5,
        help='empty zsh spawns to time (1-20, default: 5)',
    )
    args = parser.parse_args()
    if not 1 <= args.shell_samples <= 20:
        parser.error('--shell-samples must be between 1 and 20')

    wall_time = time.time()
    memory = command_sample(['memory_pressure', '-Q'])
    vm_stat = command_sample(['vm_stat'])
    swap = command_sample(['sysctl', '-n', 'vm.swapusage'])
    load = command_sample(['sysctl', '-n', 'vm.loadavg'])
    pressure_level = command_sample(['sysctl', '-n', 'kern.memorystatus_vm_pressure_level'])

    sample = {
        'schema_version': 1,
        'sampled_at_utc': dt.datetime.fromtimestamp(wall_time, dt.timezone.utc).isoformat(),
        'wall_time_unix': wall_time,
        'monotonic_ns': time.monotonic_ns(),
        'host': {
            'memory_pressure': memory,
            'memory_free_percent': memory_free_percent(memory.get('stdout', '')),
            'vm_stat': vm_stat,
            'vm_stat_pages': vm_stat_pages(vm_stat.get('stdout', '')),
            'vm_stat_page_size_bytes': vm_stat_page_size_bytes(vm_stat.get('stdout', '')),
            'vm_stat_counters': vm_stat_counters(vm_stat.get('stdout', '')),
            'swap_usage': swap,
            'load_average': load,
            'memory_pressure_level': pressure_level,
            'shell_spawn': shell_spawn_sample(args.shell_samples),
        },
        'limits': [
            'Host-level signals are not attributable to an individual CLI agent.',
            'Shell spawn latency is a responsiveness proxy, not an interactive UI measurement.',
            'The sampler does not manage processes or poll agent PIDs.',
        ],
    }
    print(json.dumps(sample, sort_keys=True))


if __name__ == '__main__':
    main()
