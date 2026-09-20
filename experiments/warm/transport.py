"""Reconnect to one immutable attempt and verify its returned evidence."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from snapshot import digest
import time
import uuid

SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']


def query(host, attempt, action='status', offsets=None):
    script = Path(__file__).resolve().parent.parent / 'routing/control.py'
    args = [attempt, action, *(str(x) for x in (offsets or [0, 0]))]
    result = subprocess.run(['ssh', *SSH_OPTIONS, host, 'python3 - ' + ' '.join(args)],
                            input=script.read_text(), capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise ConnectionError(result.stderr.strip() or 'SSH status unavailable')
    return json.loads(result.stdout)


def validate_evidence(stage, attempt):
    terminal = json.loads((stage / 'terminal.json').read_text())
    manifest = json.loads((stage / 'artifacts.json').read_text())
    if terminal.get('attempt') != attempt or not terminal.get('cleanup_verified'):
        raise ValueError('Unverified terminal identity or cleanup')
    for name, expected in manifest.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError('Unsafe artifact path')
        target = stage / path
        if target.is_symlink() or not target.is_file():
            raise ValueError('Missing or nonregular artifact: ' + name)
        if digest(target) != expected:
            raise ValueError('Artifact checksum mismatch: ' + name)
    if terminal['exit_code'] == 0:
        if not {'results/exit-code', 'results/junit.xml'} <= set(manifest):
            raise ValueError('Successful run lacks test evidence')
        if (stage / 'results/exit-code').read_text().strip() != '0':
            raise ValueError('Test evidence disagrees with successful terminal')
    return terminal


def retrieve(host, output, attempt):
    stage = output / ('.download-' + uuid.uuid4().hex)
    stage.mkdir()
    remote = f'{host}:pandora-warm/runs/{attempt}/'
    try:
        subprocess.run(['rsync', '-rt', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                        remote + 'artifacts.json', remote + 'terminal.json', str(stage) + '/'],
                       check=True, timeout=60)
        manifest = json.loads((stage / 'artifacts.json').read_text())
        for name in manifest:
            p = Path(name)
            if p.is_absolute() or '..' in p.parts or '\n' in name or '\r' in name:
                raise ValueError('Unsafe artifact path')
        subprocess.run(['rsync', '-rt', '--files-from=-', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                        remote, str(stage) + '/'], input='\n'.join(manifest) + '\n',
                       text=True, check=True, timeout=120)
        terminal = validate_evidence(stage, attempt)
        # Promote evidence only after complete verification. The terminal is last.
        for child in list(stage.iterdir()):
            if child.name == 'terminal.json':
                continue
            destination = output / child.name
            if destination.is_dir():
                shutil.rmtree(destination)
            os.replace(child, destination)
        os.replace(stage / 'terminal.json', output / 'terminal.json')
        return terminal
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def follow(host, output, reconnect_seconds=45):
    metadata = json.loads((output / 'submission.json').read_text())
    attempt = metadata['attempt']
    if not re.fullmatch('[0-9a-f]{32}', attempt):
        raise ValueError('Invalid attempt')
    offsets = [0, 0]
    unavailable = None
    unregistered = time.monotonic()
    following = time.monotonic()
    while True:
        if time.monotonic() - following > 2445:
            print('[pandora] Follow deadline reached; remote state remains unresolved. No replacement submitted.', flush=True)
            return 75
        try:
            state = query(host, attempt, offsets=offsets)
        except (ConnectionError, subprocess.TimeoutExpired, ValueError) as error:
            unavailable = unavailable or time.monotonic()
            print(f'[pandora] connection interrupted for {attempt}; preserving request: {error}', flush=True)
            if time.monotonic() - unavailable >= reconnect_seconds:
                print('[pandora] Retry the same command to recover this request. No new run was submitted.', flush=True)
                return 75
            time.sleep(3)
            continue
        unavailable = None
        for index, key in enumerate(['stdout', 'stderr']):
            if state.get(key):
                print(state[key], end='', file=sys.stderr if key == 'stderr' else sys.stdout, flush=True)
            offsets[index] = state.get('offsets', offsets)[index]
        if state.get('cleanup_verified'):
            if state.get('more_logs'):
                continue
            try:
                terminal = retrieve(host, output, attempt)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f'[pandora] Evidence retrieval incomplete: {error}. Retry the same command to recover {attempt}.', flush=True)
                return 75
            print(f'[pandora] attempt={attempt}; exit={terminal["exit_code"]}; evidence={output}', flush=True)
            if (output / 'results/junit.xml').exists():
                print(f'[pandora] test report: {output / "results/junit.xml"}', flush=True)
            if (output / 'results/playwright').exists():
                print(f'[pandora] test diagnostics: {output / "results/playwright"}', flush=True)
            return terminal['exit_code']
        if not state.get('registered') and time.monotonic() - unregistered > 30:
            print('[pandora] Submission is unresolved before worker registration. No replacement started.', flush=True)
            return 75
        time.sleep(2)


if __name__ == '__main__':
    import sys
    raise SystemExit(follow(sys.argv[1], Path(sys.argv[2])))
