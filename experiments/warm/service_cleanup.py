"""Idempotent attempt-owned cleanup, including systemd's worker-death path."""
import json
from pathlib import Path
import re
import subprocess
import sys

import cleanup_identity


def cleanup(attempt):
    attempt = Path(attempt)
    if not re.fullmatch('[0-9a-f]{32}', attempt.name):
        raise ValueError('Invalid attempt identity')
    pending = attempt / 'service-cleanup.pending'
    if not pending.exists():
        return True
    name = 'pandora-warm-' + attempt.name
    # Names are reserved before creation. A lost create acknowledgement does not
    # prevent cleanup. Never discover ownership from user-supplied container IDs.
    names = [name + suffix for suffix in ('-proxy', '-pool', '-db', '')]
    errors = []
    for owned in names:
        removed, error = cleanup_identity.remove_container(owned, {
            'pandora.attempt': attempt.name, 'pandora.workflow': 'journey'})
        if not removed:
            errors.append(error)
    removed, error = cleanup_identity.remove_network(name, {'pandora.attempt': attempt.name})
    if not removed:
        errors.append(error)
    for owned in names:
        absent, error = cleanup_identity.absent_container(owned)
        if error:
            errors.append(error)
        elif not absent:
            errors.append('Container name remains present after cleanup: ' + owned)
    absent, error = cleanup_identity.absent_network(name)
    if error:
        errors.append(error)
    elif not absent:
        errors.append('Network name remains present after cleanup: ' + name)
    check = subprocess.run(['sudo', 'docker', 'ps', '-a', '--filter',
                            'label=pandora.attempt=' + attempt.name, '--format', '{{.Names}}'],
                           capture_output=True, text=True, timeout=30)
    networks = subprocess.run(['sudo', 'docker', 'network', 'ls', '--filter',
                               'label=pandora.attempt=' + attempt.name, '--format', '{{.Name}}'],
                              capture_output=True, text=True, timeout=30)
    verified = not errors and check.returncode == 0 and not check.stdout.strip() and networks.returncode == 0 and not networks.stdout.strip()
    (attempt / 'service-cleanup.json').write_text(json.dumps({'verified': verified, 'errors': errors}) + '\n')
    if verified:
        subprocess.run(['sudo', 'systemctl', 'stop', name + '-deadline.timer'],
                       capture_output=True, timeout=30)
        pending.unlink(missing_ok=True)
    return verified


if __name__ == '__main__':
    from docker_cleanup import cleanup as cleanup_docker
    from suite_parent_cleanup import cleanup as cleanup_suite
    suite_clean = cleanup_suite(Path(sys.argv[1]))
    services = cleanup(sys.argv[1])
    containers = cleanup_docker(Path(sys.argv[1]))
    from dependencies import cleanup as cleanup_dependencies
    dependencies = cleanup_dependencies(Path(sys.argv[1]))
    from admission import record_cleanup
    verified = record_cleanup(Path(sys.argv[1]), services and containers and suite_clean and dependencies)
    raise SystemExit(0 if verified else 70)
