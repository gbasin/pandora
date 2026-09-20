#!/usr/bin/env python3
"""Bounded Linux/Docker admission probe. Uses its own ledger and named containers."""
import argparse
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import time
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'warm'))
from resource_admission import Scheduler

CONFIG = {'version': 1, 'cpu_millis': 1000, 'memory_mib': 256, 'max_running': 2, 'policy': 'fair'}
DEMAND = {'cpu_millis': 500, 'memory_mib': 128}


def docker(*args):
    return subprocess.run(['sudo', 'docker', *args], check=True, capture_output=True, text=True, timeout=30)


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


def worker(root, boot, invocation, attempt, image, label, events, release):
    path = Path(root) / 'runs' / attempt
    path.mkdir(parents=True)
    name = 'pandora-resource-probe-' + attempt
    scheduler = Scheduler(root, CONFIG, boot_id=boot)
    lease = None
    created = False
    with (path / 'attempt.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        try:
            scheduler.enqueue(attempt, invocation, DEMAND)
            events.put({'event': 'queued', 'attempt': attempt})
            while lease is None:
                lease = scheduler.claim(attempt)
                if lease is None:
                    time.sleep(.03)
            # Reserve the name before creation, so failed acknowledgement still cleans it.
            created = True
            docker('run', '-d', '--name', name, '--label', 'pandora.resource-probe=' + label,
                   '--cpus=.5', '--memory=128m', '--memory-swap=128m', '--pids-limit=64',
                   '--network=none', image, 'node', '-e', 'setTimeout(()=>process.exit(0),30000)')
            config = json.loads(docker('inspect', name, '--format', '{{json .HostConfig}}').stdout)
            assert config['NanoCpus'] == 500000000 and config['Memory'] == 128 * 1024**2
            events.put({'event': 'running', 'attempt': attempt, 'time': time.monotonic(),
                        'cpu_millis': 500, 'memory_mib': 128, 'queue_seconds': lease.queue_seconds})
            if not release.wait(20):
                raise TimeoutError('Probe controller did not release task')
        finally:
            if created:
                docker('rm', '-f', name)
            if lease is not None:
                write(path / 'terminal.json', {'attempt': attempt, 'cleanup_verified': True, 'exit_code': 0})
                scheduler.settle(attempt)
                lease.close()
        events.put({'event': 'finished', 'attempt': attempt, 'time': time.monotonic()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    label = uuid.uuid4().hex
    scheduler = Scheduler(args.root, CONFIG, boot_id=boot)
    ctx = multiprocessing.get_context('spawn')
    events = ctx.Queue()
    children, releases, trace = {}, {}, []
    identities = {name: uuid.uuid4().hex for name in ('suite1', 'suite2', 'suite3', 'focused', 'dead', 'after-dead')}
    groups = {name: uuid.uuid4().hex for name in ('suite', 'focus', 'death', 'after')}

    def start(name, group):
        release = ctx.Event()
        child = ctx.Process(target=worker, args=(str(args.root), boot, groups[group], identities[name], args.image, label, events, release))
        releases[name], children[name] = release, child
        child.start()

    def until(event, names):
        pending = {identities[name] for name in names}
        pending -= {item['attempt'] for item in trace if item['event'] == event}
        deadline = time.monotonic() + 30
        while pending:
            item = events.get(timeout=max(.1, deadline-time.monotonic()))
            trace.append(item)
            if item['event'] == 'running':
                snapshot = scheduler.snapshot()
                running = [row for row in snapshot['requests'] if row['phase'] == 'running']
                assert len(running) <= 2 and sum(row['cpu_millis'] for row in running) <= 1000
                assert sum(row['memory_mib'] for row in running) <= 256
                live = docker('ps', '--filter', 'label=pandora.resource-probe=' + label, '--format', '{{.Names}}').stdout.splitlines()
                assert len(live) <= 2
            if item['event'] == event:
                if item['attempt'] not in pending:
                    raise AssertionError(('unexpected dispatch order', item, pending))
                pending.remove(item['attempt'])

    try:
        scheduler.register(groups['suite'], max_parallel=2)
        start('suite1', 'suite'); until('queued', ['suite1'])
        start('suite2', 'suite'); until('queued', ['suite2'])
        until('running', ['suite1', 'suite2'])
        start('suite3', 'suite'); until('queued', ['suite3'])
        scheduler.register(groups['focus'])
        start('focused', 'focus'); until('queued', ['focused'])
        releases['suite1'].set()
        until('running', ['focused'])
        releases['suite2'].set()
        until('running', ['suite3'])
        releases['focused'].set(); releases['suite3'].set()
        for name in ('suite1', 'suite2', 'suite3', 'focused'):
            children[name].join(10)
            assert children[name].exitcode == 0
        # Drain completion notifications before the death probe.
        while True:
            try: trace.append(events.get_nowait())
            except queue.Empty: break
        scheduler.register(groups['death'])
        start('dead', 'death'); until('queued', ['dead']); until('running', ['dead'])
        os.kill(children['dead'].pid, signal.SIGKILL)
        children['dead'].join(5)
        scheduler.register(groups['after'])
        start('after-dead', 'after'); until('queued', ['after-dead'])
        try:
            item = events.get(timeout=.6)
            raise AssertionError(('dead owner was bypassed', item))
        except queue.Empty:
            pass
        dead = args.root / 'runs' / identities['dead']
        assert not (dead / 'terminal.json').exists()
        assert docker('inspect', 'pandora-resource-probe-' + identities['dead'], '--format', '{{.State.Running}}').stdout.strip() == 'true'
        docker('rm', '-f', 'pandora-resource-probe-' + identities['dead'])
        write(dead / 'admission-cleanup.json', {'attempt': identities['dead'], 'cleanup_verified': True})
        until('running', ['after-dead'])
        releases['after-dead'].set(); children['after-dead'].join(10)
        assert children['after-dead'].exitcode == 0
        assert not (dead / 'terminal.json').exists()
        result = {'config': CONFIG, 'identities': identities, 'groups': groups, 'events': trace,
                  'snapshot': scheduler.snapshot(), 'dead_result_invented': False}
        write(args.root / 'result.json', result)
        print(json.dumps({'passed': True, 'result': str(args.root / 'result.json')}))
    finally:
        for child in children.values():
            if child.is_alive(): child.terminate()
            child.join(5)
            if child.is_alive(): child.kill(); child.join()
        for attempt in identities.values():
            subprocess.run(['sudo','docker','rm','-f','pandora-resource-probe-'+attempt], capture_output=True, timeout=30)
        live = docker('ps', '-a', '--filter', 'label=pandora.resource-probe=' + label, '--format', '{{.Names}}').stdout.strip()
        if live: raise RuntimeError('Probe cleanup is unresolved: ' + live)
        events.close()


if __name__ == '__main__':
    main()
