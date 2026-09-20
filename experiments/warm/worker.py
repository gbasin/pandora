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
import tarfile
import time
from snapshot import encode, verify, digest
from dependencies import prepare
from retention import remote as prune_remote, remember_image


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
            if time.monotonic() - queued >= 900:
                print('[pandora] Queue deadline reached; no tests started', flush=True)
                return 75
            print('[pandora] queued; worker occupied; no local validation started', flush=True)
            time.sleep(10)
    from dependencies import CONTAINER
    running_builder = docker('ps', '--filter', 'name=^/' + CONTAINER + '$',
                             '--format', '{{.Names}}', capture_output=True, text=True)
    if running_builder.stdout.strip():
        raise RuntimeError('Dependency builder still active without its worker lease; operator cleanup required. No tests started.')
    prune_remote(root)
    if shutil.disk_usage(root).free < 10 * 1024**3:
        raise RuntimeError('Worker disk has less than 10 GiB free. No preparation or tests started; operator retention cleanup required.')
    metrics = {'queue_seconds': time.monotonic() - queued}
    print('[pandora] worker acquired; preparing dependencies', flush=True)
    started = time.monotonic()
    recipe = (attempt / 'runtime.Dockerfile').read_text()
    # Includes all workspace package manifests, installation settings and patches.
    dep_entries = [e for e in manifest if Path(e['path']).name == 'package.json'
                   or e['path'] in {'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc',
                                    '.pnpmfile.cjs', 'pnpmfile.cjs'}
                   or e['path'].startswith('patches/')]
    key = hashlib.sha256(b'deps-recipe-v3' + recipe.encode() + encode(dep_entries)).hexdigest()
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
        (context / 'Dockerfile').write_text(recipe +
            '\nUSER root\nRUN mkdir -p /workspace/source && chown -R node:node /workspace\n'
            'USER node\nWORKDIR /workspace/source\nENV CI=true\n'
            'COPY --chown=node:node files/ /workspace/source/\n'
            'RUN --mount=type=cache,target=/pnpm/store,uid=1000,gid=1000 '
            'pnpm install --frozen-lockfile --store-dir=/pnpm/store\n')
        (context / 'buildkitd.toml').write_text(
            '[worker.oci]\n  gc = true\n  reservedSpace = "2GB"\n  maxUsedSpace = "12GB"\n  minFreeSpace = "10GB"\n')
        pending = attempt / 'dependency-cleanup.pending'
        pending.touch()
        try:
            prepare(context, image)
        finally:
            from dependencies import CONTAINER
            state = subprocess.run(['sudo', 'docker', 'ps', '--filter', 'name=^/' + CONTAINER + '$',
                                    '--format', '{{.Names}}'], capture_output=True, text=True)
            if state.returncode == 0 and not state.stdout.strip():
                pending.unlink()

    metrics['dependency_seconds'] = time.monotonic() - started
    image_id = docker('image', 'inspect', image, '--format', '{{.Id}}',
                      capture_output=True, text=True).stdout.strip()
    metrics['image_id'] = image_id
    remember_image(root, image)
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
        # Installation inputs already exist byte-for-byte in the keyed image.
        # Preserve their timestamps: pnpm's patch freshness check uses mtime.
        install_paths = {e['path'] for e in dep_entries}
        overlay = attempt / 'source-overlay.tar'
        with tarfile.open(overlay, 'w') as archive:
            for directory in sorted((attempt / 'source').rglob('*')):
                if directory.is_dir() and not directory.is_symlink():
                    archive.add(directory, arcname=str(directory.relative_to(attempt / 'source')), recursive=False)
            for item in manifest:
                if item['path'] not in install_paths:
                    archive.add(attempt / 'source' / item['path'], arcname=item['path'], recursive=False)
        with overlay.open('rb') as archive:
            docker('cp', '-a', '-', name + ':/workspace/source', stdin=archive)
        overlay.unlink()
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
    attempt_lock = Path('attempt.lock').open('a')
    try:
        fcntl.flock(attempt_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(75)
    if Path('worker.json').exists():
        raise SystemExit(75)  # A crashed attempt is never re-executed.
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
    # This profile returns ordinary generated files only. Do not silently omit
    # a symlink or special file and advertise an incomplete build as complete.
    for item in Path('results/outputs').rglob('*'):
        if item.is_symlink() or not (item.is_file() or item.is_dir()):
            print('[pandora] unsupported generated output file type: ' + str(item), flush=True)
            status = 70
    artifacts = {}
    for item in [Path('stdout.log'), Path('stderr.log'), Path('container.json'),
                 Path('metrics.json'), *Path('results').rglob('*')]:
        if item.is_file() and not item.is_symlink():
            artifacts[str(item)] = digest(item)
    Path('artifacts.json').write_text(json.dumps(artifacts, indent=2) + '\n')
    terminal = {'state': 'terminal', 'attempt': Path.cwd().name, 'exit_code': status,
                'cleanup_verified': check.returncode == 0 and not check.stdout.strip() and not Path('dependency-cleanup.pending').exists()}
    Path('terminal.json.tmp').write_text(json.dumps(terminal) + '\n')
    Path('terminal.json.tmp').replace('terminal.json')
    raise SystemExit(status)
