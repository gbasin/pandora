"""Clean one Docker workflow, also invoked by systemd after worker death."""
import json
import os
from pathlib import Path
import re
import subprocess

BUILDER = 'pandora-docker-builds-v1'
BUILDER_CONTAINER = 'buildx_buildkit_' + BUILDER + '0'


def cleanup(attempt):
    if not re.fullmatch('[a-f0-9]{32}', attempt.name):
        raise ValueError('Invalid attempt')
    pending = attempt / 'docker-cleanup.pending'
    if not pending.exists():
        return True
    kind = pending.read_text()
    name = 'pandora-warm-' + attempt.name
    errors = []
    if kind == 'build':
        exists = subprocess.run(['sudo', 'docker', 'buildx', 'inspect', BUILDER], capture_output=True, timeout=30)
        if exists.returncode == 0:
            stopped = subprocess.run(['sudo', 'docker', 'buildx', 'stop', BUILDER], capture_output=True, text=True, timeout=30)
            if stopped.returncode:
                errors.append(stopped.stderr)
        check_name = BUILDER_CONTAINER
        command = ['sudo', 'docker', 'ps', '--filter', 'name=^/' + check_name + '$', '--format', '{{.Names}}']
    else:
        removed = subprocess.run(['sudo', 'docker', 'rm', '-f', '-v', name], capture_output=True, text=True, timeout=30)
        if removed.returncode and 'No such container' not in removed.stderr:
            errors.append(removed.stderr)
        command = ['sudo', 'docker', 'ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}']
    check = subprocess.run(command, capture_output=True, text=True, timeout=30)
    mount = attempt / 'mount'
    if mount.exists():
        ownership = subprocess.run(['sudo', 'chown', '-R', '--no-dereference', f'{os.getuid()}:{os.getgid()}', str(mount)],
                                   capture_output=True, text=True, timeout=30)
        if ownership.returncode:
            errors.append(ownership.stderr)
    verified = not errors and check.returncode == 0 and not check.stdout.strip()
    (attempt / 'docker-cleanup.json').write_text(json.dumps({'verified': verified, 'errors': errors}) + '\n')
    if verified:
        subprocess.run(['sudo', 'systemctl', 'stop', name + '-deadline.timer'], capture_output=True, timeout=30)
        pending.unlink(missing_ok=True)
    return verified
