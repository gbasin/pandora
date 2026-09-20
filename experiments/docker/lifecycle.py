"""Fault injection for Docker runs and builds through normal routing."""
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
base = Path(__file__).resolve().parent
sys.path.insert(0, str(base.parent / 'warm'))
from transport import query
command = [sys.executable, str(base.parent / 'routing/launch.py'), '--host', a.host,
           '--state', str(a.state), '--docker-profile', str(base / 'profile.json'), '--', 'docker']
key = hashlib.sha256(str(a.repo.resolve()).encode()).hexdigest()
active = a.state / key / 'active.json'
results = []


def start(args, label):
    log = (a.output / (label + '.log')).open('w')
    return subprocess.Popen([*command, *args], cwd=a.repo, stdout=log, stderr=subprocess.STDOUT), log


def ready(child, old, needle):
    for _ in range(90):
        if child.poll() is not None:
            raise AssertionError('Exited before fault injection')
        if active.exists():
            record = json.loads(active.read_text())
            if record['attempt'] != old:
                state = query(a.host, record['attempt'])
                if needle in state.get('stdout', '') + state.get('stderr', ''):
                    return record
        time.sleep(2)
    raise TimeoutError('Readiness deadline')


def clean(attempt):
    response = subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host,
                              'sudo docker ps --filter name=^/buildx_buildkit_pandora-docker-builds-v10$ --format "{{.Names}}"; '
                              'sudo docker ps -a --filter label=pandora.attempt=' + attempt + ' --format "{{.Names}}"'],
                             check=True, capture_output=True, text=True, timeout=30)
    assert not response.stdout.strip(), response.stdout


original = (a.repo / 'Dockerfile').read_text()
for mode in ('run-cancel', 'build-cancel', 'disconnect', 'build-worker-kill'):
    build = mode.startswith('build')
    old = json.loads(active.read_text())['attempt'] if active.exists() else None
    if build:
        (a.repo / 'Dockerfile').write_text(original + '\nRUN sleep 60\n')
    args = ['build', '-t', 'app:test', '.'] if build else ['run', '--rm', 'app:test', 'node', '-e',
            "console.log('DOCKER_READY');setTimeout(()=>import('./check.mjs'),20000)"]
    child, log = start(args, mode)
    record = ready(child, old, 'RUN sleep 60' if build else 'DOCKER_READY')
    attempt = record['attempt']
    print(mode + ': injecting at ' + attempt, flush=True)
    if mode == 'disconnect':
        children = subprocess.check_output(['pgrep', '-P', str(child.pid)], text=True).split()
        assert len(children) == 1
        os.killpg(int(children[0]), signal.SIGKILL)
        assert child.wait(timeout=30) == 137
        retry, retry_log = start(args, 'disconnect-retry')
        assert retry.wait(timeout=120) == 0
        retry_log.close()
        assert json.loads(active.read_text())['attempt'] == attempt
    elif mode == 'build-worker-kill':
        subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host,
                        'sudo systemctl kill --kill-whom=main --signal=SIGKILL pandora-worker-' + attempt + '.service'], check=True)
        for _ in range(30):
            response = subprocess.run(['ssh', '-o', 'BatchMode=yes', a.host,
                                       'cat pandora-warm/runs/' + attempt + '/docker-cleanup.json'], capture_output=True, text=True)
            if response.returncode == 0 and json.loads(response.stdout).get('verified'):
                break
            time.sleep(2)
        else:
            raise AssertionError('Missing worker-death cleanup')
        child.send_signal(signal.SIGINT)
        assert child.wait(timeout=90) == 130
        assert not query(a.host, attempt).get('cleanup_verified')
    else:
        child.send_signal(signal.SIGINT)
        assert child.wait(timeout=90) == 130
        assert query(a.host, attempt).get('cleanup_verified')
    log.close()
    (a.repo / 'Dockerfile').write_text(original)
    clean(attempt)
    results.append({'case': mode, 'attempt': attempt, 'cleanup': True,
                    'terminal': {k: v for k, v in query(a.host, attempt).items() if k not in ('stdout', 'stderr')}})
    (a.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print(mode + ': verified', flush=True)
