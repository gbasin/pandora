"""Execute one admitted validation request in an attempt-private Docker network."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time

from journey import SERVICES
from service_cleanup import cleanup
from worker_config import docker_limits, execution_seconds, limits


SERVICE_SUITES = {'postgres', 'browser-integration'}
REQUIRED_ADAPTER_FILES = ('validation.mjs', 'validation-stack.mjs', 'validation-artifacts.mjs')


def docker(*args, **kwargs):
    return subprocess.run(['sudo', 'docker', *args], check=True, timeout=120, **kwargs)


def validation_config(submitted, attempt):
    """Validate the admitted request before any attempt-owned resource exists."""
    if submitted.get('workflow') != 'validation':
        raise ValueError('Validation executor requires workflow validation')
    from validation_request import validate_request
    request = validate_request(submitted.get('validation'))
    # The adapter may parallelize only within capacity the worker admitted.
    workers = min(2, max(1, limits(submitted)['cpu_millis'] // 1000))
    return request | {'attempt': attempt.name, 'workers': workers}


def adapter_sources(attempt):
    """Return the explicitly bundled adapter files, rejecting incomplete bundles."""
    sources = []
    for adapter in REQUIRED_ADAPTER_FILES:
        source = attempt / adapter
        if not source.is_file():
            raise ValueError('Validation adapter bundle lacks ' + adapter)
        sources.append((adapter, source))
    return sources


def validation_command(config, name):
    return ['sudo', 'docker', 'exec', '-e',
            'PANDORA_VALIDATION_CONFIG=' + json.dumps(config, separators=(',', ':')),
            '-w', '/workspace/source', name,
            'node', '/workspace/source/pandora-validation.mjs']


def _overlay(attempt, manifest, dep_entries):
    """Create a node-owned source overlay without copying installed dependencies."""
    overlay = attempt / 'source-overlay.tar'
    install_paths = {entry['path'] for entry in dep_entries}
    with tarfile.open(overlay, 'w') as archive:
        for entry in manifest:
            if entry['path'] in install_paths:
                continue
            path = attempt / 'source' / entry['path']
            info = archive.gettarinfo(str(path), arcname=entry['path'])
            info.uid = info.gid = 1000
            info.uname = info.gname = 'node'
            if info.isfile():
                with path.open('rb') as contents:
                    archive.addfile(info, contents)
            else:
                archive.addfile(info)
    return overlay


def _copy_results(attempt, name):
    docker('cp', name + ':/workspace/results', str(attempt / 'results'))
    subprocess.run(['sudo', 'chown', '-R', '--no-dereference',
                    f'{os.getuid()}:{os.getgid()}', str(attempt / 'results')],
                   check=True, timeout=30)


def _service_states(name, services):
    states = []
    for suffix in ['', *['-' + service[0] for service in services]]:
        raw = docker('inspect', name + suffix, '--format', '{{json .State}}',
                     capture_output=True, text=True)
        states.append({'name': name + suffix, 'state': json.loads(raw.stdout)})
    return states


def _resources(name):
    """Capture the main container's cgroup receipt before attempt cleanup."""
    paths = {
        'memory_events': '/sys/fs/cgroup/memory.events',
        'memory_peak_bytes': '/sys/fs/cgroup/memory.peak',
        'cpu_stat': '/sys/fs/cgroup/cpu.stat',
    }
    receipt = {}
    for key, path in paths.items():
        raw = docker('exec', name, 'cat', path, capture_output=True, text=True).stdout
        receipt[key] = raw
    events = dict(line.split(maxsplit=1) for line in receipt['memory_events'].splitlines() if line)
    oom_kill = events.get('oom_kill')
    if oom_kill is None or not oom_kill.isdigit():
        raise RuntimeError('Container cgroup receipt lacks a valid oom_kill counter')
    receipt['oom_kill'] = int(oom_kill)
    return receipt


