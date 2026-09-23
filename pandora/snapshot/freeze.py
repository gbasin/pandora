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
  input_id, which is what makes `same_tree_as` meaningful.

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
import time
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


def git_status(repo):
    """{name: 'untracked' | 'ignored'} for every name git would answer about wrongly.

    A run's tree arrives without `.git`, and a repository whose checks ask git
    (`git ls-files`, `git diff HEAD`) gets a synthetic one built on the worker.
    `git add -A` over the tree would get two sets wrong: an untracked file would
    become tracked -- so eichler's markdown-status policy would read a scratch
    note it never reads here -- and a tracked file matching an ignore rule would
    become untracked. These are the exceptions that make the synthetic index say
    what this worktree's index says. Both sets are small; everything else is
    tracked and needs no mark.
    """
    marks = {}
    for flag, args in (('untracked', ('--others', '--exclude-standard')),
                       ('ignored', ('--cached', '--ignored', '--exclude-standard'))):
        for item in _git(repo, 'ls-files', '-z', *args).split(b'\0'):
            if item:
                marks[item.decode()] = flag
    return marks


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


def entry(root, name, known=None):
    """One manifest record, or None for a tracked file that has been deleted.

    `known` is a `Digests`: a file whose stat matches what it recorded is not
    read again.
    """
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
    stat = path.stat()
    sha = known.get(name, stat) if known is not None else None
    if sha is None:
        sha = digest(path)
        if known is not None:
            known.put(name, stat, sha)
    return {'path': name, 'sha256': sha, 'executable': bool(stat.st_mode & 0o111)}


class Digests:
    """sha256 by (size, mtime, ctime, inode), kept between freezes of one worktree.

    Git's index idea. Freezing eichler read 375 MiB twice -- 6 s with the files
    in the page cache and 21 s without, on a loaded Mac, which is most of what a
    warm remote `check` cost its caller. A file whose stat is unchanged since
    the last freeze is not read again, and the freeze's own second pass becomes
    a stat comparison.

    A stat taken within `RACY_NS` of the file's mtime is never stored: a write
    that lands in the same timestamp tick as our read would otherwise be
    invisible to the next freeze. Such a file is simply hashed every time.
    """
    RACY_NS = 2 * 10 ** 9

    def __init__(self, path):
        self.path = Path(path)
        self.now = time.time_ns()
        try:
            self.table = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.table = {}
        self.fresh = {}

    @staticmethod
    def key(stat):
        return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]

    def get(self, name, stat):
        item = self.fresh.get(name) or self.table.get(name)
        if item and item[:4] == self.key(stat):
            self.fresh[name] = item
            return item[4]
        return None

    def put(self, name, stat, sha):
        if self.now - max(stat.st_mtime_ns, stat.st_ctime_ns) < self.RACY_NS:
            return
        self.fresh[name] = self.key(stat) + [sha]

    def save(self):
        """Keep exactly what this freeze saw. Never fatal: a lost cache is a slow freeze."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + '.%d' % os.getpid())
            temporary.write_text(json.dumps(self.fresh, separators=(',', ':')))
            os.replace(temporary, self.path)
        except OSError:
            pass


def digests_for(directory, repo):
    """The `Digests` file for one worktree under a state directory."""
    name = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    return Digests(Path(directory) / (name + '.json'))


def encode(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()


def input_id(manifest):
    """The identity of this content. Equal inputs produce an equal id."""
    return hashlib.sha256(encode(manifest)).hexdigest()


def freeze(repo, *, exclude_globs=(), cache=None):
    """Return (manifest, excluded_names, input_id) for a worktree.

    Nothing is copied. The manifest is read twice and the second read must agree
    with the first, so a tree that moves under us is a refusal rather than a
    snapshot nobody can reproduce.

    A record carries `git: untracked|ignored` when git's answer about it differs
    from "tracked" (see `git_status`). That is part of the identity on purpose:
    two trees with equal bytes and a different tracked set make checks that ask
    git answer differently, so they are different inputs.

    `cache` is a directory for per-worktree `Digests`; without one every file
    is read twice, which is correct and slow.
    """
    repo = Path(repo).resolve()
    known = digests_for(cache, repo) if cache is not None else None

    def read():
        nested = nested_worktree_prefixes(repo)
        first = names(repo, nested)
        marks = git_status(repo)
        selected = [name for name in first if not excluded(name, exclude_globs)]
        manifest = []
        for name in selected:
            record = entry(repo, name, known)
            if record is None:
                continue
            if name in marks:
                record['git'] = marks[name]
            manifest.append(record)
        return nested, first, manifest

    nested, first, manifest = read()
    if read() != (nested, first, manifest):
        raise SnapshotError('the worktree changed while it was being frozen; retry')
    dropped = [name for name in first if excluded(name, exclude_globs)]
    if known is not None:
        known.save()
    return manifest, dropped + nested, input_id(manifest)


def git_marks(manifest):
    """The two exception lists a synthetic index needs, from a manifest."""
    return {flag: [record['path'] for record in manifest if record.get('git') == flag]
            for flag in ('untracked', 'ignored')}


def verify(root, manifest):
    """Check a materialised tree against a manifest. Used by the engine's tests."""
    for record in manifest:
        bare = {key: value for key, value in record.items() if key != 'git'}
        if entry(Path(root), record['path']) != bare:
            raise SnapshotError('source verification failed: ' + record['path'])
    actual = {str(path.relative_to(root)) for path in Path(root).rglob('*')
              if path.is_file() or path.is_symlink()}
    if actual != {record['path'] for record in manifest}:
        raise SnapshotError('the materialised tree has unexpected or missing files')
