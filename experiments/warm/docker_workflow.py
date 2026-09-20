"""Bounded build and foreground run profiles on the shared worker lease."""
import json
import io
import tarfile
import os
from pathlib import Path
import shutil
import subprocess
import time
from docker_cleanup import BUILDER, cleanup
from docker_images import publish, remove


def docker(*args, **kwargs):
    return subprocess.run(['sudo', 'docker', *args], check=True, timeout=120, **kwargs)


def wait(child, seconds, message):
    deadline = time.monotonic() + seconds
    while True:
        try:
            return child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            if time.monotonic() >= deadline:
                raise TimeoutError(message + ': deadline exceeded')
            print('[pandora] ' + message, flush=True)


def build(attempt, spec):
    config = attempt / 'buildkitd.toml'
    config.write_text('[worker.oci]\n  gc = true\n  reservedSpace = "2GB"\n  maxUsedSpace = "12GB"\n  minFreeSpace = "10GB"\n')
    exists = subprocess.run(['sudo', 'docker', 'buildx', 'inspect', BUILDER], capture_output=True).returncode == 0
    if not exists:
        docker('buildx', 'create', '--name', BUILDER, '--driver=docker-container',
               '--driver-opt', 'memory=6g,memory-swap=6g,cpu-period=100000,cpu-quota=200000,image=moby/buildkit@sha256:28a898719c18a33f4e8000685287fa36fd0dd9560c6440227d3a732d79bb41d8',
               '--buildkitd-config', str(config))
    request = spec['request']
    dockerfile = attempt / 'source' / request['dockerfile']
    if not dockerfile.is_file() or dockerfile.is_symlink():
        raise ValueError('Declared Dockerfile is missing from captured source or is a symlink')
    image = 'pandora-build:' + attempt.name
    args = ['sudo', 'env', 'BUILDX_METADATA_PROVENANCE=max', 'docker', 'buildx', 'build',
            '--builder', BUILDER, '--platform', 'linux/amd64', '--load', '--provenance=false',
            '--progress=plain', '--metadata-file', str(attempt / 'results/build-metadata.json'),
            '-f', str(dockerfile), '-t', image, str(attempt / 'source')]
    print('[pandora] building ' + request['tag'] + ' remotely; previous mapping stays valid until success', flush=True)
    child = subprocess.Popen(args)
    status = wait(child, 900, 'Docker build running; shared BuildKit cache retained')
    if status:
        print('[pandora] build failed; previous worktree tag mapping preserved', flush=True)
        return status, None
    image_id = docker('image', 'inspect', image, '--format', '{{.Id}}', capture_output=True, text=True).stdout.strip()
    return 0, image_id


def mount_owner(name):
    user = json.loads(docker('inspect', name, '--format', '{{json .Config.User}}', capture_output=True, text=True).stdout)
    if not user:
        return '0:0'
    def rows(filename):
        result = subprocess.run(['sudo', 'docker', 'cp', name + ':/etc/' + filename, '-'], capture_output=True, timeout=30)
        if result.returncode:
            return []
        if len(result.stdout) > 1024 * 1024:
            raise ValueError('Image user database is too large')
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            member = next((m for m in archive.getmembers() if Path(m.name).name == filename and m.isfile()), None)
            return archive.extractfile(member).read().decode().splitlines() if member else []
    fields = user.split(':')
    username = fields[0]
    passwd = [line.split(':') for line in rows('passwd')]
    match = next((v for v in passwd if len(v) >= 4 and (v[0] == username or v[2] == username)), None)
    uid = username if username.isdecimal() else match[2] if match else None
    group = fields[1] if len(fields) == 2 else match[3] if match else '0'
    if not group.isdecimal():
        groups = [line.split(':') for line in rows('group')]
        match = next((v for v in groups if len(v) >= 3 and v[0] == group), None)
        group = match[2] if match else None
    if uid is None or group is None or not uid.isdecimal() or not group.isdecimal():
        raise ValueError('Cannot resolve image user for a writable snapshot: ' + user)
    return uid + ':' + group