def execute(attempt, image, manifest, dep_entries, metrics):
    """Run the supplied adapter, always recording cleanup and terminal metrics."""
    attempt = Path(attempt)
    submitted = json.loads((attempt / 'submission.json').read_text())
    config = validation_config(submitted, attempt)
    adapters = adapter_sources(attempt)
    services = SERVICES if config['suite'] in SERVICE_SUITES else []
    name = 'pandora-warm-' + attempt.name
    label = 'pandora.attempt=' + attempt.name
    status = 70
    child = None
    started = time.monotonic()
    # This marker precedes Docker creation so systemd can recover a lost create reply.
    (attempt / 'service-cleanup.pending').touch()
    try:
        subprocess.run(['sudo', 'systemd-run', '--quiet', '--unit=' + name + '-deadline',
                        '--on-active=' + str(execution_seconds(submitted)) + 's',
                        '/usr/bin/python3', str(attempt / 'deadline_stop.py'), str(attempt)],
                       check=True, timeout=30)
        docker('network', 'create', '--label', label, name, stdout=subprocess.DEVNULL)
        aliases = ['--network-alias', 'pgbouncer']
        if config['suite'] == 'postgres':
            # letters-realtime runs a Miniflare Worker that deliberately reaches
            # the direct Compose postgres service through wsproxy.
            aliases.extend(['--network-alias', 'postgres'])
        run_args = ['run', '-d', '--name', name, '--label', label,
                    '--label', 'pandora.workflow=validation', '--network', name,
                    *aliases, *docker_limits(submitted),
                    '--pids-limit=512', '--shm-size=1g', '--init', '--cap-drop=ALL',
                    '--security-opt=no-new-privileges', '-e', 'CI=true',
                    '-e', 'WRANGLER_SEND_METRICS=false',
                    '-e', 'DATABASE_OWNER_URL=postgres://app_owner:local-owner@127.0.0.1:5432/app',
                    image, 'sleep', str(execution_seconds(submitted) + 120)]
        docker(*run_args, stdout=subprocess.DEVNULL)
        overlay = _overlay(attempt, manifest, dep_entries)
        try:
            docker('cp', str(overlay), name + ':/tmp/source.tar')
            docker('exec', name, 'tar', 'xf', '/tmp/source.tar', '-C', '/workspace/source')
        finally:
            overlay.unlink(missing_ok=True)
        for adapter, source in adapters:
            docker('cp', str(source), name + ':/workspace/source/pandora-' + adapter)
        for short, service_image, environment, memory in services:
            if config['suite'] == 'postgres' and short == 'proxy':
                environment = [
                    'ALLOW_ADDR_REGEX=^(pgbouncer:6432|postgres:5432)$'
                    if value.startswith('ALLOW_ADDR_REGEX=') else value
                    for value in environment
                ]
            docker('run', '-d', '--name', name + '-' + short, '--label', label,
                   '--label', 'pandora.workflow=validation', '--network', 'container:' + name,
                   *docker_limits(submitted, short), '--pids-limit=128',
                   *[value for item in environment for value in ('-e', item)], service_image,
                   stdout=subprocess.DEVNULL)
            if short == 'db':
                for _ in range(60):
                    ready = subprocess.run(['sudo', 'docker', 'exec', name + '-db',
                                            'pg_isready', '-U', 'app_owner', '-d', 'app'],
                                           capture_output=True, timeout=10)
                    if ready.returncode == 0:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('Database readiness deadline reached')
        docker('exec', name, 'node', 'tools/check-worktree-deps.mjs')
        child = subprocess.Popen(validation_command(config, name))
        while True:
            try:
                status = child.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                print('[pandora] validation ' + config['suite'] +
                      ' running; services owned by this attempt', flush=True)
        states = _service_states(name, services)
        (attempt / 'service-state.json').write_text(json.dumps(states, indent=2) + '\n')
        resources = _resources(name)
        (attempt / 'resources.json').write_text(json.dumps(resources, indent=2) + '\n')
        if not all(state['state']['Running'] and not state['state']['OOMKilled'] for state in states):
            status = 70
        if resources['oom_kill']:
            print('[pandora] worker RAM OOM killed a process; reporting infrastructure failure', flush=True)
            status = 70
        _copy_results(attempt, name)
    except BaseException:
        # Metrics must not retain an adapter success if collection or execution failed.
        status = 70
        raise
    finally:
        if child is not None and child.poll() is None:
            # Do not leave a docker exec client attached if cleanup follows an interruption.
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        print('[pandora] removing this attempt’s services and private network', flush=True)
        if not cleanup(attempt):
            status = 70
        metrics['execution_seconds'] = time.monotonic() - started
        metrics['exit_code'] = status
        (attempt / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    return status
