"""One service-backed journey on the shared snapshot, image and worker lease."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
import re
from service_cleanup import cleanup

SERVICES = [
    ('db', 'postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6',
     ['POSTGRES_USER=ike_owner', 'POSTGRES_PASSWORD=local-owner', 'POSTGRES_DB=ike', 'POSTGRES_HOST_AUTH_METHOD=password'], '768m'),
    ('pool', 'edoburu/pgbouncer@sha256:4c1ca296ef525f108f5d3552cc337c0c09587cf8dae7f0067fd93349e47dc1cd',
     ['DB_HOST=127.0.0.1', 'DB_PORT=5432', 'DB_USER=ike_application', 'DB_PASSWORD=local-application', 'AUTH_TYPE=plain', 'POOL_MODE=transaction', 'LISTEN_PORT=6432'], '256m'),
    ('proxy', 'ghcr.io/neondatabase/wsproxy@sha256:7f2e2149aa6a57ba382a140102fba44f5053f3e44389ccc18adcecf896054efb',
     ['LISTEN_PORT=:5433', 'ALLOW_ADDR_REGEX=^pgbouncer:6432$', 'APPEND_PORT=', 'LOG_TRAFFIC=false', 'LOG_CONN_INFO=false'], '128m'),
]

SCENARIO_ID_PATTERN = re.compile(r'^(?:S[0-6]|SX)-\d{2}\Z')


def journey_config(submitted):
    """Return one canonical focused journey invocation and its explicit mode."""
    selectors = submitted.get('selectors')
    if not isinstance(selectors, list) or not selectors or not isinstance(selectors[0], str):
        raise ValueError('Use journey <id> [--fault dropped] [--update]; other journey options are not routed')
    journey_id = selectors[0]
    if not SCENARIO_ID_PATTERN.fullmatch(journey_id):
        raise ValueError('Use journey <id> [--fault dropped] [--update]; other journey options are not routed')
    tail = selectors[1:]
    if tail == []:
        return {'id': journey_id, 'fault': None, 'update': False}
    if tail == ['--update']:
        return {'id': journey_id, 'fault': None, 'update': True}
    if tail == ['--fault', 'dropped']:
        return {'id': journey_id, 'fault': 'dropped', 'update': False}
    if tail in (['--fault', 'dropped', '--update'], ['--update', '--fault', 'dropped']):
        return {'id': journey_id, 'fault': 'dropped', 'update': True}
    raise ValueError('Use journey <id> [--fault dropped] [--update]; other journey options are not routed')


def journey_command(config):
    """Build the isolated child command without changing the container-wide CI mode."""
    command = ['sudo', 'docker', 'exec', '-e',
               'PANDORA_JOURNEY_CONFIG=' + json.dumps(config, separators=(',', ':'))]
    command.extend(['-w', '/workspace/source/packages/scenarios',
                    'pandora-warm-' + config['attempt']])
    if config['update']:
        # The scenario CLI rejects updates when CI is set. Keep CI for the image,
        # services and ordinary invocation; remove it for this child alone.
        command.extend(['env', '-u', 'CI'])
    command.extend(['node', '--import', 'tsx', '/workspace/source/pandora-journey.mjs'])
    return command


def mark_cleanup_failure(attempt):
    """Ensure a report cannot advertise a successful run with failed cleanup."""
    suite_report = attempt / 'results/suite-shard.json'
    if suite_report.is_file():
        report = json.loads(suite_report.read_text())
        report['exit_code'] = 70
        report['errors']['infrastructureFailures'] += 1
        report['detail'] = 'Attempt-owned service cleanup failed'
        suite_report.write_text(json.dumps(report, indent=2) + '\n')
    report_path = attempt / 'results' / 'journey.json'
    if not report_path.is_file():
        return
    report = json.loads(report_path.read_text())
    report['status'] = 'fail'
    detail = report.get('detail', '')
    report['detail'] = (detail + '\n' if detail else '') + 'Attempt-owned service cleanup failed'
    report_path.write_text(json.dumps(report, indent=2) + '\n')


def docker(*args, **kwargs):
    return subprocess.run(['sudo', 'docker', *args], check=True, timeout=120, **kwargs)


def execute(attempt, image, manifest, dep_entries, metrics):
    submitted = json.loads((attempt / 'submission.json').read_text())
    is_suite = submitted.get('workflow') == 'suite'
    if is_suite:
        from suite import suite_config, suite_command
        config = suite_config(submitted) | {'attempt': attempt.name}
        command = suite_command(config)
        adapter = 'suite.mjs'
        label_text = 'suite ' + config['action']
    else:
        config = journey_config(submitted) | {'attempt': attempt.name}
        command = journey_command(config)
        adapter = 'journey.mjs'
        label_text = 'journey ' + config['id']
    services = [] if is_suite and config['action'] == 'plan' else SERVICES
    name = 'pandora-warm-' + attempt.name
    label = 'pandora.attempt=' + attempt.name
    status = 70
    started = time.monotonic()
    # This intent exists before any service creation and is also consumed by
    # systemd ExecStopPost after SIGKILL or the overall worker deadline.
    (attempt / 'service-cleanup.pending').touch()
    try:
        subprocess.run(['sudo', 'systemd-run', '--quiet', '--unit=' + name + '-deadline',
                        '--on-active=20m', '/usr/bin/systemctl', 'stop',
                        'pandora-worker-' + attempt.name + '.service'], check=True, timeout=30)
        docker('network', 'create', '--label', label, name, stdout=subprocess.DEVNULL)
        docker('run', '-d', '--name', name, '--label', label, '--label', 'pandora.workflow=journey',
               '--network', name, '--network-alias', 'pgbouncer', '--cpus=2',
               '--memory=6g', '--memory-swap=6g', '--pids-limit=512', '--init',
               '--cap-drop=ALL', '--security-opt=no-new-privileges', '-e', 'CI=true',
               '-e', 'WRANGLER_SEND_METRICS=false',
               '-e', 'DATABASE_OWNER_URL=postgres://ike_owner:local-owner@127.0.0.1:5432/ike',
               image, 'sleep', '1200', stdout=subprocess.DEVNULL)
        overlay = attempt / 'source-overlay.tar'
        install_paths = {e['path'] for e in dep_entries}
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
        docker('cp', str(overlay), name + ':/tmp/source.tar')
        docker('exec', name, 'tar', 'xf', '/tmp/source.tar', '-C', '/workspace/source')
        overlay.unlink()
        docker('cp', str(attempt / adapter), name + ':/workspace/source/pandora-' + adapter)
        print('[pandora] starting isolated database, pooler and proxy; no host ports' if services else
              '[pandora] planning suite in an isolated container; no database services', flush=True)
        for short, service_image, env, memory in services:
            docker('run', '-d', '--name', name + '-' + short, '--label', label,
                   '--label', 'pandora.workflow=journey', '--network', 'container:' + name,
                   '--cpus=.5', '--memory=' + memory, '--memory-swap=' + memory,
                   '--pids-limit=128', *[v for item in env for v in ['-e', item]],
                   service_image, stdout=subprocess.DEVNULL)
            if short == 'db':
                for _ in range(60):
                    ready = subprocess.run(['sudo', 'docker', 'exec', name + '-db',
                                            'pg_isready', '-U', 'ike_owner', '-d', 'ike'],
                                           capture_output=True, timeout=10)
                    if ready.returncode == 0:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('Database readiness deadline reached')
        docker('exec', name, 'node', 'tools/check-worktree-deps.mjs')
        child = subprocess.Popen(command)
        while True:
            try:
                status = child.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                print('[pandora] ' + label_text + ' running; services owned by this attempt', flush=True)
        states = []
        for suffix in ['', *['-' + service[0] for service in services]]:
            raw = docker('inspect', name + suffix, '--format', '{{json .State}}', capture_output=True, text=True)
            state = json.loads(raw.stdout)
            states.append({'name': name + suffix, 'state': state})
        (attempt / 'service-state.json').write_text(json.dumps(states, indent=2) + '\n')
        if not all(s['state']['Running'] and not s['state']['OOMKilled'] for s in states):
            status = 70
        docker('cp', name + ':/workspace/results', str(attempt / 'results'))
        subprocess.run(['sudo', 'chown', '-R', '--no-dereference', f'{os.getuid()}:{os.getgid()}',
                        str(attempt / 'results')], check=True, timeout=30)
    finally:
        print('[pandora] removing this attempt’s services and private network', flush=True)
        verified = cleanup(attempt)
        if not verified:
            status = 70
            mark_cleanup_failure(attempt)
        metrics['execution_seconds'] = time.monotonic() - started
        metrics['exit_code'] = status
        (attempt / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    return status
