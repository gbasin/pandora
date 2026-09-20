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
DECLARATIONS_FILE = 'publication/tracked-declarations.json'
RESOLUTION_FILE = 'publication/tracked-resolution.json'


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


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + '\n').encode('utf-8')


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as handle:
        handle.write(_json_bytes(value).decode('utf-8'))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _encode_committed(committed, declarations):
    names = tuple(declarations)
    positions = {name: index for index, name in enumerate(names)}
    bits = bytearray((len(names) + 7) // 8)
    for name in committed:
        if name not in positions:
            raise ValueError('Invalid tracked output receipt')
        index = positions[name]
        bits[index // 8] |= 1 << (index % 8)
    return base64.b64encode(bits).decode('ascii')


def _decode_committed(encoded, declarations):
    if not isinstance(encoded, str):
        raise ValueError('Invalid tracked output receipt')
    try:
        bits = base64.b64decode(encoded.encode('ascii'), validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError('Invalid tracked output receipt') from error
    names = tuple(declarations)
    if len(bits) != (len(names) + 7) // 8 or (len(names) % 8 and bits[-1] >> (len(names) % 8)):
        raise ValueError('Invalid tracked output receipt')
    return tuple(name for index, name in enumerate(names) if bits[index // 8] & (1 << (index % 8)))


def _write_intent(output, phase, committed, declarations_digest, declarations):
    _atomic_json(Path(output) / INTENT_FILE, {
        'version': 2, 'phase': phase, 'declarations_sha256': declarations_digest,
        'committed': _encode_committed(committed, declarations),
    })


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


def _declarations_document(declarations):
    return {'version': 1, 'declarations': _encode(declarations)}


def _prepare_declarations(output, declarations):
    """Persist an attempt's immutable declarations and return their digest."""
    path = Path(output) / DECLARATIONS_FILE
    document = _declarations_document(declarations)
    encoded = _json_bytes(document)
    digest = _digest(encoded)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ValueError('Tracked output declarations are not a regular file')
        if path.read_bytes() != encoded:
            raise ValueError('Tracked output declarations differ from pending intent')
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_json(path, document)
    return digest


def _read_intent(output):
    """Read intent and its v2 declaration digest, if one is available."""
    path = Path(output) / INTENT_FILE
    if not path.exists():
        return None, None
    if path.is_symlink() or not path.is_file():
        raise ValueError('Tracked output intent is not a regular file')
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or not isinstance(document.get('version'), int):
        raise ValueError('Invalid tracked output intent schema')
    if document['version'] == 1:
        if set(document) != {'version', 'phase', 'declarations', 'committed'}:
            raise ValueError('Invalid tracked output intent schema')
        declarations = _decode(document['declarations'])
        digest = None
    elif document['version'] == 2:
        if set(document) != {'version', 'phase', 'declarations_sha256', 'committed'} or \
           not isinstance(document['declarations_sha256'], str):
            raise ValueError('Invalid tracked output intent schema')
        declarations_path = Path(output) / DECLARATIONS_FILE
        if not declarations_path.exists() or declarations_path.is_symlink() or not declarations_path.is_file():
            raise ValueError('Tracked output declarations are not a regular file')
        encoded = declarations_path.read_bytes()
        digest = document['declarations_sha256']
        if _digest(encoded) != digest:
            raise ValueError('Tracked output declarations digest mismatch')
        declarations_document = json.loads(encoded)
        if not isinstance(declarations_document, dict) or set(declarations_document) != {'version', 'declarations'} or \
           declarations_document['version'] != 1:
            raise ValueError('Invalid tracked output declarations schema')
        declarations = _decode(declarations_document['declarations'])
    else:
        raise ValueError('Invalid tracked output intent schema')
    if document['phase'] not in ('applying', 'conflicted', 'published', 'resolved'):
        raise ValueError('Invalid tracked output intent state')
    if document['version'] == 1:
        if not isinstance(document['committed'], list):
            raise ValueError('Invalid tracked output intent state')
        committed = tuple(document['committed'])
    else:
        committed = _decode_committed(document['committed'], declarations)
    if len(set(committed)) != len(committed) or any(name not in declarations for name in committed):
        raise ValueError('Invalid tracked output receipt')
    return TrackedIntent(document['phase'], declarations, committed), digest


def read_intent(output):
    """Return durable intent, or None when publication has not begun."""
    return _read_intent(output)[0]


def _read_resolution(output, intent):
    path = Path(output) / RESOLUTION_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError('Tracked publication resolution receipt is not a regular file')
    document = json.loads(path.read_text())
    if set(document) != {'version', 'kind', 'paths'} or document['version'] != 1 or \
       document['kind'] != 'keep-local-unvalidated' or not isinstance(document['paths'], dict):
        raise ValueError('Invalid tracked publication resolution receipt')
    if set(document['paths']) != set(intent.declarations):
        raise ValueError('Tracked publication resolution receipt paths differ from intent')
    accepted = {}
    for name, item in intent.declarations.items():
        recorded = document['paths'][name]
        if not isinstance(recorded, dict) or set(recorded) != {
                'base_sha256', 'target_sha256', 'accepted', 'accepted_sha256'}:
            raise ValueError('Invalid tracked publication resolution receipt')
        if recorded['base_sha256'] != _digest(item['base']) or recorded['target_sha256'] != _digest(item['target']) or \
           (recorded['accepted'] is not None and not isinstance(recorded['accepted'], str)) or \
           (recorded['accepted_sha256'] is not None and not isinstance(recorded['accepted_sha256'], str)):
            raise ValueError('Tracked publication resolution receipt differs from intent')
        try:
            value = _unb64(recorded['accepted'])
        except (ValueError, TypeError) as error:
            raise ValueError('Invalid tracked publication resolution receipt') from error
        if _digest(value) != recorded['accepted_sha256']:
            raise ValueError('Tracked publication resolution receipt digest mismatch')
        accepted[name] = value
    return accepted


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


def _raise_conflict(output, conflicts):
    if conflicts:
        raise PublicationConflict(conflicts, output)


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
    intent, declarations_digest = _read_intent(output)
    if intent is None:
        _verify_returned_targets(output, requested)
        conflicts = _conflicts(output, repo, requested, allow_target=False)
        declarations_digest = _prepare_declarations(output, requested)
        if conflicts:
            # Preserve immutable, verified base/target intent before reporting a
            # conflict. A later explicit local resolution must never infer it
            # from mutable source or re-run the remote journey.
            _write_intent(output, 'conflicted', (), declarations_digest, requested)
            _raise_conflict(output, conflicts)
        _write_intent(output, 'applying', (), declarations_digest, requested)
        intent = TrackedIntent('applying', requested, ())
    elif intent.declarations != requested:
        raise ValueError('Pending tracked publication belongs to different declarations')
    elif intent.phase == 'resolved':
        raise ValueError('Tracked output publication was explicitly resolved with local contents')
    elif intent.phase == 'conflicted':
        conflicts = tuple((Path(output) / 'publication' / 'conflicts').rglob('*.json'))
        if not conflicts:
            raise ValueError('Conflicted tracked publication lacks conflict evidence')
        _raise_conflict(output, conflicts)
    if declarations_digest is None:
        # A v1 intent stays readable forever. The first recovery write upgrades
        # it after capturing an immutable copy of the already-verified values.
        declarations_digest = _prepare_declarations(output, intent.declarations)
    conflicts = _conflicts(output, repo, intent.declarations, allow_target=True, committed=intent.committed)
    if conflicts:
        _write_intent(output, 'conflicted', intent.committed, declarations_digest, intent.declarations)
        _raise_conflict(output, conflicts)
    committed = list(intent.committed)
    for name, item in intent.declarations.items():
        if _value(repo, name) == item['target']:
            if name not in committed:
                committed.append(name)
                _write_intent(output, 'applying', committed, declarations_digest, intent.declarations)
            continue
        _write_target(repo, output, name, item, fault)
        fault('after_write')
        committed.append(name)
        _write_intent(output, 'applying', committed, declarations_digest, intent.declarations)
    _write_intent(output, 'published', committed, declarations_digest, intent.declarations)
    return PublicationReceipt(tuple(committed))


def resolve_with_local_contents(repo, output, declarations, fault=lambda point: None):
    """Durably accept current declared files without writing or validating them.

    The returned targets remain verified remote evidence. The accepted local
    bytes are merely the user's explicit merge result and require a subsequent
    ordinary validation run.
    """
    repo, output = Path(repo), Path(output)
    requested = _normalize(declarations)
    intent, declarations_digest = _read_intent(output)
    if intent is None or intent.phase not in ('conflicted', 'resolved'):
        raise ValueError('No conflicted tracked publication is available to resolve')
    if intent.declarations != requested:
        raise ValueError('Conflicted tracked publication belongs to different declarations')
    receipt_path = output / RESOLUTION_FILE
    if receipt_path.exists() or receipt_path.is_symlink():
        _read_resolution(output, intent)
    else:
        if intent.phase == 'resolved':
            raise ValueError('Resolved tracked publication is missing its immutable local resolution receipt')
        accepted = {name: _value(repo, name) for name in intent.declarations}
        receipt = {
            'version': 1,
            'kind': 'keep-local-unvalidated',
            'paths': {
                name: {
                    'base_sha256': _digest(item['base']),
                    'target_sha256': _digest(item['target']),
                    'accepted': _b64(accepted[name]),
                    'accepted_sha256': _digest(accepted[name]),
                }
                for name, item in intent.declarations.items()
            },
        }
        _atomic_json(receipt_path, receipt)
        fault('after_receipt')
    if intent.phase != 'resolved':
        if declarations_digest is None:
            declarations_digest = _prepare_declarations(output, intent.declarations)
        _write_intent(output, 'resolved', intent.committed, declarations_digest, intent.declarations)
        fault('after_intent')
    return tuple(intent.declarations)
