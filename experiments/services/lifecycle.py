"""Evaluator-only fault injection against the normal journey command path.
Requires a disposable, bootstrapped worktree. Does not run an agent CLI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--repo', required=True, type=Path)
p.add_argument('--state', required=True, type=Path)
p.add_argument('--output', required=True, type=Path)
p.add_argument('--host', required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'warm'))
from transport import query
command = [sys.executable, str(root / 'routing/launch.py'), '--host', a.host,
           '--state', str(a.state), '--', 'pnpm', 'journey', 'S0-01']
key = hashlib.sha256(str(a.repo.resolve()).encode()).hexdigest()
active = a.state / key / 'active.json'
records = []


def start(label):
    log = (a.output / (label + '.log')).open('w')
    child = subprocess.Popen(command, cwd=a.repo, stdout=log, stderr=subprocess.STDOUT)
    return child, log


def ready(child, old):
    started = time.monotonic()
    while time.monotonic() - started < 180:
        if child.poll() is not None:
            raise RuntimeError('Command exited before fault injection')
        if active.exists():
            record = json.loads(active.read_text())
            if record['attempt'] != old:
                state = query(a.host, record['attempt'])
                if 'Workers runtime ready' in state.get('stdout', ''):
                    return record
        time.sleep(2)
    raise TimeoutError('Journey did not reach runtime readiness')


def absent(attempt):
    for kind in ('container', 'network'):
        args = 'ps -a' if kind == 'container' else 'network ls'
        result = subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host,
                                 'sudo docker ' + args + ' --filter label=pandora.attempt=' + attempt + ' --format "{{.ID}}"'],
                                capture_output=True, text=True, check=True, timeout=30)
        if result.stdout.strip():
            raise AssertionError('Owned resources remain: ' + kind)


for mode in ('cancel', 'disconnect', 'worker-kill'):
    old = json.loads(active.read_text())['attempt'] if active.exists() else None
    child, log = start(mode)
    record = ready(child, old)
    attempt = record['attempt']
    print(mode + ': fault at runtime readiness, attempt=' + attempt, flush=True)
    if mode == 'cancel':
        child.send_signal(signal.SIGINT)
        status = child.wait(timeout=90)
        if status != 130:
            raise AssertionError('Cancellation exit: ' + str(status))
        state = query(a.host, attempt)
        if state.get('exit_code') != 130 or not state.get('cleanup_verified'):
            raise AssertionError('Cancellation lacks verified cleanup')
    elif mode == 'disconnect':
        # Kill only the local transport child. The route observes its exit and
        # preserves the active record. This is not an explicit cancellation.
        children = subprocess.check_output(['pgrep', '-P', str(child.pid)], text=True).split()
        if len(children) != 1:
            raise AssertionError('Expected one transport child')
        os.killpg(int(children[0]), signal.SIGKILL)
        status = child.wait(timeout=30)
        if status != 137:
            raise AssertionError('Lost client exit: ' + str(status))
        retry, retry_log = start('disconnect-retry')
        status = retry.wait(timeout=180)
        retry_log.close()
        if status != 0 or json.loads(active.read_text())['attempt'] != attempt:
            raise AssertionError('Retry did not recover the successful existing run')
        state = query(a.host, attempt)
    else:
        subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host,
                        'sudo systemctl kill --kill-whom=main --signal=SIGKILL pandora-worker-' + attempt + '.service'],
                       check=True, timeout=30)
        # Wait for systemd's stop hook, not for a terminal record that the killed
        # worker cannot write. Bound the observer separately.
        for _ in range(30):
            result = subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host,
                                     'cat pandora-warm/runs/' + attempt + '/service-cleanup.json'],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and json.loads(result.stdout).get('verified'):
                break
            time.sleep(2)
        else:
            raise AssertionError('Worker-death cleanup did not finish')
        child.send_signal(signal.SIGINT)
        status = child.wait(timeout=90)
        state = query(a.host, attempt)
        if state.get('cleanup_verified'):
            raise AssertionError('Worker death invented a terminal result')
    log.close()
    absent(attempt)
    records.append({'mode': mode, 'attempt': attempt, 'client_exit': status,
                    'state': {k: v for k, v in state.items() if k not in ('stdout', 'stderr')}})
    (a.output / 'results.json').write_text(json.dumps(records, indent=2) + '\n')
    print(mode + ': verified', flush=True)
