#!/usr/bin/env python3
"""Frozen dirty-source upload and a warm, isolated surface experiment."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import uuid
from snapshot import encode, freeze
from transport import follow, SSH_OPTIONS


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True)
    p.add_argument('--repo', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--require-warm', action='store_true')
    p.add_argument('--attempt', default=None)
    p.add_argument('selectors', nargs='*')
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*', args.host):
        p.error('Invalid SSH destination')
    if any(s.startswith('-') for s in args.selectors):
        p.error('Only file selectors are supported in this experiment')
    attempt = args.attempt or uuid.uuid4().hex
    if not re.fullmatch('[0-9a-f]{32}', attempt):
        p.error('Invalid attempt identity')
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    print('[pandora] freezing current tracked and nonignored source', flush=True)
    manifest, excluded = freeze(args.repo, output / 'source')
    identity = hashlib.sha256(encode(manifest)).hexdigest()
    (output / 'manifest.json').write_bytes(encode(manifest))
    metadata = {'attempt': attempt, 'source_digest': identity, 'excluded': excluded,
                'selectors': args.selectors, 'require_warm': args.require_warm, 'snapshot_seconds': time.monotonic() - started}
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    scripts = Path(__file__).resolve().parent
    ssh = ['ssh', *SSH_OPTIONS, args.host]
    home = run(*ssh, 'pwd', capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', home):
        raise RuntimeError('Unsupported remote home path')
    root = home + '/pandora-warm'
    remote = root + '/runs/' + attempt
    run(*ssh, f'mkdir -p {remote}/source')
    cached = run(*ssh, f'readlink -f {root}/latest || true', capture_output=True,
                 text=True).stdout.strip()
    options = ['--link-dest=' + cached] if cached else []
    print(f'[pandora] transferring changed source; frozen identity {identity[:12]}', flush=True)
    transfer = time.monotonic()
    result = run('rsync', '-rlpc', '--delete', '--stats',
                 '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=15', *options,
                 str(output / 'source') + '/', f'{args.host}:{remote}/source/',
                 capture_output=True, text=True)
    (output / 'transfer.log').write_text(result.stdout + result.stderr)
    metadata['transfer_seconds'] = time.monotonic() - transfer
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    run('scp', '-q', str(output / 'manifest.json'), str(output / 'submission.json'),
        str(scripts / 'snapshot.py'), str(scripts / 'worker.py'),
        str(scripts / 'in-container.sh'), f'{args.host}:{remote}/')
    # All links reference complete immutable source directories, never containers.
    run(*ssh, f'ln -s {remote}/source {root}/latest-{attempt} && '
        f'mv -Tf {root}/latest-{attempt} {root}/latest')
    print(f'[pandora] accepted {attempt}; source transfer {metadata["transfer_seconds"]:.1f}s', flush=True)
    # systemd owns the worker independently of this SSH connection. Never retry
    # this start after ambiguous acknowledgement; reconnect by the same attempt.
    worker_command = 'exec python3 -u worker.py >stdout.log 2>stderr.log'
    command = (f'sudo systemd-run --quiet --collect --unit=pandora-worker-{attempt} '
               f'--uid=ubuntu --working-directory={remote} '
               '--property=RuntimeMaxSec=40m --property=TimeoutStopSec=30s '
               '--property=KillMode=control-group /bin/bash -c ' + shlex.quote(worker_command))
    launched = subprocess.run([*ssh, command])
    if launched.returncode:
        print('[pandora] Start acknowledgement unavailable; checking the existing attempt only.', flush=True)
    status = follow(args.host, output)
    metadata['total_seconds'] = time.monotonic() - started
    metadata['exit_code'] = status
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    return status


if __name__ == '__main__':
    raise SystemExit(main())
