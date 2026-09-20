"""Idempotent attempt-owned cleanup, including systemd's worker-death path."""
import json
from pathlib import Path
import re
import subprocess
import sys


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
        result = subprocess.run(['sudo', 'docker', 'rm', '-f', '-v', owned],
                                capture_output=True, text=True, timeout=30)
        if result.returncode and 'No such container' not in result.stderr:
            errors.append(result.stderr)
    result = subprocess.run(['sudo', 'docker', 'network', 'rm', name],
                            capture_output=True, text=True, timeout=30)
    if result.returncode and 'not found' not in result.stderr:
        errors.append(result.stderr)
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
    raise SystemExit(0 if cleanup(sys.argv[1]) else 70)
