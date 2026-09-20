"""Read/cancel exactly one experiment attempt. Sent to the worker over SSH."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys


def artifact_stats(path, attempt):
    root = path.lstat()
    if not stat.S_ISDIR(root.st_mode):
        raise ValueError('Attempt root is not an ordinary directory')
    terminal_path = path / 'terminal.json'
    if terminal_path.is_symlink() or not terminal_path.is_file():
        raise ValueError('Missing verified terminal receipt')
    terminal = json.loads(terminal_path.read_text())
    if terminal.get('attempt') != attempt or terminal.get('cleanup_verified') is not True:
        raise ValueError('Unverified terminal identity or cleanup')
    manifest_path = path / 'artifacts.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Missing artifact manifest')
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or any(not isinstance(name, str) or not isinstance(digest, str)
                                             for name, digest in manifest.items()):
        raise ValueError('Invalid artifact manifest')
    sizes = {}
    for name in manifest:
        relative = PurePosixPath(name)
        if (not name or '\x00' in name or '\n' in name or '\r' in name or str(relative) != name or
                relative.is_absolute() or '.' in relative.parts or '..' in relative.parts):
            raise ValueError('Unsafe artifact path')
        current = path
        for part in relative.parts[:-1]:
            current = current / part
            try:
                parent = current.lstat()
            except FileNotFoundError:
                raise ValueError('Missing declared artifact: ' + name) from None
            if stat.S_ISLNK(parent.st_mode):
                raise ValueError('Declared artifact path contains a symlink: ' + name)
            if not stat.S_ISDIR(parent.st_mode):
                raise ValueError('Declared artifact parent is not a directory: ' + name)
        target = path.joinpath(*relative.parts)
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            raise ValueError('Missing declared artifact: ' + name) from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Declared artifact is not a regular file: ' + name)
        sizes[name] = metadata.st_size
    return {'artifact_sizes': sizes, 'artifact_total_bytes': sum(sizes.values())}


def main():
    attempt, action = sys.argv[1:3]
    offsets = [int(x) for x in sys.argv[3:5]] or [0, 0]
    if len(offsets) != 2 or any(x < 0 for x in offsets):
        raise ValueError('Invalid log offsets')
    if not re.fullmatch('[0-9a-f]{32}', attempt) or action not in {'status', 'cancel', 'release', 'artifact-stats'}:
        raise ValueError('Invalid attempt/action')
    path = Path.home() / 'pandora-warm/runs' / attempt
    if action == 'artifact-stats':
        print(json.dumps(artifact_stats(path, attempt)))
        return
    if action == 'cancel':
        path.mkdir(parents=True, exist_ok=True)
        (path / 'cancel.request').touch()
        registration = path / 'worker.json'
        if registration.exists():
            worker = json.loads(registration.read_text())
            pid = worker['pid']
            proc = Path(f'/proc/{pid}/stat')
            if proc.exists() and proc.read_text().split()[21] == worker['start_ticks']:
                os.kill(pid, signal.SIGTERM)
    terminal = path / 'terminal.json'
    if terminal.exists():
        result = json.loads(terminal.read_text())
        if action == 'release' and result.get('cleanup_verified'):
            (path / 'released').touch()
            if result.get('workflow') == 'suite-run':
                sys.path.insert(0, str(path))
                from suite_parent_cleanup import validate_registry
                for identity in validate_registry(path, json.loads((path / 'children.json').read_text())):
                    child = path.parent / identity
                    if (child / 'terminal.json').exists():
                        receipt = json.loads((child / 'terminal.json').read_text())
                        if receipt.get('attempt') == identity and receipt.get('cleanup_verified'):
                            (child / 'released').touch()
    elif not (path / 'worker.json').exists() and (path / 'cancel.request').exists():
        # Registration precedes the worker's cancel-marker check. A late worker
        # therefore exits before preparation/execution even if no PID exists yet.
        result = {'exit_code': 130, 'cleanup_verified': True,
                  'state': 'cancelled-before-start'}
    else:
        result = {'state': 'active-or-unresolved'}
    result['registered'] = (path / 'worker.json').exists()
    result['offsets'] = offsets
    result['more_logs'] = False
    for index, name in enumerate(['stdout', 'stderr']):
        log = path / (name + '.log')
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(offsets[index])
                result[name] = stream.read(65536).decode('utf-8', errors='replace')
                result['offsets'][index] = stream.tell()
                result['more_logs'] = result['more_logs'] or stream.tell() < log.stat().st_size
    print(json.dumps(result))


if __name__ == '__main__':
    main()
