#!/usr/bin/env python3
"""Bounded real-container ownership check against two experimental admissions."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'warm'))
from resource_admission import Scheduler
from resource_ownership import check, OwnershipUnresolved

CONFIG = {'version': 1, 'cpu_millis': 1000, 'memory_mib': 256, 'max_running': 2, 'policy': 'fair'}


def docker(*args):
    return subprocess.run(['sudo', 'docker', *args], check=True, capture_output=True, text=True, timeout=30)


def scheduler(root):
    return Scheduler(root, CONFIG, boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())


def child(root, identity, image):
    attempt = root / 'runs' / identity
    with (attempt / 'attempt.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        queue = scheduler(root)
        queue.register(identity)
        queue.enqueue(identity, identity, {'cpu_millis': 500, 'memory_mib': 128})
        lease = queue.claim(identity)
        assert lease is not None
        name = 'pandora-warm-' + identity
        (attempt / 'service-cleanup.pending').touch()
        try:
            docker('network', 'create', '--label', 'pandora.attempt=' + identity, name)
            docker('run', '-d', '--name', name, '--network', name, '--label', 'pandora.attempt=' + identity,
                   '--label', 'pandora.workflow=journey', '--cpus=.5', '--memory=128m', '--memory-swap=128m',
                   '--pids-limit=64', image, 'sleep', '60')
            (attempt / 'ready').touch()
            deadline = time.monotonic() + 45
            while not (attempt / 'release').exists():
                if time.monotonic() > deadline:
                    raise TimeoutError('Probe controller did not release owner')
                time.sleep(.1)
        finally:
            docker('rm', '-f', name)
            docker('network', 'rm', name)
            (attempt / 'service-cleanup.pending').unlink()
            (attempt / 'terminal.json').write_text(json.dumps({'attempt': identity, 'cleanup_verified': True}))
            queue.settle(identity)
            lease.close()


def main(root, image, production_root):
    root.mkdir(parents=True, exist_ok=False)
    children, attempts = [], []
    with (production_root / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not docker('ps', '--format', '{{.Names}}').stdout.strip(), 'Worker must be idle'
        try:
            for _ in range(2):
                attempt = root / 'runs' / uuid.uuid4().hex
                attempt.mkdir(parents=True)
                (attempt / 'submission.json').write_text('{"workflow":"journey"}')
                attempts.append(attempt)
                children.append(subprocess.Popen([sys.executable, '-B', __file__, '--child', str(root), attempt.name, image]))
                deadline = time.monotonic() + 20
                while not (attempt / 'ready').exists():
                    if children[-1].poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Probe owner failed to start')
                    time.sleep(.1)
            queue = scheduler(root)
            admitted = {row['attempt'] for row in queue.snapshot()['requests'] if row['phase'] == 'running'}
            check(root, admitted)
            try:
                check(root, {attempts[0].name})
            except OwnershipUnresolved:
                pass
            else:
                raise AssertionError('Live but unadmitted peer was accepted')
            children[0].kill()
            children[0].wait(timeout=10)
            try:
                check(root, admitted)
            except OwnershipUnresolved:
                pass
            else:
                raise AssertionError('Dead resource owner was accepted')
            name = 'pandora-warm-' + attempts[0].name
            assert docker('ps', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}').stdout.strip()
            docker('rm', '-f', name)
            docker('network', 'rm', name)
            (attempts[0] / 'service-cleanup.pending').unlink()
            (attempts[0] / 'admission-cleanup.json').write_text(json.dumps({'attempt': attempts[0].name, 'cleanup_verified': True}))
            assert not (attempts[0] / 'terminal.json').exists()
            admitted = {row['attempt'] for row in queue.snapshot()['requests'] if row['phase'] == 'running'}
            check(root, admitted)
            (attempts[1] / 'release').touch()
            assert children[1].wait(timeout=20) == 0
            result = {'admitted_peers_accepted': True, 'live_unadmitted_peer_blocked': True,
                      'dead_owner_blocked_without_removal': True, 'verified_cleanup_restored_check': True,
                      'attempts': [a.name for a in attempts]}
            (root / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
        finally:
            for process in children:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
            for attempt in attempts:
                name = 'pandora-warm-' + attempt.name
                subprocess.run(['sudo', 'docker', 'rm', '-f', name], capture_output=True, timeout=30)
                subprocess.run(['sudo', 'docker', 'network', 'rm', name], capture_output=True, timeout=30)


if __name__ == '__main__':
    if sys.argv[1] == '--child':
        child(Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    else:
        main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
