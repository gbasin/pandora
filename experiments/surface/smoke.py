#!/usr/bin/env python3
"""Clean-commit surface baseline. Not the agent router or a job queue."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='SSH destination, e.g. ubuntu@worker')
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--revision', default='origin/main')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('selectors', nargs='*', help='Playwright test file selectors')
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*', args.host):
        parser.error('Use an SSH hostname or user@hostname (configure ports in SSH config).')
    if any(s.startswith('-') for s in args.selectors):
        parser.error('This baseline accepts test file selectors only, not Playwright flags.')
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    scripts = Path(__file__).resolve().parent
    revision = run('git', '-C', str(args.repo), 'rev-parse', '--verify',
                   args.revision + '^{commit}', capture_output=True, text=True).stdout.strip()
    print(f'[pandora baseline] snapshotting committed revision {revision}', flush=True)
    archive = output / 'source.tar.gz'
    run('git', '-C', str(args.repo), 'archive', '--format=tar.gz',
        '--output=' + str(archive), revision)
    digest = hashlib.sha256()
    with archive.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    (output / 'source.sha256').write_text(f'{digest.hexdigest()}  source.tar.gz\n')
    snapshot_seconds = time.monotonic() - started
    attempt = uuid.uuid4().hex
    remote = f'pandora-smoke/{attempt}'
    metadata = {'attempt': attempt, 'revision': revision, 'sha256': digest.hexdigest(),
                'selectors': args.selectors, 'source_mode': 'committed-only',
                'host': args.host, 'remote_directory': remote,
                'archive_bytes': archive.stat().st_size, 'snapshot_seconds': snapshot_seconds}
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', args.host]
    run(*ssh, f'mkdir -p {remote}')
    print(f'[pandora baseline] uploading {archive.stat().st_size / 1024**2:.1f} MiB', flush=True)
    transfer_started = time.monotonic()
    run('scp', '-q', str(archive), str(output / 'source.sha256'),
        str(scripts / 'in-container.sh'), str(scripts / 'on-worker.sh'),
        f'{args.host}:{remote}/')
    metadata['transfer_seconds'] = time.monotonic() - transfer_started
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    run(*ssh, 'sudo docker image inspect pandora-surface:smoke --format '
        + shlex.quote('{{.Id}}') + f' > {remote}/image-id')
    print(f'[pandora baseline] accepted {attempt}; source {revision}; SHA256 {digest.hexdigest()}', flush=True)
    print('[pandora baseline] 2 CPUs, 6 GiB RAM, one worker, 20-minute worker deadline', flush=True)
    command = f'cd {remote} && bash on-worker.sh ' + shlex.join(args.selectors)
    status = 70
    try:
        status = subprocess.run([*ssh, command]).returncode
    finally:
        # Retrieve even on test failure. Missing results remain an infrastructure
        # error rather than turning an incomplete run into success.
        retrieval = subprocess.run(['scp', '-q', '-r',
            f'{args.host}:{remote}/workspace/results',
            f'{args.host}:{remote}/stdout.log', f'{args.host}:{remote}/stderr.log',
            f'{args.host}:{remote}/container.json', f'{args.host}:{remote}/image-id',
            str(output)])
        if retrieval.returncode and status == 0:
            status = 70
    metadata['total_seconds'] = time.monotonic() - started
    metadata['exit_code'] = status
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'[pandora baseline] exit={status}; evidence={output}', flush=True)
    return status if status >= 0 else 128 - status


if __name__ == '__main__':
    raise SystemExit(main())
