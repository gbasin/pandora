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
            if (submitted.get('profile') == PROFILE and terminal.get('cleanup_verified')
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
        # A terminal receipt alone must never authorize killing a live container.
        running = subprocess.run(['sudo', 'docker', 'ps', '--filter', 'name=^/' + name + '$',
                                  '--format', '{{.Names}}'], capture_output=True, text=True)
        if running.returncode or running.stdout.strip():
            continue
        containers = subprocess.run(['sudo', 'docker', 'ps', '-a', '--filter', 'name=^/' + name + '$',
                                     '--format', '{{.Names}}'], capture_output=True, text=True)
        if containers.returncode:
            continue
        if containers.stdout.strip():
            removed = subprocess.run(['sudo', 'docker', 'rm', name], stdout=subprocess.DEVNULL)
            if removed.returncode:
                continue
        shutil.rmtree(path)
