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

A later change, not from v0.1.1: a tracked file git vouches for is not read.
Its sha256 comes from `Blobs`, a machine-wide map from git blob id to sha256
(see `index_blobs` and `Blobs` for what git must vouch for, and why the digest
stays sha256 rather than becoming the blob id).

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
import tempfile
import threading
import time
from pathlib import Path

from ..errors import SnapshotError

# Names that are a secret by their shape, not by configuration. A repository may
# add to this through `[secrets] exclude_globs`; nothing can remove from it.
SECRET_DIRS = {'.git', 'node_modules', '.pnpm-store', '.ssh'}
# Credential files a repository may well track or forget to ignore: direnv's
# `.envrc`, netrc, git's credential store, PyPI's upload config, SSH keys.
SECRET_NAMES = {'.env', '.dev.vars', '.envrc', '.netrc', '.git-credentials', '.pypirc',
                'id_rsa', 'id_ecdsa', 'id_ed25519', 'id_dsa'}
SECRET_PREFIXES = ('.env.', '.dev.vars.')
# Compared against the lower-cased name: `SERVER.PEM` is the same key file.
SECRET_SUFFIXES = ('.pem', '.key', '.p12', '.pfx')
NOT_SECRET_SUFFIXES = ('.example', '.sample', '.template')
NPMRC_MARKERS = ('_authToken', '_password', '_auth=')


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def digest_blob(path, oid, size):
    """(sha256, whether the bytes are git blob `oid`), from one read.

    `size` is the stat size, for the blob header; a file that changed size
    while it was read simply fails to match. A 64-character id is a sha256
    repository's.
    """
    result = hashlib.sha256()
    blob = (hashlib.sha1 if len(oid) == 40 else hashlib.sha256)(b'blob %d\0' % size)
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
            blob.update(chunk)
    return result.hexdigest(), blob.hexdigest() == oid


def _git(repo, *args, input=None):
    try:
        return subprocess.check_output(['git', '-C', str(repo), *args], input=input,
                                       stderr=subprocess.DEVNULL if input is not None else None)
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


# Attributes under which checkout writes other bytes than the blob's.
CONVERTING = ('filter', 'ident', 'working-tree-encoding')


