"""Idempotent cleanup for one attempt-owned surface container."""
import json
from pathlib import Path
import re
import subprocess

import cleanup_identity


def cleanup(attempt):
    attempt = Path(attempt)
    if not re.fullmatch('[0-9a-f]{32}', attempt.name):
        raise ValueError('Invalid attempt identity')
    pending = attempt / 'surface-cleanup.pending'
    if not pending.exists():
        return True
    name = 'pandora-warm-' + attempt.name
    errors = []
    removed, error = cleanup_identity.remove_container(name, {
        'pandora.attempt': attempt.name,
        'pandora.workflow': 'surface',
        'pandora.experiment': 'warm-surface',
    })
    if not removed:
        errors.append(error)
    absent, error = cleanup_identity.absent_container(name)
    if error:
        errors.append(error)
    elif not absent:
        errors.append('Container name remains present after cleanup: ' + name)
    check = subprocess.run(['sudo', 'docker', 'ps', '-a', '--filter',
                            'label=pandora.attempt=' + attempt.name, '--format', '{{.Names}}'],
                           capture_output=True, text=True, timeout=30)
    verified = not errors and check.returncode == 0 and not check.stdout.strip()
    (attempt / 'surface-cleanup.json').write_text(json.dumps({'verified': verified, 'errors': errors}) + '\n')
    if verified:
        subprocess.run(['sudo', 'systemctl', 'stop', name + '-deadline.timer'],
                       capture_output=True, timeout=30)
        pending.unlink(missing_ok=True)
    return verified
