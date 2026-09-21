"""Configured multi-slot worker admission with one queue clock per invocation."""
import hashlib
import json
from pathlib import Path
import time

from admission import receipt, QueueUnavailable
from resource_admission import Scheduler, InvocationStopped, SchedulerUnavailable
from snapshot import encode
from worker_config import validate, identity


def scheduler(root, config):
    config = validate(config)
    return Scheduler(root, config['scheduler'], boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def group(snapshot, invocation):
    return next(row for row in snapshot['invocations'] if row['identity'] == invocation)


def register(root, submitted, invocation):
    queue = scheduler(root, submitted['worker_config'])
    queue.register(invocation, queue_budget=submitted['queue_timeout_seconds'],
                   max_parallel=submitted['worker_config']['max_parallel'] if submitted.get('parent_attempt') or submitted.get('workflow') in ('suite-run', 'surface-run') else 1)
    return queue


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def dependency_identity(attempt, manifest):
    recipe = (attempt / 'runtime.Dockerfile').read_text()
    entries = [e for e in manifest if Path(e['path']).name == 'package.json'
               or e['path'] in {'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc', '.pnpmfile.cjs', 'pnpmfile.cjs'}
               or e['path'].startswith('patches/')]
    key = hashlib.sha256(b'deps-recipe-v3' + recipe.encode() + encode(entries)).hexdigest()
    return recipe, entries, 'pandora-deps:' + key


class Lease:
    def __init__(self, attempt, queue, invocation, inner, config, elapsed):
        self.attempt, self.queue, self.invocation = attempt, queue, invocation
        self.inner, self.ticket, self.waited = inner, inner.ticket, elapsed
        self.config = config

    def admitted(self):
        return {r['attempt'] for r in self.queue.snapshot()['requests'] if r['phase'] == 'running'}

    def close(self):
        try:
            if receipt(self.attempt / 'terminal.json', self.attempt.name):
                self.queue.settle(self.attempt.name)
        finally:
            self.inner.close()


def acquire(attempt, submitted, demand):
    root = attempt.parent.parent
    config = validate(submitted['worker_config'])
    invocation = submitted.get('parent_attempt', attempt.name)
    queue = register(root, submitted, invocation)
    started = time.monotonic()
    write(attempt / 'queue-start.json', {'monotonic': started})
    ticket = queue.enqueue(attempt.name, invocation, demand)
    last_report = float('-inf')
    print(f'[pandora] queued remotely; request {attempt.name}; invocation {invocation}; ticket {ticket}; '
          f'limits {demand["cpu_millis"]}m CPU/{demand["memory_mib"]} MiB RAM; no local validation', flush=True)
    while True:
        if (attempt / 'cancel.request').exists():
            raise KeyboardInterrupt
        inner = None
        try:
            inner = queue.claim(attempt.name)
            snapshot = queue.snapshot()
            current = group(snapshot, invocation)
            report = {'mode': 'resource', 'invocation': invocation, 'ticket': ticket,
                      'waited': time.monotonic() - started,
                      'invocation_waited': current['waited'], 'acquired': inner is not None,
                      'config_digest': identity(config), 'demand': demand}
            write(attempt / 'queue.json', report)
            if inner is not None:
                print(f'[pandora] running remotely; request {attempt.name}; invocation queue used {current["waited"]:.1f}s', flush=True)
                return Lease(attempt, queue, invocation, inner, config, report['waited'])
        except InvocationStopped as error:
            current = group(queue.snapshot(), invocation)
            write(attempt / 'queue.json', {'mode': 'resource', 'invocation': invocation, 'ticket': ticket,
                'waited': time.monotonic() - started, 'invocation_waited': current['waited'], 'acquired': False,
                'config_digest': identity(config), 'demand': demand, 'stopped': error.reason})
            raise QueueUnavailable('Invocation stopped: ' + error.reason + '; no replacement or local validation started') from error
        except SchedulerUnavailable as error:
            if inner is not None:
                inner.close()
            raise QueueUnavailable(str(error)) from error
        except BaseException:
            if inner is not None:
                inner.close()
            raise
        if time.monotonic() - last_report >= 10:
            running = sum(r['phase'] == 'running' for r in snapshot['requests'])
            print(f'[pandora] queued; {running}/{config["scheduler"]["max_running"]} slots occupied; '
                  f'CPU/RAM/disk/builder reservations apply; invocation queue {current["waited"]:.1f}s/{current["budget"]:g}s', flush=True)
            last_report = time.monotonic()
        time.sleep(.25)