def _config(repo, key):
    proc = subprocess.run(['git', '-C', str(repo), 'config', '--get', key],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    return proc.stdout.decode().strip().lower()


def index_blobs(repo):
    """{name: blob id} for each tracked file whose bytes git vouches are that blob.

    Git already knows the content of every clean tracked file: the index has its
    blob id and a stat record, and `git status` compares the stat rather than
    reading the file. A fresh worktree is all clean tracked files and a new stat
    everywhere, so this is what spares its first freeze reading 364 MiB.

    A file is vouched for when all of these hold, and is otherwise hashed:

    * stage 0, a regular file, and tagged `H` by `ls-files -v` -- not
      assume-unchanged or skip-worktree, where git's "clean" means "not
      looked at";
    * `git status` finds the work tree equal to the index. Status runs with
      `--no-optional-locks`: it refreshes stat in memory and never writes the
      index, so a freeze cannot collide with the agent's own `git add` on
      index.lock. A racily clean entry (written in the index's own second) is
      compared by content, by git, as it is for any status;
    * checkout writes the blob's own bytes: no filter (LFS), ident, or
      working-tree-encoding, and no CRLF on the way out. Otherwise the file's
      bytes are not the blob's and a sha256 learned from the blob is wrong.

    Status runs before ls-files: a `git add` landing between them then leaves
    the file modified in status's answer, never vouched with a stale blob id.
    Anything that stops git answering -- not a repository root, an old git, an
    unexpected record -- returns {} and every file is hashed, as before.
    """
    try:
        root = Path(repo).resolve()
        top = Path(_git(root, 'rev-parse', '--show-toplevel').decode().strip()).resolve()
        if top != root:
            return {}
        changed = set()
        fields = iter(_git(root, '--no-optional-locks', 'status', '--porcelain=v2', '-z',
                           '--untracked-files=no', '--no-renames',
                           '--ignore-submodules=none').split(b'\0'))
        for field in fields:
            if not field:
                continue
            kind = field[:1]
            if kind == b'1':
                _, xy, rest = field.split(b' ', 2)
                if xy[1:2] != b'.':
                    changed.add(rest.split(b' ', 6)[6].decode())
            elif kind == b'2':
                # A rename record is followed by its origin path; both count.
                changed.add(field.split(b' ', 9)[9].decode())
                changed.add(next(fields).decode())
            elif kind == b'u':
                changed.add(field.split(b' ', 10)[10].decode())
            else:
                return {}
        staged = {}
        for field in _git(root, 'ls-files', '-s', '-v', '-z').split(b'\0'):
            if not field:
                continue
            meta, name = field.split(b'\t', 1)
            tag, mode, oid, stage = meta.decode().split(' ')
            name = name.decode()
            if tag == 'H' and stage == '0' and mode in ('100644', '100755') \
                    and name not in changed:
                staged[name] = oid
        if not staged:
            return {}
        crlf = _config(root, 'core.autocrlf') in ('true', 'yes', 'on', '1') \
            or _config(root, 'core.eol') == 'crlf'
        names = sorted(staged)
        answer = _git(root, 'check-attr', '-z', '--stdin', *CONVERTING, 'eol', 'text',
                      input=b'\0'.join(name.encode() for name in names) + b'\0').split(b'\0')
        attrs = {}
        for at in range(0, len(answer) - 2, 3):
            attrs.setdefault(answer[at].decode(), {})[answer[at + 1].decode()] = \
                answer[at + 2].decode()
        vouched = {}
        for name in names:
            found = attrs.get(name)
            if found is None:
                continue
            if any(found.get(key) not in ('unspecified', 'unset') for key in CONVERTING):
                continue
            if found.get('eol') == 'crlf' or (crlf and found.get('text') != 'unset'):
                continue
            vouched[name] = staged[name]
        return vouched
    except (SnapshotError, ValueError, IndexError, StopIteration, OSError):
        return {}


def excluded(name, globs=()):
    parts = Path(name).parts
    base = parts[-1]
    if any(part in SECRET_DIRS for part in parts):
        return True
    if base in SECRET_NAMES or base.lower().endswith(SECRET_SUFFIXES):
        return True
    if base.startswith(SECRET_PREFIXES) and not base.endswith(NOT_SECRET_SUFFIXES):
        return True
    return any(fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(base, glob) for glob in globs)


def entry(root, name, known=None):
    """One manifest record, or None for a tracked file that has been deleted.

    `known` is an `Identity`, which may know the sha256 without reading the
    file; without one the file is read.
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
    sha = known.sha256(name, path, stat) if known is not None else digest(path)
    return {'path': name, 'sha256': sha, 'executable': bool(stat.st_mode & 0o111)}


def _atomic_write(path, text):
    """Write-then-rename, with a name unique to this call: the daemon freezes
    from several threads of one process, so a pid alone is not unique."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(handle, 'w') as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


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
            _atomic_write(self.path, json.dumps(self.fresh, separators=(',', ':')))
        except OSError:
            pass


class Blobs:
    """sha256 by git blob id, shared by every worktree on this machine.

    Why sha256 stays the digest rather than the blob id: the manifest's sha256
    is not opaque. Write-back compares it with `proposals.digest` of local files
    and with the worker's `changes`, both sha256 of bytes, and `verify` hashes a
    materialized tree. A blob id would change the manifest, the input_id of
    every tree and that comparison. So git supplies the name of the content and
    this map supplies the digest the rest of the system already speaks; the
    first freeze of a blob reads it once, and every later worktree holding it
    does not.

    An entry is stored only when the bytes read hash to the blob id itself
    (`digest_blob`), so a file that moved under the read cannot teach this map
    a wrong answer that every later worktree would believe. A hit also needs
    the file's size to equal the blob's, a last guard against bytes that are
    not the blob's under a conversion `index_blobs` could not see.

    Entries record the day they were last used. Saving merges with what is on
    disk, drops entries unused for `KEEP_DAYS`, keeps at most `CAP` of the most
    recently used, and replaces the file atomically. Concurrent freezes in one
    daemon serialize on a lock; across processes the last writer wins, which
    loses entries (a slower freeze) and never corrupts one.
    """
    KEEP_DAYS = 30
    CAP = 200_000
    NAME = 'blobs.json'
    _lock = threading.Lock()

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.today = int(time.time() // 86400)
        self.table = self._load() if self.path is not None else {}
        self.fresh = {}

    def _load(self):
        try:
            table = json.loads(self.path.read_text())
            return table if isinstance(table, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, oid, size):
        item = self.fresh.get(oid) or self.table.get(oid)
        if not item or item[0] != size:
            return None
        if item[2] != self.today:
            self.fresh[oid] = [item[0], item[1], self.today]
        return item[1]

    def put(self, oid, size, sha):
        self.fresh[oid] = [size, sha, self.today]

    def save(self):
        """Never fatal: a lost map is a slower freeze."""
        if self.path is None or not self.fresh:
            return
        with self._lock:
            try:
                table = self._load()
                table.update(self.fresh)
                horizon = self.today - self.KEEP_DAYS
                kept = [(oid, item) for oid, item in table.items()
                        if isinstance(item, list) and len(item) == 3 and item[2] >= horizon]
                if len(kept) > self.CAP:
                    kept.sort(key=lambda pair: pair[1][2], reverse=True)
                    kept = kept[:self.CAP]
                _atomic_write(self.path, json.dumps(dict(kept), separators=(',', ':')))
            except OSError:
                pass


class Identity:
    """Where each file's sha256 comes from in one freeze, and a count of each.

    A vouched file (`index_blobs`) is looked up by blob id; any other file goes
    through the per-worktree `Digests` when there is one; the rest are read.
    """

    def __init__(self, digests=None, blobs=None):
        self.digests = digests
        self.blobs = blobs if blobs is not None else Blobs()
        self.vouched = {}
        self.counts = {'read': 0, 'index': 0, 'stat': 0}

    def sha256(self, name, path, stat):
        oid = self.vouched.get(name)
        if oid is not None:
            sha = self.blobs.get(oid, stat.st_size)
            if sha is not None:
                self.counts['index'] += 1
                return sha
            sha, same = digest_blob(path, oid, stat.st_size)
            self.counts['read'] += 1
            if same:
                self.blobs.put(oid, stat.st_size, sha)
            return sha
        if self.digests is not None:
            sha = self.digests.get(name, stat)
            if sha is not None:
                self.counts['stat'] += 1
                return sha
        sha = digest(path)
        self.counts['read'] += 1
        if self.digests is not None:
            self.digests.put(name, stat, sha)
        return sha

    def save(self):
        if self.digests is not None:
            self.digests.save()
        self.blobs.save()


def digests_for(directory, repo):
    """The `Digests` file for one worktree under a state directory."""
    name = hashlib.sha256(str(Path(repo).resolve()).encode()).hexdigest()[:16]
    return Digests(Path(directory) / (name + '.json'))


def encode(manifest):
    return json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()


def input_id(manifest):
    """The identity of this content. Equal inputs produce an equal id."""
    return hashlib.sha256(encode(manifest)).hexdigest()


def freeze(repo, *, exclude_globs=(), cache=None, index=True, counts=None):
    """Return (manifest, excluded_names, input_id) for a worktree.

    Nothing is copied. The manifest is read twice and the second read must agree
    with the first, so a tree that moves under us is a refusal rather than a
    snapshot nobody can reproduce.

    A record carries `git: untracked|ignored` when git's answer about it differs
    from "tracked" (see `git_status`). That is part of the identity on purpose:
    two trees with equal bytes and a different tracked set make checks that ask
    git answer differently, so they are different inputs.

    `cache` is a directory for per-worktree `Digests` and the machine-wide
    `Blobs`. Without one, the blob map lives for this call only: the second
    pass still skips every vouched file, the first reads them all.

    `index=False` hashes every file, as before `index_blobs`; it is what the
    equivalence tests compare against. `counts`, a dict when given, receives
    how many files were read, taken from the index, or taken from the stat
    cache, over both passes.
    """
    repo = Path(repo).resolve()
    known = Identity(digests_for(cache, repo) if cache is not None else None,
                     Blobs(Path(cache) / Blobs.NAME) if cache is not None else None)

    def read():
        nested = nested_worktree_prefixes(repo)
        first = names(repo, nested)
        marks = git_status(repo)
        known.vouched = index_blobs(repo) if index else {}
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
    known.save()
    if counts is not None:
        counts.update(known.counts)
    return manifest, dropped + nested, input_id(manifest)


def git_marks(manifest):
    """The two exception lists a synthetic index needs, from a manifest."""
    return {flag: [record['path'] for record in manifest if record.get('git') == flag]
            for flag in ('untracked', 'ignored')}


def verify(root, manifest):
    """Check a materialized tree against a manifest. Used by the engine's tests."""
    for record in manifest:
        bare = {key: value for key, value in record.items() if key != 'git'}
        if entry(Path(root), record['path']) != bare:
            raise SnapshotError('source verification failed: ' + record['path'])
    actual = {str(path.relative_to(root)) for path in Path(root).rglob('*')
              if path.is_file() or path.is_symlink()}
    if actual != {record['path'] for record in manifest}:
        raise SnapshotError('the materialized tree has unexpected or missing files')


def main(argv=None):
    """`python3 -m pandora.snapshot.freeze --time PATH`: one timed freeze."""
    import argparse
    parser = argparse.ArgumentParser(prog='python3 -m pandora.snapshot.freeze')
    parser.add_argument('--time', action='store_true', required=True,
                        help='freeze PATH and print wall time and where digests came from')
    parser.add_argument('--cache', help='the digests directory (default: none, in-memory)')
    parser.add_argument('--no-index', action='store_true', help='hash every file, as before')
    parser.add_argument('path')
    args = parser.parse_args(argv)
    counts = {}
    started = time.monotonic()
    manifest, dropped, identity = freeze(args.path, cache=args.cache,
                                         index=not args.no_index, counts=counts)
    seconds = time.monotonic() - started
    print('%.2f s  %d entries  read %d  index %d  stat %d  (both passes)  input %s'
          % (seconds, len(manifest), counts['read'], counts['index'], counts['stat'],
             identity))


if __name__ == '__main__':
    main()
