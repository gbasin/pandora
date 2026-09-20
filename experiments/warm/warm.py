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
from worker_bundle import bundle
from transport import follow, SSH_OPTIONS
from artifact_limits import artifact_delivery_limit


def queue_timeout_seconds(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 86400:
        raise ValueError('Queue timeout must be an integer from 1 through 86400 seconds')
    return value


def effective_queue_timeout(default, spec):
    value = spec.get('config', {}).get('queue_timeout_seconds', default) if spec else default
    return queue_timeout_seconds(value)


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
    p.add_argument('--workflow', choices=['surface', 'journey', 'docker', 'suite', 'suite-run'], default='surface')
    p.add_argument('--require-warm', action='store_true')
    p.add_argument('--docker-request')
    p.add_argument('--suite-request', type=Path, help='Private plan/shard request JSON; suite routing remains experimental')
    p.add_argument('--attempt', default=None)
    p.add_argument('--journey-update', action='store_true')
    p.add_argument('--selectors-json')
    p.add_argument('--surface-app', choices=['borrower-web', 'desk'], default='borrower-web')
    p.add_argument('--queue-timeout-seconds', type=int, default=900)
    p.add_argument('--artifact-delivery-limit-bytes', type=int, default=None)
    p.add_argument('selectors', nargs='*')
    args = p.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@-]*', args.host):
        p.error('Invalid SSH destination')
    if args.selectors_json is not None:
        if args.selectors:
            p.error('Do not combine positional selectors and --selectors-json')
        try:
            args.selectors = json.loads(args.selectors_json)
        except ValueError:
            p.error('Invalid selectors JSON')
        if not isinstance(args.selectors, list) or any(not isinstance(x, str) for x in args.selectors):
            p.error('Selectors must be a list of strings')
    spec = json.loads(args.docker_request) if args.workflow == 'docker' else None
    try:
        timeout = effective_queue_timeout(args.queue_timeout_seconds, spec)
        artifact_limit = artifact_delivery_limit(args.artifact_delivery_limit_bytes)
    except ValueError as error:
        p.error(str(error))
    suite = None
    if args.workflow in ('suite', 'suite-run'):
        if args.suite_request is None or args.selectors:
            p.error('Suite workflow requires --suite-request and no positional selectors')
        try:
            from suite import suite_request
            suite = suite_request(json.loads(args.suite_request.read_text()))
            if (suite['action'] == 'run') != (args.workflow == 'suite-run'):
                raise ValueError('Suite run requests require workflow suite-run')
        except (OSError, ValueError) as error:
            p.error(str(error))
    elif args.suite_request is not None:
        p.error('--suite-request requires --workflow suite')
    if args.journey_update:
        if args.workflow != 'journey':
            p.error('--journey-update requires a journey workflow')
        args.selectors.append('--update')
    try:
        if args.workflow == 'journey':
            from journey import journey_config
            journey_config({'selectors': args.selectors})
        elif args.workflow == 'surface':
            from workflow_options import surface_selectors
            surface_selectors(args.selectors)
    except ValueError as error:
        p.error(str(error))
    attempt = args.attempt or uuid.uuid4().hex
    if not re.fullmatch('[0-9a-f]{32}', attempt):
        p.error('Invalid attempt identity')
    started = time.monotonic()
    args.output.mkdir(parents=True, exist_ok=False)
    output = args.output.resolve()
    uses_source = spec is None or spec['request']['kind'] == 'build' or spec['request'].get('mount')
    cache_key = repository_key(args.repo) if uses_source else None
    if uses_source:
        print('[pandora] freezing current tracked and nonignored source', flush=True)
        manifest, excluded = freeze(args.repo, output / 'source')
    else:
        print('[pandora] image-only run uses the built image; rebuild to include local source edits', flush=True)
        (output / 'source').mkdir()
        manifest, excluded = [], []
    identity = hashlib.sha256(encode(manifest)).hexdigest()
    (output / 'manifest.json').write_bytes(encode(manifest))
    metadata = {'profile': PROFILE, 'attempt': attempt, 'source_digest': identity, 'excluded': excluded,
                'repository_key': cache_key, 'workflow': args.workflow, 'selectors': args.selectors, 'require_warm': args.require_warm,
                'queue_timeout_seconds': timeout, 'snapshot_seconds': time.monotonic() - started}
    if suite is not None:
        metadata['suite'] = suite
        from suite import suite_config
        suite_config(metadata)  # Reject changed source before any remote submission.
    if args.workflow == 'surface':
        metadata['surface_app'] = args.surface_app
    if args.workflow == 'docker':
        metadata['docker'] = spec
    write_metadata(output / 'submission.json', metadata)
    scripts = Path(__file__).resolve().parent
    ssh = ['ssh', *SSH_OPTIONS, args.host]
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
    bundle_id, payload = bundle(scripts)
    metadata['worker_bundle'] = bundle_id
    request = {'identity': bundle_id, 'attempt_id': attempt, 'repo_key': cache_key}
    def prepare_remote(request):
        command = 'python3 - ' + shlex.quote(json.dumps(request))
        return json.loads(run(*ssh, command, input=(scripts / 'worker_bundle.py').read_text(),
                              capture_output=True, text=True).stdout)
    prepared = prepare_remote(request)
    metadata['worker_bundle_cache_hit'] = not prepared['missing']
    if prepared['missing']:
        # Payload travels over stdin, never in shell argv (large bundles can exceed ARG_MAX).
        script = (scripts / 'worker_bundle.py').read_text().split("if __name__ == '__main__':")[0]
        script += '\nprint(json.dumps(prepare(Path.home() / "pandora-warm", **' + repr(request | {'payload': payload}) + ')))\n'
        prepared = json.loads(run(*ssh, 'python3 -', input=script, capture_output=True, text=True).stdout)
    if prepared.get('worker_config'):
        from worker_config import validate
        metadata['worker_config'] = validate(prepared['worker_config'])
    home = prepared['home']
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', home):
        raise RuntimeError('Unsupported remote home path')
    root = home + '/pandora-warm'
    remote = root + '/runs/' + attempt
    cached = prepared['cached']
    options = ['--link-dest=' + cached] if cached else []
    if uses_source:
        print(f'[pandora] transferring changed source; frozen identity {identity[:12]}', flush=True)
    else:
        print('[pandora] preparing image-only request', flush=True)
    transfer = time.monotonic()
    result = run('rsync', '-rlpc', '--delete', '--stats',
                 '-e', 'ssh ' + ' '.join(SSH_OPTIONS), *options,
                 str(output / 'source') + '/', f'{args.host}:{remote}/source/',
                 capture_output=True, text=True)
    (output / 'transfer.log').write_text(result.stdout + result.stderr)
    metadata['transfer_seconds'] = time.monotonic() - transfer
    write_metadata(output / 'submission.json', metadata)
    run('scp', *SSH_OPTIONS, '-q', str(output / 'manifest.json'), str(output / 'submission.json'),
        f'{args.host}:{remote}/')
    # All links reference complete immutable source directories, never containers.
    phase = 'source transfer' if uses_source else 'request staging'
    print(f'[pandora] staged {attempt}; {phase} {metadata["transfer_seconds"]:.1f}s', flush=True)
    # systemd owns the worker independently of this SSH connection. Never retry
    # this start after ambiguous acknowledgement; reconnect by the same attempt.
    runtime_limit = timeout + (metadata.get('worker_config', {}).get('execution_seconds', 1500) + 180)
    worker_command = 'exec python3 -u worker.py >stdout.log 2>stderr.log'
    command = (f'sudo systemd-run --quiet --collect --unit=pandora-worker-{attempt} '
               f'--uid=ubuntu --working-directory={remote} '
               f'--property=RuntimeMaxSec={runtime_limit}s --property=TimeoutStopSec=30s '
               '--property=KillMode=control-group '
               f'--property=ExecStopPost={shlex.quote("/usr/bin/python3 " + remote + "/service_cleanup.py " + remote)} /bin/bash -c ' + shlex.quote(worker_command))
    if uses_source:
        command = f'python3 {remote}/source_cache.py publish {cache_key} {attempt} && ' + command
    launched = subprocess.run([*ssh, command])
    if launched.returncode:
        print('[pandora] Start acknowledgement unavailable; checking the existing attempt only.', flush=True)
    status = follow(args.host, output, artifact_delivery_limit_bytes=artifact_limit)
    metadata['total_seconds'] = time.monotonic() - started
    metadata['exit_code'] = status
    write_metadata(output / 'submission.json', metadata)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
