"""One service-backed journey on the shared snapshot, image and worker lease."""
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time
from service_cleanup import cleanup

SERVICES = [
    ('db', 'postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6',
     ['POSTGRES_USER=ike_owner', 'POSTGRES_PASSWORD=local-owner', 'POSTGRES_DB=ike', 'POSTGRES_HOST_AUTH_METHOD=password'], '768m'),
    ('pool', 'edoburu/pgbouncer@sha256:4c1ca296ef525f108f5d3552cc337c0c09587cf8dae7f0067fd93349e47dc1cd',
     ['DB_HOST=127.0.0.1', 'DB_PORT=5432', 'DB_USER=ike_application', 'DB_PASSWORD=local-application', 'AUTH_TYPE=plain', 'POOL_MODE=transaction', 'LISTEN_PORT=6432'], '256m'),
    ('proxy', 'ghcr.io/neondatabase/wsproxy@sha256:7f2e2149aa6a57ba382a140102fba44f5053f3e44389ccc18adcecf896054efb',
     ['LISTEN_PORT=:5433', 'ALLOW_ADDR_REGEX=^pgbouncer:6432$', 'APPEND_PORT=', 'LOG_TRAFFIC=false', 'LOG_CONN_INFO=false'], '128m'),
]


def docker(*args, **kwargs):
    return subprocess.run(['sudo', 'docker', *args], check=True, timeout=120, **kwargs)


def execute(attempt, image, manifest, dep_entries, metrics):
    submitted = json.loads((attempt / 'submission.json').read_text())
    if submitted['selectors'] != ['S0-01']:
        raise ValueError('Unsupported journey selection')
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
        docker('cp', str(attempt / 'journey.mjs'), name + ':/workspace/source/pandora-journey.mjs')
        print('[pandora] starting isolated database, pooler and proxy; no host ports', flush=True)
        for short, service_image, env, memory in SERVICES:
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
        child = subprocess.Popen(['sudo', 'docker', 'exec', '-w', '/workspace/source/packages/scenarios',
                                  name, 'node', '--import', 'tsx', '/workspace/source/pandora-journey.mjs'])
        while True:
            try:
                status = child.wait(timeout=10)
                break
            except subprocess.TimeoutExpired:
                print('[pandora] journey S0-01 running; services owned by this attempt', flush=True)
        states = []
        for suffix in ('', '-db', '-pool', '-proxy'):
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
        metrics['execution_seconds'] = time.monotonic() - started
        metrics['exit_code'] = status
        (attempt / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    return status
