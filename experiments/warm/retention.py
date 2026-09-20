"""Conservative retention of acknowledged attempts, never unresolved work."""
import json
from pathlib import Path
import re
import shutil
import subprocess
from source_cache import locked, protected
from dependency_images import remember as remember_image

PROFILE = 'integrated-surface-v1'
KEEP_ATTEMPTS = 10


def candidates(root, marker, keep=KEEP_ATTEMPTS, protected=()):
    protected = {Path(p).resolve() for p in protected}
    eligible = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir() or not re.fullmatch('[a-f0-9]{32}', path.name):
            continue
        try:
            submitted = json.loads((path / 'submission.json').read_text())
            terminal = json.loads((path / 'terminal.json').read_text())
            receipt = path / marker
            if (submitted.get('profile') == PROFILE and terminal.get('attempt') == path.name and terminal.get('cleanup_verified') is True
                    and receipt.is_file() and path.resolve() not in protected):
                eligible.append((receipt.stat().st_mtime_ns, path))
        except (OSError, ValueError):
            continue
    eligible.sort(reverse=True)
    return [path for _, path in eligible[keep:]]


def local(state, current):
    for path in candidates(state, 'completed.json', protected=[current]):
        shutil.rmtree(path)


def remote(root):
    with locked(root):
        _remote_locked(root)


def _remote_locked(root):
    for path in candidates(root / 'runs', 'released', protected=protected(root)):
        name = 'pandora-warm-' + path.name
        found, container = _stopped_surface_container(name, path.name)
        if found and container is None:
            continue
        if container is not None:
            # Delete the inspected immutable ID, never a name that a peer could replace.
            try:
                removed = subprocess.run(['sudo', 'docker', 'rm', container], stdout=subprocess.DEVNULL, timeout=30)
            except subprocess.SubprocessError:
                continue
            if removed.returncode:
                continue
        shutil.rmtree(path)


def _stopped_surface_container(name, identity):
    """Return whether a name exists and its removable inspected container ID."""
    try:
        listed = subprocess.run(['sudo', 'docker', 'ps', '-a', '--filter', 'name=^/' + name + '$',
                                 '--format', '{{json .}}'], capture_output=True, text=True, timeout=30)
    except subprocess.SubprocessError:
        return True, None
    if listed.returncode:
        return True, None
    try:
        entries = [json.loads(line) for line in listed.stdout.splitlines() if line]
        if not entries:
            return False, None
        if len(entries) != 1 or not isinstance(entries[0].get('ID'), str):
            return True, None
        inspected = subprocess.run(['sudo', 'docker', 'inspect', entries[0]['ID'], '--format', '{{json .}}'],
                                    capture_output=True, text=True, timeout=30)
        if inspected.returncode:
            return True, None
        container = json.loads(inspected.stdout)
        container_id = container.get('Id')
        state = container.get('State')
        labels = container.get('Config', {}).get('Labels')
    except (AttributeError, TypeError, ValueError, subprocess.SubprocessError):
        return True, None
    modern = {'pandora.experiment': 'warm-surface', 'pandora.workflow': 'surface',
              'pandora.attempt': identity}
    legacy = {'pandora.experiment': 'warm-surface'}
    if (not isinstance(container_id, str) or not re.fullmatch('[a-f0-9]{64}', container_id) or
            not container_id.startswith(entries[0]['ID']) or
            container.get('Name') != '/' + name or not isinstance(state, dict) or
            state.get('Status') != 'exited' or state.get('Running') is not False or
            labels not in (modern, legacy)):
        return True, None
    return True, container_id
