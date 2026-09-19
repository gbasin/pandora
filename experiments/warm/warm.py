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


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True)
    p.add_argument('--repo', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('selectors', nargs='*')
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*', args.host):
        p.error('Invalid SSH destination')
    if any(s.startswith('-') for s in args.selectors):
        p.error('Only file selectors are supported in this experiment')
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    print('[pandora] freezing current tracked and nonignored source', flush=True)
    manifest, excluded = freeze(args.repo, output / 'source')
    identity = hashlib.sha256(encode(manifest)).hexdigest()
    (output / 'manifest.json').write_bytes(encode(manifest))
    attempt = uuid.uuid4().hex
    metadata = {'attempt': attempt, 'source_digest': identity, 'excluded': excluded,
                'selectors': args.selectors, 'snapshot_seconds': time.monotonic() - started}
    (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    scripts = Path(__file__).resolve().parent
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', args.host]
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
    status = 70
    try:
        command = f'cd {remote} && set -o pipefail && python3 -u worker.py 2> >(tee stderr.log >&2) | tee stdout.log'
        status = subprocess.run([*ssh, 'bash -c ' + shlex.quote(command)]).returncode
    finally:
        result = subprocess.run(['scp', '-q', '-r', f'{args.host}:{remote}/results',
                                 f'{args.host}:{remote}/container.json',
                                 f'{args.host}:{remote}/metrics.json',
                                 f'{args.host}:{remote}/stdout.log',
                                 f'{args.host}:{remote}/stderr.log', str(output)])
        if result.returncode and status == 0:
            status = 70
        metadata['total_seconds'] = time.monotonic() - started
        metadata['exit_code'] = status
        (output / 'submission.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(f'[pandora] exit={status}; evidence={output}', flush=True)
    return status if status >= 0 else 128 - status


if __name__ == '__main__':
    raise SystemExit(main())
