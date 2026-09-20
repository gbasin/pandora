"""Recoverable publication of explicitly declared regular files.

This is deliberately a narrow, cooperative local publisher.  It records the
frozen value and requested value for each declared path before making a change.
It does not discover files, merge them, or protect against arbitrary writers.
"""
import base64
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


INTENT_FILE = 'publication/tracked-intent.json'


@dataclass(frozen=True)
class TrackedIntent:
    phase: str
    declarations: dict
    committed: tuple[str, ...]


@dataclass(frozen=True)
class PublicationReceipt:
    committed: tuple[str, ...]


class PublicationConflict(ValueError):
    """A cooperative writer changed a declared path; evidence is retained."""
    def __init__(self, paths, output):
        self.paths = tuple(paths)
        proposals = [str(Path(output) / 'results' / 'updates' /
                         path.relative_to(Path(output) / 'publication' / 'conflicts').with_suffix(''))
                     for path in self.paths]
        super().__init__('Tracked output conflict. Proposed files: ' + ', '.join(proposals) +
                         '. Conflict evidence: ' + ', '.join(map(str, self.paths)))


def _b64(value):
    return None if value is None else base64.b64encode(value).decode('ascii')


def _unb64(value):
    return None if value is None else base64.b64decode(value.encode('ascii'), validate=True)


def _digest(value):
    return None if value is None else hashlib.sha256(value).hexdigest()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(directory):
    """Persist a rename where the platform permits directory fsync."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            # macOS filesystems can reject directory fsync; the file itself is fsynced.
            pass
    finally:
        os.close(descriptor)


def _relative_path(name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('Tracked output path must be a nonempty POSIX relative path')
    path = PurePosixPath(name)
    if str(path) != name or path.is_absolute() or not path.parts or '.' in path.parts or '..' in path.parts:
        raise ValueError('Unsafe tracked output path: ' + name)
    return path


def _regular_path(repo, name):
    relative = _relative_path(name)
    current = repo
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError('Tracked output path contains a symlink: ' + name)
        if current.exists() and not current.is_dir():
            raise ValueError('Tracked output parent is not a directory: ' + name)
    target = repo.joinpath(*relative.parts)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError('Tracked output must be a regular file or absent: ' + name)
    if target.exists() and stat.S_IMODE(target.stat(follow_symlinks=False).st_mode) != 0o644:
        raise ValueError('Tracked output must have mode 0644: ' + name)
    return target


def _value(repo, name):
    path = _regular_path(repo, name)
    return path.read_bytes() if path.exists() else None


def _normalize(declarations):
    if not isinstance(declarations, dict) or not declarations:
        raise ValueError('Tracked output declarations must be a nonempty mapping')
    normalized = {}
    for name, declaration in declarations.items():
        _relative_path(name)
        if not isinstance(declaration, dict) or set(declaration) != {'base', 'target'}:
            raise ValueError('Each tracked output declaration needs exactly base and target')
        base, target = declaration['base'], declaration['target']
        if base is not None and not isinstance(base, bytes) or target is not None and not isinstance(target, bytes):
            raise ValueError('Tracked output values must be bytes or None')
        normalized[name] = {'base': base, 'target': target}
    normalized = dict(sorted(normalized.items()))
    paths = [(name, _relative_path(name).parts) for name in normalized]
    for index, (name, parts) in enumerate(paths):
        for other, other_parts in paths[index + 1:]:
            if parts == other_parts[:len(parts)] or other_parts == parts[:len(other_parts)]:
                raise ValueError('Overlapping tracked output declarations: ' + name + ', ' + other)
    return normalized


def _encode(declarations):
    return {name: {'base': _b64(item['base']), 'base_sha256': _digest(item['base']),
                   'target': _b64(item['target']), 'target_sha256': _digest(item['target'])}
            for name, item in declarations.items()}


def _decode(encoded):
    if not isinstance(encoded, dict):
        raise ValueError('Invalid tracked output intent schema')
    decoded = {}
    for name, item in encoded.items():
        if not isinstance(name, str) or not isinstance(item, dict) or set(item) != {'base', 'base_sha256', 'target', 'target_sha256'}:
            raise ValueError('Invalid tracked output intent schema')
        if (item['base'] is not None and not isinstance(item['base'], str)) or (item['target'] is not None and not isinstance(item['target'], str)) or \
           (item['base_sha256'] is not None and not isinstance(item['base_sha256'], str)) or (item['target_sha256'] is not None and not isinstance(item['target_sha256'], str)):
            raise ValueError('Invalid tracked output intent schema')
        try:
            base, target = _unb64(item['base']), _unb64(item['target'])
        except (ValueError, TypeError) as error:
            raise ValueError('Invalid tracked output intent encoding') from error
        if _digest(base) != item['base_sha256'] or _digest(target) != item['target_sha256']:
            raise ValueError('Tracked output intent digest mismatch')
        decoded[name] = {'base': base, 'target': target}
    return _normalize(decoded)


def read_intent(output):
    """Return durable intent, or None when publication has not begun."""
    path = Path(output) / INTENT_FILE
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError('Tracked output intent is not a regular file')
    document = json.loads(path.read_text())
    if set(document) != {'version', 'phase', 'declarations', 'committed'} or document['version'] != 1:
        raise ValueError('Invalid tracked output intent schema')
    if document['phase'] not in ('applying', 'published') or not isinstance(document['committed'], list):
        raise ValueError('Invalid tracked output intent state')
    declarations = _decode(document['declarations'])
    committed = tuple(document['committed'])
    if len(set(committed)) != len(committed) or any(name not in declarations for name in committed):
        raise ValueError('Invalid tracked output receipt')
    return TrackedIntent(document['phase'], declarations, committed)


def source_matches_intent(repo, output):
    """Accept only the durable base or target values for this attempt's paths."""
    intent = read_intent(output)
    if intent is None:
        return False
    repo = Path(repo)
    return all(_value(repo, name) in (item['base'], item['target'])
               for name, item in intent.declarations.items())


