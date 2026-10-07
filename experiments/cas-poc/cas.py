"""Disposable local CAS prototype, never a production transfer implementation.

The default integrity policy rehashes reused blobs before publication and
includes that cost in the transfer. An explicit 'trusted' policy skips those
reads after verified insertion, for a separately measured comparison only.
Neither policy establishes a production zero-read CAS design. Inputs must be
controlled scratch fixtures, not hostile live worktrees: this prototype does
not close ancestor-symlink races during rsync.
No SSH, production source caches, gateway feeds, or daemon state are used.
"""
import errno
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import time

_SPEC = importlib.util.spec_from_file_location(
    'transfer_benchmark', Path(__file__).parents[1] / 'transfer-profile' / 'benchmark.py')
_BENCHMARK = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BENCHMARK)
CHUNK = 1 << 20
READONLY_MODES = (0o444, 0o555)


def validate(records):
    """Only unique relative files and confined symlinks can enter a stage."""
    if not isinstance(records, list):
        raise ValueError('records must be a list')
    names = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('record must be an object')
        name = record.get('path')
        if not isinstance(name, str) or not name or '\0' in name:
            raise ValueError('invalid source path')
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
            raise ValueError('source path must be canonical and relative: %r' % name)
        if name in names:
            raise ValueError('duplicate source path: %r' % name)
        names.add(name)
        if 'link' in record:
            target = record['link']
            if ('sha256' in record or 'mode' in record or not isinstance(target, str)
                    or not target or '\0' in target or PurePosixPath(target).is_absolute()):
                raise ValueError('invalid symlink record: %r' % name)
            parts = list(path.parent.parts)
            for part in PurePosixPath(target).parts:
                if part == '..':
                    if not parts:
                        raise ValueError('symlink leaves source tree: %r' % name)
                    parts.pop()
                elif part != '.':
                    parts.append(part)
        elif (not isinstance(record.get('sha256'), str)
              or not re.fullmatch('[0-9a-f]{64}', record['sha256'])
              or not isinstance(record.get('mode'), int)
              or isinstance(record.get('mode'), bool)
              or record.get('mode') not in READONLY_MODES):
            raise ValueError('file identity requires sha256 and canonical readonly mode: %r' % name)
    for name in names:
        if any(parent.as_posix() in names for parent in PurePosixPath(name).parents
               if parent.as_posix() != '.'):
            raise ValueError('source paths have an ancestor conflict: %r' % name)


def controlled_source_path(source, record):
    path = source / record['path']
    for parent in path.parents:
        if parent == source:
            break
        if parent.is_symlink():
            raise ValueError('source parent is a symlink: %s' % record['path'])
    if path.is_symlink() or not path.is_file():
        raise ValueError('source file is not regular: %s' % record['path'])
    return path


class CasStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.blobs = self.root / 'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)

    def blob(self, record):
        return self.blobs / ('%s-%04o' % (record['sha256'], record['mode']))

    def known(self, record):
        """Stat lookup assumes this private store contains only our insertions.

        A trusted run needs that assumption to remain true. No persistent
        provenance/authentication or protection against external writes is
        implemented in this disposable experiment.
        """
        path = self.blob(record)
        try:
            value = path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o7777 != record['mode']:
            raise ValueError('existing blob is not a canonical immutable file')
        return True

    def install(self, path, record):
        """Copy/hash once, then publish without modifying any shared inode."""
        descriptor, temporary = tempfile.mkstemp(prefix='.insert-', dir=self.blobs)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, 'wb') as target:
                descriptor = None
                source_descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(source_descriptor, 'rb') as source:
                    value = os.fstat(source.fileno())
                    if not stat.S_ISREG(value.st_mode) or value.st_mode & 0o7777 != record['mode']:
                        raise ValueError('received file has unexpected readonly mode')
                    sha = hashlib.sha256()
                    for chunk in iter(lambda: source.read(CHUNK), b''):
                        sha.update(chunk)
                        target.write(chunk)
                if sha.hexdigest() != record['sha256']:
                    raise ValueError('received bytes differ from manifest: %s' % record['path'])
                target.flush()
                os.fchmod(target.fileno(), record['mode'])
            # A verified temporary file is private until this exclusive link.
            # A concurrent insertion never overwrites an existing blob inode.
            try:
                os.link(temporary, self.blob(record))
            except FileExistsError:
                # This key may have appeared after planning. It was not in
                # the reused-blob set, so neither policy can trust the winner
                # until its actual bytes satisfy verified insertion too.
                if not self.known(record) or _BENCHMARK.digest(self.blob(record)) != record['sha256']:
                    raise ValueError('insertion winner differs from manifest: %s' % record['path'])
        finally:
            if descriptor is not None:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def seed(self, source, records):
        validate(records)
        source = Path(source).resolve()
        for record in records:
            if 'sha256' in record:
                self.install(controlled_source_path(source, record), record)

    def transfer(self, source, target, records, rsync=None, *, integrity='rehash'):
        """Return wall/phase costs for a verified scratch snapshot publication."""
        if integrity not in ('rehash', 'trusted'):
            raise ValueError('integrity must be rehash or trusted')
        validate(records)
        source, target = Path(source).resolve(), Path(target).absolute()
        if os.path.lexists(target):
            raise FileExistsError('target must be fresh')
        if target.resolve().is_relative_to(self.root):
            raise ValueError('target must be outside the CAS store')
        target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=target.name + '.partial.', dir=target.parent)).resolve()
        result = {'steps': {}, 'missing_files': 0, 'missing_bytes': 0, 'stats': {},
                  'integrity': integrity}
        started = time.monotonic()

        def measured(name, operation):
            began = time.monotonic()
            try:
                return operation()
            finally:
                result['steps'][name] = time.monotonic() - began

        try:
            missing = []
            cached = {}

            def plan():
                for record in records:
                    path = stage / record['path']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if 'link' in record:
                        path.symlink_to(record['link'])
                    elif self.known(record):
                        os.link(self.blob(record), path)
                        cached[(record['sha256'], record['mode'])] = record
                    else:
                        original = controlled_source_path(source, record)
                        missing.append(record)
                        result['missing_bytes'] += original.stat().st_size
                # Resolve symlink chains too, before rsync or publication.
                for record in records:
                    if 'link' in record:
                        try:
                            try:
                                (stage / record['path']).resolve(strict=True)
                            except FileNotFoundError:
                                pass  # Confined dangling links are allowed.
                            confined = (stage / record['path']).resolve().is_relative_to(stage)
                        except RuntimeError:
                            raise ValueError('cyclic source symlink') from None
                        except OSError as error:
                            if error.errno == errno.ELOOP:
                                raise ValueError('cyclic source symlink') from None
                            raise
                        if not confined:
                            raise ValueError('symlink chain leaves source tree')
                result['missing_files'] = len(missing)

            measured('plan', plan)
            if missing:
                rsync = rsync or shutil.which('rsync')
                if not rsync:
                    raise RuntimeError('rsync is required for missing files')
                argv = [rsync, '-a', '--no-times', '--files-from=-', '--from0',
                        '--stats', '--no-whole-file', str(source) + '/', str(stage) + '/']
                names = b'\0'.join(record['path'].encode() for record in missing) + b'\0'
                proc = measured('rsync', lambda: subprocess.run(
                    argv, input=names, capture_output=True, timeout=1800,
                    env={**os.environ, 'LC_ALL': 'C'}))
                result['stats'] = _BENCHMARK.parse_stats(proc.stdout.decode('utf-8', 'replace'))
                if proc.returncode:
                    raise RuntimeError('scratch rsync failed (%d): %s'
                                       % (proc.returncode, proc.stderr.decode('utf-8', 'replace')))

            def place():
                for record in missing:
                    path = stage / record['path']
                    self.install(path, record)
                    # The verified received inode remains private. Replace its
                    # directory entry with the trusted readonly store inode.
                    path.unlink()
                    os.link(self.blob(record), path)
                actual = {path.relative_to(stage).as_posix() for path in stage.rglob('*')
                          if path.is_file() or path.is_symlink()}
                if actual != {record['path'] for record in records}:
                    raise ValueError('stage contains unexpected or missing source files')

            measured('place', place)

            def verify_cached():
                for record in cached.values():
                    if not self.known(record) or _BENCHMARK.digest(self.blob(record)) != record['sha256']:
                        raise ValueError('reused blob differs from manifest: %s' % record['path'])

            if integrity == 'rehash':
                measured('verify_cached', verify_cached)
            measured('publish', lambda: os.rename(stage, target))
            result['wall_seconds'] = time.monotonic() - started
            return result
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def writable_copy(source, target):
    """Independent execution bytes and metadata; never writable CAS hardlinks."""
    source, target = Path(source).resolve(), Path(target).absolute()
    if os.path.lexists(target):
        raise FileExistsError('execution target must be fresh')
    if target.resolve().is_relative_to(source):
        raise ValueError('execution target must be outside source')
    try:
        shutil.copytree(source, target, symlinks=True)
        for path in target.rglob('*'):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
