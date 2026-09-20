"""Verify cancellation and client-loss recovery through the optimized transport."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument('--repo', type=Path, required=True)
p.add_argument('--state', type=Path, required=True)
p.add_argument('--profile', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--host', required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
base = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(base / 'warm'))
from transport import query
key = hashlib.sha256(str(a.repo.resolve()).encode()).hexdigest()
active = a.state / key / 'active.json'
command = [sys.executable, '-B', str(base / 'routing/launch.py'), '--host', a.host,
           '--state', str(a.state), '--docker-profile', str(a.profile), '--', 'docker', 'run',
           '--rm', 'compiled:test', 'node', '-e',
           "console.log('TRANSPORT_READY');setTimeout(()=>require('./check.cjs'),20000)"]
results = []
for mode in ['cancel', 'disconnect']:
    previous = json.loads(active.read_text())['attempt'] if active.exists() else None
    with (a.output / (mode + '.log')).open('w') as log:
        child = subprocess.Popen(command, cwd=a.repo, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(45):
            if child.poll() is not None:
                raise AssertionError('Request exited before injection')
            if active.exists():
                record = json.loads(active.read_text())
                if record['attempt'] != previous and 'TRANSPORT_READY' in query(a.host, record['attempt']).get('stdout', ''):
                    break
            time.sleep(1)
        else:
            raise TimeoutError('No readiness evidence')
        attempt = record['attempt']
        if mode == 'cancel':
            child.send_signal(signal.SIGINT)
            assert child.wait(timeout=90) == 130
        else:
            children = subprocess.check_output(['pgrep', '-P', str(child.pid)], text=True).split()
            assert len(children) == 1
            os.killpg(int(children[0]), signal.SIGKILL)
            assert child.wait(timeout=30) == 137
            with (a.output / 'retry.log').open('w') as retry_log:
                subprocess.run(command, cwd=a.repo, stdout=retry_log, stderr=subprocess.STDOUT, check=True, timeout=120)
            assert json.loads(active.read_text())['attempt'] == attempt
        terminal = query(a.host, attempt)
        assert terminal.get('cleanup_verified'), terminal
        if mode == 'disconnect':
            assert terminal['exit_code'] == 0
        results.append({'case': mode, 'attempt': attempt, 'cleanup_verified': True,
                        'exit_code': terminal['exit_code']})
    (a.output / 'summary.json').write_text(json.dumps(results, indent=2) + '\n')
print('Cancellation and same-attempt recovery verified')