def execute_run(attempt, spec):
    request = spec['request']
    name = 'pandora-warm-' + attempt.name
    image = spec['image']['image_id']
    mount_args = []
    if request['mount']:
        # Never expose shared rsync hardlinks to a writable container.
        mount = attempt / 'mount'
        shutil.copytree(attempt / 'source', mount, symlinks=True)
        options = 'type=bind,src=' + str(mount) + ',dst=' + request['mount']['target']
        if request['mount']['readonly']:
            options += ',readonly'
        mount_args = ['--mount', options]
    print(f'[pandora] running {request["tag"]} at pinned image {image}; worktree {spec["worktree_key"][:12]}', flush=True)
    docker('create', '--name', name, '--label', 'pandora.workflow=docker',
           '--label', 'pandora.attempt=' + attempt.name,
           '--cpus=2', '--memory=6g', '--memory-swap=6g', '--pids-limit=512',
           '--shm-size=1g', '--cap-drop=ALL', '--security-opt=no-new-privileges',
           '--network', spec['config'].get('network', 'none'), '--init',
           *mount_args, image, *request['command'], stdout=subprocess.DEVNULL)
    if request['mount']:
        subprocess.run(['sudo', 'chown', '-R', '--no-dereference', mount_owner(name), str(mount)], check=True, timeout=30)
    status = wait(subprocess.Popen(['sudo', 'docker', 'start', '--attach', name]), 1200,
                  'Docker run active; no local container started')
    state = json.loads(docker('inspect', name, '--format', '{{json .State}}', capture_output=True, text=True).stdout)
    (attempt / 'results/container-state.json').write_text(json.dumps(state, indent=2) + '\n')
    if state['OOMKilled']:
        print('[pandora] container exceeded its memory limit', flush=True)
        status = 137
    elif state['ExitCode'] != status:
        status = state['ExitCode'] if state['ExitCode'] else 70
    for output in spec['config']['outputs']:
        destination = attempt / 'results/outputs' / output['workspace']
        destination.parent.mkdir(parents=True, exist_ok=True)
        copied = subprocess.run(['sudo', 'docker', 'cp', name + ':' + output['container'], str(destination)], timeout=120)
        if copied.returncode:
            print('[pandora] declared output missing: ' + output['container'], flush=True)
            if status == 0:
                status = 70
        elif destination.exists():
            subprocess.run(['sudo', 'chown', '-R', '--no-dereference', f'{os.getuid()}:{os.getgid()}',
                            str(destination)], check=True, timeout=30)
    return status, image


def execute(attempt, submitted, metrics):
    root = attempt.parent.parent
    spec = submitted['docker']
    request = spec['request']
    kind = request['kind']
    tag = request['tag']
    key = spec['worktree_key']
    results = attempt / 'results'
    results.mkdir()
    started = time.monotonic()
    status = 70
    image = None
    if kind == 'remove':
        removed = remove(root, key, tag)
        if removed is not None:
            image = removed['image_id']
            status = 0
            print(f'[pandora] removed worktree mapping {tag}; accepted runs keep their pinned image', flush=True)
        else:
            print('[pandora] unknown worktree image: ' + tag, flush=True)
            status = 1
    else:
        (attempt / 'docker-cleanup.pending').write_text(kind)
        name = 'pandora-warm-' + attempt.name
        try:
            subprocess.run(['sudo', 'systemd-run', '--quiet', '--unit=' + name + '-deadline',
                            '--on-active=' + ('15m' if kind == 'build' else '20m'),
                            '/usr/bin/systemctl', 'stop', 'pandora-worker-' + attempt.name + '.service'],
                           check=True, timeout=30)
            if kind == 'build':
                status, image = build(attempt, spec)
            elif kind == 'run':
                status, image = execute_run(attempt, spec)
            else:
                raise ValueError('Unsupported Docker workflow')
        finally:
            if not cleanup(attempt):
                status = 70
    if status == 0 and kind == 'build':
        publish(root, key, tag, {'tag': tag, 'image_id': image, 'source_digest': submitted['source_digest'],
                                'attempt': attempt.name, 'platform': 'linux/amd64'})
        print(f'[pandora] built {tag} -> {image}; worktree {key[:12]}', flush=True)
    report = {'kind': kind, 'tag': tag, 'image_id': image, 'exit_code': status, 'worktree_key': key,
              'source_digest': submitted['source_digest'] if kind == 'build' or request.get('mount') else None,
              'image_source_digest': spec.get('image', {}).get('source_digest'), 'platform': 'linux/amd64',
              'builder': BUILDER if kind == 'build' else None}
    (results / 'docker.json').write_text(json.dumps(report, indent=2) + '\n')
    (results / 'exit-code').write_text(str(status) + '\n')
    metrics['execution_seconds'] = time.monotonic() - started
    metrics['exit_code'] = status
    (attempt / 'metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    return status
