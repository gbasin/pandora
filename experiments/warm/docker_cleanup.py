"""Clean one Docker workflow, also invoked by systemd after worker death."""
import json
import os
from pathlib import Path
import re
import subprocess
import builder_owner

BUILDER = 'pandora-docker-builds-v1'
BUILDER_CONTAINER = 'buildx_buildkit_' + BUILDER + '0'


def cleanup(attempt, builder_lease=None):
    if not re.fullmatch('[a-f0-9]{32}', attempt.name):
        raise ValueError('Invalid attempt')
    pending = attempt / 'docker-cleanup.pending'
    if not pending.exists():
        # Claim can be interrupted between durable ownership and marker creation.
        return builder_owner.cleanup(attempt, BUILDER, "docker-cleanup.pending")
    kind = pending.read_text()
    name = 'pandora-warm-' + attempt.name
    errors = []
    if kind == 'build':
        verified_builder = (builder_lease.close() if builder_lease is not None else
                            builder_owner.cleanup(attempt, BUILDER, 'docker-cleanup.pending'))
        if not verified_builder:
            errors.append('Docker builder ownership or cleanup remains unresolved')
        check = subprocess.CompletedProcess([], 0, '', '')
    elif kind == 'run':
        removed = subprocess.run(['sudo', 'docker', 'rm', '-f', '-v', name], capture_output=True, text=True, timeout=30)
        if removed.returncode and 'No such container' not in removed.stderr:
            errors.append(removed.stderr)
        command = ['sudo', 'docker', 'ps', '-a', '--filter', 'name=^/' + name + '$', '--format', '{{.Names}}']
        check = subprocess.run(command, capture_output=True, text=True, timeout=30)
    else:
        raise ValueError("Unknown Docker cleanup kind")
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
