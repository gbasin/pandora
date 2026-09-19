#!/usr/bin/env python3
"""Single-slot warm experiment worker. Invoked in an uploaded attempt directory."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import traceback
import subprocess
import sys
import time
from snapshot import encode, verify


def run(*args, heartbeat=None, **kwargs):
    if heartbeat is None:
        return subprocess.run(args, check=True, **kwargs)
    child = subprocess.Popen(args, **kwargs)
    while True:
        try:
            status = child.wait(timeout=10)
            if status:
                raise subprocess.CalledProcessError(status, args)
            return subprocess.CompletedProcess(args, status)
        except subprocess.TimeoutExpired:
            print(heartbeat, flush=True)


def docker(*args, **kwargs):
    return run('sudo', 'docker', *args, **kwargs)


def main():
    attempt = Path.cwd()
    root = attempt.parent.parent
    manifest = json.loads((attempt / 'manifest.json').read_text())
    submitted = json.loads((attempt / 'submission.json').read_text())
    if hashlib.sha256(encode(manifest)).hexdigest() != submitted['source_digest']:
        raise RuntimeError('Manifest identity mismatch')
    if (attempt / 'cancel.request').exists():
        return 130
    verify(attempt / 'source', manifest)
    print('[pandora] source verified; waiting for the experiment worker', flush=True)
    lock = (root / 'worker.lock').open('w')
    queued = time.monotonic()
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            print('[pandora] queued; worker occupied; no local validation started', flush=True)
            time.sleep(10)
    metrics = {'queue_seconds': time.monotonic() - queued}
    print('[pandora] worker acquired; preparing dependencies', flush=True)
    started = time.monotonic()
    base = docker('image', 'inspect', 'pandora-surface:smoke', '--format', '{{.Id}}',
                  capture_output=True, text=True).stdout.strip()
    # Includes all workspace package manifests, installation settings and patches.
    dep_entries = [e for e in manifest if Path(e['path']).name == 'package.json'
                   or e['path'] in {'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc',
                                    '.pnpmfile.cjs', 'pnpmfile.cjs'}
                   or e['path'].startswith('patches/')]
    key = hashlib.sha256(b'deps-recipe-v2' + base.encode() + encode(dep_entries)).hexdigest()
    image = 'pandora-deps:' + key
    exists = subprocess.run(['sudo', 'docker', 'image', 'inspect', image],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    metrics['dependency_cache_hit'] = exists
    if not exists and submitted.get('require_warm'):
        raise RuntimeError('Dependency image is not prepared for these inputs; this agent trial requires a warm image. No tests started.')
    if not exists:
        context = attempt / 'deps-context'
        (context / 'files').mkdir(parents=True)
        for e in dep_entries:
            if 'link' in e:
                raise ValueError('Dependency inputs must be ordinary files')
            destination = context / 'files' / e['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(attempt / 'source' / e['path'], destination)
        (context / 'Dockerfile').write_text(
            f'FROM {base}\nUSER root\nRUN mkdir -p /workspace/source && chown -R node:node /workspace\nUSER node\nWORKDIR /workspace/source\nENV CI=true\n'
            'COPY --chown=node:node files/ /workspace/source/\n'
            'RUN pnpm install --frozen-lockfile\n')
        # Classic Docker builder honors these limits for RUN containers.
        docker('build', '--force-rm', '--memory=6g', '--memory-swap=6g', '--cpu-period=100000',
               '--cpu-quota=200000', '-t', image, str(context),
               env={**os.environ, 'DOCKER_BUILDKIT': '0'},
               heartbeat='[pandora] preparing dependency image; worker remains occupied')
    metrics['dependency_seconds'] = time.monotonic() - started
    image_id = docker('image', 'inspect', image, '--format', '{{.Id}}',
                      capture_output=True, text=True).stdout.strip()
    metrics['image_id'] = image_id
    (attempt / 'metrics.json').write_text(json.dumps(metrics, indent=2))
    name = 'pandora-warm-' + attempt.name
    created = False
    status = 70
    execution = time.monotonic()
    try:
        docker('create', '--name', name, '--label', 'pandora.experiment=warm-surface',
               '--cpus=2', '--memory=6g', '--memory-swap=6g', '--pids-limit=512',
               '--shm-size=1g', '--cap-drop=ALL', '--security-opt=no-new-privileges',
               '--init', '-e', 'CI=true', image_id, 'bash', '/tmp/pandora-run.sh',
               *submitted['selectors'], stdout=subprocess.DEVNULL)
        created = True
        docker('cp', '-a', str(attempt / 'source') + '/.', name + ':/workspace/source')
        docker('cp', str(attempt / 'in-container.sh'), name + ':/tmp/pandora-run.sh')
        # docker cp writes root-owned input; dependencies retain node ownership.
        # No writable host source mount is exposed to the test process.
        run('sudo', 'systemd-run', '--quiet', '--unit=' + name + '-deadline',
            '--on-active=20m', '/usr/bin/docker', 'stop', '--time', '10', name)
        print('[pandora] running surface validation; installed dependencies reused', flush=True)
        status = subprocess.run(['sudo', 'docker', 'start', '--attach', name]).returncode
    finally:
        if created:
            subprocess.run(['sudo', 'docker', 'stop', '--time', '10', name],
                           stdout=subprocess.DEVNULL)
            with (attempt / 'container.json').open('w') as target:
                docker('inspect', name, stdout=target)
            result = subprocess.run(['sudo', 'docker', 'cp', name + ':/workspace/results',
                                     str(attempt / 'results')])
            if result.returncode and status == 0:
                status = 70
            # Retain failed stopped containers for diagnostics, release processes.
            if status == 0:
                docker('rm', name, stdout=subprocess.DEVNULL)
            subprocess.run(['sudo', 'systemctl', 'stop', name + '-deadline.timer'])
        metrics['execution_seconds'] = time.monotonic() - execution
        metrics['exit_code'] = status
        (attempt / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    print(f'[pandora] terminal exit={status}', flush=True)
    return status


def cancelled(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    print('[pandora] cancellation received; stopping this attempt', flush=True)
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    signal.signal(signal.SIGHUP, cancelled)
    registration = {'pid': os.getpid(),
                    'start_ticks': Path(f'/proc/{os.getpid()}/stat').read_text().split()[21]}
    Path('worker.json.tmp').write_text(json.dumps(registration))
    Path('worker.json.tmp').replace('worker.json')
    try:
        status = main()
    except KeyboardInterrupt:
        status = 130
    except Exception:
        traceback.print_exc()
        status = 70
    # A terminal record is usable for another submission only when the owned
    # container is absent or stopped. Unknown Docker state cannot clear a job.
    name = 'pandora-warm-' + Path.cwd().name
    check = subprocess.run(['sudo', 'docker', 'ps', '--filter', 'name=^/' + name + '$',
                            '--format', '{{.Names}}'], capture_output=True, text=True)
    terminal = {'state': 'terminal', 'exit_code': status,
                'cleanup_verified': check.returncode == 0 and not check.stdout.strip()}
    Path('terminal.json.tmp').write_text(json.dumps(terminal) + '\n')
    Path('terminal.json.tmp').replace('terminal.json')
    raise SystemExit(status)
