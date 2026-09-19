"""Freeze tracked and nonignored source without touching Git's index."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def names(repo):
    raw = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '-z',
                                   '--cached', '--others', '--exclude-standard'])
    return sorted(set(p.decode() for p in raw.split(b'\0') if p))


def excluded(name):
    parts = Path(name).parts
    base = parts[-1]
    return (any(p in {'.git', 'node_modules', '.pnpm-store', '.ssh'} for p in parts)
            or base in {'.env', '.dev.vars', 'id_rsa', 'id_ed25519'}
            or (base.startswith(('.env.', '.dev.vars.'))
                and not base.endswith(('.example', '.sample', '.template')))
            or base.endswith(('.pem', '.key', '.p12', '.pfx')))


def entry(root, name):
    p = root / name
    if p.is_symlink():
        target = os.readlink(p)
        if Path(target).is_absolute() or not p.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'External source symlink: {name}')
        return {'path': name, 'link': target}
    if not p.exists():
        return None  # Tracked deletion.
    if not p.is_file():
        raise ValueError(f'Unsupported source entry (including submodule): {name}')
    if p.name == '.npmrc' and any(s in p.read_text() for s in ['_authToken', '_password', '_auth=']):
        raise ValueError('Credential-bearing .npmrc cannot be submitted')
    return {'path': name, 'sha256': digest(p),
            'executable': bool(p.stat().st_mode & 0o111)}


def freeze(repo, destination):
    repo = repo.resolve()
    first_names = names(repo)
    selected = [n for n in first_names if not excluded(n)]
    destination.mkdir(parents=True, exist_ok=False)
    manifest = []
    for name in selected:
        source = repo / name
        record = entry(repo, name)
        if record is None:
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if 'link' in record:
            target.symlink_to(record['link'])
        else:
            shutil.copyfile(source, target)
            target.chmod(0o755 if record['executable'] else 0o644)
        if entry(destination, name) != record:
            raise RuntimeError(f'Source changed during capture: {name}; retry explicitly')
        manifest.append(record)
    final = [e for n in selected if (e := entry(repo, n)) is not None]
    if names(repo) != first_names or final != manifest:
        raise RuntimeError('Source changed during capture; retry explicitly')
    return manifest, [n for n in first_names if excluded(n)]


def encode(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()


def verify(root, manifest):
    for record in manifest:
        if entry(root, record['path']) != record:
            raise RuntimeError(f'Source verification failed: {record["path"]}')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != {e['path'] for e in manifest}:
        raise RuntimeError('Unexpected or missing snapshot files')