def _verify_returned_targets(output, declarations):
    output = Path(output)
    manifest_path = output / 'artifacts.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('Missing artifact manifest')
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in manifest.items()):
        raise ValueError('Invalid artifact manifest')
    root = output / 'results' / 'updates'
    expected = {name for name, item in declarations.items() if item['target'] is not None}
    found = set()
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Returned updates must be a regular directory')
        for path in root.rglob('*'):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ValueError('Returned updates contain an unsafe path')
            if path.is_file():
                name = path.relative_to(root).as_posix()
                _relative_path(name)
                if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o644:
                    raise ValueError('Returned target must have mode 0644: ' + name)
                found.add(name)
                artifact = 'results/updates/' + name
                if artifact not in manifest or hashlib.sha256(path.read_bytes()).hexdigest() != manifest[artifact]:
                    raise ValueError('Returned target artifact is unverified: ' + name)
                if path.read_bytes() != declarations.get(name, {}).get('target'):
                    raise ValueError('Returned target disagrees with declaration: ' + name)
    if found != expected:
        raise ValueError('Returned target set differs from declarations')


def _conflicts(output, repo, declarations, *, allow_target, committed=()):
    conflicts = {}
    for name, item in declarations.items():
        current = _value(repo, name)
        accepted = (item['base'], item['target']) if allow_target else (item['base'],)
        # Once a receipt exists, a later return to its base is an outside edit.
        if current not in accepted or (name in committed and item['base'] != item['target'] and current == item['base']):
            conflicts[name] = current
    if not conflicts:
        return ()
    root = Path(output) / 'publication' / 'conflicts'
    paths = []
    for name, current in conflicts.items():
        evidence = root / (name + '.json')
        evidence.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(evidence, {'path': name, 'base': _b64(declarations[name]['base']),
                                'target': _b64(declarations[name]['target']), 'local': _b64(current)})
        paths.append(evidence)
    return tuple(paths)


def _write_target(repo, output, name, item, fault):
    path = _regular_path(repo, name)
    backup = Path(output) / 'publication' / 'backups' / name
    if path.exists() and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
    if item['target'] is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Check parents again after mkdir. This remains cooperative ownership, not fencing.
    _regular_path(repo, name)
    staging = Path(output) / 'publication' / 'staging'
    staging.mkdir(parents=True, exist_ok=True)
    if staging.stat().st_dev != path.parent.stat().st_dev:
        raise ValueError('Tracked output staging must share the destination filesystem: ' + name)
    descriptor, temporary_name = tempfile.mkstemp(prefix='tracked-output-', dir=staging)
    try:
        with os.fdopen(descriptor, 'wb') as temporary:
            temporary.write(item['target'])
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o644)
        fault('before_replace')
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def publish(repo, output, declarations, fault=lambda point: None):
    """Publish declared targets after verified success; never reruns remote work."""
    repo, output = Path(repo), Path(output)
    if repo.is_symlink() or not repo.is_dir():
        raise ValueError('Repository must be a regular directory')
    requested = _normalize(declarations)
    intent = read_intent(output)
    if intent is None:
        _verify_returned_targets(output, requested)
        conflicts = _conflicts(output, repo, requested, allow_target=False)
        if conflicts:
            raise PublicationConflict(conflicts, output)
        intent_path = output / INTENT_FILE
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(intent_path, {'version': 1, 'phase': 'applying', 'declarations': _encode(requested), 'committed': []})
        intent = read_intent(output)
    elif intent.declarations != requested:
        raise ValueError('Pending tracked publication belongs to different declarations')
    conflicts = _conflicts(output, repo, intent.declarations, allow_target=True, committed=intent.committed)
    if conflicts:
        raise PublicationConflict(conflicts, output)
    committed = list(intent.committed)
    for name, item in intent.declarations.items():
        if _value(repo, name) == item['target']:
            if name not in committed:
                committed.append(name)
                _atomic_json(output / INTENT_FILE, {'version': 1, 'phase': 'applying',
                    'declarations': _encode(intent.declarations), 'committed': committed})
            continue
        _write_target(repo, output, name, item, fault)
        fault('after_write')
        committed.append(name)
        _atomic_json(output / INTENT_FILE, {'version': 1, 'phase': 'applying',
            'declarations': _encode(intent.declarations), 'committed': committed})
    _atomic_json(output / INTENT_FILE, {'version': 1, 'phase': 'published',
        'declarations': _encode(intent.declarations), 'committed': committed})
    return PublicationReceipt(tuple(committed))
