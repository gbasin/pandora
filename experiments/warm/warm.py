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
from retention import PROFILE
from source_cache import repository_key
from transport import follow, SSH_OPTIONS


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def write_metadata(path, metadata):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(metadata, indent=2) + '\n')
    temporary.replace(path)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host', required=True)
    p.add_argument('--repo', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--workflow', choices=['surface', 'journey', 'docker'], default='surface')
    p.add_argument('--require-warm', action='store_true')
    p.add_argument('--docker-request')
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
    spec = json.loads(args.docker_request) if args.workflow == 'docker' else None
    uses_source = spec is None or spec['request']['kind'] == 'build' or spec['request'].get('mount')
    cache_key = repository_key(args.repo) if uses_source else None
    if uses_source:
        print('[pandora] freezing current tracked and nonignored source', flush=True)
        manifest, excluded = freeze(args.repo, output / 'source')
    else:
        print('[pandora] image-only request; local source is not captured or injected', flush=True)
        (output / 'source').mkdir()
        manifest, excluded = [], []
    identity = hashlib.sha256(encode(manifest)).hexdigest()
    (output / 'manifest.json').write_bytes(encode(manifest))
    metadata = {'profile': PROFILE, 'attempt': attempt, 'source_digest': identity, 'excluded': excluded,
                'repository_key': cache_key, 'workflow': args.workflow, 'selectors': args.selectors, 'require_warm': args.require_warm, 'snapshot_seconds': time.monotonic() - started}
    if args.workflow == 'docker':
        metadata['docker'] = spec
    write_metadata(output / 'submission.json', metadata)
    scripts = Path(__file__).resolve().parent
    ssh = ['ssh', *SSH_OPTIONS, args.host]
    home = run(*ssh, 'pwd', capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', home):
        raise RuntimeError('Unsupported remote home path')
    if args.workflow == 'docker' and metadata['docker']['request']['kind'] == 'run':
        spec = metadata['docker']
        resolved = subprocess.run([*ssh, 'python3 - ' + shlex.quote(spec['worktree_key']) + ' ' + shlex.quote(spec['request']['tag']) + ' ' + attempt],
                                  input=(scripts / 'docker_images.py').read_text(), capture_output=True, text=True)
        if resolved.returncode:
            print('[pandora] ' + resolved.stderr.strip(), flush=True)
            # No remote directory or worker exists yet. Let the route clear this
            # pre-submission failure instead of pinning an unrecoverable request.
            (output / 'submission.json').unlink()
            return 64 if resolved.returncode == 64 else 75
        spec['image'] = json.loads(resolved.stdout)
        print(f'[pandora] pinned {spec["request"]["tag"]} to {spec["image"]["image_id"]}; built source {spec["image"]["source_digest"][:12]}', flush=True)
    root = home + '/pandora-warm'
    remote = root + '/runs/' + attempt
    run(*ssh, f'mkdir -p {root}/runs && mkdir {remote} && mkdir {remote}/source')
    cached = ''
    if uses_source:
        cached = run(*ssh, f'python3 - prepare {cache_key} {attempt}',
                     input=(scripts / 'source_cache.py').read_text(),
                     capture_output=True, text=True).stdout.strip()
    options = ['--link-dest=' + cached] if cached else []
    print(f'[pandora] transferring changed source; frozen identity {identity[:12]}', flush=True)
    transfer = time.monotonic()
    result = run('rsync', '-rlpc', '--delete', '--stats',
                 '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=15', *options,
                 str(output / 'source') + '/', f'{args.host}:{remote}/source/',
                 capture_output=True, text=True)
    (output / 'transfer.log').write_text(result.stdout + result.stderr)
    metadata['transfer_seconds'] = time.monotonic() - transfer
    write_metadata(output / 'submission.json', metadata)
    run('scp', '-q', str(output / 'manifest.json'), str(output / 'submission.json'),
        str(scripts / 'snapshot.py'), str(scripts / 'worker.py'), str(scripts / 'dependencies.py'), str(scripts / 'retention.py'), str(scripts / 'source_cache.py'),
        str(scripts / 'in-container.sh'), str(scripts / 'journey.py'),
        str(scripts / 'service_cleanup.py'), str(scripts / 'journey.mjs'),
        str(scripts / 'docker_workflow.py'), str(scripts / 'docker_cleanup.py'), str(scripts / 'docker_images.py'), str(scripts / 'image_gc.py'), f'{args.host}:{remote}/')
    run('scp', '-q', str(scripts.parent / 'surface/Dockerfile'), f'{args.host}:{remote}/runtime.Dockerfile')
    # All links reference complete immutable source directories, never containers.
    if uses_source:
        run(*ssh, f'python3 {remote}/source_cache.py publish {cache_key} {attempt}')
    print(f'[pandora] accepted {attempt}; source transfer {metadata["transfer_seconds"]:.1f}s', flush=True)
    # systemd owns the worker independently of this SSH connection. Never retry
    # this start after ambiguous acknowledgement; reconnect by the same attempt.
    worker_command = 'exec python3 -u worker.py >stdout.log 2>stderr.log'
    command = (f'sudo systemd-run --quiet --collect --unit=pandora-worker-{attempt} '
               f'--uid=ubuntu --working-directory={remote} '
               '--property=RuntimeMaxSec=40m --property=TimeoutStopSec=30s '
               '--property=KillMode=control-group '
               f'--property=ExecStopPost={shlex.quote("/usr/bin/python3 " + remote + "/service_cleanup.py " + remote)} /bin/bash -c ' + shlex.quote(worker_command))
    launched = subprocess.run([*ssh, command])
    if launched.returncode:
        print('[pandora] Start acknowledgement unavailable; checking the existing attempt only.', flush=True)
    status = follow(args.host, output)
    metadata['total_seconds'] = time.monotonic() - started
    metadata['exit_code'] = status
    write_metadata(output / 'submission.json', metadata)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
