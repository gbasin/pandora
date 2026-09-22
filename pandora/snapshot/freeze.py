"""Freeze a worktree into a manifest, without touching Git's index.

Copied down from `experiments/warm/snapshot.py` (v0.1.1, frozen) and trimmed to
what the slice needs. Three changes, each deliberate:

* The v0.1.1 version *copied* every selected file into a staging directory and
  re-hashed it there. The slice does not: it hashes in place and hands the name
  list to rsync, which reads the same files once more. Eichler is 375 MiB over
  ~4,900 files; the staging copy doubled the I/O for no property the manifest
  did not already give.
* Secret exclusion is the built-in name list plus whatever the repository's
  `[secrets] exclude_globs` adds, so a repository can widen it and never narrow
  it.
* `input_id` is the manifest digest, which is the identity the engine
  deduplicates on. Two worktrees with byte-identical tracked content produce one
  input_id, which is what makes `same_input_as` meaningful.

What is kept verbatim because it was hard-won: the nested-worktree exclusion
(eichler has ~90 registered worktrees, several inside the repository), the
symlink containment check, the credential-bearing `.npmrc` refusal, and the
re-read at the end that turns "the tree moved while we read it" into a refusal
rather than a corrupt snapshot.
"""
import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path

from ..errors import SnapshotError

# Names that are a secret by their shape, not by configuration. A repository may
# add to this through `[secrets] exclude_globs`; nothing can remove from it.
SECRET_DIRS = {'.git', 'node_modules', '.pnpm-store', '.ssh'}
SECRET_NAMES = {'.env', '.dev.vars', 'id_rsa', 'id_ed25519'}
SECRET_PREFIXES = ('.env.', '.dev.vars.')
SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx')
NOT_SECRET_SUFFIXES = ('.example', '.sample', '.template')
NPMRC_MARKERS = ('_authToken', '_password', '_auth=')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _git(repo, *args):
    try:
        return subprocess.check_output(['git', '-C', str(repo), *args])
    except (OSError, subprocess.SubprocessError) as error:
        raise SnapshotError('git %s failed in %s: %s' % (args[0], repo, error)) from None


def nested_worktree_prefixes(repo):
    """Repository-relative prefixes of registered worktrees living below `repo`.

    A worktree checked out inside another worktree is a separate checkout that
    `git ls-files --others` happily lists. Shipping it would ship a second copy
    of the repository at a different commit.
    """
    root = Path(repo).resolve()
    common = Path(_git(root, 'rev-parse', '--path-format=absolute',
                       '--git-common-dir').decode().strip()).resolve()
    prefixes = set()
    for field in _git(root, 'worktree', 'list', '--porcelain', '-z').split(b'\0'):
        if not field.startswith(b'worktree '):
            continue
        worktree = Path(os.fsdecode(field[len(b'worktree '):])).resolve()
        try:
            relative = worktree.relative_to(root)
        except ValueError:
            continue
        if relative == Path('.') or not (worktree / '.git').is_file():
            continue
        try:
            nested_root = Path(subprocess.check_output(
                ['git', '-C', str(worktree), 'rev-parse', '--show-toplevel'],
                text=True).strip()).resolve()
            nested_common = Path(subprocess.check_output(
                ['git', '-C', str(worktree), 'rev-parse', '--path-format=absolute',
                 '--git-common-dir'], text=True).strip()).resolve()
        except (OSError, subprocess.SubprocessError):
            continue
        if nested_root == worktree and nested_common == common:
            prefixes.add(relative.as_posix().rstrip('/') + '/')
    return sorted(prefixes)


def names(repo, nested_prefixes):
    raw = _git(repo, 'ls-files', '-z', '--cached', '--others', '--exclude-standard')
    candidates = {item.decode() for item in raw.split(b'\0') if item}
    return sorted(name for name in candidates
                  if not any(name == prefix[:-1] or name.startswith(prefix)
                             for prefix in nested_prefixes))


def excluded(name, globs=()):
    parts = Path(name).parts
    base = parts[-1]
    if any(part in SECRET_DIRS for part in parts):
        return True
    if base in SECRET_NAMES or base.endswith(SECRET_SUFFIXES):
        return True
    if base.startswith(SECRET_PREFIXES) and not base.endswith(NOT_SECRET_SUFFIXES):
        return True
    return any(fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(base, glob) for glob in globs)


def entry(root, name):
    """One manifest record, or None for a tracked file that has been deleted."""
    path = root / name
    if path.is_symlink():
        target = os.readlink(path)
        if Path(target).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
            raise SnapshotError('symlink leaves the worktree: %s -> %s' % (name, target))
        return {'path': name, 'link': target}
    if not path.exists():
        return None
    if not path.is_file():
        raise SnapshotError('unsupported source entry (a submodule?): ' + name)
    if path.name == '.npmrc':
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            text = ''
        if any(marker in text for marker in NPMRC_MARKERS):
            raise SnapshotError('credential-bearing .npmrc cannot be submitted: ' + name)
    return {'path': name, 'sha256': digest(path),
            'executable': bool(path.stat().st_mode & 0o111)}


def encode(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()


def input_id(manifest):
    """The identity of this content. Equal inputs produce an equal id."""
    return hashlib.sha256(encode(manifest)).hexdigest()


def freeze(repo, *, exclude_globs=()):
    """Return (manifest, excluded_names, input_id) for a worktree.

    Nothing is copied. The manifest is read twice and the second read must agree
    with the first, so a tree that moves under us is a refusal rather than a
    snapshot nobody can reproduce.
    """
    repo = Path(repo).resolve()
    nested = nested_worktree_prefixes(repo)
    first = names(repo, nested)
    selected = [name for name in first if not excluded(name, exclude_globs)]
    manifest = [record for name in selected if (record := entry(repo, name)) is not None]
    if (nested_worktree_prefixes(repo) != nested or names(repo, nested) != first
            or [record for name in selected if (record := entry(repo, name)) is not None] != manifest):
        raise SnapshotError('the worktree changed while it was being frozen; retry')
    dropped = [name for name in first if excluded(name, exclude_globs)]
    return manifest, dropped + nested, input_id(manifest)


def verify(root, manifest):
    """Check a materialised tree against a manifest. Used by the engine's tests."""
    for record in manifest:
        if entry(Path(root), record['path']) != record:
            raise SnapshotError('source verification failed: ' + record['path'])
    actual = {str(path.relative_to(root)) for path in Path(root).rglob('*')
              if path.is_file() or path.is_symlink()}
    if actual != {record['path'] for record in manifest}:
        raise SnapshotError('the materialised tree has unexpected or missing files')
